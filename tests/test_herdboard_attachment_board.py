"""The card detail lists the card's attachments, and opens one in a viewer or another application."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from textual.widgets import Markdown, OptionList  # noqa: E402

from herdboard import attachments, board  # noqa: E402
from herdboard.board import AttachmentChoiceScreen, AttachmentScreen, DetailScreen  # noqa: E402
from test_herdboard_board import BoardTestCase, settle  # noqa: E402

SIZE = (120, 40)


class AttachmentDetailTests(BoardTestCase):
    def attach(self, ref, name, content):
        source = self.root / name
        if isinstance(content, bytes):
            source.write_bytes(content)
        else:
            source.write_text(content, encoding="utf-8")
        return attachments.add(self.store, ref, source)

    async def open_detail(self, pilot):
        await pilot.pause()
        pilot.app.col, pilot.app.row = 1, 0
        await pilot.press("enter")
        await pilot.pause()
        self.assertIsInstance(pilot.app.screen, DetailScreen)

    async def test_the_detail_lists_the_attachments_with_their_sizes(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.attach(card.ref, "plan.md", "# Plan\n")
        self.attach(card.ref, "shot.png", b"\x89PNG" + b"\x00" * 2044)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            block = app.screen.query_one("#detail-files")
            self.assertEqual(block.border_title, "Attachments")
            self.assertEqual(block.plain.splitlines(), ["plan.md   7 B", "shot.png  2.0 KB"])
            self.assertIn("o: open attachment", app.screen.query(".dim").last().render().plain)

    async def test_a_card_without_attachments_has_no_block_and_o_says_so(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            self.assertEqual(list(app.screen.query("#detail-files")), [])
            await pilot.press("o")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertIn("The card has no attachments", [n.message for n in app._notifications])

    async def test_o_opens_the_only_markdown_attachment_in_a_viewer(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.attach(card.ref, "spec.md", "# Spec\n\nLong text.\n")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            await pilot.press("o")
            await pilot.pause()
            self.assertIsInstance(app.screen, AttachmentScreen)
            self.assertEqual(app.screen.query_one("#attachment-title").plain, "spec.md")
            self.assertIn("Long text.", app.screen.query_one(Markdown).source)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)

    async def test_a_text_attachment_shows_as_plain_text(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.attach(card.ref, "notes.txt", "# not a heading\nline two\n")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            await pilot.press("o")
            await pilot.pause()
            self.assertIsInstance(app.screen, AttachmentScreen)
            self.assertEqual(list(app.screen.query(Markdown)), [])
            self.assertEqual(app.screen.query_one("#attachment-text").plain, "# not a heading\nline two\n")

    async def test_o_with_several_attachments_asks_which_one(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.attach(card.ref, "a.md", "first\n")
        self.attach(card.ref, "b.md", "second\n")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_detail(pilot)
            await pilot.press("o")
            await pilot.pause()
            self.assertIsInstance(app.screen, AttachmentChoiceScreen)
            options = app.screen.query_one(OptionList)
            self.assertEqual([str(options.get_option_at_index(i).prompt) for i in range(options.option_count)],
                             ["a.md", "b.md"])
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, AttachmentScreen)
            self.assertIn("second", app.screen.query_one(Markdown).source)

    async def test_a_binary_attachment_opens_in_the_default_application(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        added = self.attach(card.ref, "shot.png", b"\x89PNG\r\n\x1a\n\x00")
        app = self.make_app()
        with mock.patch.object(board.subprocess, "Popen") as popen, \
                mock.patch.object(board.sys, "platform", "linux"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_detail(pilot)
                await pilot.press("o")
                await settle(pilot)
                self.assertIsInstance(app.screen, DetailScreen)
        self.assertEqual(popen.call_args.args[0], ["xdg-open", added["path"]])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    async def test_macos_uses_open(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        added = self.attach(card.ref, "shot.png", b"\x89PNG\r\n\x1a\n\x00")
        app = self.make_app()
        with mock.patch.object(board.subprocess, "Popen") as popen, \
                mock.patch.object(board.sys, "platform", "darwin"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_detail(pilot)
                await pilot.press("o")
                await settle(pilot)
        self.assertEqual(popen.call_args.args[0], ["open", added["path"]])

    async def test_a_missing_opener_is_an_error_notice(self):
        card = self.store.add(self.repo_path, "Ship it", "Todo")
        self.attach(card.ref, "shot.png", b"\x89PNG\r\n\x1a\n\x00")
        app = self.make_app()
        with mock.patch.object(board.subprocess, "Popen", side_effect=FileNotFoundError("xdg-open")), \
                mock.patch.object(board.sys, "platform", "linux"):
            async with app.run_test(size=SIZE) as pilot:
                await self.open_detail(pilot)
                await pilot.press("o")
                await settle(pilot)
                messages = [n.message for n in app._notifications]
        self.assertTrue(any(m.startswith("Cannot open shot.png with xdg-open") for m in messages), messages)


if __name__ == "__main__":
    unittest.main()
