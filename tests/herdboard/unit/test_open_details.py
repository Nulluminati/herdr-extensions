import json
import os
import subprocess
import unittest
from unittest.mock import patch

from support.scripts import load_script

open_details = load_script("herdboard", "open_details")


class OpenDetailsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.code = 0
        environment = patch.dict(os.environ, {"HERDR_BIN_PATH": "herdr", "HERDR_PLUGIN_ROOT": "/plugin"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("HERDR_PLUGIN_CONTEXT_JSON", "HERDR_PANE_ID"):
            os.environ.pop(name, None)

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "uv":
            return subprocess.CompletedProcess(argv, self.code, "", "herdboard: No card" if self.code else "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def run_script(self):
        with patch.object(open_details.subprocess, "run", self.fake_run):
            return open_details.main()

    def test_opens_the_pane_for_this_herdr_pane(self):
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(
            self.calls,
            [["uv", "run", "--quiet", "--project", "/plugin", "herdboard", "details", "--open", "--pane", "w1:p3"]],
        )

    def test_falls_back_to_the_focused_pane_of_the_context(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_id": "w2:p1"})
        self.run_script()
        self.assertEqual(self.calls[0][-2:], ["--pane", "w2:p1"])

    def test_a_failure_shows_a_notification(self):
        self.code = 1
        with patch("sys.stderr"):
            self.assertEqual(self.run_script(), 1)
        self.assertEqual(
            self.calls[1],
            ["herdr", "notification", "show", "herdboard: no details pane", "--body", "herdboard: No card"],
        )


if __name__ == "__main__":
    unittest.main()
