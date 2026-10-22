import re
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "plugins/herdboard/src"
SKILL = ROOT / "plugins/herdboard/skills/herdboard/SKILL.md"
sys.path.insert(0, str(SRC))

from herdboard import cli  # noqa: E402
from herdboard.linear import LinearError, LinearWriteRejectedError, LinearWriteUnknownError  # noqa: E402
from herdboard.linear_source import LinearDisabledError  # noqa: E402
from herdboard.linear_writes import LinearOfflineError, TeamRequiredError  # noqa: E402
from herdboard.model import ArchivedError, CardActiveError, ConflictError, IoError, NotFoundError, UnknownTypeError  # noqa: E402
from herdboard.promote import AlreadyPromotedError, NotATodoError  # noqa: E402


def skill_commands():
    """Every `herdboard ...` line inside the skill's bash blocks."""
    text = SKILL.read_text(encoding="utf-8")
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            if line.startswith("herdboard "):
                commands.append(shlex.split(line, comments=True)[1:])
    return commands


class SkillTests(unittest.TestCase):
    def test_frontmatter_names_the_skill(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: herdboard\ndescription: "))

    def test_every_command_in_the_skill_parses(self):
        commands = skill_commands()
        self.assertGreater(len(commands), 8)
        parser = cli.build_parser()
        for argv in commands:
            with self.subTest(argv=argv):
                parser.parse_args(argv)

    def test_body_examples_do_not_use_literal_newline_escapes(self):
        for argv in skill_commands():
            if "--body" in argv:
                with self.subTest(argv=argv):
                    self.assertNotIn("\\n", argv[argv.index("--body") + 1])

    def test_agents_log_progress_instead_of_editing_the_card(self):
        commands = skill_commands()
        self.assertIn(["--json", "log", "tep3", "Ready for review: 3 commits."], commands)
        text = SKILL.read_text(encoding="utf-8")
        handed = text[text.index("## When a card was handed to you"):text.index("## Errors")]
        self.assertIn("herdboard --json log", handed)
        self.assertNotIn("edit --body", handed)
        self.assertIn("`agent_edit_denied`", text)

    def test_start_documents_the_base_and_its_errors(self):
        self.assertIn(["--json", "start", "tep3", "--base", "feature-x"], skill_commands())
        text = SKILL.read_text(encoding="utf-8")
        for code in ("base_unknown", "upstream_failed"):
            self.assertIn(f"`{code}`", text)

    def test_sub_cards_are_documented(self):
        commands = skill_commands()
        self.assertIn(["--json", "add", "Add invoice table", "--parent", "t5aa"], commands)
        self.assertIn(["--json", "edit", "tep3", "--parent", "t5aa"], commands)
        self.assertIn(["--json", "edit", "tep3", "--no-parent"], commands)

    def test_review_merge_and_finish_are_documented(self):
        commands = skill_commands()
        for argv in (["--json", "review", "tep3"], ["--json", "merge", "tep3"], ["--json", "merge", "tep3", "--pr"],
                     ["--json", "merge", "tep3", "--pr", "--rewrite"], ["--json", "finish", "tep3"],
                     ["--json", "pr-sync", "tep3"], ["--json", "pr-sync", "tep3", "--fetch"]):
            self.assertIn(argv, commands)

    def test_the_card_agent_may_sync_its_own_pull_request_description(self):
        text = SKILL.read_text(encoding="utf-8")
        handed = text[text.index("## When a card was handed to you"):text.index("## Errors")]
        self.assertIn("pr-sync <id>", handed)
        self.assertIn("pr-sync <id> --fetch", handed)
        start = text.index("Ask the user first")
        guarded = text[start:text.index("```bash", start)]
        # The slice must hold the ask-first list, or `assertNotIn` below proves nothing.
        self.assertIn("`merge` and `finish`", guarded)
        self.assertNotIn("pr-sync", guarded)

    def test_agent_review_is_documented(self):
        commands = skill_commands()
        self.assertIn(["--json", "review", "tep3", "--agent", "--reviewer", "pi", "--model", "zai/glm-5.3"], commands)
        self.assertIn(["--json", "review", "tep3", "--both"], commands)
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("agent review", text)
        self.assertIn("agree or disagree", text)
        self.assertIn("Describe each finding", text)

    def test_planning_and_attachments_are_documented(self):
        commands = skill_commands()
        self.assertIn(["--json", "plan", "tep3", "--agent", "claude", "--model", "opus"], commands)
        self.assertIn(["--json", "attach", "tep3", "docs/spec.md"], commands)
        self.assertIn("## When you plan a card", SKILL.read_text(encoding="utf-8"))

    def test_every_error_code_in_the_skill_exists(self):
        text = SKILL.read_text(encoding="utf-8")
        table = text[text.index("| Code |"):]
        named = set(re.findall(r"`([a-z_]+)`", "\n".join(
            line.split("|")[1] for line in table.splitlines() if line.startswith("| `"))))
        known = {cls.code for cls in (
            NotFoundError, ConflictError, LinearWriteUnknownError, LinearOfflineError, LinearError,
            LinearWriteRejectedError, LinearDisabledError, TeamRequiredError,
            AlreadyPromotedError, NotATodoError, IoError)}
        known.add(cli.UsageError.code)
        known |= {cli.ConfirmationRequired.code, cli.LinearDeleteError.code, cli.LinearEditError.code,
                  cli.AgentEditDenied.code}
        known |= {"ambiguous_state", "no_state", "checkout_unknown", "repo_required", "herdr_unavailable", "herdr_error"}
        known |= {"base_unknown", "upstream_failed", "not_started", "hunk_unavailable", "agent_unavailable"}
        known |= {"worktree_dirty", "nothing_to_merge", "base_dirty", "rebase_conflict", "merge_failed", "not_merged"}
        known |= {"gh_unavailable", "push_failed", "pr_failed"}
        known |= {"parent_missing", "parent_nested", "children_open", "parent_not_promoted"}
        known |= {"review_running", "nothing_to_review", "worker_failed", "pr_writing", "plan_running"}
        known |= {"no_pr", "no_description"}
        known |= {"no_repository", "new_repo_family", "path_exists", "new_repo_failed"}
        known |= {ArchivedError.code, CardActiveError.code, cli.LinearArchiveError.code, "has_sub_cards"}
        known.add(UnknownTypeError.code)
        known |= {"remote_unknown", "branch_exists", "agent_running", "workspace_closed", "worktree_missing",
                  "unsent_notes", "gh_failed", "git_failed", "hunk_failed", "new_commits"}
        # Herdr's own error codes pass through `start` unchanged.
        known.add("worktree_create_failed")
        self.assertEqual(named - known, set())


if __name__ == "__main__":
    unittest.main()
