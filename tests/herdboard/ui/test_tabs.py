"""The tab bar at the top, with the Board, Pull requests, Notes and Automations tabs, and the keys of the current
tab at the bottom."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Footer

from herdboard.board import HeaderBar, HerdboardApp, KeyBar, PullRequestsScreen, TabBar
from herdboard.config import Config
from herdboard.pr_reviews import Page
from herdboard.store import Store


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
            self.assertEqual(bar.tab_labels(), ["▦ Board", "⑂ Pull requests", "✎ Notes", "↻ Automations"])
            self.assertIn("G", bar.query_one("#tab-next").plain)
            keys = " ".join(self.keys(app).keys_text().plain.split())
            for part in ("a Add", "/ Filter", "s Start", "? Help", "q Quit"):
                self.assertIn(part, keys)
            self.assertNotIn("PRs", keys)
            self.assertEqual(bar.region.y, app.screen.query_one(HeaderBar).region.bottom)
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
            self.assertEqual(bar.tab_labels(), ["▦ Board", "⑂ Pull requests 0", "✎ Notes", "↻ Automations"])
            keys = " ".join(self.keys(app).keys_text().plain.split())
            for part in ("/ Search", "n Next", "R Review", "esc Back"):
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
            await pilot.press("G", "G", "G")
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIs(app.screen, first)
            self.assertEqual(self.service.page.call_count, 1)

    async def test_switching_back_after_a_minute_reads_github_again(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G", "G", "G")
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

    async def test_the_header_names_herdboard_the_place_the_view_and_the_version(self):
        with patch("herdboard.board.herdboard_version", return_value="0.3.1"):
            app = self.app()
            async with app.run_test(size=(160, 30)) as pilot:
                await self.settle(app, pilot)
                header = app.screen.query_one(HeaderBar)
                self.assertEqual(header.region.y, 0)
                self.assertTrue(header.plain.startswith("herdboard │ inbox · Linear: off"), header.plain)
                self.assertTrue(header.plain.endswith("│ inbox  v0.3.1"), header.plain)
                # The view and the version stand at the right edge, inside the padding.
                self.assertEqual(app.screen.query_one("#header-view").region.right, 159)
                self.assertEqual(self.bar(app).region.y, header.region.bottom)
                await pilot.press("G")
                await self.settle(app, pilot)
                header = app.screen.query_one(HeaderBar)
                self.assertEqual(header.plain, "herdboard │ acme/api │ inbox  v0.3.1")

    async def test_the_header_leaves_out_a_missing_version(self):
        from importlib.metadata import PackageNotFoundError
        from herdboard import board
        with patch("importlib.metadata.version", side_effect=PackageNotFoundError("herdboard")):
            self.assertEqual(board.herdboard_version(), "")
            self.assertEqual(HeaderBar("acme-api", "repo").plain, "herdboard │ acme-api │ repo")

    async def test_the_current_tab_has_an_accent_border_and_all_keep_their_size(self):
        tabs = ("board", "prs", "notes", "automations")
        for theme in ("textual-dark", "ansi-dark"):
            app = self.app()
            app.theme = theme
            async with app.run_test(size=(160, 30)) as pilot:
                await pilot.press("G")
                await self.settle(app, pilot)
                sizes = {tab: app.screen.query_one(f"#tab-{tab}").region.size for tab in tabs}
                for active_tab in ("notes", "automations", "board", "prs"):
                    await pilot.press("G")
                    await self.settle(app, pilot)
                    active = app.screen.query_one(".tab.active")
                    other = next(tab for tab in app.screen.query(".tab") if not tab.has_class("active"))
                    self.assertEqual(active.id, f"tab-{active_tab}")
                    self.assertEqual(active.styles.border_top[0], "round")
                    self.assertNotEqual(active.styles.border_top[1], other.styles.border_top[1], theme)
                    self.assertTrue(active.query_one(".tab-label").visual_style.bold, theme)
                    for tab in tabs:
                        self.assertEqual(app.screen.query_one(f"#tab-{tab}").region.size, sizes[tab],
                                         (theme, active_tab, tab))

    async def test_the_pull_request_tab_shows_the_open_count_after_a_read(self):
        from herdboard.pr_reviews import PullRequest
        prs = [PullRequest(n, f"PR {n}", "u", "octo", False, "a" * 40, "b", False, False) for n in (2, 1)]
        self.service.page.return_value = ("acme/api", Page(prs, 7, None))
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).tab_labels(), ["▦ Board", "⑂ Pull requests", "✎ Notes", "↻ Automations"])
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).tab_labels(), ["▦ Board", "⑂ Pull requests 7", "✎ Notes", "↻ Automations"])
            await pilot.press("G", "G")
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).tab_labels(), ["▦ Board", "⑂ Pull requests 7", "✎ Notes", "↻ Automations"])

    async def test_a_tab_opened_before_the_read_shows_the_count_when_it_comes_back(self):
        # The Notes and Automations tabs stay installed after they hide, so a read must reach their bars too.
        self.service.page.return_value = ("acme/api", Page([], 7, None))
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.click("#tab-automations")
            await self.settle(app, pilot)
            await pilot.click("#tab-notes")
            await self.settle(app, pilot)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            for tab in ("notes", "automations", "board"):
                await pilot.click(f"#tab-{tab}")
                await self.settle(app, pilot)
                self.assertEqual(self.bar(app).active, tab)
                self.assertEqual(self.bar(app).tab_labels(),
                                 ["▦ Board", "⑂ Pull requests 7", "✎ Notes", "↻ Automations"], tab)

    async def test_a_later_read_changes_the_count_on_every_tab(self):
        self.service.page.return_value = ("acme/api", Page([], 7, None))
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G", "G", "G", "G")
            await self.settle(app, pilot)
            self.service.page.return_value = ("acme/api", Page([], 3, None))
            app.get_screen("pull-requests").read_at -= 61
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).tab_labels()[1], "⑂ Pull requests 3")
            for tab in ("notes", "automations", "board"):
                await pilot.press("G")
                await self.settle(app, pilot)
                self.assertEqual(self.bar(app).active, tab)
                self.assertEqual(self.bar(app).tab_labels()[1], "⑂ Pull requests 3", tab)

    async def test_the_tabs_use_nerd_icons_only_in_nerd_mode(self):
        self.store.config.icon_style = "nerd"
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).tab_labels(), ["\uf0db Board", "\uf407 Pull requests", "\uf249 Notes",
                                                           "\uf017 Automations"])

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

    async def test_the_label_does_not_move_when_a_tab_is_selected(self):
        # The G stands on the bar outside the tabs, so no tab text holds its place and no tab resizes.
        for tab, label in (("board", "Board"), ("prs", "Pull requests"), ("notes", "Notes"),
                           ("automations", "Automations")):
            texts = {TabBar(active)._tab_text(tab, label).plain
                     for active in ("board", "prs", "notes", "automations")}
            self.assertEqual(len(texts), 1, tab)
            self.assertNotIn("G", texts.pop())

    async def test_the_tab_text_is_centered_in_a_box_as_wide_as_the_widest_tab(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            boxes = [app.screen.query_one(f"#tab-{tab}") for tab in ("board", "prs", "notes", "automations")]
            self.assertEqual(len({box.region.width for box in boxes}), 1, [box.region.width for box in boxes])
            for box in boxes:
                label = box.query_one(".tab-label")
                self.assertEqual(label.styles.content_align_horizontal, "center", box.id)
                # The box padding and the centering put the same cells at the left of the text.
                self.assertEqual(label.region.x - box.region.x - box.styles.padding.left,
                                 box.region.right - label.region.right - box.styles.padding.right, box.id)

    async def test_no_tab_hides_its_label(self):
        # Every tab is as wide as the widest label plus its own padding and border, so no label is cut.
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            for active in ("prs", "notes", "automations", "board"):
                bar = self.bar(app)
                self.assertEqual(bar.active, active)
                for box in bar.query(".tab"):
                    label = box.query_one(".tab-label")
                    line = "".join(segment.text for segment in label.render_line(0))
                    self.assertEqual(line.strip(), box.plain, (active, box.id, line, box.plain))
                    self.assertEqual(label.content_size.width, box.content_size.width, (active, box.id))
                await pilot.press("G")
                await self.settle(app, pilot)

    async def test_the_g_indicator_stands_on_the_bar_outside_the_tabs(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            bar = self.bar(app)
            hint = bar.query_one("#tab-next")
            self.assertIn("G", hint.plain)
            self.assertNotIn("G", "".join(box.plain for box in bar.query(".tab")))
            # Outside the boxes and to the right of the last one.
            last = bar.query(".tab").last()
            self.assertGreaterEqual(hint.region.x, last.region.right)
            self.assertEqual(hint.region.y, bar.region.y)
            # It does not change when another tab shows.
            after = self.bar(app)
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertEqual(self.bar(app).query_one("#tab-next").plain, after.query_one("#tab-next").plain)

    async def test_a_narrow_pane_puts_the_g_indicator_above_the_tabs(self):
        # The tabs keep their text and their box, and the hint takes a row of its own above them.
        app = self.app()
        async with app.run_test(size=(40, 30)) as pilot:
            await self.settle(app, pilot)
            bar = self.bar(app)
            hint = bar.query_one("#tab-next")
            first = bar.query(".tab").first()
            self.assertTrue(bar.narrow)
            self.assertLessEqual(hint.region.bottom, first.region.y)
            self.assertIn("G next tab", hint.plain)
            self.assertEqual(bar.tab_labels(), ["▦ Board", "⑂ Pull requests", "✎ Notes", "↻ Automations"])

    async def test_the_pull_request_rows_fill_the_width_and_cut_long_titles(self):
        from herdboard.board import PullRequestRow
        from herdboard.pr_reviews import PullRequest
        for count in (3, 40):
            prs = [PullRequest(n, f"PR {n} " + "long title " * 20, "u", "octo", False, "a" * 40, "b", False, False)
                   for n in range(count, 0, -1)]
            self.service.page.return_value = ("acme/api", Page(prs, count, None))
            for width in (80, 160):
                app = self.app()
                async with app.run_test(size=(width, 30)) as pilot:
                    await pilot.press("G")
                    await self.settle(app, pilot)
                    listing = app.screen.query_one("#pr-list")
                    row = app.screen.query(PullRequestRow).first()
                    self.assertEqual(listing.virtual_size.width, listing.scrollable_content_region.width)
                    self.assertEqual(row.region.width, listing.scrollable_content_region.width, (count, width))
                    first = "".join(segment.text for segment in row.render_line(0))
                    self.assertIn("…", first, (count, width))

    def test_a_tab_has_no_hover_color(self):
        rules = [line for line in HerdboardApp.CSS.splitlines() if ".tab" in line]
        self.assertTrue(rules)
        self.assertEqual([line for line in rules if ":hover" in line], [])

    async def test_each_key_is_a_keycap_before_its_description(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            label = next(text for text in self.keys(app).key_labels() if "Add" in text.plain)
            self.assertEqual(label.plain, "\xa0a\xa0\xa0Add")
            caps = [label.plain[span.start:span.end] for span in label.spans if span.style.reverse]
            self.assertEqual(caps, ["\xa0a\xa0"])
            plain = [span for span in label.spans if label.plain[span.start:span.end].endswith("Add")]
            self.assertFalse(any(span.style.reverse for span in plain))

    async def test_a_click_on_a_keycap_presses_its_key(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            keys = self.keys(app)
            line = "".join(segment.text for segment in keys.render_line(0))
            await pilot.click(keys, offset=(keys.styles.padding.left + line.index("\xa0a\xa0") + 1, 0))
            await self.settle(app, pilot)
            self.assertEqual(type(app.screen).__name__, "AddScreen")

    async def test_each_key_can_be_clicked(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            text = self.keys(app).keys_text()
            clicks = {span.style.meta.get("@click") for span in text.spans if hasattr(span.style, "meta")}
        self.assertIn("app.simulate_key('a')", clicks)


if __name__ == "__main__":
    unittest.main()
