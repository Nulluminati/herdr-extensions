"""The board with the mouse: click, double-click, the card menu, drag between columns, and detail clicks."""
import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from rich.cells import cell_len  # noqa: E402
from textual import events  # noqa: E402
from textual.widgets import OptionList  # noqa: E402

from herdboard import attachments  # noqa: E402
from herdboard.board import (  # noqa: E402
    CardMenuScreen, CardWidget, DetailScreen, EditScreen, HELP_KEYS, card_actions, detail_lines,
)
from test_herdboard_board import BoardTestCase, settle  # noqa: E402

# Wide enough that every default column shows.
SIZE = (160, 40)


def content_offset(widget, x=0, y=0):
    """An offset inside the widget's content, past its border and padding."""
    return (widget.content_region.x - widget.region.x + x, widget.content_region.y - widget.region.y + y)


def clicks(text):
    """The `@click` actions in a Rich text's spans."""
    return [span.style.meta.get("@click") for span in text.spans
            if not isinstance(span.style, str) and span.style.meta.get("@click")]


class MouseTestCase(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.store.add(self.repo_path, "first todo", "Todo")
        self.second = self.store.add(self.repo_path, "second todo", "Todo")
        self.waiting = self.store.add(self.repo_path, "waiting card", "Review")

    def widget(self, app, ref):
        return next(w for w in app.query(CardWidget) if w.card.ref == ref)

    def column(self, app, name):
        return next(w for w in app.query(".column") if w.column_name == name)

    def column_of(self, ref):
        return {card.ref: card.column for card in self.store.list_cards([self.repo_path])}[ref]

    def titles(self, column):
        return [card.title for card in self.store.list_cards([self.repo_path]) if card.column == column]


class ClickTests(MouseTestCase):
    async def test_a_click_selects_the_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.click(self.widget(app, self.second.ref), offset=content_offset(self.widget(app, self.second.ref)))
            await pilot.pause()
            todo = [c.name for c in app.columns].index("Todo")
            self.assertEqual((app.col, app.row), (todo, 1))
            self.assertEqual(len(self.selected(app)), 1)
            self.assertIn("second todo", self.selected(app)[0])
            self.assertNotIsInstance(app.screen, DetailScreen)

    async def test_a_click_on_a_column_header_selects_the_column(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            header = self.column(app, "Review").query_one(".column-header")
            await pilot.click(header)
            await pilot.pause()
            self.assertEqual(app.columns[app.col].name, "Review")
            self.assertIn("waiting card", self.selected(app)[0])

    async def test_a_double_click_opens_the_detail(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.waiting.ref)
            await pilot.click(widget, offset=content_offset(widget), times=2)
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertEqual(app.screen.card.ref, self.waiting.ref)


class DetailClickTests(MouseTestCase):
    async def open_detail(self, pilot, ref):
        app = pilot.app
        await pilot.pause()
        widget = self.widget(app, ref)
        await pilot.click(widget, offset=content_offset(widget), times=2)
        await pilot.pause()
        self.assertIsInstance(app.screen, DetailScreen)

    async def test_a_click_on_a_checklist_line_checks_it(self):
        card = self.store.add(self.repo_path, "list card", "Backlog", body=["Intro", "- [ ] step one", "- [ ] step two"])
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot, card.ref)
            block = app.screen.query_one("#detail-body")
            await pilot.click(block, offset=content_offset(block, 2, 2))
            await settle(pilot)
            self.assertEqual([line.strip() for line in self.store.show(card.ref).body], ["Intro", "- [ ] step one", "- [x] step two"])
            self.assertIsInstance(app.screen, DetailScreen)

    async def test_a_click_on_another_line_changes_nothing(self):
        card = self.store.add(self.repo_path, "list card", "Backlog", body=["Intro", "- [ ] step one"])
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot, card.ref)
            block = app.screen.query_one("#detail-body")
            await pilot.click(block, offset=content_offset(block, 1, 0))
            await settle(pilot)
            self.assertEqual([line.strip() for line in self.store.show(card.ref).body], ["Intro", "- [ ] step one"])

    def test_checklist_lines_have_a_click_action_only_when_they_can_change(self):
        lines = ["Intro", "- [ ] step one", "- [x] step two"]
        self.assertEqual(set(clicks(detail_lines(lines, checks=True))),
                         {"screen.toggle_line(1)", "screen.toggle_line(2)"})
        self.assertEqual(clicks(detail_lines(lines)), [])

    async def test_a_linear_description_has_no_click_action(self):
        linear = dataclasses.replace(self.first, ref="ENG-1", source="linear", path=None,
                                     body=["- [ ] step one"])
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app.push_screen(DetailScreen(linear, None))
            await pilot.pause()
            self.assertEqual(clicks(app.screen.query_one("#detail-body").rich_text), [])

    async def test_a_click_on_an_attachment_opens_it(self):
        card = self.store.add(self.repo_path, "file card", "Backlog")
        for name in ("a.md", "b.md"):
            (self.root / name).write_text("# Text\n", encoding="utf-8")
            attachments.add(self.store, card.ref, self.root / name)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot, card.ref)
            with mock.patch.object(DetailScreen, "open_attachment") as opened:
                block = app.screen.query_one("#detail-files")
                await pilot.click(block, offset=content_offset(block, 1, 1))
                await pilot.pause()
            opened.assert_called_once()
            self.assertEqual(opened.call_args.args[0]["name"], "b.md")


class DragTests(MouseTestCase):
    async def drag(self, pilot, ref, column):
        app = pilot.app
        await pilot.pause()
        widget = self.widget(app, ref)
        await pilot.mouse_down(widget, offset=content_offset(widget))
        target = self.column(app, column).query_one(".column-header")
        await pilot.hover(target)
        await pilot.mouse_up(target)
        await settle(pilot)

    async def test_a_drop_on_another_column_moves_the_card_to_its_bottom(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.drag(pilot, self.first.ref, "Review")
            self.assertEqual(self.titles("Review"), ["waiting card", "first todo"])
            self.assertIn("first todo", self.selected(app)[0])
            self.assertEqual(list(app.query(".drop-target")), [])

    async def test_the_target_column_shows_while_the_card_is_dragged(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.first.ref)
            await pilot.mouse_down(widget, offset=content_offset(widget))
            await pilot.hover(self.column(app, "Todo").query_one(".column-header"))
            self.assertEqual(list(app.query(".drop-target")), [])
            await pilot.hover(self.column(app, "Review").query_one(".column-header"))
            self.assertEqual([w.column_name for w in app.query(".drop-target")], ["Review"])
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(list(app.query(".drop-target")), [])
            await pilot.mouse_up(self.column(app, "Review").query_one(".column-header"))
            await settle(pilot)
            self.assertEqual(self.column_of(self.first.ref), "Todo")

    async def test_a_drop_on_the_same_column_writes_nothing(self):
        before = self.repo_path.read_text()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.drag(pilot, self.first.ref, "Todo")
            self.assertEqual(self.repo_path.read_text(), before)

    async def test_the_click_after_a_drop_does_not_select_again(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.drag(pilot, self.first.ref, "Review")
            await pilot._post_mouse_events([events.Click], self.widget(app, self.second.ref), button=1)
            await pilot.pause()
            self.assertIn("first todo", self.selected(app)[0])

    async def test_a_click_after_a_drop_without_its_own_click_still_counts(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.drag(pilot, self.first.ref, "Review")
            await pilot.click(self.column(app, "Todo").query_one(".column-header"))
            await pilot.pause()
            self.assertEqual(app.columns[app.col].name, "Todo")

    async def test_a_drop_of_a_card_that_a_reload_removed_moves_nothing(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.first.ref)
            await pilot.mouse_down(widget, offset=content_offset(widget))
            self.store.delete(self.first.ref)
            await app.reload()
            await pilot.mouse_up(self.column(app, "Review").query_one(".column-header"))
            await settle(pilot)
            self.assertEqual(self.titles("Review"), ["waiting card"])
            self.assertEqual(self.titles("Todo"), ["second todo"])

    async def test_a_drag_of_an_archived_card_warns(self):
        done = self.store.done(self.store.add(self.repo_path, "old card", "Todo").ref)
        self.store.archive(done.ref)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("z")
            await settle(pilot)
            await self.drag(pilot, done.ref, "Done")
            self.assertIn("Press A to move the card back to the done column.",
                          [n.message for n in app._notifications])
            self.assertEqual([c.title for c in self.store.archived_cards([self.repo_path])], ["old card"])

    async def test_a_drop_on_the_archive_column_archives_the_todo(self):
        done = self.store.done(self.store.add(self.repo_path, "old card", "Todo").ref)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("z")
            await settle(pilot)
            archive = app.query(".column").last().column_name
            await self.drag(pilot, done.ref, archive)
            self.assertEqual([c.title for c in self.store.archived_cards([self.repo_path])], ["old card"])


class CardActionTests(MouseTestCase):
    def actions(self, card, **changes):
        return [action for _, action in card_actions(dataclasses.replace(card, **changes))]

    def test_a_todo_without_a_worktree(self):
        self.assertEqual(self.actions(self.first),
                         ["detail", "done", "edit", "move_board", "delete", "archive", "start", "choose_agent",
                          "plan", "parent"])

    def test_a_todo_with_a_worktree(self):
        self.assertEqual(self.actions(self.first, worktree=True),
                         ["detail", "done", "edit", "move_board", "delete", "start", "go", "review", "parent",
                          "merge", "finish"])

    def test_a_linear_card(self):
        self.assertEqual(self.actions(self.first, source="linear", ref="ENG-1"),
                         ["detail", "done", "start", "choose_agent", "plan"])
        self.assertEqual(self.actions(self.first, source="linear", ref="ENG-1", worktree=True),
                         ["detail", "done", "start", "go", "review", "merge", "finish"])

    def test_an_archived_todo(self):
        self.assertEqual(card_actions(dataclasses.replace(self.first, archived=True, checked=True)),
                         [("enter  Detail", "detail"), ("x  Delete", "delete"), ("A  Unarchive", "archive")])

    def test_a_done_card_whose_worktree_is_left(self):
        actions = self.actions(self.first, checked=True, worktree_left=True)
        self.assertNotIn("done", actions)
        self.assertIn("finish", actions)
        self.assertEqual(self.actions(self.first, source="linear", ref="ENG-1", checked=True, worktree=True,
                                      worktree_left=True),
                         ["detail", "start", "go", "review", "merge", "finish"])

    def test_labels_show_the_key(self):
        self.assertEqual(card_actions(self.first)[:2], [("enter  Detail", "detail"), ("d  Done", "done")])


class MenuTests(MouseTestCase):
    def marker_offset(self, widget):
        title = widget.plain.split("\n")[0]
        self.assertTrue(widget.rich_text.plain.startswith(f"{title} ⋯"))
        return content_offset(widget, cell_len(title) + 1)

    async def choose(self, pilot, action):
        menu = pilot.app.screen
        options = menu.query_one(OptionList)
        options.highlighted = [name for _, name in menu.actions].index(action)
        await pilot.press("enter")
        await settle(pilot)

    async def test_a_right_click_opens_the_menu_and_edit_opens_the_edit_form(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.second.ref)
            await pilot.click(widget, offset=content_offset(widget), button=3)
            await pilot.pause()
            self.assertIsInstance(app.screen, CardMenuScreen)
            self.assertIn("second todo", self.selected(app)[0])
            await self.choose(pilot, "edit")
            self.assertIsInstance(app.screen, EditScreen)
            self.assertEqual(app.screen.card.ref, self.second.ref)

    async def test_a_click_on_the_marker_opens_the_menu_not_the_detail(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.waiting.ref)
            await pilot.click(widget, offset=self.marker_offset(widget))
            await pilot.pause()
            self.assertIsInstance(app.screen, CardMenuScreen)
            self.assertIn("waiting card", self.selected(app)[0])

    async def test_a_click_on_the_title_opens_no_menu(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.waiting.ref)
            await pilot.click(widget, offset=content_offset(widget))
            await pilot.pause()
            self.assertNotIsInstance(app.screen, CardMenuScreen)

    async def test_escape_closes_the_menu_and_runs_nothing(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.first.ref)
            await pilot.click(widget, offset=self.marker_offset(widget))
            await pilot.pause()
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, CardMenuScreen)
            self.assertNotIsInstance(app.screen, DetailScreen)

    def test_help_lists_the_mouse(self):
        keys = [key for key, _ in HELP_KEYS]
        for key in ("click", "double-click", "right-click, ⋯", "drag"):
            self.assertIn(key, keys)


if __name__ == "__main__":
    unittest.main()
