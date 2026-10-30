import unittest
from unittest import mock

from herdboard import kanban_md
from herdboard.board import HerdboardApp
from herdboard.linear_links import LabelLink, ProjectLink, with_link
from herdboard.model import FormatError, HerdboardError
from support.herdboard_board import BoardTestCase
from support.herdboard_cli import CliTestCase
from support.herdboard_linear_cli import LinearCliCase

FRONT = "---\n\nkanban-plugin: board\nrepo: github.com/acme/api\n\n---\n\n## Todo\n"


class FrontmatterValueTests(unittest.TestCase):
    def test_adds_a_key_before_the_trailing_blank_line(self):
        text = kanban_md.set_frontmatter_value(FRONT, "merge", "pr")
        self.assertEqual(text, FRONT.replace("api\n", "api\nmerge: pr\n"))

    def test_replaces_a_key_in_place(self):
        before = FRONT.replace("board\n", "board\nmerge: local\n")
        text = kanban_md.set_frontmatter_value(before, "merge", "pr")
        self.assertEqual(text, FRONT.replace("board\n", "board\nmerge: pr\n"))

    def test_none_removes_the_key_and_only_the_key(self):
        before = FRONT.replace("board\n", "board\nmerge: local\n")
        self.assertEqual(kanban_md.set_frontmatter_value(before, "merge", None), FRONT)

    def test_none_without_the_key_changes_nothing(self):
        self.assertEqual(kanban_md.set_frontmatter_value(FRONT, "merge", None), FRONT)

    def test_crlf_is_kept(self):
        before = FRONT.replace("\n", "\r\n")
        text = kanban_md.set_frontmatter_value(before, "merge", "pr")
        self.assertEqual(text, FRONT.replace("api\n", "api\nmerge: pr\n").replace("\n", "\r\n"))

    def test_unclosed_frontmatter_is_a_format_error(self):
        with self.assertRaisesRegex(FormatError, "frontmatter must be closed"):
            kanban_md.set_frontmatter_value("---\nkanban-plugin: board\n## Todo\n", "merge", "pr")

    def test_a_nested_value_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge:\n  mode: pr\n")
        with self.assertRaisesRegex(FormatError, "'merge' has a nested value"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")

    def test_an_indented_comment_or_blank_line_after_the_key_stays(self):
        for extra in ("  # Merge policy", "  "):
            before = FRONT.replace("board\n", f"board\nmerge: local\n{extra}\n")
            text = kanban_md.set_frontmatter_value(before, "merge", "pr")
            self.assertEqual(text, FRONT.replace("board\n", f"board\nmerge: pr\n{extra}\n"))

    def test_a_nested_value_after_a_comment_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge:\n  # policy\n\n  mode: pr\n")
        with self.assertRaisesRegex(FormatError, "nested value"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")

    def test_a_duplicate_key_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge: pr\nmerge: local\n")
        with self.assertRaisesRegex(FormatError, "duplicate key 'merge'"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")


class WithLinkTests(unittest.TestCase):
    def test_a_new_project_goes_last(self):
        links = [ProjectLink("p1", "One")]
        self.assertEqual(
            with_link(links, ProjectLink("p2", "Two")), [ProjectLink("p1", "One"), ProjectLink("p2", "Two")]
        )

    def test_a_linked_project_is_replaced_in_its_place(self):
        links = [ProjectLink("p1", "One"), ProjectLink("p2", "Two")]
        changed = ProjectLink("p1", "One", [LabelLink("l1", "repo:api")])
        self.assertEqual(with_link(links, changed), [changed, ProjectLink("p2", "Two")])


class StoreMergeTests(BoardTestCase):
    def test_set_merge_writes_and_removes_the_key(self):
        self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
        self.store.set_merge(self.repo_path, None)
        self.assertIsNone(self.store.board_meta(self.repo_path).merge)
        self.assertNotIn("merge:", self.repo_path.read_text())

    def test_set_merge_refuses_an_unknown_mode(self):
        before = self.repo_path.read_text()
        with self.assertRaisesRegex(HerdboardError, "local or pr"):
            self.store.set_merge(self.repo_path, "squash")
        self.assertEqual(self.repo_path.read_text(), before)

    def test_merge_and_links_change_in_one_write(self):
        self.store.update_links(self.repo_path, lambda links: [ProjectLink("p1", "One")])
        self.store.update_config(
            self.repo_path, merge="pr", change_links=lambda links: links + [ProjectLink("p2", "Two")]
        )
        meta = self.store.board_meta(self.repo_path)
        self.assertEqual((meta.merge, [link.id for link in meta.links]), ("pr", ["p1", "p2"]))

    def test_a_failed_link_change_leaves_the_merge_mode(self):
        text = self.repo_path.read_text().replace("repo:", "other: 1\nother: 2\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "duplicate key 'other'"):
            self.store.update_config(self.repo_path, merge="pr", change_links=lambda links: [])
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_nested_merge_value_is_not_written(self):
        text = self.repo_path.read_text().replace("repo:", "merge:\n  mode: pr\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "nested value"):
            self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_change_that_makes_the_frontmatter_invalid_is_not_written(self):
        text = self.repo_path.read_text()
        with mock.patch.object(
            kanban_md,
            "set_frontmatter_value",
            side_effect=lambda text, key, value: text.replace("repo:", "bad: [\nrepo:"),
        ):
            with self.assertRaisesRegex(FormatError, "invalid YAML"):
                self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_duplicate_merge_key_is_not_written(self):
        text = self.repo_path.read_text().replace("repo:", "merge: pr\nmerge: local\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "duplicate key 'merge'"):
            self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)


def board_arguments(case, *args):
    """The keyword arguments that the `board` command gives the app."""
    seen = {}
    with (
        mock.patch.object(HerdboardApp, "__init__", lambda self, store, view, **kw: seen.update(kw)),
        mock.patch.object(HerdboardApp, "run"),
    ):
        case.run_cli("board", *args, directory=case.repo)
    return seen


class BoardCommandTests(CliTestCase):
    def test_without_linear_the_board_gets_no_project_lookup(self):
        self.init_repo_board()
        self.assertIsNone(board_arguments(self)["resolve_link"])

    def test_the_board_shows_agent_logos_when_the_font_is_installed(self):
        self.init_repo_board()
        for installed in (True, False):
            with (
                self.subTest(installed=installed),
                mock.patch("herdboard.board.agent_font_installed", return_value=installed),
            ):
                self.assertEqual(board_arguments(self)["agent_logos"], installed)

    def test_prs_opens_the_board_on_the_pull_request_list(self):
        self.init_repo_board()
        self.assertFalse(board_arguments(self)["open_prs"])
        self.assertTrue(board_arguments(self, "--prs")["open_prs"])


class BoardCommandLinearTests(LinearCliCase):
    def test_the_board_looks_up_a_project_and_its_labels(self):
        self.board()
        resolve = board_arguments(self)["resolve_link"]
        self.assertEqual(
            resolve("platform", ["repo:api"]), ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")])
        )
        with self.assertRaisesRegex(HerdboardError, "No Linear project named 'Nope'"):
            resolve("Nope", [])

    def test_an_offline_board_gets_no_project_lookup(self):
        self.board()
        self.assertIsNone(board_arguments(self, "--offline")["resolve_link"])


if __name__ == "__main__":
    unittest.main()
