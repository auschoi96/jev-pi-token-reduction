"""Optional MLflow tracing of Pi runs, built after each run from the router's recorded events.

Enabled when JEV_MLFLOW_EXPERIMENT_ID is set and mlflow is installed (`pip install -e '.[mlflow]'`).
Each Pi agent run becomes one trace: an AGENT root span, an LLM span per model call (token usage, model,
list cost), and a TOOL span per tool result with a child span for Jev's visibility decision. By default
only metadata is logged; JEV_MLFLOW_CONTENT=full also logs the prompt, assistant text, tool arguments,
and the tool output the model saw. Building spans after the fact keeps tracing off the agent's hot path.
"""
from collections import Counter
import json
import os

from .cost import jev_cost, model_key, usage_cost

TEXT_LIMIT = 20000


def enabled():
    return bool(os.environ.get("JEV_MLFLOW_EXPERIMENT_ID"))


def content_mode():
    return os.environ.get("JEV_MLFLOW_CONTENT", "metadata") == "full"


def _text(value):
    blocks = value.get("content") if isinstance(value, dict) else value
    if isinstance(blocks, str):
        text = blocks
    else:
        text = "\n".join(b.get("text", "") for b in blocks or [] if isinstance(b, dict) and b.get("type") == "text")
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f"\n[... {len(text) - TEXT_LIMIT} more characters]"


def _price(usage, model):
    try:
        model_key(model)
        return round(usage_cost(usage, model), 6)
    except KeyError:
        return None


def plan(events, tools, content=False):
    """Pure span plan for one run. `events` are (id, kind, time, data) in order; `tools` maps
    snapshot_id -> tool input. Returns a root span dict with `children`; times are epoch seconds."""
    start = next((t for _, k, t, _ in events if k == "agent_start"), events[0][2])
    end = next((t for _, k, t, _ in reversed(events) if k == "agent_settled"), events[-1][2])
    begun = next((d for _, k, _, d in events if k == "agent_start"), {})
    settled = next((d for _, k, _, d in reversed(events) if k == "agent_settled"), {})
    children, last_context, last_model_end = [], start, start
    totals = Counter()
    jev_usage = []
    for _, kind, when, data in events:
        if kind == "context":
            last_context = when
        elif kind == "model_usage":
            usage = data.get("usage") or {}
            prompt = sum(usage.get(k) or 0 for k in ("input", "cacheRead", "cacheWrite"))
            cost = _price(usage, data.get("model"))
            compaction = data.get("kind") == "compaction"
            for k in ("input", "cacheRead", "cacheWrite", "output"):
                totals[k] += usage.get(k) or 0
            totals["model_calls"] += 1
            totals["cost"] += cost or 0
            totals["unpriced"] += cost is None
            attributes = {"mlflow.chat.tokenUsage": {"input_tokens": prompt, "output_tokens": usage.get("output") or 0,
                                                      "total_tokens": prompt + (usage.get("output") or 0)},
                          "mlflow.llm.model": data.get("model"), "pi.cache_read_tokens": usage.get("cacheRead") or 0,
                          "pi.cache_write_tokens": usage.get("cacheWrite") or 0, "pi.list_cost_usd": cost,
                          "pi.stop_reason": data.get("stop_reason"), "pi.role": data.get("role", "main"), "pi.tool_calls": data.get("tool_calls")}
            outputs = {"text": data.get("text", "")} if content else {"tool_calls": data.get("tool_call_names", [])}
            children.append({"name": "compaction" if compaction else "model_call", "type": "LLM",
                             "start": min(last_context, when), "end": when, "inputs": {"turn": data.get("turn")},
                             "outputs": outputs, "attributes": attributes, "error": data.get("error")})
            last_model_end = when
        elif kind == "tool_result":
            metrics = data.get("metrics", {})
            tool = tools.get(data.get("snapshot_id"), {})
            name = tool.get("name", "tool")
            jev_usage += metrics.get("jev_usage", [])
            before, shown = metrics.get("tokens_before_est", 0), metrics.get("tokens_shown_est", 0)
            eligible = bool(metrics.get("eligible_tokens_est"))
            totals["retrieval_before"] += before if eligible else 0
            totals["retrieval_shown"] += shown if eligible else 0
            totals["jev_calls"] += metrics.get("jev_calls", 0)
            totals["jev_failures"] += metrics.get("jev_failures", 0)
            levels = Counter(d.get("level") for d in data.get("decisions", []))
            span = {"name": name, "type": "TOOL", "start": last_model_end, "end": when,
                    "inputs": {k: v for k, v in tool.items() if k != "name"} if content else {"tool": name, "arguments": sorted(k for k in tool if k != "name")},
                    "outputs": {"shown_to_model": _text(data.get("visible_output", ""))} if content else {"tokens_before": before, "tokens_shown": shown},
                    "attributes": {"jev.reason": metrics.get("reason"), "jev.tokens_before_est": before, "jev.tokens_shown_est": shown,
                                   "jev.hidden_chunks": len(data.get("hidden_refs", [])), "jev.calls": metrics.get("jev_calls", 0),
                                   "jev.failures": metrics.get("jev_failures", 0), "jev.ms": metrics.get("jev_ms", 0)},
                    "children": []}
            if metrics.get("jev_calls") or levels:
                span["children"].append({"name": "jev_visibility", "type": "CHAIN", "start": max(last_model_end, when - metrics.get("jev_ms", 0) / 1000),
                                         "end": when, "inputs": {"chunks": sum(levels.values())}, "outputs": dict(levels),
                                         "attributes": {"jev.cache_hits": metrics.get("cache_hits", 0), "jev.retries": metrics.get("jev_retries", 0),
                                                        "jev.duplicate_tracebacks": metrics.get("duplicate_tracebacks", 0)}})
            children.append(span)
        elif kind == "expansion":
            totals["expansions"] += 1
            children.append({"name": "jev_expand", "type": "TOOL", "start": last_model_end, "end": when,
                             "inputs": {"hidden_ref": data.get("hidden_ref")},
                             "outputs": {"text": _text(data.get("original_text", ""))} if content else {"chars": len(data.get("original_text", ""))},
                             "attributes": {}})
    jev = jev_cost(jev_usage)
    saved = totals["retrieval_before"] - totals["retrieval_shown"]
    prompt = begun.get("prompt") if content else f"[prompt withheld: {begun.get('prompt_chars', 0)} characters; set JEV_MLFLOW_CONTENT=full to log it]"
    root = {"name": "pi_agent_run", "type": "AGENT", "start": start, "end": max(end, start), "children": children,
            "inputs": {"messages": [{"role": "user", "content": prompt}]},
            "outputs": {"text": settled.get("text", "")} if content else {"model_calls": totals["model_calls"]},
            "attributes": {"pi.mode": begun.get("mode"), "pi.model_calls": totals["model_calls"],
                           "pi.input_tokens": totals["input"], "pi.cache_read_tokens": totals["cacheRead"],
                           "pi.cache_write_tokens": totals["cacheWrite"], "pi.output_tokens": totals["output"],
                           # A total that silently skipped unpriced calls would understate cost; report it as unknown.
                           "pi.list_cost_usd": None if totals["unpriced"] else round(totals["cost"] + jev, 6),
                           "pi.unpriced_model_calls": totals["unpriced"], "jev.list_cost_usd": round(jev, 6),
                           "jev.retrieval_tokens_before_est": totals["retrieval_before"], "jev.retrieval_tokens_shown_est": totals["retrieval_shown"],
                           "jev.retrieval_reduction_pct": round(100 * saved / totals["retrieval_before"], 2) if totals["retrieval_before"] else 0,
                           "jev.calls": totals["jev_calls"], "jev.failures": totals["jev_failures"], "jev.expansions": totals["expansions"]}}
    return root


def _emit(mlflow, spec, parent, experiment_id, metadata=None):
    ns = lambda t: int(t * 1e9)
    # experiment_id is only valid on the root: MLflow 3.16 silently drops a child span given one.
    root_only = {"metadata": metadata, "experiment_id": experiment_id} if parent is None else {}
    span = mlflow.start_span_no_context(spec["name"], span_type=spec["type"], parent_span=parent, inputs=spec.get("inputs"),
                                        attributes={k: v for k, v in spec.get("attributes", {}).items() if v is not None},
                                        start_time_ns=ns(spec["start"]), **root_only)
    for child in spec.get("children", []):
        _emit(mlflow, child, span, experiment_id)
    span.end(outputs=spec.get("outputs"), status="ERROR" if spec.get("error") else "OK", end_time_ns=ns(max(spec["end"], spec["start"])))
    return span


def export_run(store, session_id):
    """Log every not-yet-exported event of this session as one trace. Never raises."""
    if not enabled():
        return {"skipped": "JEV_MLFLOW_EXPERIMENT_ID not set"}
    try:
        import mlflow
    except Exception as error:  # Missing, or a broken install (e.g. mismatched numpy/scipy).
        return {"skipped": f"mlflow unavailable ({type(error).__name__}); pip install -e '.[mlflow]'"}
    try:
        done = store.db.execute("SELECT max(id) FROM events WHERE session=? AND kind='trace_exported'", (session_id,)).fetchone()[0] or 0
        rows = store.db.execute("SELECT id,kind,time,data FROM events WHERE session=? AND id>? ORDER BY id", (session_id, done)).fetchall()
        events = [(i, k, t, json.loads(d)) for i, k, t, d in rows if k != "trace_exported"]
        if not any(k == "model_usage" for _, k, _, _ in events):
            return {"skipped": "no model calls since the last trace"}
        tools = {sid: json.loads(d).get("tool", {}) for sid, d in store.db.execute("SELECT id,data FROM snapshots WHERE session=?", (session_id,))}
        spec = plan(events, tools, content_mode())
        experiment_id = os.environ["JEV_MLFLOW_EXPERIMENT_ID"]
        mlflow.set_experiment(experiment_id=experiment_id)
        root = _emit(mlflow, spec, None, experiment_id, metadata={"mlflow.trace.session": session_id})
        store.event(session_id, "trace_exported", {"trace_id": root.trace_id, "experiment_id": experiment_id, "spans": 1 + _count(spec)})
        return {"trace_id": root.trace_id, "experiment_id": experiment_id}
    except Exception as error:  # Tracing must never break the agent.
        store.event(session_id, "trace_error", {"error": f"{type(error).__name__}: {error}"[:500]})
        return {"error": type(error).__name__}


def _count(spec):
    return sum(1 + _count(c) for c in spec.get("children", []))


def flush():
    if not enabled():
        return {"skipped": True}
    try:
        import mlflow
        mlflow.flush_trace_async_logging()
        return {"flushed": True}
    except Exception as error:
        return {"error": type(error).__name__}
