import unittest

from herdboard import kanban_md
from herdboard.promote_convert import SubItem, convert, promoted_identifier
from support.paths import ROOT

FIXTURE = ROOT / "tests/herdboard/fixtures/kanban-promote.md"
FULL = ROOT / "tests/herdboard/fixtures/kanban-2.0.51-full.md"


def card_lines(path, block_id=None, text=None):
    board = kanban_md.parse(path.read_text(encoding="utf-8"))
    for _, card in board.all_cards():
        if (block_id and card.block_id == block_id) or (text and text in card.first_line):
            return list(card.lines)
    raise AssertionError("card not in fixture")


class TitleTests(unittest.TestCase):
    def test_title_drops_checkbox_tags_due_date_and_block_id(self):
        found = convert(card_lines(FIXTURE, "t5r1"))
        self.assertEqual(found.title, "Renovate the bathroom")

    def test_due_date_and_tags_are_read_from_the_first_line(self):
        found = convert(card_lines(FIXTURE, "t5r1"))
        self.assertEqual(found.due, "2026-11-02")
        self.assertEqual(found.tags, ["home", "Unknown-tag"])

    def test_card_without_due_date_or_tags(self):
        found = convert(["- [ ] Plain card ^t0aa"])
        self.assertEqual((found.title, found.due, found.tags), ("Plain card", None, []))
        self.assertEqual((found.subs, found.leftover), ([], []))

    def test_card_without_block_id(self):
        self.assertEqual(convert(["- [ ] No id #x"]).title, "No id")

    def test_spaces_left_by_removed_parts_are_collapsed(self):
        found = convert(["- [ ] Fix #a the 📅 2026-01-02 door #b ^t0aa"])
        self.assertEqual(found.title, "Fix the door")

    def test_note_links_stay_in_the_title_and_are_listed(self):
        found = convert(card_lines(FIXTURE, "t6n2"))
        self.assertEqual(found.title, "Read [[Tile notes]] and [[Missing note]]")
        self.assertEqual(found.links, ["Tile notes", "Missing note"])

    def test_link_with_alias_lists_the_note_name(self):
        self.assertEqual(convert(["- [ ] See [[Tile notes|tiles]]"]).links, ["Tile notes"])

    def test_a_card_without_text_has_no_title(self):
        from herdboard.model import HerdboardError

        with self.assertRaises(HerdboardError):
            convert(["- [ ] #only-a-tag 📅 2026-01-02 ^t0aa"])


class SubItemTests(unittest.TestCase):
    def test_checklist_items_become_sub_issues_in_order_with_their_flag(self):
        found = convert(card_lines(FIXTURE, "t5r1"))
        self.assertEqual(
            found.subs,
            [SubItem("Choose tiles", False), SubItem("Measure the room", True), SubItem("Book the plumber", False)],
        )

    def test_fixture_card_with_tab_indented_items(self):
        found = convert(card_lines(FULL, text="Plan the lighting"))
        self.assertEqual(found.subs, [SubItem("Nested item one", False), SubItem("Nested item two", True)])
        self.assertEqual(found.leftover, ["Second line of the same card"])

    def test_four_space_indentation_counts_like_a_tab(self):
        lines = [line.replace("\t", "    ") for line in card_lines(FIXTURE, "t5r1")]
        found = convert(lines)
        self.assertEqual([s.title for s in found.subs], ["Choose tiles", "Measure the room", "Book the plumber"])
        self.assertEqual(found.leftover[0], "Budget is tight, ask for two quotes")

    def test_checked_flag_follows_the_card_rule(self):
        found = convert(["- [ ] Card", "\t- [X] Upper", "\t- [ ] Open"])
        self.assertEqual([s.checked for s in found.subs], [True, False])

    def test_an_empty_checklist_item_is_text(self):
        found = convert(["- [ ] Card", "\t- [ ] "])
        self.assertEqual(found.subs, [])
        self.assertEqual(found.leftover, ["- [ ]"])


class LeftoverTests(unittest.TestCase):
    def test_other_lines_stay_as_description_text(self):
        found = convert(card_lines(FIXTURE, "t5r1"))
        self.assertEqual(
            found.leftover,
            [
                "Budget is tight, ask for two quotes",
                "\t- [ ] Measure the ceiling",
                "\tCeiling is uneven",
                "- Not a checklist item",
            ],
        )

    def test_deeper_items_keep_their_indentation_relative_to_the_description(self):
        found = convert(["- [ ] Card", "    - [ ] One", "        - [ ] Deeper"])
        self.assertEqual([s.title for s in found.subs], ["One"])
        self.assertEqual(found.leftover, ["    - [ ] Deeper"])

    def test_leading_and_trailing_blank_lines_are_dropped(self):
        found = convert(["- [ ] Card", "\t", "\tText", "\t", "\tMore", "\t"])
        self.assertEqual(found.leftover, ["Text", "", "More"])


class PromotedTests(unittest.TestCase):
    def test_marker_before_the_block_id(self):
        self.assertEqual(promoted_identifier("- [x] Done thing → ENG-123 ^t7k2"), "ENG-123")

    def test_marker_without_block_id(self):
        self.assertEqual(promoted_identifier("- [x] Done thing → OPS-7"), "OPS-7")

    def test_no_marker(self):
        self.assertIsNone(promoted_identifier("- [ ] Go → there ^t7k2"))
        self.assertIsNone(promoted_identifier("- [ ] Plain ^t7k2"))
        self.assertIsNone(promoted_identifier("- [ ] Look at → ENG-12x ^t7k2"))


if __name__ == "__main__":
    unittest.main()
