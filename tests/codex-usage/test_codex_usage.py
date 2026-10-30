import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from support.paths import ROOT
from support.scripts import load_script

FIXTURES = ROOT / "tests/codex-usage/fixtures"
codex_usage = load_script("codex-usage", "codex_usage")


def codex(pane_id, session="s-1"):
    return {"pane_id": pane_id, "agent": "codex",
            "agent_session": {"agent": "codex", "kind": "id", "value": session}}


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "CODEX_HOME": str(FIXTURES / "codex"),
            "PI_CODING_AGENT_DIR": str(FIXTURES / "pi"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_BIN_PATH", None)
        os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        self.calls = []
        self.agents = [codex("w1:p1"), {"pane_id": "w1:p2", "agent": "claude"}]
        self.failing_pane = None
        self.stderr = io.StringIO()
        replacement = patch.object(codex_usage, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fake_command(self, argv):
        self.calls.append(argv)
        if argv[1:3] == ["agent", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agents": self.agents}}), "")
        if argv[1:3] == ["pane", "report-metadata"]:
            if argv[3] == self.failing_pane:
                raise subprocess.CalledProcessError(1, argv, "", "pane not found")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(f"unexpected command {argv}")

    def run_main(self):
        with contextlib.redirect_stderr(self.stderr):
            codex_usage.main()

    def reports(self):
        result = {}
        for argv in self.calls:
            if argv[1:3] == ["pane", "report-metadata"]:
                self.assertEqual(argv[4:6], ["--source", "nulluminati.codex-usage"])
                result[argv[3]] = argv[8:]
        return result

    def test_codex_pane_gets_context_and_estimated_cost(self):
        self.run_main()
        # Call 1: 600 input, 400 cached, 100 output at the base price. A repeated count is not
        # charged twice. Call 2: 300000 input tokens pass 272000, so the tier price applies.
        self.assertEqual(self.reports(), {
            "w1:p1": ["--token", "codex_context=ctx 60%", "--token", "codex_cost=$0.457"],
        })

    def test_unknown_model_shows_context_only(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(FIXTURES / "missing")
        self.run_main()
        self.assertEqual(self.reports()["w1:p1"],
                         ["--token", "codex_context=ctx 60%", "--clear-token", "codex_cost"])

    def test_a_model_change_prices_each_call_at_its_own_model(self):
        self.agents = [codex("w1:p1", session="s-model-change")]
        self.run_main()
        # 100000 input and 10000 output tokens at gpt-6.1-sol ($0.300), then at gpt-6.1-mini ($0.140).
        self.assertEqual(self.reports()["w1:p1"],
                         ["--token", "codex_context=ctx 22%", "--token", "codex_cost=$0.440"])

    def test_calls_of_an_unpriced_model_do_not_hide_the_cost_of_priced_calls(self):
        self.agents = [codex("w1:p1", session="s-unknown-model")]
        self.run_main()
        self.assertEqual(self.reports()["w1:p1"],
                         ["--token", "codex_context=ctx 22%", "--token", "codex_cost=$0.300"])

    def test_missing_session_log_clears_both_tokens(self):
        self.agents = [codex("w1:p1", session="unknown")]
        self.run_main()
        self.assertEqual(self.reports()["w1:p1"],
                         ["--clear-token", "codex_context", "--clear-token", "codex_cost"])

    def test_codex_without_a_session_clears_both_tokens(self):
        self.agents = [{"pane_id": "w1:p1", "agent": "codex"}]
        self.run_main()
        self.assertEqual(self.reports()["w1:p1"],
                         ["--clear-token", "codex_context", "--clear-token", "codex_cost"])

    def test_a_pane_that_no_longer_runs_codex_loses_its_codex_values(self):
        self.agents = [{"pane_id": "w1:p2", "agent": "claude",
                        "tokens": {"codex_context": "ctx 60%", "codex_cost": "$0.457", "claude_cost": "$1.000"}}]
        self.run_main()
        self.assertEqual(self.reports(), {
            "w1:p2": ["--clear-token", "codex_context", "--clear-token", "codex_cost"],
        })

    def test_an_event_clears_the_codex_values_of_its_pane_after_an_agent_change(self):
        self.agents = [codex("w1:p1"), {"pane_id": "w1:p2", "agent": "pi", "tokens": {"codex_cost": "$0.457"}}]
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
            {"event": "pane.agent_detected", "data": {"pane_id": "w1:p2"}})
        self.run_main()
        self.assertEqual(self.reports(), {
            "w1:p2": ["--clear-token", "codex_context", "--clear-token", "codex_cost"],
        })

    def test_event_reports_only_the_event_pane(self):
        self.agents.append(codex("w1:p3"))
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
            {"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p3"}})
        self.run_main()
        self.assertEqual(list(self.reports()), ["w1:p3"])

    def test_event_without_pane_reports_nothing(self):
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps({"event": "pane.agent_status_changed", "data": {}})
        self.run_main()
        self.assertEqual(self.reports(), {})

    def test_herdr_bin_path_is_used(self):
        os.environ["HERDR_BIN_PATH"] = "/opt/herdr"
        self.run_main()
        self.assertTrue(all(argv[0] == "/opt/herdr" for argv in self.calls))

    def test_failed_report_does_not_stop_other_panes(self):
        self.agents = [codex("w1:p1"), codex("w1:p3")]
        self.failing_pane = "w1:p1"
        self.run_main()
        self.assertIn("w1:p3", self.reports())
        self.assertIn("pane not found", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
