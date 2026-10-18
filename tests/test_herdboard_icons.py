"""Display icons stay optional, keep labels, and fit narrow card layouts."""

from dataclasses import asdict
from datetime import date
from io import StringIO
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from rich.cells import cell_len  # noqa: E402
from rich.console import Console  # noqa: E402
from herdboard import attachments, board  # noqa: E402
from herdboard.store import CardView  # noqa: E402
from test_herdboard_board import BoardTestCase, settle  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402


TYPE_ICONS = {"bug": "\uf188", "feature": "\uf0eb", "chore": "\uf0ad", "docs": "\uf02d", "refactor": "\uf021"}
CALENDAR = "\uf073"
ATTACHMENT = "\uf0c6"
BRANCH = "\ue0a0"
PULL_REQUEST = "\uf407"


def card(**kwargs):
    fields = dict(ref="t123", board="api", path=None, column="Todo", title="Ship it",
                  checked=False, due=None, tags=[], body=[])
    fields.update(kwargs)
    return CardView(**fields)


class IconTextTests(unittest.TestCase):
    def test_type_badges_keep_names_colors_and_case(self):
        for kind, icon in TYPE_ICONS.items():
            for shown in (kind, kind.upper()):
                with self.subTest(kind=shown):
                    plain = board.type_badge(shown)
                    nerd = board.type_badge(shown, icon_style="nerd")
                    self.assertEqual(plain.plain, shown)
                    self.assertEqual(nerd.plain, f"{icon} {shown}")
                    self.assertEqual(plain.style, nerd.style)

    def test_custom_and_empty_types_do_not_get_a_placeholder(self):
        for kind in ("spike", "", "calendar"):
            for style in ("plain", "nerd"):
                with self.subTest(kind=kind, style=style):
                    self.assertEqual(board.type_badge(kind, icon_style=style).plain, kind)

    def test_dates_keep_labels_and_urgency_in_both_modes(self):
        today = date(2026, 10, 15)
        for due, shown, color in (("2026-10-14", "Oct 14", "bold red"),
                                  ("2026-10-17", "Oct 17", "yellow"),
                                  ("2027-01-01", "Jan 1 2027", "dim"),
                                  ("invalid", "invalid", "dim")):
            for style in ("plain", "nerd"):
                with self.subTest(due=due, style=style):
                    label = board.due_label(due, today, icon_style=style)
                    prefix = f"{CALENDAR} " if style == "nerd" else ""
                    self.assertEqual(label.plain, f"{prefix}Due {shown}")
                    self.assertEqual(label.style, color)

    def test_pull_requests_and_worktrees_use_shared_icons(self):
        for style in ("plain", "nerd"):
            item = card(source="linear", pr=True, pr_state="merged", pr_writing=True,
                        worktree_removed=True, worktree_left=True)
            text = board.card_text(item, False, icon_style=style).plain
            for label, icon in (("PR", PULL_REQUEST), ("PR merged", PULL_REQUEST),
                                ("PR: writing", PULL_REQUEST), ("worktree removed", BRANCH),
                                ("worktree still exists", BRANCH)):
                expected = f"{icon} {label}" if style == "nerd" else label
                self.assertIn(expected, text)
                if style == "plain":
                    self.assertNotIn(icon, text)
            meta = board.DetailScreen(item, None, icon_style=style)._meta().plain
            self.assertIn("Pull request", meta)
            self.assertIn(f"{PULL_REQUEST} writing" if style == "nerd" else "writing", meta)
            self.assertIn(f"{BRANCH} removed" if style == "nerd" else "removed", meta)
            item.pr_writing = False
            item.worktree_removed = False
            meta = board.DetailScreen(item, None, icon_style=style)._meta().plain
            self.assertIn(f"{PULL_REQUEST} merged" if style == "nerd" else "merged", meta)
            self.assertIn(f"{BRANCH} still exists" if style == "nerd" else "still exists", meta)

    def test_plain_local_detail_status_rows_match_approved_output(self):
        base = "Board        api\nColumn       Todo\nID           t123\nType         bug"
        cases = (
            ({}, base),
            ({"pr_state": "open", "worktree_left": True},
             base + "\nPull request open\nWorktree     still exists"),
            ({"pr_state": "merged", "worktree_removed": True},
             base + "\nPull request merged\nWorktree     removed"),
            ({"pr_state": "open", "pr_writing": True}, base + "\nPull request writing"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                item = card(type="bug", **status)
                self.assertEqual(item.source, "todo")
                self.assertEqual(board.DetailScreen(item, None)._meta().plain, expected)

    def test_detail_type_values_are_unstyled_in_plain_mode_and_badges_in_nerd_mode(self):
        for kind in ("bug", "spike"):
            for mode in ("plain", "nerd"):
                with self.subTest(kind=kind, mode=mode):
                    meta = board.DetailScreen(card(type=kind), None, icon_style=mode)._meta()
                    offset = meta.plain.rindex(kind)
                    style = meta.get_style_at_offset(Console(), offset)
                    if mode == "plain":
                        self.assertFalse(style.bold)
                        self.assertIsNone(style.color)
                    else:
                        self.assertTrue(style.bold)
                        self.assertEqual(style.color.name if style.color else None, "red" if kind == "bug" else None)

    def test_rendering_does_not_change_data_or_filter_results(self):
        item = card(title="Ship it #type/bug #api 📅 2026-11-01", type="bug", tags=["api"],
                    due="2026-11-01", has_attachments=True)
        before = asdict(item)
        for style in ("plain", "nerd"):
            widget = board.CardWidget(item, False, tag_style="block", icon_style=style)
            self.assertNotIn("📅", widget.plain)
            self.assertEqual(board.card_matches(item, ["#type/bug", "ship"]), True)
            self.assertEqual(board.card_matches(item, ["#type/docs"]), False)
            self.assertEqual(asdict(item), before)

    def test_attachment_label_stays_at_the_right_edge_at_24_and_80_columns(self):
        for width in (24, 80):
            for style in ("plain", "nerd"):
                with self.subTest(width=width, style=style):
                    item = card(title="Ship it #type/bug 📅 2026-11-01", type="bug", has_attachments=True)
                    widget = board.CardWidget(item, False, icon_style=style)
                    output = StringIO()
                    Console(file=output, width=width, color_system=None).print(widget.content)
                    first, *rest = output.getvalue().splitlines()
                    label = f"{ATTACHMENT} files" if style == "nerd" else "files"
                    self.assertTrue(first.endswith(f"{label} t123"), first)
                    self.assertEqual(cell_len(first), width)
                    self.assertTrue(all(cell_len(line) <= width for line in rest))
                    self.assertIn(label, widget.plain)


class IconBoardTests(BoardTestCase):
    async def test_modes_reach_cards_details_and_card_replacement_at_narrow_widths(self):
        item = self.store.add(self.repo_path, "Ship it #type/feature #api 📅 2026-11-01", "Backlog")
        source = self.root / "spec.md"
        source.write_text("# Spec\n")
        attachments.add(self.store, item.ref, source)
        stored = self.repo_path.read_bytes()
        for width in (24, 80):
            for style in ("plain", "nerd"):
                with self.subTest(width=width, style=style):
                    self.config.icon_style = style
                    self.config.tag_style = "block"
                    app = self.make_app()
                    async with app.run_test(size=(width, 30)) as pilot:
                        await pilot.pause()
                        widget = app.query_one(".card")
                        self.assertIn(f"{TYPE_ICONS['feature']} feature" if style == "nerd" else "feature", widget.plain)
                        self.assertIn(f"{CALENDAR} Due Nov 1" if style == "nerd" else "Due Nov 1", widget.plain)
                        label = f"{ATTACHMENT} files" if style == "nerd" else "files"
                        self.assertIn(label, widget.plain)
                        first = widget.render_line(0).text.rstrip()
                        self.assertTrue(first.endswith(f"{label} {item.ref}"), first)
                        self.assertEqual(cell_len(first), widget.size.width)
                        await pilot.press("enter")
                        await settle(pilot)
                        screen = app.screen
                        self.assertIsInstance(screen, board.DetailScreen)
                        self.assertIn(f"{TYPE_ICONS['feature']} feature" if style == "nerd" else "feature",
                                      screen.query_one("#detail-meta").plain)
                        self.assertIn(f"{CALENDAR} Due Nov 1" if style == "nerd" else "Due Nov 1",
                                      screen.query_one("#detail-chips").plain)
                        self.assertEqual(str(screen.query_one("#detail-files").border_title),
                                         f"{ATTACHMENT} Attachments" if style == "nerd" else "Attachments")
                        await pilot.press("escape")
                        self.store.edit(item.ref, body=["Updated details"])
                        await app.reload(item.ref)
                        await pilot.pause()
                        self.assertIn("Updated details", app.query_one(".card").plain)
                        if style == "nerd":
                            self.assertIn(TYPE_ICONS["feature"], app.query_one(".card").plain)
                        self.store.edit(item.ref, body=[])
        self.assertEqual(self.repo_path.read_bytes(), stored)

    async def test_round_tags_stay_independent_in_plain_mode(self):
        self.config.icon_style = "plain"
        self.config.tag_style = "round"
        self.store.add(self.repo_path, "Ship it #api", "Backlog")
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertIn(f"{board.ROUND_LEFT}api{board.ROUND_RIGHT}", app.query_one(".card").plain)


class IconCliTests(CliTestCase):
    def test_modes_leave_markdown_text_and_json_output_unchanged(self):
        title = "Ship it #type/bug #api 📅 2026-11-01"
        item = self.run_json("add", title)["card"]
        commands = (("list",), ("--json", "list"), ("--json", "show", item["ref"]))
        baseline = [self.run_cli(*args) for args in commands]
        path = Path(item["path"])
        stored = path.read_bytes()
        for style in ("plain", "nerd"):
            with self.subTest(style=style):
                self.config.write_text(f'todos_dir = "{self.todos}"\nicon_style = "{style}"\n')
                for args, expected in zip(commands, baseline):
                    output, errors = self.run_cli(*args)
                    self.assertEqual((output, errors), expected)
                    self.assertEqual(errors, "")
                self.assertEqual(self.run_json("show", item["ref"])["card"]["title"], title)
                self.assertEqual(path.read_bytes(), stored)


if __name__ == "__main__":
    unittest.main()
