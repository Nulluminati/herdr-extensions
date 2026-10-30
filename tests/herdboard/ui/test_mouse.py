"""The board with the mouse: click, double-click, the card menu, drag between columns, and detail clicks."""
import dataclasses
import unittest
from unittest import mock

from textual import events
from textual.widgets import OptionList

from herdboard import attachments
from herdboard.board import HELP_KEYS, CardMenuScreen, CardWidget, DetailScreen, EditScreen, detail_lines
from support.herdboard_board import settle
from support.herdboard_mouse import MouseTestCase, content_offset

# Wide enough that every default column shows.
SIZE = (160, 40)


def clicks(text):
    """The `@click` actions in a Rich text's spans."""
    return [span.style.meta.get("@click") for span in text.spans
            if not isinstance(span.style, str) and span.style.meta.get("@click")]


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

    async def test_the_checklist_block_selects_no_text(self):
        # A text drag that ends on a checklist line would also check it.
        card = self.store.add(self.repo_path, "list card", "Backlog", body=["- [ ] step one"])
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot, card.ref)
            self.assertFalse(app.screen.query_one("#detail-body").allow_select)
            self.assertTrue(app.screen.query_one("#detail-meta").allow_select)

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

    async def test_the_attachments_block_selects_no_text(self):
        # A text drag that ends on an attachment line would also open it.
        card = self.store.add(self.repo_path, "file card", "Backlog")
        (self.root / "a.md").write_text("# Text\n", encoding="utf-8")
        attachments.add(self.store, card.ref, self.root / "a.md")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot, card.ref)
            self.assertFalse(app.screen.query_one("#detail-files").allow_select)

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

    async def test_only_the_left_button_drops(self):
        before = self.repo_path.read_text()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.first.ref)
            await pilot.mouse_down(widget, offset=content_offset(widget))
            target = self.column(app, "Review").query_one(".column-header")
            await pilot._post_mouse_events([events.MouseUp], target, button=3)
            await settle(pilot)
            self.assertEqual(self.repo_path.read_text(), before)
            self.assertIsNone(app.drag)
            self.assertFalse(app.dropped)

    async def test_a_drop_on_the_own_column_after_the_columns_moved_writes_nothing(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.first.ref)
            await pilot.mouse_down(widget, offset=content_offset(widget))
            # Another program adds a column before Todo while the mouse holds the card.
            self.repo_path.write_text(self.repo_path.read_text().replace("## Todo", "## Extra\n\n## Todo", 1))
            await app.reload()
            await pilot.pause()
            await pilot.mouse_up(self.column(app, "Todo").query_one(".column-header"))
            await settle(pilot)
            self.assertEqual(self.titles("Todo"), ["first todo", "second todo"])

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


class MenuTests(MouseTestCase):
    async def open_menu(self, pilot, widget):
        """A right click on the card opens its menu."""
        await pilot.click(widget, offset=content_offset(widget), button=3)

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
            await self.open_menu(pilot, widget)
            await pilot.pause()
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, CardMenuScreen)
            self.assertNotIsInstance(app.screen, DetailScreen)

    async def remount_todo_column(self, pilot):
        """A change to another card of the column: the reload replaces every card widget there."""
        old = list(self.column(pilot.app, "Todo").query(CardWidget))
        self.store.edit(self.first.ref, title="first todo, changed elsewhere")
        await pilot.app.reload()
        await pilot.pause()
        self.assertFalse(any(widget.is_attached for widget in old))

    async def test_a_menu_choice_runs_after_a_reload_replaced_the_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.second.ref)
            await self.open_menu(pilot, widget)
            await pilot.pause()
            self.assertIsInstance(app.screen, CardMenuScreen)
            await self.remount_todo_column(pilot)
            await self.choose(pilot, "edit")
            self.assertIsInstance(app.screen, EditScreen)

    async def test_e_in_a_double_clicked_detail_works_after_a_reload_replaced_the_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.second.ref)
            await pilot.click(widget, offset=content_offset(widget), times=2)
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            await self.remount_todo_column(pilot)
            await pilot.press("e")
            await settle(pilot)
            self.assertIsInstance(app.screen, EditScreen)
            self.assertEqual(app.screen.card.ref, self.second.ref)

    async def test_a_menu_choice_for_a_card_that_a_reload_removed_changes_no_other_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.second.ref)
            await self.open_menu(pilot, widget)
            await pilot.pause()
            self.assertIsInstance(app.screen, CardMenuScreen)
            self.store.delete(self.second.ref)
            await app.reload()
            await pilot.pause()
            await self.choose(pilot, "done")
            self.assertFalse(self.store.show(self.first.ref).checked)
            self.assertIn("The card is no longer on the board.", [n.message for n in app._notifications])

    async def test_the_menu_shows_a_title_with_brackets_as_text(self):
        card = self.store.add(self.repo_path, "Fix the [/auth] bug", "Review")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, card.ref)
            await self.open_menu(pilot, widget)
            await pilot.pause()
            self.assertIsInstance(app.screen, CardMenuScreen)
            self.assertIn("Fix the [/auth] bug", app.screen.query_one("#card-menu-title").render().plain)

    async def test_a_double_click_on_a_card_that_a_reload_removed_opens_no_detail(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            widget = self.widget(app, self.waiting.ref)
            self.store.delete(self.waiting.ref)
            await app.reload()
            await pilot.pause()
            self.assertTrue(app.select_card(self.widget(app, self.first.ref).card))
            widget.on_click(mock.Mock(button=1, chain=2, style=mock.Mock(meta={})))
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DetailScreen)

    def test_help_lists_the_mouse(self):
        keys = [key for key, _ in HELP_KEYS]
        for key in ("click", "double-click", "right-click", "drag"):
            self.assertIn(key, keys)


if __name__ == "__main__":
    unittest.main()
