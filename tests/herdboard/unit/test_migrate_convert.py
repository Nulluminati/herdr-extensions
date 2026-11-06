import unittest

from herdboard.migrate_convert import convert, slug
from support.paths import ROOT

FIXTURES = ROOT / "tests/herdboard/fixtures/migrate"


def load(name):
    return convert((FIXTURES / name).read_text(encoding="utf-8"))


def lines(item):
    return item.card_lines()


class SlugTests(unittest.TestCase):
    def test_slug_rules(self):
        self.assertEqual(slug("Follow Ups"), "follow-ups")
        self.assertEqual(slug("Pantry & Misc."), "pantry-misc")
        self.assertEqual(slug("  Skills / Prompts "), "skills-prompts")
        self.assertEqual(slug("CI/CD (2026)"), "ci-cd-2026")
        self.assertEqual(slug("???"), None)


class HeadingStyleTests(unittest.TestCase):
    def test_atx_headings(self):
        result = load("atx.md")
        self.assertEqual([s.name for s in result.sections], ["Garden", "Kitchen Ideas", "Pantry & Misc."])
        self.assertEqual([s.tag for s in result.sections], ["garden", "kitchen-ideas", "pantry-misc"])
        self.assertEqual(
            [i.title for i in result.sections[1].items], ["label the jars", "sharpen knives", "clean the oven"]
        )

    def test_atx_closing_hashes_and_levels(self):
        result = convert("## Deep ##\n- a\n###### Six\n- b\n")
        self.assertEqual([s.name for s in result.sections], ["Deep", "Six"])

    def test_setext_with_colon_and_underlines(self):
        result = load("setext-colon.md")
        self.assertEqual([s.name for s in result.sections], ["Garden Tasks", "Shed Cleanup"])
        self.assertEqual([i.title for i in result.items], ["Mow the lawn", "Sort tools", "Oil the hinges"])
        self.assertEqual(result.non_list, [])
        for item in result.items:
            self.assertNotIn("=", item.title + "".join(item.body))
            self.assertNotIn("---", item.title + "".join(item.body))

    def test_setext_without_colon(self):
        result = convert("Plain Heading\n=====\n- a\n")
        self.assertEqual(result.sections[0].name, "Plain Heading")

    def test_plain_colon_heading(self):
        result = load("colon-tabs.md")
        self.assertEqual([s.name for s in result.sections], ["Errands", "Follow Ups"])
        self.assertEqual([i.tag for i in result.sections[1].items], ["follow-ups"] * 2)

    def test_colon_line_without_list_is_text(self):
        result = convert("- a\nSee below:\n\nNothing here\n")
        self.assertEqual([s.name for s in result.sections], [None])
        self.assertEqual(result.items[0].body, ["\tSee below:", "\tNothing here"])


class BulletTests(unittest.TestCase):
    def test_every_bullet_and_checkbox_style(self):
        items = load("bullets.md").items
        self.assertEqual(
            [(i.title, i.checked) for i in items],
            [
                ("open box item", False),
                ("checked box item", True),
                ("upper checked item", True),
                ("star item", False),
                ("plus item", False),
                ("numbered item", False),
                ("paren numbered item", False),
                ("plain item", False),
            ],
        )
        self.assertEqual(lines(items[0]), ["- [ ] open box item"])
        self.assertEqual(lines(items[1]), ["- [x] checked box item"])
        self.assertEqual(lines(items[2]), ["- [x] upper checked item"])
        self.assertEqual(lines(items[5]), ["- [ ] numbered item"])

    def test_nested_items_keep_checkboxes(self):
        item = load("bullets.md").items[-1]
        self.assertEqual(item.body, ["\t- [x] nested checked", "\t- nested star", "\t- nested numbered"])

    def test_tag_goes_after_title(self):
        item = load("atx.md").items[0]
        self.assertEqual(lines(item), ["- [ ] water the ferns #garden"])


class IndentTests(unittest.TestCase):
    def test_tabs_and_depth(self):
        item = load("atx.md").sections[1].items[0]
        self.assertEqual(item.body, ["\t- glass ones", "\t\t- small sizes", "\t- tin ones"])

    def test_eight_space_unit(self):
        result = load("setext-colon.md")
        self.assertEqual(result.indent_unit, 8)
        self.assertEqual(result.items[0].body, ["\t- front", "\t- back", "\t\t- corners"])

    def test_tab_only_has_no_space_unit(self):
        self.assertIsNone(load("colon-tabs.md").indent_unit)

    def test_two_four_eight_and_tab_in_one_file(self):
        result = load("indents.md")
        self.assertEqual(result.indent_unit, 2)
        by_section = {s.name: s.items for s in result.sections}
        self.assertEqual(by_section["Two Spaces"][0].body, ["\t- b"])
        self.assertEqual(by_section["Four Spaces"][0].body, ["\t- e"])
        self.assertEqual([i.title for i in by_section["Four Spaces"]], ["d", "f"])
        self.assertEqual(by_section["Mixed Units"][0].body, ["\t- h"])
        self.assertEqual(by_section["Mixed Units"][1].body, ["\t- j"])

    def test_four_space_unit_alone(self):
        result = convert("T:\n- a\n    - b\n        - c\n    - d\n- e\n")
        self.assertEqual(result.indent_unit, 4)
        self.assertEqual(result.items[0].body, ["\t- b", "\t\t- c", "\t- d"])

    def test_uniformly_indented_file_has_top_level_cards(self):
        result = convert("    - a\n    - b\n        - c\n")
        self.assertEqual([i.title for i in result.items], ["a", "b"])
        self.assertEqual(result.items[1].body, ["\t- c"])

    def test_jump_in_depth_is_clamped(self):
        result = convert("- a\n\t\t\t- deep\n")
        self.assertEqual(result.items[0].body, ["\t- deep"])


class SectionTests(unittest.TestCase):
    def test_items_before_first_heading_have_no_tag(self):
        result = load("indents.md")
        self.assertIsNone(result.sections[0].name)
        self.assertIsNone(result.sections[0].items[0].tag)
        self.assertEqual(lines(result.sections[0].items[0]), ["- [ ] Loose first item", "\t- under it"])

    def test_heading_with_unsluggable_name_gives_no_tag(self):
        result = convert("# ???\n- a\n")
        self.assertEqual(lines(result.items[0]), ["- [ ] a"])

    def test_empty_section_is_kept_out(self):
        result = convert("# Empty\n# Full\n- a\n")
        self.assertEqual([s.name for s in result.sections], ["Full"])

    def test_blank_lines_and_underlines_dropped(self):
        result = load("atx.md")
        for item in result.items:
            for text in item.card_lines():
                self.assertTrue(text.strip())


class NonListTests(unittest.TestCase):
    def setUp(self):
        self.result = load("loose-text.md")

    def test_standalone_and_attached(self):
        items = {i.title: i for i in self.result.items}
        self.assertIn("Intro line before any list", items)
        self.assertEqual(items["first item"].body, ["\tA note about the first item", "\tan indented note"])
        self.assertIn("Standalone text in notes", items)
        self.assertEqual(
            [(n.text, n.action) for n in self.result.non_list],
            [
                ("Intro line before any list", "card"),
                ("A note about the first item", "attached"),
                ("an indented note", "attached"),
                ("Standalone text in notes", "card"),
            ],
        )

    def test_colon_heading_wins_over_text(self):
        names = [s.name for s in self.result.sections]
        self.assertEqual(names, [None, "Later", "Notes"])
        later = self.result.sections[1].items
        self.assertEqual([i.title for i in later], ["later item"])

    def test_line_numbers_and_sections(self):
        first = self.result.non_list[0]
        self.assertEqual((first.number, first.section), (1, None))
        self.assertEqual(self.result.non_list[-1].section, "Notes")

    def test_stray_rule_line_dropped(self):
        self.assertNotIn("----", "".join(line for item in self.result.items for line in item.card_lines()))

    def test_crlf_input(self):
        result = convert("# A\r\n- x\r\n\t- y\r\n")
        self.assertEqual(lines(result.items[0]), ["- [ ] x #a", "\t- y"])


class IndentAmbiguityTests(unittest.TestCase):
    def test_isolated_one_space_does_not_set_the_unit(self):
        result = convert("- root\n\t- tab sibling\n    - space sibling\n - stray\n")
        self.assertEqual(result.indent_unit, 4)
        self.assertEqual([i.title for i in result.items], ["root"])
        self.assertEqual(result.items[0].body, ["\t- tab sibling", "\t- space sibling", "\t- stray"])
        self.assertEqual(
            [(n.number, n.text, n.action, n.parent) for n in result.non_list], [(4, "stray", "ambiguous", "root")]
        )

    def test_mixed_prefix_expands_in_order_with_tab_stops(self):
        result = convert("- root\n    - a\n \t- b\n\t- c\n")
        self.assertEqual(result.indent_unit, 4)
        self.assertEqual(result.items[0].body, ["\t- a", "\t- b", "\t- c"])
        self.assertEqual(result.non_list, [])

    def test_mixed_prefix_deeper_than_its_parent(self):
        result = convert("- root\n    - a\n    \t- b\n")
        self.assertEqual(result.items[0].body, ["\t- a", "\t\t- b"])

    def test_tab_unit_with_space_is_ambiguous(self):
        result = convert("- root\n\t- a\n \t- b\n")
        self.assertIsNone(result.indent_unit)
        self.assertEqual(result.items[0].body, ["\t- a", "\t- b"])
        self.assertEqual([(n.number, n.action, n.parent) for n in result.non_list], [(3, "ambiguous", "root")])

    def test_stray_space_in_a_tab_file_reports_its_parent(self):
        result = convert("T:\n- a\n\t- b\n- url\n - stray\n")
        self.assertIsNone(result.indent_unit)
        self.assertEqual([i.title for i in result.items], ["a", "url"])
        self.assertEqual(result.items[1].body, ["\t- stray"])
        self.assertEqual(
            [(n.number, n.text, n.action, n.section, n.parent) for n in result.non_list],
            [(5, "stray", "ambiguous", "T", "url")],
        )

    def test_stray_space_as_first_card_has_no_parent(self):
        result = convert("\t- x\n - y\n")
        self.assertEqual([n.parent for n in result.non_list], [None])

    def test_one_space_steps_stay_a_unit_when_nothing_larger(self):
        result = convert("- a\n - b\n  - c\n   - d\n")
        self.assertEqual(result.indent_unit, 1)
        self.assertEqual(result.items[0].body, ["\t- b", "\t\t- c", "\t\t\t- d"])
        self.assertEqual(result.non_list, [])

    def test_two_space_steps_ignore_one_stray_space(self):
        result = convert("- a\n  - b\n    - c\n - d\n")
        self.assertEqual(result.indent_unit, 2)
        self.assertEqual(result.items[0].body, ["\t- b", "\t\t- c", "\t- d"])
        self.assertEqual([n.number for n in result.non_list], [4])

    def test_clean_indent_is_not_ambiguous(self):
        self.assertEqual(convert("- a\n    - b\n        - c\n").non_list, [])


class HeadingIndentTests(unittest.TestCase):
    def test_up_to_three_spaces_make_a_heading(self):
        for prefix in ("", " ", "   "):
            result = convert(f"{prefix}## Testing\n- check configuration\n")
            self.assertEqual([s.name for s in result.sections], ["Testing"])
            self.assertEqual([(i.title, i.tag) for i in result.items], [("check configuration", "testing")])
            self.assertEqual(result.non_list, [])

    def test_four_spaces_or_tab_are_text(self):
        for prefix in ("    ", "\t"):
            result = convert(f"{prefix}## Testing\n- check configuration\n")
            self.assertEqual([s.name for s in result.sections], [None])
            self.assertEqual(
                [(i.title, i.tag) for i in result.items], [("## Testing", None), ("check configuration", None)]
            )
            self.assertEqual([(n.number, n.action) for n in result.non_list], [(1, "card")])

    def test_underlined_heading_text_allows_three_spaces(self):
        result = convert("   Garden\n=====\n- water\n")
        self.assertEqual([s.name for s in result.sections], ["Garden"])
        self.assertEqual([(i.title, i.tag) for i in result.items], [("water", "garden")])
        self.assertEqual(result.non_list, [])

    def test_underlined_heading_text_with_four_spaces_or_tab_is_text(self):
        for prefix in ("    ", "\t"):
            result = convert(f"{prefix}Garden\n=====\n- water\n")
            self.assertEqual([s.name for s in result.sections], [None])
            self.assertEqual([(i.title, i.tag) for i in result.items], [("Garden", None), ("water", None)])
            self.assertEqual([(n.number, n.action) for n in result.non_list], [(1, "card")])


class FenceTests(unittest.TestCase):
    def test_fence_lines_stay_on_one_card(self):
        result = convert("- script task\n```sh\n# Explain shell command\n- command argument\n```\n- next\n")
        self.assertEqual([s.name for s in result.sections], [None])
        self.assertEqual([i.title for i in result.items], ["script task", "next"])
        self.assertEqual(
            result.items[0].body, ["\t```sh", "\t# Explain shell command", "\t- command argument", "\t```"]
        )
        self.assertEqual(result.items[1].body, [])

    def test_fence_in_section_does_not_change_tags(self):
        result = convert("# S\n- a\n~~~\n## not a heading\n~~~\n- b\n")
        self.assertEqual([s.name for s in result.sections], ["S"])
        self.assertEqual([(i.title, i.tag) for i in result.items], [("a", "s"), ("b", "s")])
        self.assertEqual(result.items[0].body, ["\t~~~", "\t## not a heading", "\t~~~"])

    def test_fence_without_a_card_is_skipped(self):
        result = convert("```\n- item\n\n# h\n```\n- after\n")
        self.assertEqual([i.title for i in result.items], ["after"])
        self.assertEqual(result.items[0].body, [])
        self.assertEqual(
            [(n.number, n.text, n.action) for n in result.non_list],
            [(1, "```", "skipped"), (2, "- item", "skipped"), (4, "# h", "skipped"), (5, "```", "skipped")],
        )

    def test_install_snippet_before_the_first_card_is_skipped(self):
        result = convert("```bash\nuv pip install x\n```\n\n- [ ] real task\n")
        self.assertEqual([i.title for i in result.items], ["real task"])
        self.assertEqual([n.action for n in result.non_list], ["skipped"] * 3)

    def test_skipped_fence_in_a_section_does_not_end_the_section(self):
        result = convert("# S\n~~~\n# not a heading\n~~~\n- a\n")
        self.assertEqual([(i.title, i.tag) for i in result.items], [("a", "s")])

    def test_unclosed_fence_alone_gives_no_cards(self):
        result = convert("```python\n")
        self.assertEqual(result.items, [])
        self.assertEqual([(n.number, n.action) for n in result.non_list], [(1, "skipped")])

    def test_unclosed_fence_at_end_of_file_without_a_card(self):
        result = convert("```python\nprint(1)")
        self.assertEqual(result.items, [])

    def test_unclosed_fence_after_a_card_drops_trailing_blank_lines(self):
        result = convert("- a\n```\ncode\n\n")
        self.assertEqual(result.items[0].body, ["\t```", "\tcode"])

    def test_closing_fence_needs_same_character_and_length(self):
        result = convert("- a\n~~~~\n~~~\n```\n- x\n~~~~~\n- b\n")
        self.assertEqual([i.title for i in result.items], ["a", "b"])
        self.assertEqual(result.items[0].body, ["\t~~~~", "\t~~~", "\t```", "\t- x", "\t~~~~~"])

    def test_nested_item_fence_keeps_card_depth(self):
        result = convert("- a\n\t- b\n\t```\n\t# c\n\t  indented\n\t```\n")
        self.assertEqual([i.title for i in result.items], ["a"])
        self.assertEqual(result.items[0].body, ["\t- b", "\t```", "\t# c", "\t  indented", "\t```"])

    def test_unclosed_fence_runs_to_the_end(self):
        result = convert("- a\n```\n- b\n# h\n")
        self.assertEqual([i.title for i in result.items], ["a"])
        self.assertEqual(result.items[0].body, ["\t```", "\t- b", "\t# h"])

    def test_inline_triple_backticks_do_not_open_a_fence(self):
        result = convert("- a\n```code``` text\n- b\n")
        self.assertEqual([i.title for i in result.items], ["a", "b"])


class SourceIdTests(unittest.TestCase):
    def test_id_is_dropped_and_recorded(self):
        result = convert("- imported ^tabc\n")
        item = result.items[0]
        self.assertEqual((item.title, item.dropped_id), ("imported", "tabc"))
        self.assertEqual(item.card_lines(), ["- [ ] imported"])

    def test_id_before_a_section_tag(self):
        result = convert("# Shed\n- [x] oil hinges ^tabc\n")
        self.assertEqual(result.items[0].card_lines(), ["- [x] oil hinges #shed"])
        self.assertEqual(result.items[0].dropped_id, "tabc")

    def test_ids_outside_the_format_and_repeated_ids(self):
        result = convert("- a ^long-id-here\n- b ^tabc\n- c ^tabc\n- d ^tabc extra\n")
        self.assertEqual(
            [(i.title, i.dropped_id) for i in result.items],
            [("a", "long-id-here"), ("b", "tabc"), ("c", "tabc"), ("d ^tabc extra", None)],
        )

    def test_text_line_card_drops_id(self):
        result = convert("plain text ^tabc\n")
        self.assertEqual((result.items[0].title, result.items[0].dropped_id), ("plain text", "tabc"))


if __name__ == "__main__":
    unittest.main()
