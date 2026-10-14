"""Sub-cards: a todo that points to a parent todo on the same board with `[[#^id]]`."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import cli, merge, start  # noqa: E402
from herdboard.board import card_matches, mark_family  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.model import HerdboardError, parent_of, split_title, with_parent  # noqa: E402
from herdboard.store import CardView, Store  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_merge import MergeTestCase  # noqa: E402


class ParentLinkTests(unittest.TestCase):
    def test_the_link_names_the_parent(self):
        self.assertEqual(parent_of("Add invoice table [[#^t5aa]] #api"), "t5aa")
        self.assertIsNone(parent_of("Add invoice table [[Notes]]"))
        self.assertIsNone(parent_of("Add invoice table [[#Heading]]"))

    def test_with_parent_sets_replaces_and_removes_the_link(self):
        self.assertEqual(with_parent("Add table #api", "t5aa"), "Add table #api [[#^t5aa]]")
        self.assertEqual(with_parent("Add table [[#^t5aa]] #api", "t9zz"), "Add table #api [[#^t9zz]]")
        self.assertEqual(with_parent("Add table [[#^t5aa]] #api", None), "Add table #api")

    def test_the_link_is_not_part_of_the_display_title(self):
        self.assertEqual(split_title("Add table [[#^t5aa]] #api 📅 2026-11-01"), ("Add table", ["api"], "2026-11-01"))


def linear_card(ref, title, parent=None, checked=False):
    return CardView(ref=ref, board="Platform", path=None, column="Todo", title=title, checked=checked,
                    due=None, tags=[], body=[], source="linear", read_only=True, parent=parent)


class LinearFamilyTests(unittest.TestCase):
    def test_sub_issues_show_like_sub_cards(self):
        parent = linear_card("ENG-1", "Billing v2")
        open_child = linear_card("ENG-2", "Add invoice table", parent="ENG-1")
        done_child = linear_card("ENG-3", "Add tax field", parent="ENG-1", checked=True)
        mark_family([parent, open_child, done_child])
        self.assertEqual((open_child.parent_title, parent.sub_cards), ("Billing v2", (1, 2)))
        self.assertIsNone(parent.parent_title)

    def test_a_parent_outside_the_view_shows_its_identifier(self):
        child = linear_card("ENG-2", "Add invoice table", parent="ENG-1")
        mark_family([child])
        self.assertEqual(child.parent_title, "ENG-1")

    def test_list_text_keeps_link_text_in_a_linear_title(self):
        card = linear_card("ENG-2", "Fix [[#^t123]] parsing")
        self.assertEqual(cli._card_text(card), "  ENG-2  Fix [[#^t123]] parsing")

    def test_a_caret_filter_finds_a_sub_issue_family(self):
        child = linear_card("ENG-2", "Add invoice table", parent="ENG-1")
        self.assertTrue(card_matches(child, ["^eng-1"]))
        self.assertFalse(card_matches(linear_card("ENG-5", "Other"), ["^eng-1"]))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(todos_dir=self.root / "todos", vault_root=self.root / "vault")
        (self.root / "vault").mkdir()
        self.store = Store(self.config)
        self.inbox = self.store.ensure_inbox()
        self.parent = self.store.add(self.inbox, "Billing v2 #feature")


class StoreParentTests(StoreTestCase):
    def test_add_with_a_parent(self):
        child = self.store.add(self.inbox, "Add invoice table", parent=self.parent.ref)
        self.assertEqual(child.parent, self.parent.ref)
        self.assertEqual(child.title, f"Add invoice table [[#^{self.parent.ref}]]")
        self.assertIn(f"- [ ] Add invoice table [[#^{self.parent.ref}]] ^{child.ref}",
                      self.inbox.read_text(encoding="utf-8"))
        self.assertIsNone(self.parent.parent)

    def test_the_parent_must_be_on_the_same_board(self):
        projects = self.store.projects_dir
        projects.mkdir(parents=True, exist_ok=True)
        board = projects / "other.md"
        board.write_text("---\nkanban-plugin: board\n---\n\n## Todo\n\n", encoding="utf-8")
        with self.assertRaises(HerdboardError) as raised:
            self.store.add(board, "Elsewhere", parent=self.parent.ref)
        self.assertEqual(raised.exception.code, "parent_missing")

    def test_sub_cards_go_one_level_deep(self):
        child = self.store.add(self.inbox, "Child", parent=self.parent.ref)
        with self.assertRaises(HerdboardError) as raised:
            self.store.add(self.inbox, "Grandchild", parent=child.ref)
        self.assertEqual(raised.exception.code, "parent_nested")
        loner = self.store.add(self.inbox, "Loner")
        with self.assertRaises(HerdboardError) as raised:
            self.store.set_parent(self.parent.ref, loner.ref)
        self.assertEqual(raised.exception.code, "parent_nested")

    def test_a_card_is_not_its_own_parent(self):
        with self.assertRaises(HerdboardError) as raised:
            self.store.set_parent(self.parent.ref, self.parent.ref)
        self.assertEqual(raised.exception.code, "usage")

    def test_set_and_remove_a_parent(self):
        child = self.store.add(self.inbox, "Child #api")
        self.assertEqual(self.store.set_parent(child.ref, self.parent.ref).parent, self.parent.ref)
        removed = self.store.set_parent(child.ref, None)
        self.assertIsNone(removed.parent)
        self.assertEqual(removed.title, "Child #api")

    def test_a_title_edit_keeps_the_parent(self):
        child = self.store.add(self.inbox, "Child", parent=self.parent.ref)
        edited = self.store.edit(child.ref, title="Child renamed #api")
        self.assertEqual(edited.parent, self.parent.ref)
        self.assertEqual(edited.title, f"Child renamed #api [[#^{self.parent.ref}]]")

    def test_the_link_is_not_a_note(self):
        child = self.store.add(self.inbox, "Child [[Real note]]", parent=self.parent.ref)
        self.assertEqual([note["name"] for note in self.store.notes_for(child)], ["Real note"])

    def test_children_of_a_card(self):
        first = self.store.add(self.inbox, "One", parent=self.parent.ref)
        self.store.add(self.inbox, "Loner")
        second = self.store.add(self.inbox, "Two", parent=self.parent.ref)
        self.assertEqual([c.ref for c in self.store.children(self.parent.ref)], [first.ref, second.ref])


class ParentCliTests(CliTestCase):
    def add(self, *args):
        return self.run_json("add", *args, directory=self.plain)["card"]

    def test_add_parent_goes_to_the_parent_board(self):
        board = self.init_repo_board()
        parent = self.run_json("add", "Billing v2", directory=self.repo)["card"]
        child = self.add("Add invoice table", "--parent", parent["ref"])
        self.assertEqual((child["board"], child["parent"]), (parent["board"], parent["ref"]))
        self.assertTrue(board.endswith(".md"))

    def test_edit_parent_and_no_parent(self):
        parent = self.add("Billing v2")
        child = self.add("Child")
        data = self.run_json("edit", child["ref"], "--parent", parent["ref"], directory=self.plain)
        self.assertEqual(data["card"]["parent"], parent["ref"])
        data = self.run_json("edit", child["ref"], "--no-parent", directory=self.plain)
        self.assertIsNone(data["card"]["parent"])

    def test_parent_and_no_parent_together_is_a_usage_error(self):
        child = self.add("Child")
        data = self.run_json("edit", child["ref"], "--parent", "t123", "--no-parent", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_linear_cards_take_no_parent_here(self):
        data = self.run_json("add", "x", "--parent", "ENG-1", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_show_text_names_the_parent(self):
        parent = self.add("Billing v2")
        child = self.add("Child", "--parent", parent["ref"])
        out, _ = self.run_cli("show", child["ref"], directory=self.plain)
        self.assertIn(f"parent: {parent['ref']}", out)

    def test_list_text_hides_the_parent_link(self):
        parent = self.add("Billing v2")
        self.add("Child #api 📅 2026-11-01", "--parent", parent["ref"])
        out, _ = self.run_cli("list", directory=self.plain)
        self.assertNotIn("[[#^", out)
        self.assertRegex(out, r"\n  t[0-9a-z]{3}  Child #api 📅 2026-11-01\n")


class SubCardStartTests(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.parent_card = self.card
        self.card = self.store.add(self.board, "Add invoice table", parent=self.parent_card.ref)

    def test_a_sub_card_starts_from_the_parent_branch(self):
        result = self.starter.start(self.card.ref, agent="pi")
        records = self.records()
        parent = records[self.parent_card.ref]
        self.assertEqual(result["base"], parent["branch"])
        self.assertEqual(parent["base"], self.main_branch())
        self.assertNotIn("agent_name", parent)
        self.assertEqual(self.store.show(self.parent_card.ref).column, "In progress")
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", result["branch"] + "@{u}"), parent["branch"])

    def test_an_explicit_base_wins_over_the_parent(self):
        result = self.starter.start(self.card.ref, agent="pi", base=self.main_branch())
        self.assertEqual(result["base"], self.main_branch())
        self.assertNotIn(self.parent_card.ref, self.records())

    def test_a_parent_with_open_sub_cards_does_not_merge(self):
        self.starter.start(self.card.ref, agent="pi")
        parent_tree = Path(self.records()[self.parent_card.ref]["worktree_path"])
        self.commit(parent_tree, "p.txt", "p\n", "feat: p")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.parent_card.ref)
        self.assertEqual(raised.exception.code, "children_open")
        self.assertIn(self.card.ref, str(raised.exception))
        self.assertEqual(self.merger.merge(self.parent_card.ref, force=True)["mode"], "local")

    def test_a_sub_card_merges_into_the_parent_branch(self):
        self.starter.start(self.card.ref, agent="pi")
        records = self.records()
        child_tree = Path(records[self.card.ref]["worktree_path"])
        head = self.commit(child_tree, "c.txt", "c\n", "feat: c")
        self.merger.merge(self.card.ref)
        self.assertEqual(self.git(self.repo, "rev-parse", records[self.parent_card.ref]["branch"]), head)


class DoctorParentTests(CliTestCase):
    def test_missing_and_nested_parents_are_warnings(self):
        self.todos.mkdir(parents=True, exist_ok=True)
        (self.todos / "inbox.md").write_text(
            "---\nkanban-plugin: board\n---\n\n## Todo\n\n"
            "- [ ] Parent ^t100\n- [ ] Child [[#^t100]] ^t101\n- [ ] Grandchild [[#^t101]] ^t102\n"
            "- [ ] Orphan [[#^t999]] ^t103\n", encoding="utf-8")
        result = self.run_json("doctor", "--offline", directory=self.plain)
        found = {(f["code"], f["level"]) for f in result["findings"] if f["code"].startswith("parent_")}
        self.assertEqual(found, {("parent_missing", "warning"), ("parent_nested", "warning")})


if __name__ == "__main__":
    unittest.main()
