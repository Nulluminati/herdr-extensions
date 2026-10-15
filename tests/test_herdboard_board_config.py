import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import kanban_md  # noqa: E402
from herdboard.board import ConfigScreen, HerdboardApp  # noqa: E402
from herdboard.linear_links import LabelLink, ProjectLink, with_link  # noqa: E402
from herdboard.model import FormatError, HerdboardError  # noqa: E402
from test_herdboard_board import BoardTestCase, settle  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_linear_cli import LinearCliCase  # noqa: E402

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


class WithLinkTests(unittest.TestCase):
    def test_a_new_project_goes_last(self):
        links = [ProjectLink("p1", "One")]
        self.assertEqual(with_link(links, ProjectLink("p2", "Two")),
                         [ProjectLink("p1", "One"), ProjectLink("p2", "Two")])

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


class ConfigScreenTests(BoardTestCase):
    def make_app(self, *args, resolve_link=None, **kwargs):
        app = super().make_app(*args, **kwargs)
        app.resolve_link = resolve_link
        return app

    def link(self, link):
        self.store.update_links(self.repo_path, lambda links: links + [link])

    async def open_config(self, pilot):
        await pilot.press("c")
        await settle(pilot)
        self.assertIsInstance(pilot.app.screen, ConfigScreen)
        return pilot.app.screen

    async def test_c_shows_the_board_merge_mode_and_links(self):
        self.store.set_merge(self.repo_path, "pr")
        self.link(ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")]))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertIn(self.repo_path.stem, screen.query_one("#config-heading").plain)
            self.assertEqual(screen.query_one("#config-merge").value, "pr")
            links = screen.query_one("#config-links")
            self.assertEqual(links.option_count, 1)
            self.assertIn("Platform", str(links.get_option_at_index(0).prompt))
            self.assertIn("repo:api", str(links.get_option_at_index(0).prompt))

    async def test_saving_a_merge_mode_writes_the_frontmatter(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertEqual(screen.query_one("#config-merge").value, "default")
            screen.query_one("#config-merge").value = "pr"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")

    async def test_the_config_default_removes_the_merge_key(self):
        self.store.set_merge(self.repo_path, "local")
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "default"
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertNotIn("merge:", self.repo_path.read_text())

    async def test_enter_on_a_link_marks_it_for_removal_and_save_removes_it(self):
        self.link(ProjectLink("p1", "Platform"))
        self.link(ProjectLink("p2", "Billing"))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-links").focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("remove", str(screen.query_one("#config-links").get_option_at_index(0).prompt))
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertEqual(self.store.board_meta(self.repo_path).links, [ProjectLink("p2", "Billing")])

    async def test_enter_again_keeps_the_link(self):
        self.link(ProjectLink("p1", "Platform"))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-links").focus()
            await pilot.press("enter", "enter", "ctrl+s")
            await settle(pilot)
        self.assertEqual(self.store.board_meta(self.repo_path).links, [ProjectLink("p1", "Platform")])

    async def test_saving_a_project_name_links_it_with_its_labels(self):
        asked = []

        def resolve(name, labels):
            asked.append((name, labels))
            return ProjectLink("p9", "API Platform", [LabelLink("l1", "repo:api"), LabelLink("l2", "ENG/bug")])

        app = self.make_app(resolve_link=resolve)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-project").value = "API Platform"
            screen.query_one("#config-labels").value = "repo:api, ENG/bug"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(asked, [("API Platform", ["repo:api", "ENG/bug"])])
        self.assertEqual([link.id for link in self.store.board_meta(self.repo_path).links], ["p9"])

    async def test_a_failed_project_lookup_shows_in_the_modal_and_writes_nothing(self):
        def resolve(name, labels):
            raise HerdboardError(f"No Linear project named {name!r}")

        before = self.repo_path.read_text()
        app = self.make_app(resolve_link=resolve)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-project").value = "Nope"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("No Linear project named 'Nope'", screen.query_one("#config-error").plain)
        self.assertEqual(self.repo_path.read_text(), before)

    async def test_without_linear_the_add_fields_are_hidden(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertFalse(screen.query("#config-project"))

    async def test_escape_closes_without_a_write(self):
        before = self.repo_path.read_text()
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(self.repo_path.read_text(), before)

    async def test_c_without_a_repository_board_explains(self):
        app = self.make_app(view="inbox", repo=False)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
            messages = [n.message for n in app._notifications]
            self.assertTrue(any("no repository board" in m for m in messages), messages)


def board_arguments(case):
    """The keyword arguments that the `board` command gives the app."""
    seen = {}
    with mock.patch.object(HerdboardApp, "__init__", lambda self, store, view, **kw: seen.update(kw)), \
            mock.patch.object(HerdboardApp, "run"):
        case.run_cli("board", directory=case.repo)
    return seen


class BoardCommandTests(CliTestCase):
    def test_without_linear_the_board_gets_no_project_lookup(self):
        self.init_repo_board()
        self.assertIsNone(board_arguments(self)["resolve_link"])


class BoardCommandLinearTests(LinearCliCase):
    def test_the_board_looks_up_a_project_and_its_labels(self):
        self.board()
        resolve = board_arguments(self)["resolve_link"]
        self.assertEqual(resolve("platform", ["repo:api"]),
                         ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]))
        with self.assertRaisesRegex(HerdboardError, "No Linear project named 'Nope'"):
            resolve("Nope", [])


if __name__ == "__main__":
    unittest.main()
