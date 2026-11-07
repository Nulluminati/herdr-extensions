import json
import unittest

from support.paths import ROOT

POLICY = ROOT / ".pi/sandbox.json"

# Keys that pi's worker sandbox refuses because they weaken isolation.
WEAKENING_KEYS = ("enableWeakerNestedSandbox", "enableWeakerNetworkIsolation", "allowAppleEvents")


class PiSandboxPolicyTests(unittest.TestCase):
    """pi workers refuse to start without a project-owned sandbox policy."""

    def setUp(self):
        self.policy = json.loads(POLICY.read_text(encoding="utf-8"))

    def test_names_allowed_domains_and_filesystem_rules(self):
        self.assertIsInstance(self.policy["network"]["allowedDomains"], list)
        self.assertIsInstance(self.policy["filesystem"], dict)

    def test_does_not_weaken_isolation(self):
        network = self.policy["network"]
        self.assertFalse(network.get("allowAllUnixSockets", False))
        self.assertFalse(network.get("allowUnixSockets"))
        self.assertFalse(network.get("allowLocalBinding", False))
        self.assertNotIn("httpProxyPort", network)
        self.assertNotIn("socksProxyPort", network)
        self.assertFalse(self.policy["filesystem"].get("disabled", False))
        self.assertFalse(self.policy.get("excludedCommands"))
        for key in WEAKENING_KEYS:
            self.assertFalse(self.policy.get(key, False), key)

    def test_reads_and_writes_only_the_worktree(self):
        filesystem = self.policy["filesystem"]
        self.assertIn("~", filesystem["denyRead"])
        self.assertEqual(filesystem["allowWrite"], ["."])
        self.assertIn(".env", filesystem["denyWrite"])

    def test_reads_uv_and_its_python_so_workers_can_run_tests(self):
        # .venv/bin/python links to a uv-managed interpreter under the home directory.
        self.assertEqual(
            self.policy["filesystem"]["allowRead"],
            [".", "~/.local/share/uv/python", "~/.local/bin/uv"],
        )
