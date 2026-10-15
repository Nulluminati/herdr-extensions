import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.brief import agent_message, brief_text, card_token  # noqa: E402
from herdboard.store import CardView  # noqa: E402


def todo(**changes):
    fields = dict(
        ref="t83k", board="nulluminati-dotfiles-ansible", path=Path("/todos/projects/x.md"),
        column="In progress", title="Move to shared [[Stow notes]] #dots-todo 📅 2026-11-01",
        checked=False, due="2026-11-01", tags=["dots-todo"], body=["\t- bat", "\t- [ ] fish", "\t\t- nested"],
    )
    fields.update(changes)
    return CardView(**fields)


def issue(**changes):
    fields = dict(
        ref="ENG-12", board="Platform", path=None, column="In progress", title="Fix the login timeout",
        checked=False, due=None, tags=["auth", "bug"], body=["The session ends after 5 minutes.", "", "Steps:"],
        source="linear", url="https://linear.app/acme/issue/ENG-12", state="In Progress", team="ENG",
    )
    fields.update(changes)
    return CardView(**fields)


class BriefTests(unittest.TestCase):
    def brief(self, card, notes=()):
        return brief_text(card, notes=list(notes), repository="github.com/nulluminati/dotfiles-ansible",
                          worktree="/home/n/.herdr/worktrees/dotfiles-ansible/t83k-move-to-shared",
                          branch="t83k-move-to-shared", base="main", command="herdboard")

    def test_todo_brief_has_the_card_and_where_to_work(self):
        text = self.brief(todo())
        self.assertTrue(text.startswith("# Move to shared [[Stow notes]]\n"))
        for part in [
            "herdboard card `t83k`", "board `nulluminati-dotfiles-ansible`", "column `In progress`",
            "Tags: dots-todo", "Due: 2026-11-01",
            "github.com/nulluminati/dotfiles-ansible",
            "/home/n/.herdr/worktrees/dotfiles-ansible/t83k-move-to-shared", "`t83k-move-to-shared`",
        ]:
            self.assertIn(part, text)

    def test_a_typed_card_names_its_type_and_commit_type(self):
        text = self.brief(todo(type="bug"))
        self.assertIn("- Type: bug", text)
        self.assertIn("The card is a bug: use `fix` as the conventional commit type of the main change.", text)

    def test_a_card_without_a_type_names_none(self):
        text = self.brief(todo())
        self.assertNotIn("Type:", text)
        self.assertNotIn("The card is a", text)

    def test_the_brief_names_the_base_branch(self):
        self.assertIn("- Base: `main`. herdboard merges the branch into it after the review.", self.brief(todo()))

    def test_a_restarted_agent_gets_a_note_and_the_log(self):
        log = [{"time": "2026-10-12 12:00", "author": "pi", "message": "Two commits.\nTests pass."}]
        text = brief_text(todo(), notes=[], repository="r", worktree="w", branch="b", base="main",
                          command="herdboard", note="The card is in review.", log=log)
        intro = "column `In progress`.\n\n"
        self.assertIn(intro + "The card is in review.\n\n", text)
        self.assertIn("## Activity log\n\n- 2026-10-12 12:00 · pi · Two commits.\n\tTests pass.\n\n", text)

    def test_a_new_agent_gets_no_note_and_no_log(self):
        text = self.brief(todo())
        self.assertNotIn("## Activity log", text)

    def test_details_keep_their_nesting_without_the_card_indent(self):
        text = self.brief(todo())
        self.assertIn("## Details\n\n- bat\n- [ ] fish\n\t- nested\n", text)

    def test_a_card_without_details_says_so(self):
        self.assertIn("## Details\n\nNone. The title is the whole task.\n", self.brief(todo(body=[])))

    def test_linked_notes_are_included(self):
        notes = [{"name": "Stow notes", "content": "Use stow -t ~\n"}, {"name": "Gone", "content": None}]
        text = self.brief(todo(), notes)
        self.assertIn("## Linked notes\n\n### Stow notes\n\nUse stow -t ~\n", text)
        self.assertIn("### Gone\n\nThe note was not found.\n", text)

    def test_the_plan_is_included_and_other_attachments_are_listed(self):
        files = [{"name": "plan.md", "path": "/todos/.herdboard/attachments/t83k/plan.md", "size": 20},
                 {"name": "spec.pdf", "path": "/todos/.herdboard/attachments/t83k/spec.pdf", "size": 9000}]
        text = brief_text(todo(), notes=[], repository="r", worktree="w", branch="b", base="main",
                          command="herdboard", plan="## Steps\n\n1. Add stow.\n", attachments=files)
        self.assertIn("## Plan\n\nYou and the user agreed on this plan before the work started. "
                      "Follow it. Log each change to it.\n\n## Steps\n\n1. Add stow.\n\n", text)
        self.assertIn("## Attachments\n\nRead them when you need them.\n\n"
                      "- `/todos/.herdboard/attachments/t83k/spec.pdf`\n", text)
        self.assertNotIn("attachments/t83k/plan.md", text)

    def test_a_card_without_a_plan_or_attachments_has_neither_section(self):
        text = self.brief(todo())
        self.assertNotIn("## Plan", text)
        self.assertNotIn("## Attachments", text)

    def test_how_to_update_the_card(self):
        for card in (todo(), issue()):
            with self.subTest(source=card.source):
                text = self.brief(card)
                for part in [
                    f"herdboard --json show {card.ref}",
                    f'herdboard --json log {card.ref} "..."',
                    "Do not change the card title or details",
                    "Commit every change on the branch `t83k-move-to-shared`",
                    f"herdboard --json move {card.ref} Review",
                    f'herdboard --json move {card.ref} "In progress"',
                    "Do not push, merge, rebase onto another branch, or remove the worktree",
                    "Do not move the card to Done",
                ]:
                    self.assertIn(part, text)
                self.assertNotIn("--json edit", text)

    def test_the_cli_command_is_used_as_given(self):
        text = brief_text(todo(), notes=[], repository="r", worktree="w", branch="b", base="main",
                          command="'/opt/py 3/bin/python' -m herdboard")
        self.assertIn("'/opt/py 3/bin/python' -m herdboard --json show t83k", text)

    def test_linear_brief_has_the_issue_and_its_description(self):
        text = self.brief(issue())
        for part in [
            "Linear issue `ENG-12`", "https://linear.app/acme/issue/ENG-12", "State: In Progress", "Team: ENG",
            "Labels: auth, bug", "## Description\n\nThe session ends after 5 minutes.\n\nSteps:\n",
            "Ask the user before you change the Linear issue",
        ]:
            self.assertIn(part, text)
        self.assertNotIn("## Details", text)


class MessageTests(unittest.TestCase):
    def test_one_line_that_points_to_the_brief(self):
        message = agent_message(todo(), Path("/state/briefs/t83k.md"))
        self.assertEqual(
            message,
            "Start work on herdboard card t83k: Move to shared [[Stow notes]]. "
            "Read the brief at /state/briefs/t83k.md first; it has the details and how to update the card.",
        )

    def test_control_characters_never_reach_the_message(self):
        message = agent_message(todo(title="bad\x7f title\x1b[31m"), Path("/b.md"))
        self.assertNotIn("\x7f", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)


class TokenTests(unittest.TestCase):
    def test_title_and_column(self):
        self.assertEqual(card_token(todo()), "▶ Move to shared [[Stow notes]] · In progress")

    def test_long_titles_are_cut(self):
        token = card_token(todo(title="word " * 30))
        self.assertLessEqual(len(token), 60)
        self.assertTrue(token.endswith("… · In progress"))

    def test_a_done_card(self):
        self.assertEqual(card_token(todo(column="Done", checked=True)),
                         "✓ Move to shared [[Stow notes]] · Done")


if __name__ == "__main__":
    unittest.main()
