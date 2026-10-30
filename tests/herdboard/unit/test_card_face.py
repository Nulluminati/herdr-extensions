"""Card rows, column colors and card styles on the board."""

import subprocess
import unittest
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from herdboard import board
from herdboard.board import ColumnData
from herdboard.store import CardView


def card(**kwargs):
    fields = dict(ref="t123", board="api", path=None, column="Todo", title="Ship it",
                  checked=False, due=None, tags=[], body=[])
    fields.update(kwargs)
    return CardView(**fields)


def rendered(widget: board.CardWidget, width: int = 40) -> list[str]:
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(widget.content)
    return output.getvalue().splitlines()


def first_row(widget: board.CardWidget, width: int = 40) -> list[tuple[str, str]]:
    """The first row as (text, style) pairs, so a test can read which color each part has."""
    console = Console(width=width, color_system=None, file=StringIO())
    row = next(iter(widget.content.renderables))
    return [(segment.text, str(segment.style)) for line in console.render_lines(row, console.options)
            for segment in line]


class CardFaceTests(unittest.TestCase):
    def test_the_state_icon_shows_not_started_started_and_done(self):
        cases = ((card(), False, "○"), (card(worktree=True), False, "●"), (card(agent_review="pi"), False, "●"),
                 (card(pr_writing=True), False, "●"), (card(planning="claude"), False, "●"),
                 (card(worktree=True), True, "✓"))
        for item, done, icon in cases:
            with self.subTest(item=item, done=done):
                text = board.card_text(item, False, done=done)
                first, second = text.plain.splitlines()[:2]
                self.assertTrue(first.startswith(icon), text.plain)
                self.assertEqual(second, "Ship it")
                widget = board.CardWidget(item, False, done=done)
                self.assertTrue(widget.plain.startswith("Ship it"), widget.plain)

    def test_done_icons_are_green_and_started_icons_yellow(self):
        for item, done, style in ((card(), False, ""), (card(worktree=True), False, "yellow"),
                                  (card(), True, "green")):
            with self.subTest(done=done, style=style):
                self.assertEqual(str(board.card_face(item, False, done=done).icon.style), style)

    def test_done_cards_strike_their_title_and_description(self):
        text = board.card_text(card(body=["Initial release"]), False, done=True)
        struck = [text.plain[span.start:span.end] for span in text.spans if "strike" in str(span.style)]
        self.assertEqual(struck, ["Ship it", "Initial release"])
        self.assertTrue(board.CardWidget(card(), False, done=True).has_class("done"))

    def test_the_description_is_the_first_detail_line_that_is_not_a_checklist_item(self):
        item = card(body=["- [ ] write notes", "", "  - Speed up the search", "More text"])
        self.assertEqual(board.CardWidget(item, False).plain, "Ship it  t123\nSpeed up the search")

    def test_the_description_takes_at_most_two_rows(self):
        item = card(body=["word " * 40])
        lines = rendered(board.CardWidget(item, False), width=24)
        self.assertEqual(len(lines), 4, lines)
        self.assertTrue(lines[3].rstrip().endswith("…"), lines)

    def test_compact_cards_have_no_description(self):
        item = card(body=["Speed up the search"])
        widget = board.CardWidget(item, False, card_style="compact")
        self.assertEqual(widget.plain, "Ship it  t123")
        self.assertTrue(widget.has_class("compact"))

    def test_the_sub_card_count_shows_only_for_a_parent(self):
        self.assertIn("2/5 done", board.CardWidget(card(sub_cards=(2, 5)), False).plain)
        self.assertNotIn("done", board.CardWidget(card(), False).plain)
        nerd = board.CardWidget(card(sub_cards=(2, 5)), False, icon_style="nerd").plain
        self.assertIn(f"{board.ICONS['sub_cards']} 2/5 done", nerd)

    def test_the_footer_has_the_count_at_the_left_and_the_due_date_at_the_right(self):
        item = card(title="Ship it 📅 2026-11-01", sub_cards=(1, 2))
        footer = rendered(board.CardWidget(item, False), width=30)[-1]
        self.assertTrue(footer.startswith("1/2 done"), footer)
        self.assertTrue(footer.rstrip().endswith("Due Nov 1"), footer)

    def test_the_status_row_shows_only_when_there_is_status(self):
        self.assertEqual(board.CardWidget(card(tags=["api"], title="Ship it #api"), False).plain,
                         "Ship it  t123\n api ")
        widget = board.CardWidget(card(pr_state="open", parent_title="Billing"), False)
        self.assertEqual(widget.plain.splitlines()[-1], "PR open · ↳ Billing")

    def test_linear_cards_show_their_identifier_at_the_right(self):
        item = card(source="linear", ref="ENG-12")
        self.assertEqual(board.CardWidget(item, False).plain, f"Ship it  {board.LINEAR_MARK} ENG-12")

    def test_the_first_row_has_only_the_icon_and_the_corner(self):
        lines = rendered(board.CardWidget(card(), False), width=20)
        self.assertEqual([line.rstrip() for line in lines], ["○               t123", "Ship it"])
        item = card(title="Improve Auto tab titles", ref="tveq", has_attachments=True)
        lines = rendered(board.CardWidget(item, False), width=20)
        self.assertEqual([line.rstrip() for line in lines],
                         ["○         files tveq", "Improve Auto tab", "titles"])
        self.assertEqual(rendered(board.CardWidget(card(ref="api:3"), False), width=20)[0].rstrip(), "○")

    def test_cards_have_no_menu_marker(self):
        for style in ("full", "compact"):
            with self.subTest(style=style):
                self.assertNotIn("⋯", "\n".join(rendered(board.CardWidget(card(), False, card_style=style))))

    def test_nerd_cards_show_the_attachment_icon_without_its_name(self):
        item = card(has_attachments=True)
        nerd = rendered(board.CardWidget(item, False, icon_style="nerd"), width=20)[0]
        self.assertTrue(nerd.endswith(f"{board.ICONS['attachment']} t123"), nerd)
        self.assertNotIn(board.ATTACHMENT_MARK, nerd)
        plain = rendered(board.CardWidget(item, False), width=20)[0]
        self.assertTrue(plain.endswith(f"{board.ATTACHMENT_MARK} t123"), plain)

    def test_compact_cards_keep_the_title_on_the_first_row(self):
        lines = rendered(board.CardWidget(card(), False, card_style="compact"), width=20)
        self.assertEqual(lines, ["○ Ship it       t123"])

    def test_the_id_always_has_a_space_before_it(self):
        item = card(title="Archive Cards", ref="t323")
        for width in range(16, 26):
            with self.subTest(width=width):
                first = rendered(board.CardWidget(item, False), width=width)[0].rstrip()
                self.assertTrue(first.endswith(" t323"), first)

    def test_cards_without_a_block_id_show_no_id(self):
        for ref in ("api:3", "api:archive-2"):
            with self.subTest(ref=ref):
                self.assertEqual(board.CardWidget(card(ref=ref), False).plain, "Ship it")

    def test_chips_wrap_only_between_two_chips(self):
        item = card(title="Ship it #enhancement #low #documentation", type="feature")
        lines = rendered(board.CardWidget(item, False), width=24)
        self.assertEqual([line.rstrip() for line in lines[2:]], ["feature  enhancement", " low   documentation"])

    def test_tags_after_the_type_badge_do_not_take_its_bold(self):
        item = card(title="Ship it #api #low #documentation", type="feature")
        widget = board.CardWidget(item, False)
        output = Console(width=24, color_system=None, file=StringIO())
        row = next(r for r in widget.content.renderables if isinstance(r, board.ChipRow))
        for line in output.render_lines(row, output.options.update(width=24)):
            for segment in line:
                if segment.text.strip() and segment.text.strip() != "feature":
                    self.assertFalse(segment.style and segment.style.bold, segment)

    def test_outline_tags_are_text_in_their_color_on_a_tint(self):
        chip = board.tag_chip("api", "outline")
        color = board.chip_color("api")
        self.assertEqual(chip.plain, " api ")
        self.assertEqual(str(chip.style), f"{color} on {board.chip_tint(color)}")
        self.assertNotEqual(board.chip_tint(color, dark=True), board.chip_tint(color, dark=False))


class AgentLogoTests(unittest.TestCase):
    def test_the_agent_logo_shows_before_the_agent_name(self):
        item = card(agent_review="pi", planning="claude")
        self.assertIn("agent review: pi · planning: claude", board.CardWidget(item, False).plain)
        logos = board.CardWidget(item, False, agent_logos=True).plain
        self.assertIn(f"agent review: {board.AGENT_LOGOS['pi']} pi · planning: {board.AGENT_LOGOS['claude']} claude",
                      logos)
        self.assertIn("agent review: other", board.CardWidget(card(agent_review="other"), False,
                                                              agent_logos=True).plain)

    def test_the_started_agent_logo_follows_the_state_icon(self):
        item = card(worktree=True, agent="claude")
        lines = rendered(board.CardWidget(item, False, agent_logos=True), width=20)
        self.assertEqual(lines[0], f"● {board.AGENT_LOGOS['claude']}             t123")
        self.assertEqual(rendered(board.CardWidget(item, False), width=20)[0], "●               t123")
        compact = rendered(board.CardWidget(item, False, card_style="compact", agent_logos=True), width=20)[0]
        self.assertTrue(compact.startswith(f"● {board.AGENT_LOGOS['claude']} Ship it"), compact)
        self.assertEqual(board.CardWidget(item, False, agent_logos=True).plain, "Ship it  t123")

    def test_the_logos_need_the_herdr_agent_icons_font(self):
        def fc_match(output):
            return patch("herdboard.board.subprocess.run",
                         return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=""))
        board.agent_font_installed.cache_clear()
        self.addCleanup(board.agent_font_installed.cache_clear)
        with fc_match("Herdr Agent Icons Max\n") as run:
            self.assertTrue(board.agent_font_installed())
            self.assertEqual(run.call_args.args[0][:1], ["fc-match"])
        board.agent_font_installed.cache_clear()
        with fc_match("DejaVu Sans\n"):
            self.assertFalse(board.agent_font_installed())
        board.agent_font_installed.cache_clear()
        with patch("herdboard.board.subprocess.run", side_effect=OSError("no fc-match")):
            self.assertFalse(board.agent_font_installed())


HERDR_GLYPHS = (("working", "◐", "yellow"), ("blocked", "×", "red"), ("done", "✓", "cyan"),
                ("idle", "○", "green"), ("unknown", "·", "dim"))
PHASES = {"planning": ("✎", "\uf040"), "planned": ("▤", "\uf0f6"), "reviewing": ("⌕", "\uf002"),
          "reviewed": ("✔", "\uf058"), "new commits": ("↑", "\uf062")}


def status_row(item, done=False, **options):
    """The bottom status row of a card: the markers that do not follow the state icon."""
    return board.card_face(item, False, done=done, **options).status.plain


def mark_row(item, done=False, **options):
    """The planning and review markers, which follow the state icon in the first row."""
    return " · ".join(mark.plain for mark in board.card_face(item, False, done=done, **options).marks)


class AgentGlyphTests(unittest.TestCase):
    def test_the_work_agent_glyph_replaces_the_state_icon(self):
        for status, glyph, style in HERDR_GLYPHS:
            with self.subTest(status=status):
                item = card(worktree=True, agent="claude", agent_status=status)
                icon = board.card_face(item, False).icon
                self.assertEqual(icon.plain, glyph)
                self.assertEqual(str(icon.style), style)
                logos = board.card_face(item, False, agent_logos=True).icon
                self.assertEqual(logos.plain, f"{glyph} {board.AGENT_LOGOS['claude']}")

    def test_the_glyphs_are_the_same_for_both_icon_styles(self):
        item = card(worktree=True, agent_status="working")
        self.assertEqual(board.card_face(item, False, icon_style="nerd").icon.plain, "◐")

    def test_a_worktree_without_a_live_agent_keeps_the_started_icon(self):
        icon = board.card_face(card(worktree=True, agent="claude"), False).icon
        self.assertEqual((icon.plain, str(icon.style)), ("●", "yellow"))

    def test_the_completed_column_shows_done_with_a_live_agent(self):
        icon = board.card_face(card(worktree=True, agent_status="working"), False, done=True).icon
        self.assertEqual((icon.plain, str(icon.style)), ("✓", "green"))

    def test_the_phase_icons_show_without_words(self):
        for style, index in (("plain", 0), ("nerd", 1)):
            with self.subTest(style=style):
                icon = {key: glyphs[index] for key, glyphs in PHASES.items()}
                options = dict(icon_style=style)
                self.assertEqual(mark_row(card(planning="claude", planning_status="working"), **options),
                                 f"{icon['planning']} ◐ claude")
                self.assertEqual(mark_row(card(planning="claude"), **options), f"{icon['planning']} claude")
                self.assertEqual(mark_row(card(planned=True), **options), icon["planned"])
                self.assertEqual(mark_row(card(agent_review="pi", review_status="blocked"), **options),
                                 f"{icon['reviewing']} × pi")
                self.assertEqual(mark_row(card(reviewed="reviewed"), **options), icon["reviewed"])
                self.assertEqual(mark_row(card(reviewed="new commits"), **options), icon["new commits"])

    def test_the_state_row_keeps_the_icon_color_off_the_marks_and_the_id(self):
        item = card(worktree=True, planning="claude", planning_status="working", reviewed="reviewed")
        cases = ((False, "yellow", "●"), (True, "green", "✓"))
        for done, color, icon in cases:
            with self.subTest(done=done):
                segments = first_row(board.CardWidget(item, False, done=done))
                self.assertEqual(segments[0], (icon, color))
                # The gap and the separators keep their own style, not the icon's color.
                gaps = [style for text, style in segments if text.strip(" ·") == ""]
                self.assertEqual(set(gaps), {"none", "dim"})
                # The ID is plain dim, as it was before the marks moved up; the agent glyph keeps its state color.
                self.assertEqual(segments[-1], ("t123", "dim"))
                self.assertIn(("◐", "bold dim yellow"), segments)

    def test_the_phase_markers_follow_the_state_icon_and_leave_the_status_row(self):
        item = card(planning="claude", planning_status="working", reviewed="reviewed", pr_state="open",
                    parent_title="Billing")
        lines = rendered(board.CardWidget(item, False), width=40)
        self.assertEqual([line.rstrip() for line in lines],
                         ["●  ✔ · ✎ ◐ claude".ljust(36) + "t123", "Ship it", "PR open · ↳ Billing"])
        face = board.card_face(item, False)
        self.assertEqual(face.icon.plain, "●")
        self.assertEqual(face.status.plain, "PR open · ↳ Billing")
        self.assertIn("✔", board.card_text(item, False).plain.splitlines()[0])

    def test_a_narrow_card_cuts_the_phase_markers_and_keeps_the_id_whole(self):
        item = card(planning="claude", planning_status="working", agent_review="pi", review_status="blocked")
        for width in range(16, 40):
            with self.subTest(width=width):
                first = rendered(board.CardWidget(item, False), width=width)[0].rstrip()
                self.assertTrue(first.endswith(" t123"), first)
                self.assertLessEqual(len(first), width, first)
                self.assertNotIn("· …", first)
        self.assertTrue(rendered(board.CardWidget(item, False), width=24)[0].rstrip().endswith("… t123"))

    def test_a_narrow_card_without_marks_keeps_its_state_icon(self):
        first = rendered(board.CardWidget(card(), False), width=20)[0].rstrip()
        self.assertEqual(first, "○               t123")
        item = card(planning="claude")
        first = rendered(board.CardWidget(item, False), width=12)[0].rstrip()
        self.assertTrue(first.startswith("●  "), first)
        self.assertTrue(first.endswith("… t123"), first)

    def test_compact_cards_show_the_phase_markers_after_the_title(self):
        item = card(planned=True, reviewed="reviewed")
        self.assertEqual(rendered(board.CardWidget(item, False, card_style="compact"), width=40),
                         ["○ Ship it  ✔ · ▤                    t123"])
        self.assertEqual(board.CardWidget(item, False, card_style="compact").plain, "Ship it  t123\nreviewed · planned")

    def test_the_agent_logo_replaces_the_agent_name_in_a_phase(self):
        item = card(planning="claude", planning_status="idle", agent_review="pi", review_status="working")
        self.assertEqual(mark_row(item, agent_logos=True),
                         f"⌕ ◐ {board.AGENT_LOGOS['pi']} · ✎ ○ {board.AGENT_LOGOS['claude']}")
        self.assertEqual(mark_row(card(agent_review="other"), agent_logos=True), "⌕ other")

    def test_each_agent_glyph_takes_its_herdr_color(self):
        face = board.card_face(card(planning="claude", planning_status="blocked"), False)
        mark = face.marks[0]
        glyph = next(span for span in mark.spans if mark.plain[span.start:span.end] == "×")
        self.assertEqual(str(glyph.style), "red")

    def test_the_pr_writer_glyph_follows_pr_writing(self):
        self.assertEqual(status_row(card(pr_writing=True, pr_write_status="working")), "PR: writing ◐")
        self.assertEqual(status_row(card(pr_writing=True)), "PR: writing")

    def test_planned_hides_while_a_planner_runs_and_in_the_completed_column(self):
        self.assertNotIn("▤", mark_row(card(planned=True, planning="claude")))
        self.assertNotIn("▤", mark_row(card(planned=True), done=True))

    def test_the_plain_text_has_words_and_no_glyphs(self):
        item = card(worktree=True, agent_status="working", planned=True, agent_review="pi", review_status="blocked",
                    pr_writing=True, pr_write_status="done")
        plain = board.CardWidget(item, False).plain
        self.assertTrue(plain.startswith("Ship it"), plain)
        self.assertEqual(plain.splitlines()[-1], "agent review: pi · PR: writing · planned")
        self.assertEqual(board.CardWidget(card(reviewed="new commits"), False).plain.splitlines()[-1],
                         "new commits since review")
        for glyph in ("◐", "×", "✓", "▤", "⌕", "↑"):
            self.assertNotIn(glyph, plain)
        self.assertEqual(board.CardWidget(card(planning="claude", planning_status="working"), False).plain,
                         "Ship it  t123\nplanning: claude")


    def test_an_automation_card_shows_the_automation_mark(self):
        item = card(title="Automation: Ship it")
        self.assertEqual(mark_row(item), "↻")
        self.assertEqual(mark_row(item, icon_style="nerd"), "\uf01e")
        self.assertEqual(mark_row(card(planned=True, title="Automation: Ship it")), "↻ · ▤")
        self.assertEqual(mark_row(card()), "")
        self.assertEqual(mark_row(card(title="Automations are fun")), "")
        plain = board.CardWidget(item, False).plain
        self.assertTrue(plain.startswith("Automation: Ship it"), plain)
        self.assertEqual(plain.splitlines()[-1], "automation")

    def test_the_detail_screen_names_each_state_in_words(self):
        item = card(worktree=True, agent="claude", agent_status="working", planning="pi", planning_status="idle",
                    agent_review="pi", review_status="blocked", planned=True)
        meta = board.DetailScreen(item, None)._meta().plain.splitlines()
        self.assertIn("Agent        ◐ claude: working", meta)
        self.assertIn("Planner      ✎ ○ pi: idle", meta)
        self.assertIn("Agent review ⌕ × pi: blocked", meta)
        self.assertIn("Plan         ▤ planned", meta)
        for reviewed, words in (("reviewed", "✔ reviewed"), ("new commits", "↑ new commits since review")):
            meta = board.DetailScreen(card(reviewed=reviewed), None)._meta().plain.splitlines()
            self.assertIn(f"Agent review {words}", meta)
        nerd = board.DetailScreen(card(planned=True), None, icon_style="nerd")._meta().plain
        self.assertIn("Plan         \uf0f6 planned", nerd)

    def test_the_detail_screen_names_an_agent_without_a_recorded_kind(self):
        item = card(worktree=True, agent_status="blocked")
        self.assertIn("Agent        × agent: blocked", board.DetailScreen(item, None)._meta().plain.splitlines())

    def test_the_help_screen_explains_each_glyph(self):
        icons = dict(next(entries for title, entries in board.HELP_SECTIONS if title == "Icons"))
        for glyph in ("◐", "×", "✓", "○", "·", "●", "✎", "▤", "⌕", "✔", "↑", "↻"):
            self.assertIn(glyph, icons)


class ColumnColorTests(unittest.TestCase):
    def test_columns_take_the_palette_colors_in_turn(self):
        names = [board.column_class(index, ColumnData(f"c{index}")) for index in range(7)]
        self.assertEqual(names, ["col-0", "col-1", "col-2", "col-3", "col-4", "col-0", "col-1"])

    def test_the_completed_column_is_green_and_the_archive_muted(self):
        self.assertEqual(board.column_class(2, ColumnData("Done", completed=True)), "col-done")
        self.assertEqual(board.column_class(5, ColumnData("Archive", archive=True)), "col-archive")

    def test_the_column_header_has_the_icon_name_and_count(self):
        output = StringIO()
        header = board.ColumnHeader(ColumnData("Todo", limit=5, cards=[card()]))
        Console(file=output, width=20, color_system=None).print(header.content)
        self.assertEqual(output.getvalue().rstrip("\n"), "○ Todo           1/5")
        self.assertEqual(header.plain, "Todo 1/5")
        done = board.ColumnHeader(ColumnData("Done", completed=True))
        started = board.ColumnHeader(ColumnData("In progress"), "In progress")
        for widget, icon in ((done, "✓"), (started, "●")):
            output = StringIO()
            Console(file=output, width=20, color_system=None).print(widget.content)
            self.assertTrue(output.getvalue().startswith(icon), output.getvalue())


if __name__ == "__main__":
    unittest.main()
