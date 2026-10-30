"""The Herdr startup hook starts the sidebar refresh in the background and returns at once."""
import os
import unittest
from unittest.mock import patch

from support.scripts import load_script

sidebar_refresh = load_script("herdboard", "sidebar_refresh")


class SidebarRefreshHookTests(unittest.TestCase):
    def test_starts_the_refresh_detached_and_does_not_wait(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_ROOT": "/plugins/herdboard"}), \
                patch.object(sidebar_refresh.subprocess, "Popen") as popen:
            self.assertEqual(sidebar_refresh.main(), 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["uv", "run", "--quiet", "--project", "/plugins/herdboard",
                                "herdboard", "sidebar-refresh"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        popen.return_value.wait.assert_not_called()

    def test_without_uv_it_reports_the_error_and_fails(self):
        with patch.object(sidebar_refresh.subprocess, "Popen", side_effect=FileNotFoundError("uv")), \
                patch("sys.stderr") as stderr:
            self.assertEqual(sidebar_refresh.main(), 1)
        self.assertIn("uv", "".join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
