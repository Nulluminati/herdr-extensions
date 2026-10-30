"""Reviewer sandbox options: any shell command, writes in one folder, shell network to GitHub only."""
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import sandbox

FOLDER = Path("/state/reviews/r1")
REPO = Path("/work/repo")


def claude_settings(args):
    return json.loads(args[args.index("--settings") + 1])["sandbox"]


def pi_policy(args):
    return json.loads(args[args.index("--sandbox") + 1])


def codex_value(args, key):
    """The TOML value of one `-c key=value` option."""
    found = [arg for arg in args if arg.startswith(key + "=")]
    return tomllib.loads(found[0])


class PortTests(unittest.TestCase):
    def test_hunk_port_defaults_to_47657(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sandbox.hunk_port(), 47657)

    def test_hunk_port_ignores_a_value_that_is_not_a_number(self):
        with patch.dict(os.environ, {"HUNK_MCP_PORT": "abc"}):
            self.assertEqual(sandbox.hunk_port(), 47657)

    def test_hunk_port_follows_the_environment(self):
        with patch.dict(os.environ, {"HUNK_MCP_PORT": "5000"}):
            self.assertEqual(sandbox.hunk_port(), 5000)


class GitFolderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.main, self.linked = self.root / "main", self.root / "linked"
        self.main.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(self.main)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run([*git, "worktree", "add", "-q", str(self.linked), "-b", "card"], check=True)

    def test_a_linked_worktree_also_denies_its_shared_git_folder(self):
        expected = [str(self.linked), str(self.main / ".git")]
        for options in (sandbox.claude_reviewer_options, sandbox.pi_reviewer_options):
            with self.subTest(options=options.__name__):
                args = options(None, self.linked, browser=False, hunk=False)
                policy = claude_settings(args) if "--settings" in args else pi_policy(args)
                self.assertEqual(policy["filesystem"]["denyWrite"], expected)

    def test_a_main_checkout_denies_only_itself(self):
        settings = claude_settings(sandbox.claude_reviewer_options(None, self.main, browser=False, hunk=False))
        self.assertEqual(settings["filesystem"]["denyWrite"], [str(self.main)])


class ClaudeTests(unittest.TestCase):
    def test_settings_hold_a_strict_sandbox_with_github_network(self):
        args = sandbox.claude_reviewer_options(FOLDER, REPO, browser=False, hunk=False)
        self.assertEqual(args[:2], ["--permission-mode", "dontAsk"])
        settings = claude_settings(args)
        self.assertTrue(settings["enabled"])
        self.assertTrue(settings["failIfUnavailable"])
        self.assertFalse(settings["allowUnsandboxedCommands"])
        self.assertEqual(settings["network"]["allowedDomains"],
                         ["github.com", "*.github.com", "*.githubusercontent.com"])
        self.assertEqual(settings["filesystem"], {"allowWrite": [str(FOLDER)], "denyWrite": [str(REPO)]})
        self.assertNotIn("excludedCommands", settings)

    def test_no_writable_folder_means_no_allow_write(self):
        settings = claude_settings(sandbox.claude_reviewer_options(None, REPO, browser=False, hunk=False))
        self.assertEqual(settings["filesystem"], {"denyWrite": [str(REPO)]})

    def test_browser_and_hunk_are_excluded_from_the_sandbox(self):
        settings = claude_settings(sandbox.claude_reviewer_options(None, REPO, browser=True, hunk=False))
        self.assertEqual(settings["excludedCommands"], ["terminal-browser *"])
        settings = claude_settings(sandbox.claude_reviewer_options(None, REPO, browser=True, hunk=True))
        self.assertEqual(settings["excludedCommands"], ["terminal-browser *", "hunk session *"])
        # Claude's sandbox connects to 127.0.0.1 without the proxy, so a domain entry does not help.
        self.assertNotIn("127.0.0.1:47657", settings["network"]["allowedDomains"])

    def test_tools_allow_the_shell_web_skills_and_documentation(self):
        tools = sandbox.claude_reviewer_tools(None)
        self.assertEqual(tools[0], "--allowedTools")
        for tool in ("Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "Skill", "mcp__context7",
                     "mcp__grep_app"):
            self.assertIn(tool, tools)
        self.assertFalse(any(tool.startswith(("Edit", "Write")) for tool in tools))

    def test_tools_allow_edits_only_in_the_writable_folder(self):
        tools = sandbox.claude_reviewer_tools(FOLDER)
        self.assertIn(f"Edit(/{FOLDER}/**)", tools)
        self.assertIn(f"Write(/{FOLDER}/**)", tools)


class PiTests(unittest.TestCase):
    def test_options_turn_approval_off_and_pass_the_policy(self):
        args = sandbox.pi_reviewer_options(FOLDER, REPO, browser=False, hunk=False)
        self.assertEqual(args[:2], ["--approval-mode", "off"])
        policy = pi_policy(args)
        self.assertEqual(policy["filesystem"], {"allowWrite": [str(FOLDER)], "denyWrite": [str(REPO)]})
        self.assertEqual(policy["network"]["allowedDomains"],
                         ["github.com", "*.github.com", "*.githubusercontent.com"])
        self.assertNotIn("excludedCommands", policy)

    def test_tools_include_documentation_and_write_only_with_a_folder(self):
        args = sandbox.pi_reviewer_options(FOLDER, REPO, browser=False, hunk=False)
        tools = args[args.index("--tools") + 1].split(",")
        for tool in ("read", "grep", "find", "ls", "bash", "write", "web_fetch", "web_search",
                     "context7_resolve_library_id", "context7_get_library_docs",
                     "context7_get_cached_doc_raw", "code_search"):
            self.assertIn(tool, tools)
        args = sandbox.pi_reviewer_options(None, REPO, browser=False, hunk=False)
        self.assertNotIn("write", args[args.index("--tools") + 1].split(","))
        self.assertNotIn("allowWrite", pi_policy(args)["filesystem"])

    def test_browser_is_excluded_and_hunk_gets_its_port(self):
        policy = pi_policy(sandbox.pi_reviewer_options(None, REPO, browser=True, hunk=False))
        self.assertEqual(policy["excludedCommands"], ["terminal-browser *"])
        self.assertNotIn("127.0.0.1:47657", policy["network"]["allowedDomains"])
        with patch.dict(os.environ, {"HUNK_MCP_PORT": "5000"}):
            policy = pi_policy(sandbox.pi_reviewer_options(None, REPO, browser=True, hunk=True))
        self.assertIn("127.0.0.1:5000", policy["network"]["allowedDomains"])
        self.assertEqual(policy["excludedCommands"], ["terminal-browser *"])


class CodexTests(unittest.TestCase):
    def test_profile_reads_everything_and_writes_the_folder(self):
        args = sandbox.codex_reviewer_options("hb-review", FOLDER, hunk=False)
        self.assertEqual(args[:2], ["-a", "never"])
        for option in ("features.network_proxy=true", 'web_search="live"', 'default_permissions="hb-review"'):
            self.assertIn(option, args)
        filesystem = codex_value(args, "permissions.hb-review.filesystem")["permissions"]["hb-review"]["filesystem"]
        self.assertEqual(filesystem, {":root": "read", str(FOLDER): "write"})
        network = codex_value(args, "permissions.hb-review.network")["permissions"]["hb-review"]["network"]
        self.assertEqual(network, {"enabled": True, "domains": {"**.github.com": "allow",
                                                                "**.githubusercontent.com": "allow"}})

    def test_profile_without_a_folder_writes_nothing(self):
        args = sandbox.codex_reviewer_options("hb-pr-review", None, hunk=False)
        filesystem = codex_value(args, "permissions.hb-pr-review.filesystem")
        self.assertEqual(filesystem["permissions"]["hb-pr-review"]["filesystem"], {":root": "read"})

    def test_profile_quotes_the_folder(self):
        folder = Path('/reviews/a"b\\c')
        args = sandbox.codex_reviewer_options("hb-review", folder, hunk=False)
        filesystem = codex_value(args, "permissions.hb-review.filesystem")["permissions"]["hb-review"]["filesystem"]
        self.assertEqual(filesystem, {":root": "read", str(folder): "write"})

    def test_hunk_adds_its_local_port(self):
        with patch.dict(os.environ, {"HUNK_MCP_PORT": "5000"}):
            args = sandbox.codex_reviewer_options("hb-pr-review", None, hunk=True)
        domains = codex_value(args, "permissions.hb-pr-review.network")["permissions"]["hb-pr-review"]["network"]
        self.assertEqual(domains["domains"]["127.0.0.1:5000"], "allow")


if __name__ == "__main__":
    unittest.main()
