import unittest
from unittest import mock

from herdboard.board import HerdboardApp
from herdboard.linear_links import LabelLink, ProjectLink, with_link
from herdboard.model import FormatError, HerdboardError
from support.herdboard_board import BoardTestCase
from support.herdboard_cli import CliTestCase
from support.herdboard_linear_cli import LinearCliCase


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
    def board_file(self):
        return self.repo_path / "board.toml"

    def test_set_merge_writes_and_removes_the_key(self):
        self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
        self.assertIn('merge = "pr"', self.board_file().read_text())
        self.store.set_merge(self.repo_path, None)
        self.assertIsNone(self.store.board_meta(self.repo_path).merge)
        self.assertNotIn("merge", self.board_file().read_text())

    def test_set_merge_refuses_an_unknown_mode(self):
        before = self.board_file().read_text()
        with self.assertRaisesRegex(HerdboardError, "local or pr"):
            self.store.set_merge(self.repo_path, "squash")
        self.assertEqual(self.board_file().read_text(), before)

    def test_merge_and_links_change_in_one_write(self):
        self.store.update_links(self.repo_path, lambda links: [ProjectLink("p1", "One")])
        self.store.update_config(
            self.repo_path, merge="pr", change_links=lambda links: links + [ProjectLink("p2", "Two")]
        )
        meta = self.store.board_meta(self.repo_path)
        self.assertEqual((meta.merge, [link.id for link in meta.links]), ("pr", ["p1", "p2"]))

    def test_a_failed_link_change_leaves_the_merge_mode(self):
        text = self.board_file().read_text().replace("repo =", "linear-projects = 3\nrepo =", 1)
        self.board_file().write_text(text)
        with self.assertRaisesRegex(FormatError, "linear-projects"):
            self.store.update_config(self.repo_path, merge="pr", change_links=lambda links: [])
        self.assertEqual(self.board_file().read_text(), text)

    def test_an_invalid_board_file_is_not_written(self):
        text = self.board_file().read_text().replace("repo =", "merge = 3\nrepo =", 1)
        self.board_file().write_text(text)
        with self.assertRaises(FormatError):
            self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.board_file().read_text(), text)

    def test_unknown_keys_and_comments_stay(self):
        text = '# Board settings\nowner = "nate"\n' + self.board_file().read_text()
        self.board_file().write_text(text)
        self.store.set_merge(self.repo_path, "pr")
        self.store.set_merge(self.repo_path, None)
        self.assertEqual(self.board_file().read_text(), text)


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
