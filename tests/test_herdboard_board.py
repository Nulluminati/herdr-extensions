import asyncio
import fcntl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "plugins/herdboard/src"
sys.path.insert(0, str(SRC))

from herdboard import cli  # noqa: E402
from herdboard.board import AddScreen, DetailScreen, HelpScreen, HerdboardApp  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.identity import RepoIdentity  # noqa: E402
from herdboard.model import ConflictError, IoError, NotFoundError  # noqa: E402
from herdboard.store import Store  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/herdboard/kanban-2.0.51-full.md"
SIZE = (80, 24)
NARROW = (80, 24)


async def settle(pilot):
    """Wait for key handling, store writes and the reload that follows."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def fill(pilot, text, submit=True):
    """Set the Add dialog text directly; typing is covered by one test."""
    pilot.app.screen.query_one("#add-input").value = text
    if submit:
        await pilot.press("enter")
        await settle(pilot)


class BoardTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # unittest runs the loop in debug mode, which slows every Textual message several times
        # over and reports slow callbacks on a loaded machine.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(todos_dir=self.root / "todos", vault_root=self.root / "vault")
        (self.root / "vault").mkdir()
        self.store = Store(self.config)
        self.identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.repo_path = self.store.init_board(self.identity)
        self.inbox_path = self.store.ensure_inbox()

    def make_app(self, view="repo", interval=60.0, repo=True):
        paths = {
            "repo": [self.repo_path],
            "global": [self.inbox_path],
            "all": self.store.board_paths(),
        }
        app = HerdboardApp(
            self.store,
            view,
            paths_for=lambda name: paths[name] if name != "all" else self.store.board_paths(),
            repo_board=self.repo_path if repo else None,
            reload_interval=interval,
        )
        app.animation_level = "none"
        return app

    def headers(self, app):
        return [w.plain for w in app.query(".column-header")]

    def cards(self, app):
        return [w.plain for w in app.query(".card")]

    def selected(self, app):
        return [w.plain for w in app.query(".card.selected")]

    def refs(self, path):
        return {c.title: (c.ref, c.column) for c in self.store.list_cards([path])}


class RenderTests(BoardTestCase):
    async def test_columns_counts_limits_and_cards(self):
        text = FIXTURE.read_text().replace("kanban-plugin: board\n", "kanban-plugin: board\nrepo: github.com/acme/api\n", 1)
        self.repo_path.write_text(text)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(
                self.headers(app),
                ["Backlog 2", "Todo 2/5", "In progress 0", "Review 1", "Done 1"],
            )
            plain = self.cards(app)
            self.assertTrue(any("Compare tile suppliers #research" in c and "+0" not in c for c in plain))
            multi = [c for c in plain if "Plan the lighting" in c]
            self.assertEqual(len(multi), 1)
            self.assertIn("+3", multi[0])
            self.assertNotIn("^t3m9", " ".join(plain))
            self.assertIn("t3m9", " ".join(plain))
            self.assertFalse(any("Old card" in c for c in plain))
            self.assertEqual(len(list(app.query(".column.completed"))), 1)

    async def test_title_bar_and_board_name_only_when_several_boards(self):
        self.store.add(self.repo_path, "repo card", "Todo")
        self.store.add(self.inbox_path, "inbox card", "Todo")
        app = self.make_app("repo")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("repo", app.query_one("#title-bar").plain)
            self.assertIn("acme-api", app.query_one("#title-bar").plain)
            self.assertFalse(any("acme-api" in c for c in self.cards(app)))
            await pilot.press("v")
            await pilot.press("v")
            await pilot.pause()
            self.assertEqual(app.view, "all")
            plain = self.cards(app)
            self.assertTrue(any("repo card" in c and "acme-api" in c for c in plain))
            self.assertTrue(any("inbox card" in c and "inbox" in c for c in plain))

    async def test_columns_merge_by_name_and_follow_config_order(self):
        self.repo_path.write_text(
            self.repo_path.read_text().replace("## Done", "## Extra\n\n## Done", 1)
        )
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(
                [h.split()[0] for h in self.headers(app)],
                ["Backlog", "Todo", "In", "Review", "Done", "Extra"],
            )

    async def test_view_cycle_skips_repo_without_repository_board(self):
        app = self.make_app("global", repo=False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("v")
            self.assertEqual(app.view, "all")
            await pilot.press("v")
            self.assertEqual(app.view, "global")


class NavigationTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        for text, column in [("a1", "Backlog"), ("a2", "Backlog"), ("b1", "Todo")]:
            self.store.add(self.repo_path, text, column)

    async def test_arrows_and_vi_keys_move_selection(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("down")
            self.assertIn("a2", self.selected(app)[0])
            await pilot.press("k")
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("j")
            await pilot.press("right")
            self.assertIn("b1", self.selected(app)[0])
            await pilot.press("h")
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("l")
            self.assertIn("b1", self.selected(app)[0])
            await pilot.press("l")
            self.assertEqual(self.selected(app), [])
            await pilot.press("left", "left")
            self.assertIn("a", self.selected(app)[0])

    async def test_shift_keys_move_card_and_selection_follows(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["a1"][1], "Todo")
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("shift+right")
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["a1"][1], "In progress")
            app.action_move(-1)
            await settle(pilot)
            await pilot.press("H")
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["a1"][1], "Backlog")
            self.assertIn("a1", self.selected(app)[0])

    async def test_move_in_completed_column_checks_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for _ in range(4):
                app.action_move(1)
                await settle(pilot)
            card = [c for c in self.store.list_cards([self.repo_path]) if c.title == "a1"][0]
            self.assertEqual(card.column, "Done")
            self.assertTrue(card.checked)


class ActionTests(BoardTestCase):
    async def test_add_card_to_selected_column(self):
        self.store.add(self.repo_path, "seed", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "a")
            await pilot.press(*"new card")
            await pilot.press("enter")
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["new card"][1], "Todo")
            self.assertTrue(any("new card" in c for c in self.cards(app)))

    async def test_add_escape_cancels_then_global_add_goes_to_inbox(self):
        app = self.make_app("global")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.press(*"nope")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.store.list_cards(), [])
            await pilot.press("a")
            await fill(pilot, "g")
            self.assertEqual([c.board for c in self.store.list_cards()], ["inbox"])

    async def test_add_in_all_view_uses_selected_card_board(self):
        self.store.add(self.repo_path, "r", "Todo")
        self.store.add(self.inbox_path, "i", "Backlog")
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app.action_column(1)
            app.action_add()
            await pilot.pause()
            await fill(pilot, "x")
            added = [c for c in self.store.list_cards() if c.title == "x"][0]
            self.assertEqual(added.board, "acme-api")
            app.action_column(-1)
            app.action_add()
            await pilot.pause()
            await fill(pilot, "y")
            added = [c for c in self.store.list_cards() if c.title == "y"][0]
            self.assertEqual(added.board, "inbox")

    async def test_done_marks_card_done(self):
        self.store.add(self.repo_path, "finish me", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "d")
            await settle(pilot)
            card = self.store.list_cards([self.repo_path])[0]
            self.assertEqual((card.column, card.checked), ("Done", True))

    async def test_detail_opens_and_closes(self):
        (self.root / "vault" / "Tile notes.md").write_text("note body\n")
        card = self.store.add(self.repo_path, "Read [[Tile notes]] #docs 📅 2026-10-20", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            text = app.screen.query_one("#detail").plain
            for part in ["acme-api / Todo", card.ref, "2026-10-20", "docs", "note body"]:
                self.assertIn(part, text)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DetailScreen)

    async def test_help_lists_keys_and_q_quits(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            text = app.screen.query_one("#help").plain
            for key in ["shift+left", "enter", "escape", "quit"]:
                self.assertIn(key, text)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, HelpScreen)
            await pilot.press("q")
        self.assertFalse(app.is_running)


class ReloadTests(BoardTestCase):
    def change_externally(self, a):
        other = Store(self.config)
        other.move(a.ref, "Todo")
        other.add(self.repo_path, "external", "Review")

    async def test_timer_reloads_external_change(self):
        a = self.store.add(self.repo_path, "a", "Backlog")
        app = self.make_app(interval=0.05)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.change_externally(a)
            for _ in range(100):
                await pilot.pause(0.05)
                if any("external" in c for c in self.cards(app)):
                    break
            self.assertTrue(any("external" in c for c in self.cards(app)))

    async def test_manual_reload(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            self.store.add(self.repo_path, "later", "Todo")
            await pilot.press("r")
            await pilot.pause()
            self.assertTrue(any("later" in c for c in self.cards(app)))

    async def test_failed_reload_is_retried_and_selection_stays(self):
        a = self.store.add(self.repo_path, "a", "Backlog")
        b = self.store.add(self.repo_path, "b", "Backlog")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("down")
            self.change_externally(a)
            with mock.patch.object(self.store, "snapshot", side_effect=IoError("transient")):
                await app.check_files()
            await pilot.pause()
            self.assertFalse(any("external" in c for c in self.cards(app)))
            await app.check_files()
            await pilot.pause()
            self.assertTrue(any("external" in c for c in self.cards(app)))
            self.assertEqual(self.headers(app)[1], "Todo 1")
            self.assertIn(b.ref, self.selected(app)[0])

    def write_idless(self, *titles):
        text = self.inbox_path.read_text().replace(
            "## Backlog\n", "## Backlog\n\n" + "".join(f"- [ ] {t}\n" for t in titles), 1
        )
        self.inbox_path.write_text(text)

    async def test_idless_card_keeps_identity_when_earlier_card_is_removed(self):
        self.write_idless("a", "b", "c")
        app = self.make_app("global")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("down")
            self.assertIn("b", self.selected(app)[0])
            self.inbox_path.write_text(self.inbox_path.read_text().replace("- [ ] a\n", "", 1))
            await app.check_files()
            await pilot.pause()
            self.assertNotIn("c", self.selected(app)[0].split("\n")[0])
            await pilot.press("d")
            await settle(pilot)
            done = {c.title: c.column for c in self.store.list_cards([self.inbox_path])}
            self.assertEqual(done, {"b": "Done", "c": "Backlog"})

    async def test_idless_card_changed_externally_clears_selection(self):
        self.write_idless("a", "b", "c")
        app = self.make_app("global")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("down")
            self.inbox_path.write_text(self.inbox_path.read_text().replace("- [ ] b\n", "- [ ] b edited\n", 1))
            await app.check_files()
            await pilot.pause()
            self.assertIn("a", self.selected(app)[0].split("\n")[0])


class ScrollTests(BoardTestCase):
    async def test_selected_column_is_visible_at_80_columns(self):
        self.store.add(self.repo_path, "x", "Done")
        app = self.make_app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            for _ in range(4):
                app.action_column(1)
            await pilot.pause()
            await pilot.pause()
            current = list(app.query(".column.current"))
            self.assertEqual(len(current), 1)
            self.assertTrue(app.screen.region.contains_region(current[0].region))
            self.assertIn("Done", current[0].query_one(".column-header").plain)


class ColumnOrderTests(BoardTestCase):
    async def test_single_board_shows_file_order(self):
        text = self.repo_path.read_text()
        text = text.replace("## Todo", "## Review X").replace("## Review\n", "## Todo\n", 1)
        text = text.replace("## Review X", "## Review", 1)
        self.repo_path.write_text(text)
        order = [c.title for c in self.store.snapshot(self.repo_path).columns]
        self.assertLess(order.index("Review"), order.index("Todo"))
        app = self.make_app("repo")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual([h.split()[0] for h in self.headers(app)][:2], ["Backlog", "Review"])
            self.assertEqual([c.name for c in app.columns], order)


class AddDialogTests(BoardTestCase):
    async def test_destination_captured_when_dialog_opens(self):
        self.store.add(self.repo_path, "r", "Todo")
        self.store.add(self.inbox_path, "i", "Backlog")
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "a")
            self.assertIsInstance(app.screen, AddScreen)
            self.assertIn("acme-api / Todo", app.screen.query_one("#add-target").plain)
            # Delete the repository card, then reload under the open dialog.
            lines = [l for l in self.repo_path.read_text().splitlines() if "] r " not in l + " "]
            self.repo_path.write_text("\n".join(lines) + "\n")
            await app.check_files()
            await pilot.pause()
            self.assertEqual([c.title for c in self.store.list_cards([self.repo_path])], [])
            await fill(pilot, "late")
            added = [c for c in self.store.list_cards() if c.title == "late"][0]
            self.assertEqual((added.board, added.column), ("acme-api", "Todo"))

    async def test_failed_add_keeps_dialog_and_text(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            with mock.patch.object(self.store, "add", side_effect=ConflictError("locked")):
                await fill(pilot, "long title")
            self.assertIsInstance(app.screen, AddScreen)
            self.assertEqual(app.screen.query_one("#add-input").value, "long title")
            self.assertIn("locked", app.screen.query_one("#add-error").plain)
            await fill(pilot, "long title")
            self.assertNotIsInstance(app.screen, AddScreen)
            self.assertEqual(self.store.list_cards([self.repo_path])[0].title, "long title")

    async def test_failed_add_with_held_lock_keeps_input(self):
        self.store.lock_timeout = 0.05
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            with self.held_lock():
                await fill(pilot, "kept")
            self.assertIsInstance(app.screen, AddScreen)
            self.assertEqual(app.screen.query_one("#add-input").value, "kept")
            self.assertEqual(self.store.list_cards([self.repo_path]), [])

    def held_lock(self):
        return HeldLock(self.store.lock_path)

    async def test_first_card_on_fresh_install(self):
        config = Config(todos_dir=self.root / "fresh")
        store = Store(config)
        app = HerdboardApp(
            store,
            "global",
            paths_for=lambda name: [store.inbox_path] if store.inbox_path.is_file() else [],
        )
        app.animation_level = "none"
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(self.headers(app)[0], "Backlog 0")
            await pilot.press("a")
            await fill(pilot, "first")
            self.assertEqual([c.title for c in store.list_cards()], ["first"])
            self.assertTrue(any("first" in c for c in self.cards(app)))

    async def test_all_view_creates_inbox_on_fresh_install(self):
        store = Store(Config(todos_dir=self.root / "fresh"))
        app = HerdboardApp(store, "all", paths_for=lambda name: store.board_paths())
        app.animation_level = "none"
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertTrue(store.inbox_path.is_file())
            self.assertEqual(self.headers(app)[0], "Backlog 0")


class HeldLock:
    """Hold the store lock from the test thread."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.handle = open(self.path, "a")
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        self.handle.close()


class BusyTests(BoardTestCase):
    async def test_held_lock_does_not_block_keys_and_refuses_second_write(self):
        self.store.add(self.repo_path, "m", "Backlog")
        self.store.lock_timeout = 2.0
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            with HeldLock(self.store.lock_path):
                await pilot.press("L")
                await asyncio.wait_for(pilot.press("question_mark"), timeout=1.0)
                self.assertIsInstance(app.screen, HelpScreen)
                await pilot.press("escape")
                await pilot.press("L")
                self.assertTrue(any("Busy" in n.message for n in app._notifications))
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["m"][1], "Todo")


class ErrorTests(BoardTestCase):
    def notifications(self, app):
        return [(n.message, n.severity) for n in app._notifications]

    async def test_conflict_during_move_and_not_found_on_done_notify(self):
        self.store.add(self.repo_path, "c", "Backlog")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            with mock.patch.object(self.store, "move", side_effect=ConflictError("card changed")):
                await pilot.press("L")
                await settle(pilot)
            self.assertIn(("card changed", "error"), self.notifications(app))
            self.assertTrue(app.is_running)
            self.assertTrue(any("c" in c for c in self.cards(app)))
            with mock.patch.object(self.store, "done", side_effect=NotFoundError("No card x")):
                await pilot.press("d")
                await settle(pilot)
            self.assertIn(("No card x", "error"), self.notifications(app))
            self.assertTrue(app.is_running)

    async def test_missing_target_column_notifies(self):
        self.repo_path.write_text(
            self.repo_path.read_text().replace("## Done", "## Extra\n\n## Done", 1)
        )
        self.store.add(self.inbox_path, "inboxer", "Done")
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            # Columns: ..., Done, Extra. The inbox card sits in Done; move to Extra.
            for _ in range(4):
                app.action_column(1)
            self.assertIn("inboxer", self.selected(app)[0])
            await pilot.press("L")
            await settle(pilot)
            messages = [m for m, s in self.notifications(app) if s == "error"]
            self.assertEqual(len(messages), 1)
            self.assertIn("Extra", messages[0])
            self.assertTrue(app.is_running)
            self.assertEqual(self.refs(self.inbox_path)["inboxer"][1], "Done")


class CliBoardTests(unittest.TestCase):
    def test_board_command_runs_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            Store(Config(todos_dir=root / "todos")).ensure_inbox()
            with mock.patch.object(HerdboardApp, "run") as run:
                code = cli.main(["--config", str(config), "-C", str(root), "board", "--view", "all"])
            self.assertEqual(code, 0)
            run.assert_called_once_with()

    def test_board_command_default_view_is_global_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            seen = []
            with mock.patch.object(
                HerdboardApp, "__init__", lambda self, store, view, **kw: seen.append(view)
            ), mock.patch.object(HerdboardApp, "run"):
                cli.main(["--config", str(config), "-C", str(root), "board"])
            self.assertEqual(seen, ["global"])


if __name__ == "__main__":
    unittest.main()
