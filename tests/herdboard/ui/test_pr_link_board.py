"""The card detail shows a link to the card's pull request, and opens it in the browser."""

import json
import unittest
from unittest import mock

from herdboard import attachments, board, start
from herdboard.board import AttachmentChoiceScreen, DetailScreen
from support.herdboard_attachment_board import fake_opener
from support.herdboard_board import BoardTestCase, settle

SIZE = (120, 40)
PR = "https://github.com/acme/api/pull/7"


class PullRequestLinkTests(BoardTestCase):
    def with_pr(self, ref, url=PR, state="open"):
        path = start.state_dir() / "starts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({ref: {"ref": ref, "pr_url": url, "pr_state": state}}))

    async def open_detail(self, pilot):
        await pilot.pause()
        pilot.app.col, pilot.app.row = 1, 0
        await pilot.press("enter")
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, DetailScreen)

    def test_mark_removed_keeps_the_pull_request_url(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        start.mark_removed([card])
        self.assertIsNone(card.pr_url)
        self.with_pr(card.ref)
        start.mark_removed([card])
        self.assertEqual(card.pr_url, PR)

    async def test_the_detail_shows_the_pull_request_link(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.with_pr(card.ref, state="merged")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            block = app.screen.query_one("#detail-pr")
            self.assertEqual(block.border_title, "Pull request")
            self.assertEqual(block.plain, PR)
            self.assertIn("o: open", app.screen.query_one(".dialog").border_subtitle)

    async def test_a_card_without_a_pull_request_has_no_block(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            self.assertEqual(list(app.screen.query("#detail-pr")), [])

    async def test_o_opens_the_pull_request_in_the_browser(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.with_pr(card.ref)
        app = self.make_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen), mock.patch.object(board.sys, "platform", "linux"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_detail(pilot)
                await pilot.press("o")
                await settle(pilot)
                self.assertIsInstance(app.screen, DetailScreen)
        self.assertEqual(popen.call_args.args[0], ["xdg-open", PR])

    async def test_o_lists_the_pull_request_first_among_the_attachments(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.with_pr(card.ref)
        source = self.root / "plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        attachments.add(self.store, card.ref, source)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            await pilot.press("o")
            await settle(pilot)
            self.assertIsInstance(app.screen, AttachmentChoiceScreen)
            self.assertEqual([entry["name"] for entry in app.screen.files], ["Pull request", "plan.md"])

    async def test_a_click_on_the_link_opens_the_pull_request(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.with_pr(card.ref)
        app = self.make_app()
        popen = fake_opener()
        with mock.patch.object(board.subprocess, "Popen", popen), mock.patch.object(board.sys, "platform", "darwin"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_detail(pilot)
                app.screen.action_open_pr()
                await settle(pilot)
        self.assertEqual(popen.call_args.args[0], ["open", PR])


if __name__ == "__main__":
    unittest.main()
