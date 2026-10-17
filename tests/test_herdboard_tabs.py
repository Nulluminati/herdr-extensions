"""The tab bar at the top, with the Board and Pull requests tabs, and the keys of the current tab at the bottom."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from textual.widgets import Footer  # noqa: E402

from herdboard.board import HerdboardApp, KeyBar, PullRequestsScreen, TabBar  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.pr_reviews import Page  # noqa: E402
from herdboard.store import Store  # noqa: E402


class TabBarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos"))
        self.path = self.store.ensure_inbox()
        self.store.add(self.path, "Board card")
        self.service = Mock()
        self.service.page.return_value = ("acme/api", Page([], 0, None))

    def app(self):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, pr_service=self.service)
        app.animation_level = "none"
        return app

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def bar(self, app) -> TabBar:
        return app.screen.query_one(TabBar)

    def keys(self, app) -> KeyBar:
        return app.screen.query_one(KeyBar)

    async def test_the_board_has_tabs_at_the_top_and_its_keys_at_the_bottom(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            bar = self.bar(app)
            self.assertEqual(bar.active, "board")
            self.assertEqual(bar.tab_labels(), ["Board", "Pull requests G"])
            keys = self.keys(app).keys_text().plain.replace("\xa0", " ")
            for part in ("a Add", "/ Filter", "s Start", "? Help", "q Quit"):
                self.assertIn(part, keys)
            self.assertNotIn("PRs", keys)
            self.assertEqual(bar.region.y, 0)
            self.assertEqual(self.keys(app).region.bottom, 30)
            self.assertEqual(list(app.screen.query(Footer)), [])

    async def test_the_pull_request_tab_has_its_own_keys(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            bar = self.bar(app)
            self.assertEqual(bar.active, "prs")
            self.assertEqual(bar.tab_labels(), ["Board G", "Pull requests"])
            keys = self.keys(app).keys_text().plain.replace("\xa0", " ")
            for part in ("/ Search", "n Next", "s Review", "esc Back"):
                self.assertIn(part, keys)
            self.assertNotIn("a Add", keys)
            self.assertEqual(self.keys(app).region.bottom, 30)
            self.assertEqual(list(app.screen.query(Footer)), [])

    async def test_a_click_on_the_other_tab_switches(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.click("#tab-board")
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, PullRequestsScreen)

    async def test_switching_back_keeps_the_list_and_does_not_read_github_again(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            first = app.screen
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIs(app.screen, first)
            self.assertEqual(self.service.page.call_count, 1)

    async def test_switching_back_after_a_minute_reads_github_again(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            app.get_screen("pull-requests").read_at -= 61
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertEqual(self.service.page.call_count, 2)

    async def test_a_read_in_the_background_shows_no_loading_spinner(self):
        import threading
        from textual.widgets import LoadingIndicator
        release = threading.Event()

        def slow(text, after):
            release.wait(5)
            return "acme/api", Page([], 0, None)

        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            screen = app.screen
            self.service.page.side_effect = slow
            screen.action_load()
            await pilot.pause(0.2)
            self.assertFalse(screen.loading)
            self.assertEqual(list(screen.query(LoadingIndicator)), [])
            release.set()
            await self.settle(app, pilot)

    async def test_the_same_data_keeps_the_table_and_its_selection(self):
        from herdboard.pr_reviews import PullRequest
        prs = [PullRequest(n, f"PR {n}", "u", "octo", False, "a" * 40, "b", False, False) for n in range(9, 0, -1)]
        self.service.page.return_value = ("acme/api", Page(prs, 9, None))
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            screen = app.screen
            await pilot.press("j", "j", "j")
            table = screen.query_one("#pr-list")
            screen.action_load()
            await self.settle(app, pilot)
            self.assertEqual(table.cursor_row, 3)
            self.assertEqual(screen.current().number, 6)

    async def test_the_tabs_and_the_bar_under_them_have_one_color(self):
        for theme in ("textual-dark", "ansi-dark"):
            app = self.app()
            app.theme = theme
            async with app.run_test(size=(160, 30)) as pilot:
                await self.settle(app, pilot)
                for screen_key in ("", "G"):
                    if screen_key:
                        await pilot.press(screen_key)
                        await self.settle(app, pilot)
                    bar = app.screen.query_one("#pr-title" if screen_key else "#title-bar")
                    color = bar.styles.background
                    active = app.screen.query_one(".tab.active")
                    self.assertEqual(active.query_one(".tab-label").styles.background, color, theme)
                    other = next(tab for tab in app.screen.query(".tab") if not tab.has_class("active"))
                    for side in ("top", "left", "right"):
                        self.assertEqual(getattr(other.styles, f"border_{side}")[1], color, (theme, side))
                    self.assertEqual(other.styles.border_bottom[0], "", theme)
                    self.assertEqual(other.region.bottom, bar.region.y, theme)
                    self.assertEqual(active.region.bottom, bar.region.y, theme)

    async def test_a_tab_keeps_its_size_and_is_filled_when_selected(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            sizes = {tab: app.screen.query_one(f"#tab-{tab}").region.size for tab in ("board", "prs")}
            await pilot.press("G")
            await self.settle(app, pilot)
            for tab in ("board", "prs"):
                widget = app.screen.query_one(f"#tab-{tab}")
                self.assertEqual(widget.region.size, sizes[tab], tab)
                # The current tab fills the inner half of its border cells, up to where the
                # other tab draws its line, so the fill covers the tab and the size stays.
                self.assertEqual(widget.styles.border_top[0], "inner" if tab == "prs" else "round", tab)
                self.assertEqual(widget.styles.border_bottom[0], "", tab)
                self.assertEqual(widget.styles.background.a, 0, tab)

    async def test_the_tabs_stand_on_the_bar_in_a_thin_pane(self):
        for key in ("", "G"):
            app = self.app()
            async with app.run_test(size=(50, 40)) as pilot:
                await self.settle(app, pilot)
                if key:
                    await pilot.press(key)
                    await self.settle(app, pilot)
                bar = app.screen.query_one("#pr-title" if key else "#title-bar")
                for tab in app.screen.query(".tab"):
                    self.assertEqual(tab.region.bottom, bar.region.y, (key, tab.id))

    async def test_the_board_bar_and_the_pull_request_bar_look_the_same(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            board = app.screen.query_one("#title-bar").styles
            await pilot.press("G")
            await self.settle(app, pilot)
            prs = app.screen.query_one("#pr-title").styles
            for rule in ("color", "background", "text_style", "padding"):
                self.assertEqual(getattr(board, rule), getattr(prs, rule), rule)

    async def test_a_key_and_its_description_stay_on_one_line(self):
        for width in (50, 90, 160):
            app = self.app()
            async with app.run_test(size=(width, 40)) as pilot:
                await self.settle(app, pilot)
                keys = self.keys(app)
                labels = [part for part in keys.keys_text().plain.split("   ") if part]
                lines = ["".join(segment.text for segment in keys.render_line(y)) for y in range(keys.size.height)]
                for label in labels:
                    self.assertTrue(any(label in line for line in lines), (width, label, lines))

    async def test_the_keys_stay_under_an_open_filter(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("slash")
            await pilot.pause()
            self.assertEqual(self.keys(app).region.bottom, 30)
            self.assertEqual(app.screen.query_one("#filter").region.bottom, self.keys(app).region.y)
            await pilot.press("escape", "G")
            await self.settle(app, pilot)
            await pilot.press("slash")
            await pilot.pause()
            self.assertEqual(self.keys(app).region.bottom, 30)
            self.assertEqual(app.screen.query_one("#pr-search").region.bottom, self.keys(app).region.y)

    async def test_dark_text_on_the_bars_and_the_tab_is_not_bold_in_the_ansi_themes(self):
        # Terminals that draw bold text in bright colors make bold black text grey.
        for theme, bold in (("ansi-dark", False), ("textual-dark", True)):
            app = self.app()
            app.theme = theme
            async with app.run_test(size=(160, 30)) as pilot:
                await self.settle(app, pilot)
                for key in ("", "G"):
                    if key:
                        await pilot.press(key)
                        await self.settle(app, pilot)
                    bar = app.screen.query_one("#pr-title" if key else "#title-bar")
                    label = app.screen.query_one(".tab.active .tab-label")
                    for widget in (bar, label):
                        self.assertEqual(bool(widget.visual_style.bold), bold, (theme, key, widget))
                    # The bold comes from the style sheet, so the ANSI themes can turn it off.
                    text = self.bar(app)._tab_text(self.bar(app).active, "Tab")
                    self.assertFalse(text.style or any(span.style == "bold" for span in text.spans), theme)

    async def test_the_label_does_not_move_when_a_tab_is_selected(self):
        # The current tab keeps the G's place blank, so its text stays where it was.
        for tab, label in (("board", "Board"), ("prs", "Pull requests")):
            current = TabBar(tab)._tab_text(tab, label)
            other = TabBar("board" if tab == "prs" else "prs")._tab_text(tab, label)
            self.assertIn("G", other.plain)
            self.assertEqual(current.plain, other.plain.replace("G", " "), tab)

    async def test_the_pull_request_columns_fill_the_width(self):
        from herdboard.pr_reviews import PullRequest
        for count in (3, 40):
            prs = [PullRequest(n, f"PR {n}", "u", "octo", False, "a" * 40, "b", False, False) for n in range(count, 0, -1)]
            self.service.page.return_value = ("acme/api", Page(prs, count, None))
            for width in (120, 160):
                app = self.app()
                async with app.run_test(size=(width, 30)) as pilot:
                    await pilot.press("G")
                    await self.settle(app, pilot)
                    table = app.screen.query_one("#pr-list")
                    self.assertEqual(table.virtual_size.width, table.scrollable_content_region.width, (count, width))
                    # Two cells on each side of a column keep the short count columns apart.
                    self.assertEqual(table.cell_padding, 2)

    def test_a_tab_has_no_hover_color(self):
        rules = [line for line in HerdboardApp.CSS.splitlines() if ".tab" in line]
        self.assertTrue(rules)
        self.assertEqual([line for line in rules if ":hover" in line], [])

    async def test_each_key_can_be_clicked(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            text = self.keys(app).keys_text()
            clicks = {span.style.meta.get("@click") for span in text.spans if hasattr(span.style, "meta")}
        self.assertIn("app.simulate_key('a')", clicks)


if __name__ == "__main__":
    unittest.main()
