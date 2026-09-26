from pathlib import Path
import tempfile
import unittest

from jev_router.report import render, sessions
from jev_router.store import Store


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "router.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_session(self, sid, mode, shown, jev_calls):
        usage = {"input": 10, "cacheRead": 1000, "cacheWrite": 2000, "output": 100}
        self.store.event(sid, "session_config", {"mode": mode, "source": "flag"})
        self.store.event(sid, "model_usage", {"usage": usage, "model": "system.ai.gpt-6-sol"})
        self.store.event(sid, "tool_result", {"metrics": {"eligible_tokens_est": 4000, "tokens_before_est": 4000, "tokens_shown_est": shown}})
        if jev_calls:
            self.store.event(sid, "routing", {"metrics": {"jev_calls": jev_calls, "jev_failures": 0, "jev_usage": [{"input_tokens": 5000}]}})

    def test_off_and_on_sessions_are_comparable(self):
        self.run_session("off-run", "off", 4000, 0)
        self.run_session("on-run", "arrival", 1000, 3)
        rows = {r["session"]: r for r in sessions(self.store)}
        off, on = rows["off-run"], rows["on-run"]
        self.assertEqual((off["modes"], on["modes"]), (["off"], ["arrival"]))
        self.assertEqual((off["shown_tokens"], on["shown_tokens"]), (4000, 1000))
        self.assertEqual((off["jev_calls"], on["jev_calls"]), (0, 3))
        self.assertAlmostEqual(off["list_cost_usd"], 10 * 2e-6 + 1000 * 2e-7 + 2000 * 2.5e-6 + 100 * 1e-5)
        self.assertAlmostEqual(on["list_cost_usd"] - off["list_cost_usd"], 5000 * 4.2e-8)
        text = render(sessions(self.store))
        self.assertIn("arrival", text)
        self.assertIn("75%", text)

    def test_unknown_model_is_unpriced_unless_configured(self):
        self.store.event("s", "model_usage", {"usage": {"input": 1000}, "model": "my-local-model"})
        self.assertIsNone(sessions(self.store)[0]["list_cost_usd"])
        prices = {"my-local-model": {"input": 1e-6, "cache_read": 0, "cache_write": 0, "output": 0}}
        self.assertAlmostEqual(sessions(self.store, prices=prices)[0]["list_cost_usd"], 1e-3)

    def test_empty_store(self):
        self.assertIn("No sessions", render(sessions(self.store)))


if __name__ == "__main__":
    unittest.main()
