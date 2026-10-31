import unittest
from types import SimpleNamespace

from herdboard.model import HerdboardError
from herdboard.promote_convert import SubItem, convert, promoted_identifier

RENOVATE = [
    "Budget is tight, ask for two quotes",
    "- [ ] Choose tiles",
    "- [x] Measure the room",
    "\t- [ ] Measure the ceiling",
    "\tCeiling is uneven",
    "- Not a checklist item",
    "- [ ] Book the plumber",
]


def card(title="Card", body=(), *, type=None, tags=(), due=None, promoted=None):
    return SimpleNamespace(title=title, body=list(body), type=type, tags=list(tags), due=due, promoted=promoted)


class TitleTests(unittest.TestCase):
    def test_title_due_date_and_tags_come_from_the_fields(self):
        found = convert(card("Renovate the bathroom", tags=["home", "Unknown-tag"], due="2026-11-02"))
        self.assertEqual(
            (found.title, found.due, found.tags), ("Renovate the bathroom", "2026-11-02", ["home", "Unknown-tag"])
        )

    def test_the_type_comes_before_the_tags(self):
        self.assertEqual(convert(card(type="bug", tags=["api"])).tags, ["bug", "api"])

    def test_card_without_extras(self):
        found = convert(card("Plain card"))
        self.assertEqual((found.title, found.due, found.tags), ("Plain card", None, []))
        self.assertEqual((found.subs, found.leftover), ([], []))

    def test_spaces_in_the_title_are_collapsed(self):
        self.assertEqual(convert(card("Fix  the   door")).title, "Fix the door")

    def test_a_card_without_text_has_no_title(self):
        with self.assertRaises(HerdboardError):
            convert(card("   "))


class SubItemTests(unittest.TestCase):
    def test_top_level_checklist_items_become_sub_issues_in_order_with_their_flag(self):
        found = convert(card(body=RENOVATE))
        self.assertEqual(
            found.subs,
            [SubItem("Choose tiles", False), SubItem("Measure the room", True), SubItem("Book the plumber", False)],
        )

    def test_any_mark_counts_as_checked(self):
        found = convert(card(body=["- [X] Upper", "- [ ] Open"]))
        self.assertEqual([s.checked for s in found.subs], [True, False])

    def test_an_empty_checklist_item_is_text(self):
        found = convert(card(body=["- [ ] "]))
        self.assertEqual(found.subs, [])
        self.assertEqual(found.leftover, ["- [ ]"])


class LeftoverTests(unittest.TestCase):
    def test_other_lines_stay_as_description_text(self):
        found = convert(card(body=RENOVATE))
        self.assertEqual(
            found.leftover,
            [
                "Budget is tight, ask for two quotes",
                "\t- [ ] Measure the ceiling",
                "\tCeiling is uneven",
                "- Not a checklist item",
            ],
        )

    def test_leading_and_trailing_blank_lines_are_dropped(self):
        found = convert(card(body=["", "Text", "", "More", ""]))
        self.assertEqual(found.leftover, ["Text", "", "More"])


class PromotedTests(unittest.TestCase):
    def test_the_promoted_field(self):
        self.assertEqual(promoted_identifier(card(promoted="ENG-123")), "ENG-123")
        self.assertIsNone(promoted_identifier(card()))


if __name__ == "__main__":
    unittest.main()
