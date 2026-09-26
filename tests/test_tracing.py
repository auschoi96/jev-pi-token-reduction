import unittest

from jev_router.tracing import plan


def events():
    usage = {"input": 10, "cacheRead": 900, "cacheWrite": 400, "output": 50}
    return [
        (1, "agent_start", 100.0, {"prompt_chars": 12, "mode": "arrival", "prompt": "Explain wrap"}),
        (2, "context", 100.5, {}),
        (3, "model_usage", 102.0, {"usage": usage, "model": "system.ai.gpt-6-sol", "turn": 1, "tool_calls": 1,
                                   "tool_call_names": ["read"], "text": "Reading."}),
        (4, "tool_result", 103.0, {"snapshot_id": "s1", "visible_output": {"content": [{"type": "text", "text": "def wrap(): ..."}]},
                                   "hidden_refs": ["jev:a", "jev:b"], "decisions": [{"level": "hide"}, {"level": "hide"}, {"level": "full"}],
                                   "metrics": {"reason": "routed", "tokens_before_est": 4000, "tokens_shown_est": 1000,
                                               "eligible_tokens_est": 4000, "jev_calls": 1, "jev_ms": 400,
                                               "jev_usage": [{"input_tokens": 3000, "output_tokens": 20}]}}),
        (5, "context", 103.2, {}),
        (6, "model_usage", 105.0, {"usage": usage, "model": "system.ai.gpt-6-sol", "turn": 2, "tool_calls": 0, "text": "Done."}),
        (7, "agent_settled", 105.5, {"text": "Done."}),
    ]


class TracingPlanTests(unittest.TestCase):
    def test_run_becomes_agent_span_with_llm_and_tool_children(self):
        root = plan(events(), {"s1": {"name": "read", "path": "/repo/textwrap.py"}}, content=False)
        self.assertEqual((root["type"], root["start"], root["end"]), ("AGENT", 100.0, 105.5))
        self.assertEqual([c["name"] for c in root["children"]], ["model_call", "read", "model_call"])
        llm, tool, _ = root["children"]
        self.assertEqual((llm["start"], llm["end"]), (100.5, 102.0))
        self.assertEqual(llm["attributes"]["mlflow.chat.tokenUsage"], {"input_tokens": 1310, "output_tokens": 50, "total_tokens": 1360})
        self.assertEqual((tool["start"], tool["end"]), (102.0, 103.0))
        self.assertEqual(tool["attributes"]["jev.hidden_chunks"], 2)
        self.assertEqual(tool["children"][0]["outputs"], {"hide": 2, "full": 1})
        self.assertAlmostEqual(tool["children"][0]["start"], 102.6)
        self.assertEqual(root["attributes"]["jev.retrieval_reduction_pct"], 75.0)
        self.assertEqual(root["attributes"]["pi.model_calls"], 2)

    def test_unpriced_model_makes_run_cost_unknown(self):
        ev = [(i, k, t, {**d, "model": "system.ai.unknown-model"} if k == "model_usage" else d) for i, k, t, d in events()]
        root = plan(ev, {"s1": {"name": "read"}}, content=False)
        self.assertIsNone(root["attributes"]["pi.list_cost_usd"])
        self.assertEqual(root["attributes"]["pi.unpriced_model_calls"], 2)
        priced = plan([(i, k, t, {**d, "model": "system.ai.claude-opus-5-5"} if k == "model_usage" else d) for i, k, t, d in events()],
                      {"s1": {"name": "read"}}, content=False)
        self.assertEqual(priced["attributes"]["pi.unpriced_model_calls"], 0)
        self.assertGreater(priced["attributes"]["pi.list_cost_usd"], 0)

    def test_metadata_mode_withholds_text(self):
        root = plan(events(), {"s1": {"name": "read", "path": "/repo/textwrap.py"}}, content=False)
        blob = repr(root)
        for secret in ("Explain wrap", "def wrap", "/repo/textwrap.py", "Reading.", "Done."):
            self.assertNotIn(secret, blob)
        self.assertIn("prompt withheld: 12 characters", root["inputs"]["messages"][0]["content"])

    def test_full_mode_logs_prompt_output_and_tool_text(self):
        root = plan(events(), {"s1": {"name": "read", "path": "/repo/textwrap.py"}}, content=True)
        self.assertEqual(root["inputs"]["messages"][0]["content"], "Explain wrap")
        self.assertEqual(root["outputs"], {"text": "Done."})
        self.assertEqual(root["children"][1]["inputs"], {"path": "/repo/textwrap.py"})
        self.assertEqual(root["children"][1]["outputs"]["shown_to_model"], "def wrap(): ...")


if __name__ == "__main__":
    unittest.main()
