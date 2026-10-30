import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch
from support.scripts import load_script


agent_icons = load_script("agent-icons", "agent_icons")


class AgentIconsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_BIN_PATH", None)
        os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
        self.calls = []
        self.font = "Herdr Agent Icons Max"
        self.agents = [
            {"pane_id": "w1:p1", "agent": "claude"},
            {"pane_id": "w1:p2", "agent": "pi"},
        ]
        self.panes = {"w1:p1": {"pane_id": "w1:p1", "agent": "claude"}}
        self.failing_pane = None
        self.stderr = io.StringIO()
        replacement = patch.object(agent_icons, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fake_command(self, argv):
        self.calls.append(argv)
        if argv[0] == "fc-match":
            if self.font is None:
                raise FileNotFoundError("fc-match")
            return subprocess.CompletedProcess(argv, 0, self.font + "\n", "")
        if argv[1:3] == ["agent", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agents": self.agents}}), "")
        if argv[1:3] == ["pane", "get"]:
            pane = self.panes[argv[3]]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"pane": pane}}), "")
        if argv[1:3] == ["pane", "report-metadata"]:
            if argv[3] == self.failing_pane:
                raise subprocess.CalledProcessError(1, argv, "", "pane not found")
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(f"unexpected command {argv}")

    def run_main(self):
        with contextlib.redirect_stderr(self.stderr):
            agent_icons.main()

    def reports(self):
        result = {}
        for argv in self.calls:
            if argv[1:3] == ["pane", "report-metadata"]:
                self.assertEqual(argv[4:6], ["--source", "nulluminati.agent-icons"])
                result[argv[3]] = argv[-2:]
        return result

    def test_font_found_writes_vendor_logo(self):
        self.run_main()
        self.assertEqual(self.reports(), {
            "w1:p1": ["--token", "agent_icon="],
            "w1:p2": ["--token", "agent_icon="],
        })

    def test_font_missing_clears_every_token(self):
        self.font = "DejaVu Sans"
        self.run_main()
        self.assertEqual(self.reports(), {
            "w1:p1": ["--clear-token", "agent_icon"],
            "w1:p2": ["--clear-token", "agent_icon"],
        })

    def test_fc_match_not_installed_counts_as_missing(self):
        self.font = None
        self.run_main()
        self.assertEqual(self.reports()["w1:p1"], ["--clear-token", "agent_icon"])

    def test_family_match_is_exact(self):
        self.font = "Herdr Agent Icons Max Extra"
        self.assertFalse(agent_icons.font_installed())
        self.font = "Foo,Herdr Agent Icons Max"
        self.assertTrue(agent_icons.font_installed())

    def test_unknown_or_missing_agent_clears_token(self):
        self.agents = [{"pane_id": "w1:p1", "agent": "zzz"}, {"pane_id": "w1:p2"}]
        self.run_main()
        self.assertEqual(self.reports(), {
            "w1:p1": ["--clear-token", "agent_icon"],
            "w1:p2": ["--clear-token", "agent_icon"],
        })

    def test_event_reports_only_the_event_pane(self):
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps({"event": "pane.agent_detected", "data": {"pane": {"pane_id": "w1:p1"}}})
        self.run_main()
        self.assertEqual(self.reports(), {"w1:p1": ["--token", "agent_icon="]})
        self.assertFalse(any(argv[1:3] == ["agent", "list"] for argv in self.calls))

    def test_event_without_pane_reports_nothing(self):
        os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps({"event": "pane.agent_detected", "data": {}})
        self.run_main()
        self.assertEqual(self.reports(), {})

    def test_herdr_bin_path_is_used(self):
        os.environ["HERDR_BIN_PATH"] = "/opt/herdr"
        self.run_main()
        herdr_calls = [argv for argv in self.calls if argv[0] != "fc-match"]
        self.assertTrue(herdr_calls)
        self.assertTrue(all(argv[0] == "/opt/herdr" for argv in herdr_calls))

    def test_failed_report_does_not_stop_other_panes(self):
        self.failing_pane = "w1:p1"
        self.run_main()
        self.assertIn("w1:p2", self.reports())
        self.assertIn("pane not found", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
