import asyncio
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "plugins/herdboard/src"
sys.path.insert(0, str(SRC))

from herdboard import cli  # noqa: E402
from herdboard.board import (  # noqa: E402
    HELP_SECTIONS, AddScreen, ConfirmScreen, DetailScreen, EditScreen, HelpScreen, HerdboardApp,
    PullRequestDetailScreen, PullRequestsScreen,
)
from textual.widgets import Markdown  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.identity import RepoIdentity  # noqa: E402
from herdboard.model import ConflictError, HerdboardError, IoError, NotFoundError  # noqa: E402
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
    pilot.app.screen.query_one("#card-title").value = text
    if submit:
        await pilot.press("enter")
        await settle(pilot)


def help_rows(screen):
    """The key and description of every help entry, in the order the screen shows them."""
    keys = [w.plain for w in screen.query(".help-key")]
    whats = [w.plain for w in screen.query(".help-what")]
    return list(zip(keys, whats))


class BoardTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # unittest runs the loop in debug mode, which slows every Textual message several times
        # over and reports slow callbacks on a loaded machine.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Keys such as `s` reach the real start code: keep its state and Herdr calls inside the test.
        env = {"HERDR_BIN_PATH": str(self.root / "no-herdr")}
        if not os.environ.get("XDG_STATE_HOME", "").startswith(tempfile.gettempdir()):
            env["XDG_STATE_HOME"] = str(self.root / "state")
        self.enterContext(mock.patch.dict(os.environ, env))
        self.config = Config(todos_dir=self.root / "todos", vault_root=self.root / "vault")
        (self.root / "vault").mkdir()
        self.store = Store(self.config)
        self.identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.repo_path = self.store.init_board(self.identity)
        self.inbox_path = self.store.ensure_inbox()

    def make_app(self, view="repo", interval=60.0, repo=True):
        paths = {
            "repo": [self.repo_path],
            "inbox": [self.inbox_path],
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
    def test_board_tests_use_a_private_state_folder_and_no_real_herdr(self):
        from herdboard import start as start_module
        self.assertTrue(str(start_module.state_dir()).startswith(tempfile.gettempdir()))
        self.assertEqual(os.environ["HERDR_BIN_PATH"], str(self.root / "no-herdr"))

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
            self.assertIn("Compare tile suppliers  t3m9\n research ", plain)
            multi = [c for c in plain if "Plan the lighting" in c]
            self.assertEqual(len(multi), 1)
            # A card without a block ID shows no ID; its first detail line is its description.
            self.assertEqual(multi[0], "Plan the lighting\nSecond line of the same card")
            self.assertFalse(any("Old card" in c for c in plain))
            self.assertEqual(len(list(app.query(".column.completed"))), 1)

    async def test_title_bar_and_board_name_only_when_several_boards(self):
        self.store.add(self.repo_path, "repo card", "Todo")
        self.store.add(self.inbox_path, "inbox card", "Todo")
        app = self.make_app("repo")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("repo", app.query_one("#header-bar").plain)
            self.assertIn("acme-api", app.query_one("#header-bar").plain)
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
        app = self.make_app("inbox", repo=False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("v")
            self.assertEqual(app.view, "all")
            await pilot.press("v")
            self.assertEqual(app.view, "inbox")


class CardLookTests(BoardTestCase):
    async def test_cards_show_their_id_after_the_title(self):
        card = self.store.add(self.repo_path, "Write release notes", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            text = [c for c in self.cards(app) if "Write release notes" in c][0]
            self.assertEqual(text, f"Write release notes  {card.ref}")
            first = app.query_one(".card").render_line(0).text.rstrip()
            self.assertTrue(first.endswith(card.ref), first)

    async def test_tags_are_colored_chips_and_leave_the_title(self):
        card = self.store.add(self.repo_path, "Fix login #auth #web 📅 2026-11-01", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            widget = app.query_one(".card")
            first_line = widget.plain.splitlines()[0]
            self.assertEqual(first_line, f"Fix login  {card.ref}")
            chips = [widget.rich_text.plain[span.start:span.end].strip() for span in widget.rich_text.spans
                     if " on " in str(span.style)]
            self.assertEqual(chips, ["auth", "web"])
            self.assertIn(" auth   web ", widget.plain)
            self.assertIn("Due Nov 1", widget.plain)

    async def test_block_tags_for_fonts_without_round_ends(self):
        self.config.tag_style = "block"
        self.store.add(self.repo_path, "Fix login #auth", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            widget = app.query_one(".card")
            self.assertEqual(widget.plain.splitlines()[1], " auth ")
            self.assertNotIn("\ue0b6", widget.plain)

    async def test_columns_fill_the_screen(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            widths = [w.outer_size.width for w in app.query(".column")]
            self.assertEqual(len(widths), 5)
            self.assertGreaterEqual(sum(widths), 145)
            self.assertLessEqual(max(widths) - min(widths), 1)

    async def test_narrow_screens_scroll_instead_of_squeezing(self):
        app = self.make_app()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            self.assertGreaterEqual(min(w.outer_size.width for w in app.query(".column")), 24)

    async def test_scroll_bars_are_thin(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(".cards").styles.scrollbar_size_vertical, 1)
            self.assertEqual(app.query_one("#columns").styles.scrollbar_size_horizontal, 1)


class FilterTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.store.add(self.repo_path, "Fix the login timeout #auth", "Todo")
        self.store.add(self.repo_path, "Write release notes #docs", "Todo", body=["mention the login fix"])
        self.store.add(self.repo_path, "Rotate keys #ops #auth", "Backlog")

    def visible(self, app):
        return sorted(card.title for column in app.columns for card in column.cards)

    async def test_slash_filters_by_words(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"timeout")
            await pilot.pause()
            self.assertEqual(self.visible(app), ["Fix the login timeout #auth"])
            self.assertIn('filter: "timeout"', app.query_one("#header-bar").plain)

    async def test_words_match_details_too(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"login")
            await pilot.pause()
            self.assertEqual(self.visible(app), ["Fix the login timeout #auth", "Write release notes #docs"])

    async def test_hash_filters_by_tag(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"#auth")
            await pilot.pause()
            self.assertEqual(self.visible(app), ["Fix the login timeout #auth", "Rotate keys #ops #auth"])
            await pilot.press("space", *"rotate")
            await pilot.pause()
            self.assertEqual(self.visible(app), ["Rotate keys #ops #auth"])

    async def test_enter_keeps_the_filter_and_escape_clears_it(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"#ops", "enter")
            await pilot.pause()
            self.assertFalse(app.query_one("#filter").display)
            self.assertEqual(self.visible(app), ["Rotate keys #ops #auth"])
            await pilot.press("l")
            self.assertEqual(app.col, 1, "board keys work again after enter")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(self.visible(app)), 3)
            self.assertNotIn("filter:", app.query_one("#header-bar").plain)

    async def test_escape_while_typing_clears_the_filter(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"#ops")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#filter").display)
            self.assertEqual(len(self.visible(app)), 3)
            await pilot.press("l")
            self.assertEqual(app.col, 1)

    async def test_the_filter_stays_after_a_reload(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"#docs", "enter")
            await pilot.pause()
            self.store.add(self.repo_path, "Another doc #docs", "Todo")
            await app.check_files()
            await pilot.pause()
            self.assertEqual(self.visible(app), ["Another doc #docs", "Write release notes #docs"])


    async def test_an_agent_status_change_alone_redraws_the_card(self):
        self.store.add(self.repo_path, "Watch the agent", "Todo")
        status = {"value": None}

        def mark(cards):
            for card in cards:
                card.agent_status = status["value"]

        self.enterContext(mock.patch("herdboard.board.agent_states.mark", side_effect=mark))
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            first = [w for w in app.query(".card") if "Watch the agent" in w.plain][0]
            self.assertTrue(first.rich_text.plain.startswith("○"), first.rich_text.plain)
            status["value"] = "blocked"
            await app.check_files()
            await pilot.pause()
            first = [w for w in app.query(".card") if "Watch the agent" in w.plain][0]
            self.assertTrue(first.rich_text.plain.startswith("×"), first.rich_text.plain)

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

    async def test_a_move_keeps_the_column_widgets(self):
        """Rebuilding every column on each move made the screen flash."""
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            columns = list(app.query(".column"))
            untouched = list(columns[3].query(".card")) + list(columns[2].query(".card"))
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(self.refs(self.repo_path)["a1"][1], "Todo")
            self.assertEqual(list(app.query(".column")), columns)
            self.assertEqual(list(columns[3].query(".card")) + list(columns[2].query(".card")), untouched)
            self.assertEqual(self.headers(app)[:2], ["Backlog 1", "Todo 2"])

    async def test_e_edits_title_and_body(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            title = app.screen.query_one("#card-title")
            body = app.screen.query_one("#card-body")
            self.assertEqual(title.value, "a1")
            self.assertEqual(body.text, "")
            await pilot.press("x")
            self.assertEqual(title.value, "a1x", "typing must not replace the whole title")
            title.value = "a1 renamed"
            app.screen.query_one("#card-tags").value = "#tag, web"
            app.screen.query_one("#card-due").value = "2026-11-01"
            body.text = "detail one\n  nested"
            await pilot.press("ctrl+s")
            await settle(pilot)
            card = self.store.show(self.refs(self.repo_path)["a1 renamed #tag #web 📅 2026-11-01"][0])
            self.assertEqual(card.body, ["\tdetail one", "\t  nested"])
            self.assertIn("a1 renamed", self.selected(app)[0])
            self.assertNotIsInstance(app.screen, EditScreen)

    async def test_tab_moves_between_title_and_details(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            order = []
            for _ in range(4):
                await pilot.press("tab")
                order.append(app.focused.id)
            self.assertEqual(order, ["card-type", "card-tags", "card-due", "card-body"])
            await pilot.press(*"bat", "enter", *"fish")
            await pilot.press("shift+tab", "shift+tab", "shift+tab", "shift+tab")
            self.assertEqual(app.focused.id, "card-title")
            await pilot.press("ctrl+s")
            await settle(pilot)
            ref = self.refs(self.repo_path)["a1"][0]
            self.assertEqual(self.store.show(ref).body, ["\tbat", "\tfish"])

    async def test_board_keys_do_nothing_behind_a_dialog(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            await pilot.press("tab", "l", "x")
            self.assertIsInstance(app.screen, EditScreen)
            self.assertEqual(app.col, 0)

    async def test_edit_splits_the_title_into_fields(self):
        ref = self.refs(self.repo_path)["a1"][0]
        self.store.edit(ref, title="Fix the #auth login 📅 2026-11-01")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#card-title").value, "Fix the login")
            self.assertEqual(app.screen.query_one("#card-tags").value, "auth")
            self.assertEqual(app.screen.query_one("#card-due").value, "2026-11-01")

    async def test_a_bad_due_date_stays_in_the_dialog(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#card-due").value = "2026-13-40"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIsInstance(app.screen, EditScreen)
            self.assertIn("2026-13-40", app.screen.query_one("#card-error").plain)
            self.assertIn("a1", self.refs(self.repo_path))

    async def test_e_shows_the_body_without_its_indent(self):
        ref = self.refs(self.repo_path)["a1"][0]
        self.store.edit(ref, body=["one", "\ttwo"])
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#card-body").text, "one\n\ttwo")

    async def test_escape_cancels_an_edit(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            app.screen.query_one("#card-title").value = "changed"
            await pilot.press("escape")
            await settle(pilot)
            self.assertIn("a1", self.refs(self.repo_path))

    async def test_an_edit_of_a_changed_card_shows_the_conflict(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            text = self.repo_path.read_text().replace("a1", "a1 from obsidian", 1)
            self.repo_path.write_text(text)
            app.screen.query_one("#card-title").value = "mine"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIsInstance(app.screen, EditScreen)
            self.assertIn("changed on disk", app.screen.query_one("#card-error").plain)
            self.assertIn("a1 from obsidian", self.refs(self.repo_path))

    async def test_b_moves_the_card_to_another_board(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            ref = self.refs(self.repo_path)["a1"][0]
            await pilot.press("b")
            await pilot.pause()
            self.assertEqual(app.screen.board_labels, ["Inbox"])
            await pilot.press("enter")
            await settle(pilot)
            moved = self.store.show(ref)
            self.assertEqual((moved.board, moved.column), ("inbox", "Backlog"))
            self.assertNotIn("a1", self.refs(self.repo_path))
            self.assertIn("a2", self.selected(app)[0])

    async def test_b_then_escape_keeps_the_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            await pilot.press("escape")
            await settle(pilot)
            self.assertIn("a1", self.refs(self.repo_path))

    async def test_shift_keys_reorder_the_card_in_its_column(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("J")
            await settle(pilot)
            self.assertEqual([c.title for c in app.columns[0].cards], ["a2", "a1"])
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("shift+up")
            await settle(pilot)
            self.assertEqual([c.title for c in app.columns[0].cards], ["a1", "a2"])
            self.assertIn("a1", self.selected(app)[0])
            await pilot.press("K")
            await settle(pilot)
            self.assertEqual([c.title for c in self.store.list_cards([self.repo_path])][:2], ["a1", "a2"])

    async def test_board_changes_update_the_sidebar_card(self):
        from herdboard import start as start_module
        app = self.make_app()
        with mock.patch.object(start_module, "update_card_token") as update:
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause()
                await pilot.press("L")
                await settle(pilot)
                await pilot.press("x")
                await pilot.pause()
                await pilot.press("y")
                await settle(pilot)
        calls = [(call.args[0].title, call.args[0].column, call.kwargs.get("removed", False))
                 for call in update.call_args_list]
        self.assertEqual(calls, [("a1", "Todo", False), ("a1", "Todo", True)])

    async def test_x_deletes_after_y(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            self.assertIn("a1", app.screen.query_one("#delete-prompt").plain)
            await pilot.press("y")
            await settle(pilot)
            self.assertNotIn("a1", self.refs(self.repo_path))
            self.assertIn("a2", self.selected(app)[0])

    async def test_x_then_n_or_escape_keeps_the_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for key in ("n", "escape"):
                await pilot.press("x")
                await pilot.pause()
                await pilot.press(key)
                await settle(pilot)
                self.assertIn("a1", self.refs(self.repo_path))
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

    async def test_add_escape_cancels_then_inbox_add_goes_to_inbox(self):
        app = self.make_app("inbox")
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
            screen = app.screen
            self.assertEqual(screen.query_one("#detail-title").plain, "Read [[Tile notes]]")
            meta = screen.query_one("#detail-meta").plain
            for part in ["acme-api", "Todo", card.ref]:
                self.assertIn(part, meta)
            chips = screen.query_one("#detail-chips")
            self.assertIn("docs", chips.plain)
            self.assertIn("Due Oct 20", chips.plain)
            self.assertTrue(any(" on " in str(span.style) for span in chips.rich_text.spans))
            note = screen.query_one(".detail-note")
            self.assertEqual(note.border_title, "Tile notes")
            self.assertIn("note body", note.query_one(Markdown).source)
            self.assertEqual(list(screen.query("#detail-body")), [])
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DetailScreen)

    async def test_detail_shows_the_last_log_entries(self):
        from datetime import datetime
        from herdboard import activity

        card = self.store.add(self.repo_path, "Ship it", "Todo")
        for number in range(12):
            activity.append(self.store, card.ref, f"Entry {number}.", "pi", now=datetime(2026, 10, 12, 9, number))
        activity.append(self.store, card.ref, "Blocked.\nNo key.", "claude", now=datetime(2026, 10, 12, 10, 0))
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "enter")
            await pilot.pause()
            block = app.screen.query_one("#detail-log")
            self.assertEqual(block.border_title, "Log")
            lines = block.plain.split("\n")
            self.assertEqual(lines[0], "2026-10-12 09:03 · pi · Entry 3.")
            self.assertEqual(lines[-2:], ["2026-10-12 10:00 · claude · Blocked.", "\u00a0\u00a0\u00a0\u00a0No key."])

    async def test_detail_without_a_log_has_no_log_block(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "enter")
            await pilot.pause()
            self.assertEqual(list(app.screen.query("#detail-log")), [])

    async def test_detail_shows_details_in_their_own_block(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo",
                              body=["- [ ] write notes", "- [x] tag release", "plain line", "  - nested"])
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.col, app.row = 1, 0
            await pilot.press("enter")
            await pilot.pause()
            block = app.screen.query_one("#detail-body")
            self.assertEqual(block.border_title, "Details")
            self.assertEqual(block.plain.splitlines(),
                             ["☐ write notes", "☑ tag release", "plain line", "\u00a0\u00a0• nested"])
            done = [span for span in block.rich_text.spans if "strike" in str(span.style)]
            self.assertEqual([block.plain[s.start:s.end] for s in done], ["tag release"])

    async def test_space_in_the_detail_toggles_a_checklist_item(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo",
                              body=["notes first", "- [ ] write notes", "- [ ] tag release"])
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.col, app.row = 1, 0
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.items, [1, 2])
            await pilot.press("space")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).body[1], "\t- [x] write notes")
            self.assertIn("☑ write notes", screen.query_one("#detail-body").plain)
            await pilot.press("j", "space")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).body[2], "\t- [x] tag release")
            await pilot.press("k", "space")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).body[1], "\t- [ ] write notes")
            self.assertIs(app.screen, screen, "the detail view stays open")

    async def test_the_selected_checklist_item_is_highlighted(self):
        self.store.add(self.repo_path, "Ship it", "Todo", body=["- [ ] one", "- [ ] two"])
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.col, app.row = 1, 0
            await pilot.press("enter", "j")
            await pilot.pause()
            block = app.screen.query_one("#detail-body")
            marked = [block.plain[s.start:s.end] for s in block.rich_text.spans if "reverse" in str(s.style)]
            self.assertEqual(marked, ["☐ two"])

    async def test_space_without_a_checklist_does_nothing(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo", body=["just text"])
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.col, app.row = 1, 0
            await pilot.press("enter", "space")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).body, ["\tjust text"])

    async def test_e_in_the_detail_opens_the_editor(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.col, app.row = 1, 0
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertIsInstance(app.screen, EditScreen)
            self.assertEqual(app.screen.query_one("#card-title").value, "Ship it")

    async def test_help_lists_keys_and_q_quits(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            rows = help_rows(app.screen)
            keys = [key for key, _ in rows]
            for key in ["shift+left, H", "enter", "escape", "q"]:
                self.assertIn(key, keys)
            self.assertEqual(dict(rows)["q"], "quit")
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, HelpScreen)
            await pilot.press("q")
        self.assertFalse(app.is_running)

    async def test_help_wraps_a_long_entry_under_its_own_description(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            lines = [strip.text for strip in app.screen._compositor.render_strips()]
        first = next(i for i, line in enumerate(lines) if "filter: words match" in line)
        tail = lines[first + 1]
        self.assertIn("enter keeps it", tail, "the entry should wrap at 80 columns")
        # The wrapped tail sits in the description column, not under the keys.
        self.assertEqual(tail.index("matches a tag"), lines[first].index("filter: words match"))

    async def test_help_groups_the_keys_in_sections(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            titles = [w.plain for w in app.screen.query(".help-section")]
        self.assertEqual(titles, [title for title, _ in HELP_SECTIONS])
        for title in ("Board", "Card detail", "Pull requests", "Pull request detail", "Mouse"):
            self.assertIn(title, titles)


def binding_keys(screen_class) -> set[str]:
    """Every key of a screen's bindings, as the help writes it."""
    names = {"slash": "/", "question_mark": "?"}
    return {names.get(key, key) for binding in screen_class.BINDINGS for key in binding.key.split(",")}


def help_keys(title: str) -> set[str]:
    """Every key that a help section lists. An entry such as "left, h" lists two keys."""
    return {key for entry, _ in dict(HELP_SECTIONS)[title] for key in entry.split(", ")}


class HelpCoverageTests(unittest.TestCase):
    def test_every_key_of_a_screen_has_a_help_entry(self):
        everywhere = help_keys("Everywhere")
        for screen_class, titles in [
            (HerdboardApp, ("Board", "Cards", "Agents and branches")),
            (DetailScreen, ("Card detail",)),
            (PullRequestsScreen, ("Pull requests",)),
            (PullRequestDetailScreen, ("Pull request detail",)),
        ]:
            listed = everywhere.union(*(help_keys(title) for title in titles))
            with self.subTest(screen=screen_class.__name__):
                self.assertEqual(binding_keys(screen_class) - listed, set())


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
            self.assertEqual(app.current_card().ref, b.ref)

    def write_idless(self, *titles):
        text = self.inbox_path.read_text().replace(
            "## Backlog\n", "## Backlog\n\n" + "".join(f"- [ ] {t}\n" for t in titles), 1
        )
        self.inbox_path.write_text(text)

    async def test_idless_card_keeps_identity_when_earlier_card_is_removed(self):
        self.write_idless("a", "b", "c")
        app = self.make_app("inbox")
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
        app = self.make_app("inbox")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("down")
            self.inbox_path.write_text(self.inbox_path.read_text().replace("- [ ] b\n", "- [ ] b edited\n", 1))
            await app.check_files()
            await pilot.pause()
            self.assertIn("a", self.selected(app)[0].split("\n")[0])


class DialogWrapTests(BoardTestCase):
    async def test_long_dialog_text_wraps_inside_the_dialog(self):
        detail = ("Local: rebase onto the base, move the base forward, remove the worktree. "
                  "PR: an agent writes the description, then a draft pull request opens.")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app.push_screen(ConfirmScreen("Merge abcd: a card with a long title into its base?", detail))
            await pilot.pause()
            dialog = app.screen.query_one(".dialog")
            labels = list(app.screen.query("Label"))
            for label in labels:
                self.assertTrue(dialog.content_region.contains_region(label.region), str(label.render()))
            self.assertGreater(labels[0].region.height, 1, "the detail wraps onto several lines")


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
    async def test_add_with_tags_date_and_details(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#card-title").value = "Write release notes"
            screen.query_one("#card-tags").value = "docs, release"
            screen.query_one("#card-due").value = "2026-11-01"
            screen.query_one("#card-body").text = "draft\nreview"
            await pilot.press("ctrl+s")
            await settle(pilot)
            card = self.store.list_cards([self.repo_path])[0]
            self.assertEqual(card.title, "Write release notes #docs #release 📅 2026-11-01")
            self.assertEqual(card.body, ["\tdraft", "\treview"])

    async def test_add_offers_this_repository_and_the_inbox(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            choice = app.screen.query_one("#card-board")
            labels = [str(label) for label, _ in app.screen.board_options]
            self.assertEqual(labels, ["This repository (acme-api)", "Inbox"])
            self.assertEqual(choice.value, str(self.repo_path))
            choice.value = str(self.inbox_path)
            await fill(pilot, "for later")
            self.assertEqual([c.title for c in self.store.list_cards([self.inbox_path])], ["for later"])
            self.assertEqual(self.store.list_cards([self.repo_path]), [])

    async def test_add_in_the_inbox_view_defaults_to_the_inbox(self):
        app = self.make_app("inbox")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#card-board").value, str(self.inbox_path))

    async def test_destination_captured_when_dialog_opens(self):
        self.store.add(self.repo_path, "r", "Todo")
        self.store.add(self.inbox_path, "i", "Backlog")
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "a")
            self.assertIsInstance(app.screen, AddScreen)
            self.assertIn("acme-api / Todo", app.screen.query_one("#card-target").plain)
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
            self.assertEqual(app.screen.query_one("#card-title").value, "long title")
            self.assertIn("locked", app.screen.query_one("#card-error").plain)
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
            self.assertEqual(app.screen.query_one("#card-title").value, "kept")
            self.assertEqual(self.store.list_cards([self.repo_path]), [])

    def held_lock(self):
        return HeldLock(self.store.lock_path)

    async def test_first_card_on_fresh_install(self):
        config = Config(todos_dir=self.root / "fresh")
        store = Store(config)
        app = HerdboardApp(
            store,
            "inbox",
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

    def test_board_command_default_view_is_inbox_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            seen = []
            with mock.patch.object(
                HerdboardApp, "__init__", lambda self, store, view, **kw: seen.append(view)
            ), mock.patch.object(HerdboardApp, "run"):
                cli.main(["--config", str(config), "-C", str(root), "board"])
            self.assertEqual(seen, ["inbox"])



class InitAssistTests(BoardTestCase):
    def make_unboarded_app(self):
        identity = RepoIdentity("github.com/acme/web", "github.com", ("acme", "web"))
        self.web = identity

        def paths_for(name):
            if name == "repo":
                board = self.store.board_for_repo(identity)
                return [board] if board else []
            return [self.inbox_path] if name == "inbox" else self.store.board_paths()

        app = HerdboardApp(
            self.store, "inbox", paths_for=paths_for, repo_board=None, reload_interval=60.0,
            available=lambda name: name != "repo" or self.store.board_for_repo(identity) is not None,
            init_board=lambda: self.store.init_board(identity),
        )
        app.animation_level = "none"
        return app

    def title(self, app):
        """The left part of the header: the board names and the board's state."""
        return app.query_one("#header-bar").place

    async def test_title_says_how_to_create_the_board(self):
        app = self.make_unboarded_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            self.assertIn("no board for this repository: press i to create it", self.title(app))

    async def test_i_creates_the_board_and_shows_it(self):
        app = self.make_unboarded_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            for _ in range(20):
                await pilot.pause(0.05)
                if app.view == "repo":
                    break
            self.assertEqual(app.view, "repo")
            board = self.store.board_for_repo(self.web)
            self.assertIsNotNone(board)
            self.assertEqual(app.repo_board, board)
            self.assertNotIn("press i", self.title(app))
            self.assertEqual(app.query_one("#header-bar").view, "repo")
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            self.assertEqual(app.view, "repo")

    async def test_i_with_a_board_says_so(self):
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            self.assertIn("This repository already has a board.", messages)

    async def test_t_without_linear_says_linear_is_off(self):
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            messages = [n.message for n in app._notifications]
            self.assertIn("Linear is off, so there are no Linear cards to hide.", messages)

    def test_board_command_offers_init_in_a_repository_without_a_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The CLI records checkouts in the state folder; keep that inside the test.
            self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}))
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            repo = root / "web"
            repo.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            seen = {}
            with mock.patch.object(
                HerdboardApp, "__init__", lambda self, store, view, **kw: seen.update(kw)
            ), mock.patch.object(HerdboardApp, "run"):
                cli.main(["--config", str(config), "-C", str(repo), "board"])
            board = seen["init_board"]()
            self.assertTrue(board.exists())
            self.assertIn("repo:", board.read_text())

    def test_board_command_has_no_init_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The CLI records checkouts in the state folder; keep that inside the test.
            self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}))
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            seen = {}
            with mock.patch.object(
                HerdboardApp, "__init__", lambda self, store, view, **kw: seen.update(kw)
            ), mock.patch.object(HerdboardApp, "run"):
                cli.main(["--config", str(config), "-C", str(root), "board"])
            self.assertIsNone(seen["init_board"])


class TerminalModeTests(unittest.TestCase):
    """Herdr reads mouse positions as cells; pixel reports (mode 1016) break its own tabs and sidebar."""

    def driver(self):
        from herdboard.board import CellMouseDriver

        driver = CellMouseDriver.__new__(CellMouseDriver)
        driver._mouse = True
        driver._mouse_pixels = False
        driver.written = []
        driver.write = driver.written.append
        driver.flush = lambda: None
        return driver

    def test_the_app_uses_the_cell_mouse_driver(self):
        from herdboard.board import CellMouseDriver

        from textual.app import App
        from textual.drivers.linux_driver import LinuxDriver

        app = HerdboardApp.__new__(HerdboardApp)
        with mock.patch.object(App, "get_driver_class", return_value=LinuxDriver):
            self.assertIs(app.get_driver_class(), CellMouseDriver)
        with mock.patch.object(App, "get_driver_class", return_value=object):
            self.assertIs(app.get_driver_class(), object)

    def test_pixel_mouse_reports_stay_off(self):
        driver = self.driver()
        driver._enable_mouse_pixels()
        self.assertNotIn("\x1b[?1016h", "".join(driver.written))
        self.assertFalse(driver._mouse_pixels)

    def test_in_band_resize_stays_off(self):
        # After an in-band resize report, Textual's parser reads every mouse position as pixels,
        # so Herdr's cell positions would land near the top-left corner.
        driver = self.driver()
        driver._query_in_band_window_resize()
        self.assertNotIn("\x1b[?2048$p", "".join(driver.written))

    def test_cell_mouse_reports_stay_on(self):
        driver = self.driver()
        driver._enable_mouse_support()
        self.assertIn("\x1b[?1000h", "".join(driver.written))
        self.assertIn("\x1b[?1006h", "".join(driver.written))


class ThemeTests(BoardTestCase):
    async def test_the_board_uses_the_terminal_palette_by_default(self):
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "ansi-dark")
            self.assertTrue(app.native_ansi_color)

    async def test_the_board_uses_the_configured_theme(self):
        self.config.theme = "nord"
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "nord")

    def test_an_unknown_theme_is_a_config_error(self):
        self.config.theme = "no-such-theme"
        with self.assertRaises(HerdboardError) as caught:
            self.make_app()
        self.assertIn("no-such-theme", str(caught.exception))
        self.assertIn("ansi-dark", str(caught.exception))

    async def test_cards_have_a_border_in_the_terminal_palette(self):
        self.store.add(self.repo_path, "first")
        self.store.add(self.repo_path, "second")
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            card = next(w for w in app.query(".card") if not w.has_class("selected"))
            self.assertEqual(card.styles.border_top[0], "round")

    async def test_the_current_column_and_the_selected_card_have_their_own_colors(self):
        self.store.add(self.repo_path, "selected", "Todo")
        self.store.add(self.repo_path, "beside", "Todo")
        self.store.add(self.repo_path, "elsewhere", "Done")
        app = self.make_app()
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            column = app.query_one(".column.current")
            selected = app.query_one(".card.selected")
            beside = next(w for w in column.query(".card") if w.plain.startswith("beside"))
            elsewhere = next(w for w in app.query(".card") if w.plain.startswith("elsewhere"))
            # Todo is the second column: its color is magenta.
            magenta, default, bright_black = 5, -1, 8
            self.assertEqual(column.styles.border_top[0], "heavy")
            self.assertEqual(column.styles.border_top[1].ansi, magenta)
            self.assertEqual(column.query_one(".column-header").styles.color.ansi, magenta)
            self.assertEqual(selected.styles.border_top[0], "heavy")
            self.assertEqual(selected.styles.border_top[1].ansi, default)
            # Cards outside the current column fade, so the eye goes to one column.
            self.assertEqual(elsewhere.styles.color.ansi, bright_black)
            self.assertNotEqual(beside.styles.color.ansi, bright_black)


if __name__ == "__main__":
    unittest.main()


class SubCardBoardTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.parent = self.store.add(self.repo_path, "Billing v2", "Todo")
        self.child = self.store.add(self.repo_path, "Add invoice table", "Todo", parent=self.parent.ref)
        self.done = self.store.add(self.repo_path, "Add tax field", "Done", parent=self.parent.ref)
        self.other = self.store.add(self.repo_path, "Unrelated", "Todo")

    def card_text(self, app, title):
        return next(text for text in self.cards(app) if text.startswith(title))

    async def test_sub_cards_show_their_parent_and_parents_their_progress(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertIn("↳ Billing v2", self.card_text(app, "Add invoice table"))
            self.assertNotIn("[[#^", self.card_text(app, "Add invoice table"))
            self.assertIn("1/2 done", self.card_text(app, "Billing v2"))
            self.assertNotIn("done", self.card_text(app, "Unrelated"))

    async def test_a_caret_filter_shows_one_family(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *f"^{self.parent.ref}")
            await pilot.pause()
            titles = sorted(card.ref for column in app.columns for card in column.cards)
            self.assertEqual(titles, sorted([self.parent.ref, self.child.ref, self.done.ref]))

    async def test_p_sets_and_removes_the_parent(self):
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            while app.current_card().ref != self.other.ref:
                await pilot.press("down")
                await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.labels, ["No parent", "Billing v2"])
            await pilot.press("down", "enter")
            await settle(pilot)
            self.assertEqual(self.store.show(self.other.ref).parent, self.parent.ref)
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot)
            self.assertIsNone(self.store.show(self.other.ref).parent)
