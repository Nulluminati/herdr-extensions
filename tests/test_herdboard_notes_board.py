"""The Notes tab: the scratchpad of the shown board, or the global note, next to the board."""
import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard import notes  # noqa: E402
from herdboard.board import (ConfirmScreen, HerdboardApp, KeyBar, NoteChangedScreen, NotesScreen,  # noqa: E402
                             PullRequestsScreen, TabBar)
from herdboard.config import Config  # noqa: E402
from herdboard.pr_reviews import Page  # noqa: E402
from herdboard.store import Store  # noqa: E402


class NotesTabTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos"))
        self.inbox = self.store.ensure_inbox()
        self.store.add(self.inbox, "Inbox card")
        self.store.projects_dir.mkdir(parents=True)
        self.repo = self.store.projects_dir / "acme-api.md"
        shutil.copy(self.inbox, self.repo)
        self.store.add(self.repo, "Repo card")
        self.service = Mock()
        self.service.page.return_value = ("acme/api", Page([], 0, None))
        self.notices = []

    def app(self, view="repo"):
        paths = {"repo": [self.repo], "inbox": [self.inbox], "all": [self.inbox, self.repo]}
        app = HerdboardApp(self.store, view, lambda name: paths[name], repo_board=self.repo,
                           reload_interval=60, pr_service=self.service)
        app.animation_level = "none"
        app.notify = lambda message, **kwargs: self.notices.append(message)
        return app

    def note(self, board: str) -> Path:
        return notes.note_path(self.store.config, board)

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def open_notes(self, app, pilot) -> NotesScreen:
        await self.settle(app, pilot)
        await pilot.press("G", "G")
        await self.settle(app, pilot)
        self.assertIsInstance(app.screen, NotesScreen)
        return app.screen

    async def test_capital_g_cycles_the_three_tabs(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            labels = app.screen.query_one(TabBar).tab_labels()
            self.assertEqual(labels, ["▦ Board", "⑂ Pull requests G", "✎ Notes"])
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            self.assertEqual(app.screen.query_one(TabBar).tab_labels(), ["▦ Board", "⑂ Pull requests 0", "✎ Notes G"])
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, NotesScreen)
            self.assertEqual(app.screen.query_one(TabBar).tab_labels(), ["▦ Board G", "⑂ Pull requests 0", "✎ Notes"])
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIs(app.screen, app.screen_stack[0])
            self.assertEqual(len(app.screen_stack), 1)

    async def test_a_click_on_a_tab_opens_it(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.click("#tab-notes")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, NotesScreen)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.click("#tab-notes")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, NotesScreen)
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.click("#tab-board")
            await self.settle(app, pilot)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_n_on_the_board_does_not_open_the_notes(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("N")
            await self.settle(app, pilot)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_the_repository_view_shows_the_repository_note(self):
        self.note("acme-api").parent.mkdir(parents=True)
        self.note("acme-api").write_text("# Repo ideas\n")
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.assertEqual(screen.board, "acme-api")
            self.assertIn("Repo ideas", screen.text())
            self.assertIn(str(self.note("acme-api")), screen.text())

    async def test_the_inbox_view_shows_the_global_note(self):
        app = self.app("inbox")
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.assertEqual(screen.board, "inbox")
            self.assertIn("Global note", screen.text())
            self.assertIn("Empty note. e: edit", screen.text())

    async def test_the_all_view_follows_the_selected_card(self):
        app = self.app("all")
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(self.store.board_name(app.current_card().path), "inbox")
            self.assertEqual(app.notes_board(), "inbox")
            await pilot.press("j")
            self.assertEqual(app.notes_board(), "acme-api")
            app.columns = []
            self.assertEqual(app.notes_board(), "inbox")

    async def test_the_notes_show_the_board_of_the_time_they_open(self):
        app = self.app("all")
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.assertEqual(screen.board, "inbox")
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("j")
            await pilot.click("#tab-notes")
            await self.settle(app, pilot)
            self.assertEqual(app.screen.board, "acme-api")

    async def test_t_toggles_between_the_board_note_and_the_global_note(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("t")
            await self.settle(app, pilot)
            self.assertEqual(screen.board, "inbox")
            await pilot.press("t")
            await self.settle(app, pilot)
            self.assertEqual(screen.board, "acme-api")

    async def test_t_on_the_global_note_of_the_inbox_says_so(self):
        app = self.app("inbox")
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("t")
            await self.settle(app, pilot)
            self.assertEqual(screen.board, "inbox")
        self.assertIn("This board's note is the global note", self.notices)

    async def test_an_edit_saves_on_escape(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.assertFalse(self.note("acme-api").exists())
            await pilot.press("e")
            await self.settle(app, pilot)
            self.assertTrue(screen.editing)
            # The preview keys type into the editor.
            await pilot.press("t", "G", "q", "e")
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertFalse(screen.editing)
            self.assertIsInstance(app.screen, NotesScreen)
            self.assertEqual(self.note("acme-api").read_text(), "tGqe")
            self.assertIn("tGqe", screen.text())

    async def test_an_edit_saves_by_itself_after_a_pause(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            screen.autosave_seconds = 0.05
            await pilot.press("e", "h", "i")
            await pilot.pause(0.3)
            self.assertTrue(screen.editing)
            self.assertEqual(self.note("acme-api").read_text(), "hi")

    async def test_ctrl_s_saves_and_keeps_editing(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("e", "x", "ctrl+s")
            await self.settle(app, pilot)
            self.assertTrue(screen.editing)
            self.assertEqual(self.note("acme-api").read_text(), "x")

    async def test_the_edit_keys_show_while_editing(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            keys = " ".join(app.screen.query_one(KeyBar).keys_text().plain.split())
            for part in ("e Edit", "t Global note", "r Reload", "? Help"):
                self.assertIn(part, keys)
            self.assertNotIn("Save", keys)
            await pilot.press("e")
            await self.settle(app, pilot)
            keys = " ".join(app.screen.query_one(KeyBar).keys_text().plain.split())
            for part in ("esc Done", "^s Save"):
                self.assertIn(part, keys)
            self.assertNotIn("Edit", keys)
            self.assertTrue(screen.editing)

    async def test_a_tab_switch_saves_the_edit(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("e", "a", "b")
            await pilot.click("#tab-board")
            await self.settle(app, pilot)
            self.assertEqual(self.note("acme-api").read_text(), "ab")
            self.assertFalse(screen.editing)

    async def test_quit_saves_the_edit(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("e", "a")
            await app.action_quit()
        self.assertEqual(self.note("acme-api").read_text(), "a")

    async def test_an_outside_edit_shows_in_the_preview(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.note("acme-api").parent.mkdir(parents=True)
            self.note("acme-api").write_text("from Obsidian")
            screen.check_file()
            await self.settle(app, pilot)
            self.assertIn("from Obsidian", screen.text())

    async def test_the_preview_reads_the_file_on_a_timer(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            self.assertEqual(screen.timer._interval, 60)

    async def test_an_outside_edit_does_not_reload_the_editor(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("e", "m")
            self.note("acme-api").parent.mkdir(parents=True)
            self.note("acme-api").write_text("from Obsidian")
            screen.check_file()
            await self.settle(app, pilot)
            self.assertEqual(screen.editor_text(), "m")

    async def outside_conflict(self, app, pilot) -> NotesScreen:
        screen = await self.open_notes(app, pilot)
        await pilot.press("e", "m")
        self.note("acme-api").parent.mkdir(parents=True)
        self.note("acme-api").write_text("from Obsidian")
        await pilot.press("ctrl+s")
        await self.settle(app, pilot)
        self.assertIsInstance(app.screen, NoteChangedScreen)
        return screen

    async def test_a_save_over_an_outside_edit_can_overwrite(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.outside_conflict(app, pilot)
            await pilot.press("o")
            await self.settle(app, pilot)
            self.assertIs(app.screen, screen)
            self.assertEqual(self.note("acme-api").read_text(), "m")
            self.assertTrue(screen.editing)

    async def test_a_save_over_an_outside_edit_can_reload(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.outside_conflict(app, pilot)
            await pilot.press("r")
            await self.settle(app, pilot)
            self.assertIs(app.screen, screen)
            self.assertEqual(self.note("acme-api").read_text(), "from Obsidian")
            self.assertFalse(screen.editing)
            self.assertIn("from Obsidian", screen.text())

    async def test_escape_on_the_question_keeps_the_edit(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.outside_conflict(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertIs(app.screen, screen)
            self.assertTrue(screen.editing)
            self.assertEqual(screen.editor_text(), "m")
            self.assertEqual(self.note("acme-api").read_text(), "from Obsidian")

    async def test_a_write_error_shows_and_keeps_the_edit(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_notes(app, pilot)
            await pilot.press("e", "m")
            with patch("herdboard.notes.os.replace", side_effect=PermissionError("denied")):
                await pilot.press("escape")
                await self.settle(app, pilot)
            self.assertTrue(screen.editing)
            self.assertIn("cannot write the note", screen.status_text())
            self.assertEqual(screen.editor_text(), "m")

    async def test_quit_asks_when_the_edit_cannot_be_saved(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_notes(app, pilot)
            await pilot.press("e", "m")
            with patch("herdboard.notes.os.replace", side_effect=PermissionError("denied")):
                await app.action_quit()
                await self.settle(app, pilot)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await self.settle(app, pilot)
        self.assertFalse(self.note("acme-api").exists())

    async def test_escape_in_the_preview_goes_back_to_the_board(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_notes(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_the_tabs_use_nerd_icons_in_nerd_mode(self):
        self.store.config.icon_style = "nerd"
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.screen.query_one(TabBar).tab_labels()[2], " Notes")


if __name__ == "__main__":
    unittest.main()
