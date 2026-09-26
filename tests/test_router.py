import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from jev_router import Policy, Router
from jev_router.chunking import chunk_any


def answer(level="hide", confidence=0.99, probabilities=None):
    return {"type": "choice", "choice": level, "confidence": confidence,
            "probabilities": probabilities or {x: float(x == level) for x in ("hide", "short", "long", "full")}}


def source():
    return "".join(f"def {name}():\n" + "".join(f"    # {name} record {i}: " + "data " * 18 + "\n" for i in range(35))
                   for name in ("authentication", "billing", "telemetry"))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "router.sqlite"
        self.routers = []
        self.calls = 0
        self.level = "hide"

    def tearDown(self):
        for router in self.routers:
            router.close()
        self.tmp.cleanup()

    def client(self, state, questions, **_):
        self.calls += 1
        return {"answers": {k: answer(self.level) for k in questions}, "usage": {"input_tokens": 100, "output_tokens": 10}}

    def router(self, **kwargs):
        kwargs.setdefault("policy", Policy(allow_roots=(str(self.root),), min_tokens=0))
        kwargs.setdefault("mode", "on")
        kwargs.setdefault("client", self.client)
        r = Router(self.db, **kwargs)
        self.routers.append(r)
        return r

    def route(self, router, **kwargs):
        defaults = dict(session_id="session-a", turn_id="turn-1", tool_call_id="call-1", task="Fix authentication",
                        step="Inspect authentication", tool={"name": "read", "path": str(self.root / "module.py")}, raw_output=source())
        defaults.update(kwargs)
        return router.route_tool_result(**defaults)

    def message(self, content):
        return {"role": "toolResult", "toolCallId": "call-1", "toolName": "read", "content": content,
                "isError": False, "details": {"truncation": "preserve"}, "timestamp": 123}

    def test_snapshot_expansion_survives_file_change_and_restart(self):
        r = self.router()
        out = self.route(r)
        (self.root / "module.py").write_text("CHANGED")
        restored = self.router()
        expanded = [restored.expand_chunk("session-a", ref) for ref in out["hidden_refs"]]
        self.assertEqual("".join(expanded), source())
        with self.assertRaises(KeyError):
            restored.expand_chunk("session-b", out["hidden_refs"][0])

    def test_same_tool_id_different_content_or_session_cannot_collide(self):
        r = self.router()
        a = self.route(r)
        b = self.route(r, raw_output=source() + "changed\n")
        c = self.route(r, session_id="session-b")
        self.assertEqual(len({a["snapshot_id"], b["snapshot_id"], c["snapshot_id"]}), 3)

    def test_arrival_is_stable_when_task_changes(self):
        r = self.router()
        a = self.route(r)
        self.level = "full"
        b = self.route(r, step="A new step", task="A new task")
        self.assertEqual(a["visible_output"], b["visible_output"])
        self.assertTrue(b["metrics"]["arrival_reused"])
        self.assertEqual(b["metrics"]["jev_calls"], 0)

    def test_dynamic_reconsiders_original_and_preserves_history(self):
        r = self.router(selection="dynamic")
        content = [{"type": "text", "text": source(), "cache_control": {"type": "ephemeral"}}]
        a = self.route(r, raw_output={"content": content, "isError": False})
        self.assertEqual(self.calls, 0)
        history = [self.message(a["visible_output"]["content"])]
        hidden = r.select_context("session-a", "authentication", history)
        self.assertNotEqual(hidden["selected_messages"], history)
        self.assertEqual(history[0]["content"], content)
        self.level = "full"
        expanded = r.select_context("session-a", "billing", hidden["selected_messages"])
        self.assertEqual(expanded["selected_messages"], history)
        self.assertEqual(expanded["selected_messages"][0]["details"], history[0]["details"])

    def test_shadow_retains_original_and_records_proposal(self):
        r = self.router(mode="shadow")
        result = self.route(r)
        self.assertEqual(result["visible_output"], source())
        self.assertEqual(result["hidden_refs"], [])
        self.assertTrue(result["proposed_hidden_refs"])
        self.assertLess(result["metrics"]["tokens_proposed_est"], result["metrics"]["tokens_shown_est"])

    def test_opencode_wrapper_numbering_and_snapshot_are_lossless(self):
        body = "".join(f"{n}: {line}" for n, line in enumerate(source().splitlines(keepends=True), 101))
        prefix = "<path>module.py</path>\r\n<type>file</type>\r\n<content>\r\n"
        suffix = "\r\n(Showing lines 101-208 of 900. Use offset=209 to continue.)\r\n</content>\r\n<system-reminder>Keep me</system-reminder>"
        r = self.router()
        raw = prefix + body + suffix
        result = self.route(r, raw_output=raw, tool={"name":"read", "path":str(self.root / "module.py"), "output_format":"opencode_read"})
        self.assertTrue(result["hidden_refs"])
        self.assertTrue(result["visible_output"].startswith(prefix))
        self.assertTrue(result["visible_output"].endswith(suffix))
        self.assertEqual(result["decisions"][0]["start"], 101)
        self.assertEqual(result["decisions"][-1]["end"], 208)
        self.assertEqual("".join(r.expand_chunk("session-a", ref) for ref in result["hidden_refs"]), body)

    def test_unknown_or_noncontiguous_opencode_envelopes_are_not_scored(self):
        r = self.router()
        for raw in ("<content>\n1: code\n", "<content>\n1: a\n3: b\n</content>",
                    "<path>x</path>\n<type>directory</type>\n<entries>\nfile.py\n</entries>"):
            result = self.route(r, raw_output=raw, tool={"name":"read", "path":str(self.root / "module.py"), "output_format":"opencode_read"})
            self.assertEqual(result["visible_output"], raw)
            self.assertEqual(result["metrics"]["reason"], "unsupported_read_envelope")
        self.assertEqual(self.calls, 0)

    def test_off_makes_no_calls_and_restores_old_arrival(self):
        r = self.router()
        raw = {"content": [{"type": "text", "text": source()}], "isError": False}
        result = self.route(r, raw_output=raw)
        count = self.calls
        off = self.router(mode="off")
        visible = off.select_context("session-a", "new", [self.message(result["visible_output"]["content"])])
        self.assertEqual(visible["selected_messages"][0]["content"], raw["content"])
        self.assertEqual(self.calls, count)

    def test_errors_images_multiblock_and_small_outputs_pass_through(self):
        r = self.router(policy=Policy(allow_roots=(str(self.root),), min_tokens=50))
        for raw in ("short", {"content": source(), "isError": True}, [{"type": "image", "data": "abc"}],
                    [{"type": "text", "text": source()}, {"type": "text", "text": "next"}]):
            with self.subTest(raw_type=type(raw)):
                self.assertEqual(self.route(r, raw_output=raw)["visible_output"], raw)
        self.assertEqual(self.calls, 0)

    def test_missing_malformed_and_uncertain_answers_stay_visible(self):
        spread = answer(probabilities={"hide": 0.4, "short": 0.15, "long": 0.2, "full": 0.25})
        for response, policy in (({"answers": {}}, {}), ({"answers": {"c0": {"choice": "hide"}}}, {}),
                                 ({"answers": {"c0": spread}}, {}),
                                 ({"answers": {"c0": answer(confidence=0.2)}}, {"min_confidence": 0.5})):
            with self.subTest(response=response):
                r = self.router(client=lambda *a, **kw: response,
                                policy=Policy(allow_roots=(str(self.root),), min_tokens=0, **policy))
                result = self.route(r, session_id=str(response))
                self.assertEqual(result["visible_output"], source())
                self.assertTrue(all(d["level"] == "full" for d in result["decisions"]))

    def test_risk_rule_picks_most_compressed_safe_rung(self):
        from jev_router.scoring import ladder_level
        policy = Policy(omit_risk=0.2)
        cases = [({"hide": 0.85, "short": 0.1, "long": 0.05, "full": 0.0}, "hide"),
                 ({"hide": 0.5, "short": 0.35, "long": 0.1, "full": 0.05}, "short"),
                 ({"hide": 0.3, "short": 0.2, "long": 0.4, "full": 0.1}, "long"),
                 ({"hide": 0.4, "short": 0.1, "long": 0.2, "full": 0.3}, "full")]
        for probs, expected in cases:
            choice = max(probs, key=probs.get)
            self.assertEqual(ladder_level(answer(choice, probabilities=probs), policy)[0], expected)

    def test_short_and_long_rungs_condense_and_stay_recoverable(self):
        doc = "".join(f"    Detail line {j} describing behavior at length.\n" for j in range(20))
        notes = "".join(f"    # note {j}: an explanatory comment about the algorithm\n" for j in range(10))
        code = "".join(f"def f{i}(x):\n    \"\"\"Doc {i}.\n\n{doc}    \"\"\"\n{notes}    return x + {i}\n\n" for i in range(6))
        for level, kept, dropped in (("long", "return x + 3", "# note"), ("short", "def f3(x):", "Detail line")):
            with self.subTest(level=level):
                self.level = level
                r = self.router()
                result = self.route(r, session_id=level, raw_output=code)
                self.assertIn(kept, result["visible_output"])
                self.assertNotIn(dropped, result["visible_output"])
                self.assertLess(result["metrics"]["tokens_shown_est"], result["metrics"]["tokens_before_est"])
                self.assertEqual("".join(r.expand_chunk(level, ref) for ref in result["hidden_refs"]), code)

    def test_retryable_errors_retry_within_budget(self):
        import urllib.error
        attempts = []
        def flaky(state, questions, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError("https://jev", 503, "busy", {}, None)
            return {"answers": {k: answer() for k in questions}}
        r = self.router(client=flaky, policy=Policy(allow_roots=(str(self.root),), min_tokens=0,
                                                   batch_chars=50000, retry_base_seconds=0.001))
        result = self.route(r)
        self.assertEqual(result["metrics"]["jev_retries"], 2)
        self.assertEqual(result["metrics"]["jev_failures"], 0)
        self.assertEqual(len(result["hidden_refs"]), 3)

    def test_outline_first_hides_without_full_text_scoring(self):
        seen = []
        def client(state, questions, **kwargs):
            seen.append(max(len(c["text"]) for c in state["chunks"].values()))
            return {"answers": {k: answer() for k in questions}}
        r = self.router(client=client, policy=Policy(allow_roots=(str(self.root),), min_tokens=0, outline_first=True))
        result = self.route(r, tool={"name": "read", "path": str(self.root / "module.py")})
        self.assertEqual(result["metrics"]["outline_hidden"], 3)
        self.assertEqual(result["metrics"]["jev_calls"], 1)
        self.assertLess(seen[0], 200)

    def test_timeout_keeps_original_and_does_not_cache_failure(self):
        def failing(*args, **kwargs):
            raise TimeoutError()
        r = self.router(client=failing, selection="dynamic")
        raw = {"content": [{"type": "text", "text": source()}], "isError": False}
        self.route(r, raw_output=raw)
        out = r.select_context("session-a", "same", [self.message(raw["content"])])
        self.assertEqual(out["selected_messages"][0]["content"], raw["content"])
        self.assertGreater(out["metrics"]["results"][0]["jev_failures"], 0)
        r.scorer.client = self.client
        second = r.select_context("session-a", "same", [self.message(raw["content"])])
        self.assertNotEqual(second["selected_messages"][0]["content"], raw["content"])

    def test_partial_answers_only_hide_successfully_scored_chunks(self):
        def partial(state, questions, **kwargs):
            return {"answers": {"c0": answer()}}
        r = self.router(client=partial, policy=Policy(allow_roots=(str(self.root),), min_tokens=0, batch_chars=50000))
        result = self.route(r)
        self.assertEqual(len(result["hidden_refs"]), 1)
        self.assertIn("def billing", result["visible_output"])
        self.assertIn("def telemetry", result["visible_output"])
        self.assertEqual(result["metrics"]["unscored_chunks"], 2)

    def test_scoring_budget_exhaustion_preserves_remaining_chunks(self):
        import time
        def slow(state, questions, **kwargs):
            time.sleep(0.01)
            return {"answers": {k: answer() for k in questions}}
        r = self.router(client=slow, policy=Policy(allow_roots=(str(self.root),), min_tokens=0,
                                                batch_size=1, result_budget_seconds=0.005))
        result = self.route(r)
        self.assertEqual(result["metrics"]["jev_calls"], 1)
        self.assertIn("def billing", result["visible_output"])
        self.assertIn("def telemetry", result["visible_output"])
        self.assertEqual(result["metrics"]["unscored_chunks"], 2)

    def test_cache_reuses_same_query_but_invalidates_changed_policy(self):
        r = self.router()
        self.route(r)
        count = self.calls
        self.route(r, tool_call_id="another-call")
        self.assertEqual(self.calls, count)
        other = self.router(policy=Policy(allow_roots=(str(self.root),), min_tokens=0, version="v2"))
        self.route(other)
        self.assertGreater(self.calls, count)

    def test_policy_blocks_unapproved_paths_commands_secrets_and_symlinks(self):
        r = self.router()
        symlink = self.root / "link.py"
        symlink.symlink_to("/etc/passwd")
        for path in ("/tmp/outside.py", str(symlink), str(self.root / ".env")):
            self.route(r, tool={"name": "read", "path": path})
        self.route(r, tool={"name": "bash", "command": "cat /etc/passwd"})
        self.route(r, task="API_KEY=abcdefghijklmnopqrst")
        jwt = "eyJhbGciOiJSUzI1NiJ9." + "eyJzdWIiOiJ1c2VyIn0" + "." + "c2lnbmF0dXJl" * 4
        self.route(r, raw_output=source() + '{"access_token": "' + jwt + '"}')
        self.assertEqual(self.calls, 0)

    def test_numbered_crlf_reminders_and_pagination_survive(self):
        r = self.router()
        original = "".join(f"{n}\t# record {n}: " + "data " * 10 + "\r\n" for n in range(101, 181))
        tail = "<system-reminder>Keep this intact</system-reminder>\r\n[Showing 101-180; use offset=181]\r\n"
        raw = [{"type": "text", "text": original + tail, "custom": 42}]
        result = self.route(r, raw_output=raw)
        self.assertTrue(result["visible_output"][0]["text"].endswith(tail))
        self.assertEqual(result["visible_output"][0]["custom"], 42)
        self.assertIn(":L101-180:", result["hidden_refs"][0])
        self.assertEqual(r.expand_chunk("session-a", result["hidden_refs"][0]), original)

    def test_modified_host_content_is_not_overwritten(self):
        r = self.router(selection="dynamic")
        self.route(r, raw_output={"content": [{"type": "text", "text": source()}], "isError": False})
        changed = [self.message([{"type": "text", "text": "Changed by another extension"}])]
        self.assertEqual(r.select_context("session-a", "x", changed)["selected_messages"], changed)

    def test_chunking_is_lossless(self):
        for text in (source(), "", "no trailing newline", "line\r\nline2\r\n"):
            self.assertEqual("".join(x[1] for x in chunk_any(text, "x.py")), text)

    def test_explicit_fork_inherits_only_retained_snapshots(self):
        r = self.router()
        raw = {"content": [{"type": "text", "text": source()}], "isError": False}
        kept = self.route(r, raw_output=raw)
        excluded = self.route(r, tool_call_id="excluded", raw_output=raw)
        branch = [self.message(kept["visible_output"]["content"])]
        self.assertEqual(r.inherit_context("fork", "session-a", branch)["inherited_snapshots"], 1)
        self.assertEqual(r.expand_chunk("fork", kept["hidden_refs"][0]), r.expand_chunk("session-a", kept["hidden_refs"][0]))
        with self.assertRaises(KeyError):
            r.expand_chunk("fork", excluded["hidden_refs"][0])

    def cached_history(self, r, level, session="session-a"):
        self.level = level
        raw = {"content": [{"type": "text", "text": source()}], "isError": False}
        arrived = self.route(r, raw_output=raw, session_id=session)
        return [{"role": "user", "content": "Fix authentication"},
                {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "read", "arguments": {}}]},
                self.message(arrived["visible_output"]["content"])], raw

    def test_cache_aware_hot_turns_keep_prefix_byte_stable(self):
        r = self.router(selection="cache_aware")
        history, _ = self.cached_history(r, "hide")
        first = r.select_context("session-a", "step one", history, now=1000)
        self.assertEqual(first["metrics"]["cache"]["cold_reason"], "first_call")
        calls = self.calls
        self.level = "full"
        later = history + [{"role": "assistant", "content": "Next"}]
        second = r.select_context("session-a", "a different step", later, now=1010)
        self.assertEqual(second["metrics"]["cache"]["state"], "hot")
        self.assertEqual(second["selected_messages"][:3], first["selected_messages"])
        self.assertEqual(self.calls, calls)

    def test_cache_aware_reselects_when_prefix_is_already_cold(self):
        r = self.router(selection="cache_aware")
        history, raw = self.cached_history(r, "hide")
        r.select_context("session-a", "step one", history, now=1000)
        self.level = "full"
        expired = r.select_context("session-a", "billing", history, now=1000 + 301)
        self.assertEqual(expired["metrics"]["cache"]["cold_reason"], "ttl_expired")
        self.assertEqual(expired["selected_messages"][2]["content"], raw["content"])
        self.level = "hide"
        rewritten = r.select_context("session-a", "auth", [{"role": "user", "content": "Summary"}] + history[2:], now=1302)
        self.assertEqual(rewritten["metrics"]["cache"]["cold_reason"], "host_rewrite")
        self.assertNotEqual(rewritten["selected_messages"][1]["content"], raw["content"])

    def test_cache_aware_gate_rebuilds_only_when_it_pays(self):
        r = self.router(selection="cache_aware")
        history, raw = self.cached_history(r, "full")
        r.select_context("session-a", "step one", history, now=1000)
        self.level = "hide"
        paid = r.select_context("session-a", "step two", history, now=1001)
        self.assertTrue(paid["metrics"]["cache"]["gate"]["rebuild"])
        self.assertNotEqual(paid["selected_messages"][2]["content"], raw["content"])
        # A long cached suffix after the result makes invalidation cost more than it saves.
        history, raw = self.cached_history(r, "full", session="session-b")
        tail = history + [{"role": "assistant", "content": "y " * 40000}]
        r.select_context("session-b", "step one", tail, now=1000)
        self.level = "hide"
        calls = self.calls
        kept = r.select_context("session-b", "step two", tail, now=1001)
        self.assertFalse(kept["metrics"]["cache"]["gate"]["rebuild"])
        self.assertEqual(kept["selected_messages"][2]["content"], raw["content"])
        self.assertEqual(self.calls, calls)

    def test_compaction_is_query_aware_bounded_and_recoverable(self):
        r = self.router(selection="cache_aware")
        history, _ = self.cached_history(r, "full")
        self.level = "long"
        out = r.compact("session-a", "authentication", history, previous_summary="Earlier work", budget_tokens=100000)
        self.assertIn("Earlier work", out["summary"])
        self.assertIn("Tool call: read", out["summary"])
        self.assertIn("jev_expand hidden_ref=", out["summary"])
        self.assertLess(out["metrics"]["tokens_after_est"], len(source()) // 4)
        tight = r.compact("session-a", "authentication", history, budget_tokens=200)
        self.assertLess(tight["metrics"]["tokens_after_est"], out["metrics"]["tokens_after_est"])
        ref = re.search(r"hidden_ref=(jev:\S+?)\]", tight["summary"]).group(1)
        self.assertIn(r.expand_chunk("session-a", ref), source())

    def test_subcontext_carries_only_selected_tool_output(self):
        r = self.router(selection="cache_aware")
        history, _ = self.cached_history(r, "full")
        kept = r.subcontext("session-a", "Inspect authentication", history, budget_tokens=100000)
        self.assertIn("def authentication", kept["context"])
        self.assertNotIn("Fix authentication", kept["context"])  # transcript text is not forwarded
        self.level = "hide"
        dropped = r.subcontext("session-a", "Unrelated subtask", history, budget_tokens=100000)
        self.assertEqual(dropped["context"], "")

    def test_repeated_tracebacks_collapse_without_jev_and_stay_recoverable(self):
        block = ("=" * 70 + "\nERROR: test_{i} (test_mod.T.test_{i})\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
                 '  File "test_mod.py", line {n}, in test_{i}\n    self.assertEqual(m.f({i}), {i})\n'
                 '  File "mod.py", line 40, in f\n    raise ValueError("bad value %d" % {i})\nValueError: bad value {i}\n\n')
        log = "".join(block.format(i=i, n=100 + i) for i in range(40)) + "-" * 70 + "\nRan 40 tests in 0.012s\n\nFAILED (errors=40)\n"
        raw = {"content": [{"type": "text", "text": log + "\nCommand exited with code 1"}], "isError": True}
        r = self.router(policy=Policy(allow_roots=(str(self.root),), allow_commands=True, min_tokens=0, score_with_jev=False))
        result = self.route(r, tool={"name": "bash", "command": "python -m unittest -v test_mod"}, raw_output=raw)
        text = result["visible_output"]["content"][0]["text"]
        self.assertEqual(self.calls, 0)
        self.assertEqual(result["metrics"]["duplicate_tracebacks"], 39)
        self.assertIn('raise ValueError("bad value %d" % 0)', text)
        self.assertIn("ERROR: test_39 (test_mod.T.test_39)  [Jev: same traceback as test_0; jev_expand hidden_ref=jev:", text)
        self.assertIn("FAILED (errors=40)", text)
        self.assertLess(result["metrics"]["tokens_shown_est"], result["metrics"]["tokens_before_est"] // 2)
        self.assertTrue(any("ERROR: test_38 " in r.expand_chunk("session-a", ref) for ref in result["hidden_refs"]))

    def test_failed_command_output_is_routed_with_exit_line_and_evidence(self):
        log = "".join(f"test_{i} (test_mod.T.test_{i}) ... ok\n" for i in range(400))
        log += "FAIL: test_x (test_mod.T.test_x)\nTraceback (most recent call last):\n  File \"mod.py\", line 9, in f\nAssertionError: 1 != 2\n"
        raw = {"content": [{"type": "text", "text": log + "\n\nCommand exited with code 1"}], "isError": True}
        self.level = "long"
        r = self.router(policy=Policy(allow_roots=(str(self.root),), allow_commands=True, min_tokens=0))
        result = self.route(r, tool={"name": "bash", "command": "python -m unittest -v test_mod"}, raw_output=raw)
        text = result["visible_output"]["content"][0]["text"]
        self.assertTrue(result["visible_output"]["isError"])
        self.assertTrue(text.endswith("Command exited with code 1"))
        self.assertIn("AssertionError: 1 != 2", text)
        self.assertLess(result["metrics"]["tokens_shown_est"], result["metrics"]["tokens_before_est"] // 4)
        self.assertIn("AssertionError: 1 != 2", result["evidence"])
        self.assertIn("FAIL: test_x", result["evidence"])
        blocked = self.router(policy=Policy(allow_roots=(str(self.root),), min_tokens=0))
        self.assertEqual(self.route(blocked, session_id="s2", tool={"name": "bash"}, raw_output=raw)["evidence"], "")

    def test_json_lines_handles_bad_request_then_next_valid_request(self):
        lines = ['not json', json.dumps({"id": 2, "op": "route_tool_result", "params": {
            "session_id": "s", "turn_id": "t", "tool_call_id": "c", "task": "t", "step": "s",
            "tool": "read", "raw_output": "abc"}})]
        run = subprocess.run([sys.executable, "-m", "jev_router", "--db", str(self.db), "--mode", "off"],
                             input="\n".join(lines) + "\n", text=True, capture_output=True, check=True)
        outputs = [json.loads(x) for x in run.stdout.splitlines()]
        self.assertIn("error", outputs[0])
        self.assertEqual(outputs[1]["id"], 2)
        self.assertEqual(outputs[1]["result"]["visible_output"], "abc")


if __name__ == "__main__":
    unittest.main()
