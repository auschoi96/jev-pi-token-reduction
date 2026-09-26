"""Shape-preserving routing with immutable snapshots and exact recovery."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

from .chunking import chunk_any, chunk_grep, chunk_log, is_grep
from .client import JEV_MODEL, JEV_URL
from .envelopes import opencode_read
from .policy import Policy
from .rungs import duplicate, kind_of, log_key, render
from .cost import cache_multipliers
from .scoring import Scorer, ladder_level
from .store import Store, digest

TOOLS = {"read", "grep", "rg", "search", "bash", "shell", "exec_command"}
COMMANDS = {"bash", "shell", "exec_command"}
SELECTIONS = ("arrival", "dynamic", "cache_aware")
PROTECTED = re.compile(r"<system-reminder\b|(?m:^\[(?:Showing |Output |Results |Use offset=|[0-9]+ (?:lines|matches|results)))"
                       r"|(?m:^Command exited with code \d+\s*$)")
NUMBERED = re.compile(r"^\s*(\d+)(?:[\t│]|: )")
MARKER = {
    "hide": "\n[Jev omitted {source} lines {start}-{end}; recover exact snapshot with jev_expand hidden_ref={ref}]\n",
    "short": "[Jev outlined {source} lines {start}-{end}; exact text: jev_expand hidden_ref={ref}]\n",
    "long": "[Jev condensed {source} lines {start}-{end} (comments/docstrings/passing lines dropped); exact text: jev_expand hidden_ref={ref}]\n",
}


def text_of(raw, allow_error=False):
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if (raw.get("isError") or raw.get("is_error")) and not allow_error:
            return None
        raw = raw.get("content")
        if isinstance(raw, str):
            return raw
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict) and raw[0].get("type") == "text" and isinstance(raw[0].get("text"), str):
        return raw[0]["text"]
    return None


def with_text(raw, text):
    raw = deepcopy(raw)
    if isinstance(raw, str):
        return text
    if isinstance(raw, dict):
        raw["content"] = with_text(raw["content"], text)
    else:
        raw[0]["text"] = text
    return raw


def tokens(text):
    # A deliberately explicit estimate, never mixed with provider-reported usage.
    return (len(text.encode("utf-8")) + 3) // 4


class Router:
    def __init__(self, db_path=None, *, policy=None, mode="shadow", selection="arrival", client=None):
        if mode in SELECTIONS:
            selection, mode = mode, "on"
        if mode not in {"off", "shadow", "on"} or selection not in SELECTIONS:
            raise ValueError("mode must be off/shadow/on/" + "/".join(SELECTIONS) + "; selection must be " + "/".join(SELECTIONS))
        self.mode, self.selection = mode, selection
        self.policy = policy or Policy()
        self.store = Store(db_path or Path.home() / ".local/state/jev-router/router.sqlite")
        self.scorer = Scorer(self.store, self.policy, **({"client": client} if client else {}))
        self.config = digest([self.policy.fingerprint, mode, selection, JEV_MODEL, JEV_URL])

    def close(self):
        self.store.close()

    def route_tool_result(self, session_id, turn_id, tool_call_id, task, step, tool, raw_output):
        tool = {"name": tool} if isinstance(tool, str) else deepcopy(tool)
        if not all(isinstance(x, str) and x for x in (session_id, tool_call_id)):
            raise ValueError("Nonempty session_id and tool_call_id are required")
        data = {"raw": deepcopy(raw_output), "task": task, "step": step, "tool": tool, "turn_id": turn_id}
        sid = self.store.snapshot(session_id, tool_call_id, data)
        saved = self.store.arrival(session_id, sid, self.config)
        if saved:
            return {**saved, "metrics": {**saved["metrics"], "arrival_reused": True, "jev_calls": 0, "jev_ms": 0, "jev_usage": [], "jev_failures": 0}}
        if self.selection == "dynamic" and self.mode != "off":
            result = self._passthrough(raw_output, "awaiting_context", sid)
        else:
            result = self._route(session_id, tool_call_id, sid, data, step, time.monotonic() + self.policy.result_budget_seconds)
        self.store.save_arrival(session_id, sid, self.config, result)
        self.store.view(session_id, tool_call_id, result["visible_output"], sid)
        self.store.event(session_id, "tool_result", {"call_id": tool_call_id, "turn_id": turn_id, **result})
        return {**result, "evidence": self._evidence(tool, raw_output, task, step)}

    def _evidence(self, tool, raw, task, step, limit=800):
        """Signal lines of a permitted command/search result, for the next Jev query. Reasoning models often
        emit tool calls with no public text, so the call alone ("read x.py") says nothing about relevance."""
        name = tool.get("name", "").lower()
        text = text_of(raw, allow_error=name in COMMANDS)
        if not text or name not in TOOLS or name == "read" or not self.policy.permits(tool, text, task, step)[0]:
            return ""
        kind = "grep" if is_grep(text) else "text"
        return f"{name} result: " + render("short", text, kind)[0][:limit]

    def _passthrough(self, raw, reason, sid, text=None):
        size = tokens(text if text is not None else text_of(raw) or "")
        return {"visible_output": deepcopy(raw), "hidden_refs": [], "decisions": [], "snapshot_id": sid,
                "metrics": {"reason": reason, "mode": self.mode, "selection": self.selection,
                            "token_estimator": "utf8_bytes_div4", "tokens_before_est": size, "tokens_shown_est": size,
                            "tokens_proposed_est": size, "eligible_tokens_est": 0, "jev_calls": 0,
                            "jev_failures": 0, "jev_usage": [], "jev_ms": 0, "unscored_chunks": 0}}

    def _route(self, session, call_id, sid, data, step, deadline, risk=None):
        raw, tool, task = data["raw"], data["tool"], data["task"]
        text = text_of(raw, allow_error=self.policy.route_failed_commands and tool.get("name", "").lower() in COMMANDS)
        result = self._passthrough(raw, "off", sid, text)
        reason = None
        if text is None:
            reason = "unsupported_or_error"
        elif tool.get("name", "").lower() not in TOOLS:
            reason = "unsupported_tool"
        elif tokens(text) < self.policy.min_tokens:
            reason = "small_result"
        else:
            allowed, why = self.policy.permits(tool, text, task, step)
            if not allowed:
                reason = "policy:" + why
        if reason:
            result["metrics"]["reason"] = reason
            return result
        result["metrics"]["eligible_tokens_est"] = tokens(text)
        if self.mode == "off":
            return result
        prefix = ""
        base = max(1, int(tool.get("offset") or 1))
        if tool.get("output_format") == "opencode_read":
            try:
                prefix, body, tail, base = opencode_read(text)
            except ValueError:
                result["metrics"].update(reason="unsupported_read_envelope", eligible_tokens_est=0)
                return result
        else:
            protected = PROTECTED.search(text)
            body, tail = (text[:protected.start()], text[protected.start():]) if protected else (text, "")
        source = tool.get("path") or tool.get("file_path") or (tool["name"] + ":output")
        kind = kind_of(tool["name"], source, body)
        pairs = chunk_any(body, source) if tool["name"].lower() == "read" else chunk_log(body) if kind == "text" else chunk_grep(body)
        numbered = NUMBERED.match(body)
        if numbered:
            base = int(numbered[1])
        chunks = []
        line = base
        for i, (_, value) in enumerate(pairs):
            if not value:
                continue
            end = line + len(value.splitlines()) - 1
            h = digest(value)
            ref = f"jev:{sid[:24]}:{i}:{quote(source, safe='/._-')}:L{line}-{end}:{h[:16]}"
            chunk = {"ref": ref, "text": value, "source": source, "start": line, "end": end, "hash": h, "kind": kind}
            self.store.put_chunk(session, ref, sid, value)
            chunks.append(chunk)
            line = end + 1
        # Structural condensation: a traceback identical (modulo test name/numbers/literals) to an earlier
        # one in this result needs no judgment; show its header and a pointer, keep the exact text recoverable.
        firsts, dups = {}, {}
        if kind == "text" and self.policy.dedupe_tracebacks:
            for c in chunks:
                key = log_key(c["text"])
                if key and key in firsts:
                    dups[c["ref"]] = firsts[key]
                elif key:
                    head = next((ln for ln in c["text"].splitlines() if ln.startswith(("FAIL: ", "ERROR: ", "_____"))), "")
                    firsts[key] = (head.split()[1:2] or [c["ref"]])[0].strip("_")
        scored, metrics = self.scorer.score(session, task, step, [c for c in chunks if c["ref"] not in dups], deadline)
        metrics["duplicate_tracebacks"] = len(dups)
        if risk is not None:
            stricter = replace(self.policy, omit_risk=risk)
            scored = {ref: {**d, "level": ladder_level(d["judgment"], stricter)[0]} if d.get("judgment") else d
                      for ref, d in scored.items()}
        parts, hidden, decisions = [], [], []
        for chunk in chunks:
            if chunk["ref"] in dups:
                decisions.append({k: v for k, v in chunk.items() if k != "text"} | {"level": "duplicate", "omitted": True})
                parts.append(duplicate(chunk["text"], dups[chunk["ref"]], chunk["ref"]))
                hidden.append(chunk["ref"])
                continue
            d = scored.get(chunk["ref"], {"level": "full", "error": "missing_score"})
            shown, omitted = render(d["level"], chunk["text"], kind)
            decisions.append({k: v for k, v in chunk.items() if k != "text"} | d | {"omitted": omitted})
            if omitted:
                parts.append(shown)
                parts.append(MARKER[d["level"]].format(source=source, start=chunk["start"], end=chunk["end"], ref=chunk["ref"]))
                hidden.append(chunk["ref"])
            else:
                parts.append(chunk["text"])
        proposed = prefix + "".join(parts) + tail
        floor = tokens(prefix + tail + "".join(MARKER["hide"].format(source=source, start=c["start"], end=c["end"], ref=c["ref"])
                                              for c in chunks))
        if tokens(proposed) >= tokens(text):
            proposed, hidden = text, []
            for d in decisions:
                d["omitted"] = False
        shown = proposed if self.mode == "on" else text
        result.update(visible_output=with_text(raw, shown), hidden_refs=hidden if self.mode == "on" else [],
                      proposed_hidden_refs=hidden, decisions=decisions)
        result["metrics"].update(metrics, reason="routed" if hidden else "kept_all", eligible_tokens_est=tokens(text),
                                 tokens_shown_est=tokens(shown), tokens_proposed_est=tokens(proposed),
                                 floor_tokens_est=min(floor, tokens(shown)))
        fallbacks = [{"ref": d["ref"], "reason": d.get("error") or d.get("fallback")}
                     for d in decisions if d.get("error") or d.get("fallback")]
        if fallbacks:
            self.store.event(session, "fallback", {"call_id": call_id, "snapshot_id": sid, "chunks": fallbacks})
        self.store.view(session, call_id, result["visible_output"], sid)
        self.store.event(session, "routing", {"call_id": call_id, "step": step, "proposed_output": with_text(raw, proposed), **result})
        return result

    def select_context(self, session_id, current_step, model_bound_messages, model=None, now=None):
        if self.selection == "cache_aware" and self.mode != "off":
            return self._select_cache_aware(session_id, current_step, model_bound_messages, model,
                                            time.time() if now is None else now)
        messages = deepcopy(model_bound_messages)
        deadline = time.monotonic() + self.policy.result_budget_seconds
        calls, total, matched, retrieval, intercepted = [], 0, 0, 0, 0
        for message in messages:
            if message.get("role") != "toolResult":
                continue
            total += 1
            is_retrieval = message.get("toolName", "").lower() in TOOLS
            retrieval += is_retrieval
            raw = {"content": message.get("content"), "isError": message.get("isError", False)}
            found = self.store.find(session_id, message.get("toolCallId"), raw)
            if not found:
                continue
            matched += 1
            intercepted += is_retrieval
            sid, data = found
            if self.mode == "off":
                result = self._route(session_id, message["toolCallId"], sid, data, current_step, deadline)
            elif self.selection == "arrival":
                result = self.store.arrival(session_id, sid, self.config)
                if result is None:
                    # A mode change or resumed context has no arrival decision yet.
                    result = self._route(session_id, message["toolCallId"], sid, data, data["step"], deadline)
                    self.store.save_arrival(session_id, sid, self.config, result)
                else:
                    result = {**result, "metrics": {**result["metrics"], "jev_calls": 0, "jev_ms": 0, "jev_usage": [], "jev_failures": 0}}
            else:
                result = self._route(session_id, message["toolCallId"], sid, data, current_step, deadline)
            message["content"] = result["visible_output"]["content"]
            calls.append({"call_id": message["toolCallId"], "snapshot_id": sid, **result["metrics"]})
        metrics = {"tool_results": total, "intercepted_results": matched,
                   "retrieval_results": retrieval, "intercepted_retrieval_results": intercepted,
                   "intercepted_pct": 100 * intercepted / retrieval if retrieval else 0,
                   "eligible_tokens_est": sum(c["eligible_tokens_est"] for c in calls),
                   "tokens_shown_est": sum(c["tokens_shown_est"] for c in calls), "results": calls}
        self.store.event(session_id, "context", {"step": current_step, "metrics": metrics, "messages": messages})
        return {"selected_messages": messages, "metrics": metrics}

    @staticmethod
    def _view(result):
        keep = ("eligible_tokens_est", "tokens_before_est", "tokens_shown_est", "tokens_proposed_est", "reason")
        m = result["metrics"]
        return {"content": result["visible_output"]["content"],
                "metrics": {**{k: m.get(k, 0) for k in keep}, "floor_tokens_est": m.get("floor_tokens_est", m["tokens_shown_est"])}}

    @staticmethod
    def _size(message):
        content = message.get("content")
        if isinstance(content, str):
            return tokens(content)
        return sum(tokens(b["text"]) if isinstance(b, dict) and isinstance(b.get("text"), str)
                   else tokens(json.dumps(b, ensure_ascii=False, default=str)) for b in content or [])

    def _select_cache_aware(self, session_id, step, model_bound_messages, model, now):
        """Keep the provider prefix byte-stable; re-select history only when that prefix is already
        cold (first call, TTL expiry, model switch, host rewrite such as compaction) or when the
        list-price arithmetic says invalidating it pays back within `expected_remaining_calls`."""
        messages = deepcopy(model_bound_messages)
        prev = self.store.sent_state(session_id)
        host = [digest([m.get("role"), m.get("toolCallId"), m.get("content")]) for m in messages]
        cold = ("first_call" if prev is None
                else "ttl_expired" if now - prev["time"] > self.policy.cache_ttl_seconds
                else "model_switch" if model and prev["model"] and model != prev["model"]
                else "host_rewrite" if host[:len(prev["host"])] != prev["host"] else None)
        known = 0 if cold else len(prev["host"])
        deadline = time.monotonic() + self.policy.result_budget_seconds
        entries, retrieval = {}, 0
        for i, message in enumerate(messages):
            if message.get("role") != "toolResult":
                continue
            retrieval += message.get("toolName", "").lower() in TOOLS
            raw = {"content": message.get("content"), "isError": message.get("isError", False)}
            found = self.store.find(session_id, message.get("toolCallId"), raw)
            if not found:
                continue
            sid, data = found
            view = None if cold or i >= known else self.store.sent_view(session_id, message["toolCallId"])
            if view is None and cold:
                view = self._view(self._route(session_id, message["toolCallId"], sid, data, step, deadline))
            elif view is None:
                arrival = self.store.arrival(session_id, sid, self.config)
                if arrival is None:
                    arrival = self._route(session_id, message["toolCallId"], sid, data, data["step"], deadline)
                    self.store.save_arrival(session_id, sid, self.config, arrival)
                view = self._view(arrival)
            entries[i] = {"sid": sid, "data": data, "view": view, "call_id": message["toolCallId"],
                          "retrieval": message.get("toolName", "").lower() in TOOLS}
        gate = None if cold else self._rebuild_gate(session_id, step, prev, known, entries, model, deadline)
        calls = []
        for i, entry in entries.items():
            messages[i]["content"] = entry["view"]["content"]
            self.store.save_sent_view(session_id, messages[i]["toolCallId"], entry["view"])
            calls.append({"call_id": messages[i]["toolCallId"], "snapshot_id": entry["sid"], **entry["view"]["metrics"]})
        self.store.save_sent(session_id, now, model or (prev or {}).get("model"), host, [self._size(m) for m in messages])
        intercepted = sum(e["retrieval"] for e in entries.values())
        metrics = {"tool_results": sum(m.get("role") == "toolResult" for m in messages), "intercepted_results": len(entries),
                   "retrieval_results": retrieval, "intercepted_retrieval_results": intercepted,
                   "intercepted_pct": 100 * intercepted / retrieval if retrieval else 0,
                   "eligible_tokens_est": sum(c["eligible_tokens_est"] for c in calls),
                   "tokens_shown_est": sum(c["tokens_shown_est"] for c in calls), "results": calls,
                   "cache": {"state": "cold" if cold else "hot", "cold_reason": cold, "gate": gate}}
        self.store.event(session_id, "context", {"step": step, "metrics": metrics, "messages": messages})
        return {"selected_messages": messages, "metrics": metrics}

    def _rebuild_gate(self, session_id, step, prev, known, entries, model, deadline):
        """Rebuild from message i iff (S-R)(w+(N-1)r) < N*S*r, i.e. R/S > 1 - N*r/(w+(N-1)r), where S is
        the cached token suffix from i, R the tokens removed, r/w cache read/write price multipliers."""
        r, w = cache_multipliers(model, self.policy.price_table)
        n = self.policy.expected_remaining_calls
        threshold = 1 - n * r / (w + (n - 1) * r)
        sizes = prev["sizes"]
        cached = sorted(i for i, e in entries.items() if i < known and e["view"]["metrics"]["eligible_tokens_est"])
        best, best_ratio = None, 0.0
        for i in cached:
            suffix = sum(sizes[i:known])
            upper = sum(entries[k]["view"]["metrics"]["tokens_shown_est"] - entries[k]["view"]["metrics"]["floor_tokens_est"]
                        for k in cached if k >= i)
            if suffix and upper / suffix > best_ratio:
                best, best_ratio = i, upper / suffix
        gate = {"threshold": round(threshold, 4), "upper_bound_ratio": round(best_ratio, 4), "rebuild": False}
        if best is None or best_ratio <= threshold:
            return gate
        trial = {}
        for k in (k for k in cached if k >= best):
            e = entries[k]
            view = self._view(self._route(session_id, e["call_id"], e["sid"], e["data"], step, deadline))
            if view["content"] != e["view"]["content"]:
                trial[k] = view
        if not trial:
            return gate
        first = min(trial)
        suffix = sum(sizes[first:known])
        removed = sum(entries[k]["view"]["metrics"]["tokens_shown_est"] - v["metrics"]["tokens_shown_est"] for k, v in trial.items())
        gate.update(ratio=round(removed / suffix, 4) if suffix else 0, first_changed=first, removed_tokens_est=removed)
        if suffix and removed / suffix > threshold:
            for k, v in trial.items():
                entries[k]["view"] = v
            gate["rebuild"] = True
        return gate

    def compact(self, session_id, current_step, messages, previous_summary=None, budget_tokens=None):
        """Query-aware replacement for LLM compaction: transcript text plus Jev-selected tool output.
        Escalates omit_risk until the summary fits the budget; omitted text stays recoverable."""
        budget = budget_tokens or self.policy.compaction_budget_tokens
        deadline = time.monotonic() + self.policy.result_budget_seconds
        header = ("[Jev query-aware compaction of earlier turns, selected for the current step. "
                  "Omitted or condensed tool output is recoverable exactly with jev_expand hidden_ref=...]\n")
        summary, risk = "", None
        for risk in (None, 0.35, 0.5, 0.75, 0.999):
            parts = [header] + ([f"\n## Earlier summary\n{previous_summary}\n"] if previous_summary else [])
            for m in messages:
                parts.append(self._compact_message(session_id, m, current_step, deadline, risk))
            summary = "".join(parts)
            if tokens(summary) <= budget:
                break
        metrics = {"tokens_after_est": tokens(summary), "budget_tokens": budget, "final_risk": risk,
                   "messages": len(messages), "within_budget": tokens(summary) <= budget}
        self.store.event(session_id, "compaction", {"step": current_step, "metrics": metrics})
        return {"summary": summary, "metrics": metrics}

    def subcontext(self, session_id, subtask, messages, budget_tokens=None):
        """Purpose-built context for a delegated subtask: only tool output Jev keeps for it, not the transcript."""
        budget = budget_tokens or self.policy.subcontext_budget_tokens
        deadline = time.monotonic() + self.policy.result_budget_seconds
        results = [m for m in messages if m.get("role") == "toolResult"]
        context, risk = "", None
        for risk in (None, 0.35, 0.5, 0.75, 0.999):
            parts = [self._compact_message(session_id, m, subtask, deadline, risk) for m in results]
            # A result reduced to omission markers carries no evidence for the sub-agent.
            context = "".join(p for p in parts if re.sub(r"\[Jev [^\]]*\]|### Tool result \([^)]*\)|\s", "", p))
            if tokens(context) <= budget:
                break
        if tokens(context) > budget:
            context = context[:budget * 4] + "\n[... parent context truncated to budget]\n"
        metrics = {"tokens_est": tokens(context), "budget_tokens": budget, "final_risk": risk, "results": len(results)}
        self.store.event(session_id, "subcontext", {"subtask": subtask, "metrics": metrics})
        return {"context": context, "metrics": metrics}

    def _compact_message(self, session_id, m, step, deadline, risk):
        role = m.get("role")
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
        if role == "toolResult":
            raw = {"content": content, "isError": m.get("isError", False)}
            found = self.store.find(session_id, m.get("toolCallId"), raw)
            if found:
                result = self._route(session_id, m["toolCallId"], found[0], found[1], step, deadline, risk=risk)
                text = text_of(result["visible_output"]) or ""
            else:
                text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                text = text if len(text) <= 2000 else text[:2000] + "\n[... truncated; not recoverable]\n"
            return f"\n### Tool result ({m.get('toolName', 'tool')})\n{text.rstrip()}\n"
        texts = [b["text"] for b in blocks if b.get("type") == "text" and b.get("text")]
        calls = [f"Tool call: {b.get('name')} {json.dumps(b.get('arguments'), ensure_ascii=False)[:300]}"
                 for b in blocks if b.get("type") == "toolCall"]
        if role in {"user", "assistant"} and (texts or calls):
            return f"\n### {role.capitalize()}\n" + "\n".join(texts + calls) + "\n"
        return ""

    def expand_chunk(self, session_id, hidden_ref):
        value = self.store.expand(session_id, hidden_ref)
        self.store.event(session_id, "expansion", {"hidden_ref": hidden_ref, "original_text": value})
        return value

    def inherit_context(self, session_id, parent_session_id, messages):
        """Pi explicitly supplies the parent and retained branch when creating a fork.

        Copy only snapshots represented by unmodified retained tool messages. Ordinary
        route/expand calls cannot read across session boundaries.
        """
        inherited = 0
        for message in messages:
            if message.get("role") != "toolResult":
                continue
            call_id = message.get("toolCallId")
            raw = {"content": message.get("content"), "isError": message.get("isError", False)}
            found = self.store.find(parent_session_id, call_id, raw)
            if not found:
                continue
            old_id, data = found
            new_id = self.store.snapshot(session_id, call_id, data)
            self.store.view(session_id, call_id, raw, new_id)
            for row in self.store.db.execute("SELECT ref,text FROM chunks WHERE session=? AND snapshot=?", (parent_session_id, old_id)).fetchall():
                self.store.put_chunk(session_id, row[0], new_id, row[1])
            for row in self.store.db.execute("SELECT config,result FROM arrivals WHERE session=? AND snapshot=?", (parent_session_id, old_id)).fetchall():
                import json
                result = json.loads(row[1])
                result["snapshot_id"] = new_id
                self.store.save_arrival(session_id, new_id, row[0], result)
                self.store.view(session_id, call_id, result["visible_output"], new_id)
            inherited += 1
        self.store.event(session_id, "inheritance", {"parent_session_id": parent_session_id, "snapshots": inherited})
        return {"inherited_snapshots": inherited}
