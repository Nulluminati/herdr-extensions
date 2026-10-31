"""Display icons stay optional, keep labels, and fit narrow card layouts."""

import unittest
from dataclasses import asdict
from datetime import date
from io import StringIO
from pathlib import Path

from rich.cells import cell_len
from rich.console import Console

from herdboard import board
from herdboard.store import CardView
from support.herdboard_cli import CliTestCase
from support.herdboard_icons import ATTACHMENT, CALENDAR, TYPE_ICONS

BRANCH = "\ue0a0"
PULL_REQUEST = "\uf407"


def card(**kwargs):
    fields = dict(
        ref="t123", board="api", path=None, column="Todo", title="Ship it", checked=False, due=None, tags=[], body=[]
    )
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
        for due, shown, color in (
            ("2026-10-14", "Oct 14", "bold red"),
            ("2026-10-17", "Oct 17", "yellow"),
            ("2027-01-01", "Jan 1 2027", "dim"),
            ("invalid", "invalid", "dim"),
        ):
            for style in ("plain", "nerd"):
                with self.subTest(due=due, style=style):
                    label = board.due_label(due, today, icon_style=style)
                    prefix = f"{CALENDAR} " if style == "nerd" else ""
                    self.assertEqual(label.plain, f"{prefix}Due {shown}")
                    self.assertEqual(label.style, color)

    def test_pull_requests_and_worktrees_use_shared_icons(self):
        for style in ("plain", "nerd"):
            item = card(
                source="linear", pr=True, pr_state="merged", pr_writing=True, worktree_removed=True, worktree_left=True
            )
            text = board.card_text(item, False, icon_style=style).plain
            for label, icon in (
                ("PR", PULL_REQUEST),
                ("PR merged", PULL_REQUEST),
                ("PR: writing", PULL_REQUEST),
                ("worktree removed", BRANCH),
                ("worktree still exists", BRANCH),
            ):
                expected = f"{icon} {label}" if style == "nerd" else label
                self.assertIn(expected, text)
                if style == "plain":
                    self.assertNotIn(icon, text)
            meta = board.DetailScreen(item, icon_style=style)._meta().plain
            self.assertIn("Pull request", meta)
            self.assertIn(f"{PULL_REQUEST} writing" if style == "nerd" else "writing", meta)
            self.assertIn(f"{BRANCH} removed" if style == "nerd" else "removed", meta)
            item.pr_writing = False
            item.worktree_removed = False
            meta = board.DetailScreen(item, icon_style=style)._meta().plain
            self.assertIn(f"{PULL_REQUEST} merged" if style == "nerd" else "merged", meta)
            self.assertIn(f"{BRANCH} still exists" if style == "nerd" else "still exists", meta)

    def test_plain_local_detail_status_rows_match_approved_output(self):
        base = "Board        api\nColumn       Todo\nID           t123\nType         bug"
        cases = (
            ({}, base),
            ({"pr_state": "open", "worktree_left": True}, base + "\nPull request open\nWorktree     still exists"),
            ({"pr_state": "merged", "worktree_removed": True}, base + "\nPull request merged\nWorktree     removed"),
            ({"pr_state": "open", "pr_writing": True}, base + "\nPull request writing"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                item = card(type="bug", **status)
                self.assertEqual(item.source, "todo")
                self.assertEqual(board.DetailScreen(item)._meta().plain, expected)

    def test_detail_type_values_are_unstyled_in_plain_mode_and_badges_in_nerd_mode(self):
        for kind in ("bug", "spike"):
            for mode in ("plain", "nerd"):
                with self.subTest(kind=kind, mode=mode):
                    meta = board.DetailScreen(card(type=kind), icon_style=mode)._meta()
                    offset = meta.plain.rindex(kind)
                    style = meta.get_style_at_offset(Console(), offset)
                    if mode == "plain":
                        self.assertFalse(style.bold)
                        self.assertIsNone(style.color)
                    else:
                        self.assertTrue(style.bold)
                        self.assertEqual(style.color.name if style.color else None, "red" if kind == "bug" else None)

    def test_rendering_does_not_change_data_or_filter_results(self):
        item = card(
            type="bug",
            tags=["api"],
            due="2026-11-01",
            has_attachments=True,
        )
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
                    item = card(type="bug", due="2026-11-01", has_attachments=True)
                    widget = board.CardWidget(item, False, icon_style=style)
                    output = StringIO()
                    Console(file=output, width=width, color_system=None).print(widget.content)
                    first, *rest = output.getvalue().splitlines()
                    label = ATTACHMENT if style == "nerd" else "files"
                    self.assertTrue(first.endswith(f"{label} t123"), first)
                    self.assertEqual(cell_len(first), width)
                    self.assertTrue(all(cell_len(line) <= width for line in rest))
                    self.assertIn(label, widget.plain)


class IconCliTests(CliTestCase):
    def test_modes_leave_card_files_and_json_output_unchanged(self):
        title = "Ship it"
        item = self.run_json("add", title, "--type", "bug", "--tag", "api", "--due", "2026-11-01")["card"]
        commands = (("list",), ("--json", "list"), ("--json", "show", item["ref"]))
        baseline = [self.run_cli(*args) for args in commands]
        path = Path(item["path"]) / "cards" / item["ref"] / "card.md"
        stored = path.read_bytes()
        for style in ("plain", "nerd"):
            with self.subTest(style=style):
                self.config.write_text(f'todos_dir = "{self.todos}"\nicon_style = "{style}"\n')
                for args, expected in zip(commands, baseline, strict=True):
                    output, errors = self.run_cli(*args)
                    self.assertEqual((output, errors), expected)
                    self.assertEqual(errors, "")
                self.assertEqual(self.run_json("show", item["ref"])["card"]["title"], title)
                self.assertEqual(path.read_bytes(), stored)


if __name__ == "__main__":
    unittest.main()
