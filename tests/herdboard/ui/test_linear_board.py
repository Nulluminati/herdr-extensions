import os
import threading
import unittest
from unittest import mock

from textual.widgets import OptionList

from herdboard import board
from herdboard.board import AttachmentChoiceScreen, DetailScreen
from herdboard.linear_links import ProjectLink
from support.herdboard_attachment_board import fake_opener
from support.herdboard_board import SIZE, fill, settle
from support.herdboard_linear_board import LinearBoardCase
from support.herdboard_linear_source import DEAD
from support.linear_data import PR_URL, attachment


class LinearBoardTests(LinearBoardCase):
    async def test_todos_show_first_then_linear_cards_after_the_worker_returns(self):
        self.gate = threading.Event()
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(self.headers(app)[1], "Todo 1")
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertIn("Linear: loading", self.title(app))
            self.gate.set()
            await settle(pilot)
            self.assertEqual(self.headers(app), ["Backlog 0", "Todo 3", "In progress 0", "Review 1", "Done 0"])
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            self.assertEqual(app.query_one("#header-bar").view, "repo")

    async def test_linear_cards_are_marked_and_show_pr(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            cards = self.cards(app)
            marked = [c for c in cards if "Issue 1" in c]
            self.assertEqual(len(marked), 1)
            self.assertTrue(marked[0].startswith("Issue 1  ◆ ENG-1\n"), marked[0])
            self.assertEqual(marked[0].splitlines()[-1], "PR")
            self.assertTrue(any("◆ ENG-2" in c and "PR" not in c for c in cards))
            self.assertFalse(any("◆" in c for c in cards if "local todo" in c))

    async def test_sub_issues_show_their_parent_and_parents_their_progress(self):
        issue = next(i for i in self.fixtures["issues"] if i["identifier"] == "ENG-2")
        issue["parent"] = {"id": "i1", "identifier": "ENG-1"}
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            cards = self.cards(app)
            self.assertIn("↳ Issue 1", next(c for c in cards if "Issue 2" in c))
            self.assertIn("0/1 done", next(c for c in cards if "Issue 1" in c))

    async def test_t_hides_and_shows_linear_cards(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertTrue(any("◆" in c for c in self.cards(app)))
            await pilot.press("t")
            await settle(pilot)
            self.assertFalse(any("◆" in c for c in self.cards(app)))
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertIn("Linear: hidden", self.title(app))
            await pilot.press("t")
            await settle(pilot)
            self.assertTrue(any("◆" in c for c in self.cards(app)))
            self.assertNotIn("hidden", self.title(app))

    async def test_e_on_a_linear_card_says_to_use_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("e")
            await pilot.pause()
            self.assertIn(("Edit Linear issues in Linear.", "warning"), self.notifications(app))

    async def test_b_on_a_linear_card_says_it_stays_in_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("b")
            await pilot.pause()
            self.assertIn(
                ("Linear issues stay in Linear; only todos change board.", "warning"), self.notifications(app)
            )

    async def test_reordering_a_linear_card_says_to_use_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("K")
            await pilot.pause()
            self.assertIn(("Change the order of Linear issues in Linear.", "warning"), self.notifications(app))

    async def test_x_on_a_linear_card_says_to_use_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("x")
            await pilot.pause()
            self.assertIn(("Delete Linear issues in Linear.", "warning"), self.notifications(app))

    async def test_title_shows_stale_since_when_offline(self):
        await self.prime()
        self.endpoint = DEAD
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: stale since \d\d:\d\d$")
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))

    async def prime(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)

    async def test_title_off_without_linear(self):
        app = self.make_linear_app(active=False)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertIn("Linear: off", self.title(app))
            self.assertEqual(self.fake.requests, [])
            await pilot.press("v", "v", "v")
            await pilot.pause()
            self.assertEqual(app.view, "repo")

    async def test_view_cycles_through_mine(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            seen = []
            for _ in range(5):
                await pilot.press("v")
                await settle(pilot)
                seen.append(app.view)
            self.assertEqual(seen, ["inbox", "all", "mine", "repo", "inbox"])
            await pilot.press("v", "v")
            await settle(pilot)
            self.assertEqual(app.view, "mine")
            self.assertTrue(any("Issue 5" in c for c in self.cards(app)))
            self.assertFalse(any("local todo" in c for c in self.cards(app)))

    async def test_inbox_view_has_no_linear_cards(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("v")
            await settle(pilot)
            self.assertEqual(app.view, "inbox")
            self.assertFalse(any("◆" in c for c in self.cards(app)))

    async def test_write_keys_still_work_on_todos(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, [c.ref for c in app.columns[1].cards if c.source == "todo"][0])
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(app.columns[2].name, "In progress")
            self.assertEqual([c.title for c in app.columns[2].cards], ["local todo"])

    async def test_enter_shows_issue_detail(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-2")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            screen = app.screen
            self.assertEqual(screen.query_one("#detail-title").plain, "Issue 2")
            meta = screen.query_one("#detail-meta").plain
            for part in [
                "Platform",
                "Review",
                "ENG-2",
                "In Review",
                "ENG",
                "https://linear.app/workspace/issue/ENG-2",
                "Type",
                "bug",
            ]:
                self.assertIn(part, meta)
            chips = screen.query_one("#detail-chips").plain
            for part in ["repo:api", "Due Oct 20"]:
                self.assertIn(part, chips)
            body = screen.query_one("#detail-body").plain
            self.assertIn("Line one", body)
            self.assertIn("Line two", body)

    async def open_issue(self, pilot, ref):
        await settle(pilot)
        self.select(pilot.app, ref)
        await pilot.press("enter")
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, DetailScreen)

    async def test_the_issue_detail_lists_the_linear_attachments(self):
        issue = next(i for i in self.fixtures["issues"] if i["identifier"] == "ENG-1")
        issue["attachments"]["nodes"] = [
            attachment(PR_URL, "Fix login", "Open"),
            attachment("https://example.com/spec"),
        ]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_issue(pilot, "ENG-1")
            block = app.screen.query_one("#detail-links")
            self.assertEqual(block.border_title, "Linear attachments")
            self.assertEqual(block.plain.splitlines(), ["Fix login · Open", f"  {PR_URL}", "https://example.com/spec"])
            self.assertIn("o: open attachment", app.screen.query(".dim").last().render().plain)

    async def test_an_issue_without_attachments_has_no_block(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_issue(pilot, "ENG-2")
            self.assertEqual(list(app.screen.query("#detail-links")), [])
            self.assertNotIn("o: open attachment", app.screen.query(".dim").last().render().plain)

    async def test_o_opens_the_only_linear_attachment_in_the_browser(self):
        app = self.make_linear_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen), mock.patch.object(board.sys, "platform", "linux"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_issue(pilot, "ENG-1")
                await pilot.press("o")
                await settle(pilot)
                self.assertIsInstance(app.screen, DetailScreen)
        self.assertEqual(popen.call_args.args[0], ["xdg-open", PR_URL])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    async def test_o_with_several_linear_attachments_asks_which_one(self):
        issue = next(i for i in self.fixtures["issues"] if i["identifier"] == "ENG-1")
        issue["attachments"]["nodes"] = [attachment(PR_URL, "Fix login"), attachment("https://example.com/spec")]
        app = self.make_linear_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen), mock.patch.object(board.sys, "platform", "linux"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_issue(pilot, "ENG-1")
                await pilot.press("o")
                await pilot.pause()
                self.assertIsInstance(app.screen, AttachmentChoiceScreen)
                await pilot.press("down", "enter")
                await settle(pilot)
        self.assertEqual(popen.call_args.args[0], ["xdg-open", "https://example.com/spec"])

    async def test_the_chooser_shows_brackets_in_titles_as_text(self):
        issue = next(i for i in self.fixtures["issues"] if i["identifier"] == "ENG-1")
        issue["attachments"]["nodes"] = [
            attachment(PR_URL, "Fix [/api] route"),
            attachment("https://example.com/spec", "[b]Spec[/b]"),
        ]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_issue(pilot, "ENG-1")
            await pilot.press("o")
            await settle(pilot)
            self.assertIsInstance(app.screen, AttachmentChoiceScreen)
            options = app.screen.query_one(OptionList)
            self.assertEqual(
                [str(options.get_option_at_index(i).prompt) for i in range(2)], ["Fix [/api] route", "[b]Spec[/b]"]
            )

    async def test_a_url_that_is_not_a_web_address_does_not_open(self):
        issue = next(i for i in self.fixtures["issues"] if i["identifier"] == "ENG-1")
        issue["attachments"]["nodes"] = [attachment("file:///etc/passwd", "Local file")]
        app = self.make_linear_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_issue(pilot, "ENG-1")
                await pilot.press("o")
                await settle(pilot)
                self.assertIn(
                    ("Cannot open file:///etc/passwd: only http and https addresses open", "error"),
                    self.notifications(app),
                )
        popen.assert_not_called()

    async def test_a_click_on_a_linear_attachment_opens_it(self):
        app = self.make_linear_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen), mock.patch.object(board.sys, "platform", "darwin"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_issue(pilot, "ENG-1")
                app.screen.action_open_link(0)
                await settle(pilot)
        self.assertEqual(popen.call_args.args[0], ["open", PR_URL])

    async def test_add_always_adds_a_todo(self):
        app = self.make_linear_app("mine")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("a")
            await pilot.pause()
            await fill(pilot, "from mine")
            titles = [c.title for c in self.store.list_cards([self.inbox_path])]
            self.assertEqual(titles, ["from mine"])
            self.assertTrue(any(c.ref == "ENG-1" for d in app.columns for c in d.cards))

    async def test_reload_key_and_interval_fetch_again(self):
        app = self.make_linear_app(linear_interval=0.3)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            first = len(self.requests("ProjectIssues"))
            await pilot.pause(0.5)
            await settle(pilot)
            second = len(self.requests("ProjectIssues"))
            self.assertGreater(second, first)
            await pilot.press("r")
            await settle(pilot)
            self.assertGreater(len(self.requests("ProjectIssues")), second)

    async def test_inaccessible_project_notifies(self):
        self.store.update_links(self.repo_path, lambda links: links + [ProjectLink("gone", "Old project")])
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            messages = [m for m, s in self.notifications(app) if s == "warning"]
            self.assertTrue(any("Old project" in m for m in messages))
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))

    async def test_authentication_error_notifies_and_keeps_todos(self):
        os.environ["LINEAR_API_KEY"] = "wrong-key"
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertTrue(any("uthentication" in m and s == "error" for m, s in self.notifications(app)))
            self.assertIn("Linear: authentication failed", self.title(app))
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertFalse(any("◆" in c for c in self.cards(app)))


class LinearBoardFixTests(LinearBoardCase):
    async def test_revoked_key_drops_linear_cards_and_keeps_todos(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertTrue(any("◆" in c for c in self.cards(app)))
            self.fake.responses.append(
                (
                    200,
                    {
                        "errors": [
                            {"message": "Authentication required", "extensions": {"code": "AUTHENTICATION_ERROR"}}
                        ]
                    },
                    {},
                )
            )
            await pilot.press("r")
            await settle(pilot)
            self.assertIn("Linear: authentication failed", self.title(app))
            self.assertFalse(any("◆" in c for c in self.cards(app)))
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertTrue(any("uthentication" in m and s == "error" for m, s in self.notifications(app)))
            self.assertEqual(app.linear_results, {})
            # The next refresh asks Linear again and recovers.
            await pilot.press("r")
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            self.assertTrue(any("◆" in c for c in self.cards(app)))

    async def test_board_recovers_after_a_network_failure(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            client = self.source_in_use.client
            client.endpoint = DEAD
            await pilot.press("r")
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: stale since \d\d:\d\d$")
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))
            client.endpoint = self.endpoint
            count = len(self.fake.requests)
            await pilot.press("r")
            await settle(pilot)
            self.assertGreater(len(self.fake.requests), count)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")

    async def test_file_checks_and_reloads_never_call_linear(self):
        app = self.make_linear_app("project", project="Mobile")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertEqual(len(self.requests("Projects")), 1)
            self.assertTrue(any("Issue 5" in c for c in self.cards(app)))
            count = len(self.fake.requests)
            for _ in range(3):
                await app.check_files()
                await app.reload()
            await pilot.pause()
            self.assertEqual(len(self.fake.requests), count)
            await pilot.press("r")
            await settle(pilot)
            self.assertEqual(len(self.requests("Projects")), 1)

    async def test_one_fetch_runs_at_a_time_and_one_refresh_is_queued(self):
        self.fake.delay = 0.2
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("r")
            await pilot.pause(0.1)
            await settle(pilot)
            await pilot.pause(0.1)
            await settle(pilot)
            self.assertEqual(self.most_running, 1)
            self.assertEqual(self.fetches, 2)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
