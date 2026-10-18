import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import kanban_md  # noqa: E402
from herdboard.board import ConfigScreen, HerdboardApp  # noqa: E402
from herdboard.config import load_config, save_display_config  # noqa: E402
from herdboard.linear_links import LabelLink, ProjectLink, with_link  # noqa: E402
from herdboard.model import FormatError, HerdboardError  # noqa: E402
from test_herdboard_board import BoardTestCase, settle  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_linear_cli import LinearCliCase  # noqa: E402

FRONT = "---\n\nkanban-plugin: board\nrepo: github.com/acme/api\n\n---\n\n## Todo\n"


class FrontmatterValueTests(unittest.TestCase):
    def test_adds_a_key_before_the_trailing_blank_line(self):
        text = kanban_md.set_frontmatter_value(FRONT, "merge", "pr")
        self.assertEqual(text, FRONT.replace("api\n", "api\nmerge: pr\n"))

    def test_replaces_a_key_in_place(self):
        before = FRONT.replace("board\n", "board\nmerge: local\n")
        text = kanban_md.set_frontmatter_value(before, "merge", "pr")
        self.assertEqual(text, FRONT.replace("board\n", "board\nmerge: pr\n"))

    def test_none_removes_the_key_and_only_the_key(self):
        before = FRONT.replace("board\n", "board\nmerge: local\n")
        self.assertEqual(kanban_md.set_frontmatter_value(before, "merge", None), FRONT)

    def test_none_without_the_key_changes_nothing(self):
        self.assertEqual(kanban_md.set_frontmatter_value(FRONT, "merge", None), FRONT)

    def test_crlf_is_kept(self):
        before = FRONT.replace("\n", "\r\n")
        text = kanban_md.set_frontmatter_value(before, "merge", "pr")
        self.assertEqual(text, FRONT.replace("api\n", "api\nmerge: pr\n").replace("\n", "\r\n"))

    def test_unclosed_frontmatter_is_a_format_error(self):
        with self.assertRaisesRegex(FormatError, "frontmatter must be closed"):
            kanban_md.set_frontmatter_value("---\nkanban-plugin: board\n## Todo\n", "merge", "pr")

    def test_a_nested_value_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge:\n  mode: pr\n")
        with self.assertRaisesRegex(FormatError, "'merge' has a nested value"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")

    def test_an_indented_comment_or_blank_line_after_the_key_stays(self):
        for extra in ("  # Merge policy", "  "):
            before = FRONT.replace("board\n", f"board\nmerge: local\n{extra}\n")
            text = kanban_md.set_frontmatter_value(before, "merge", "pr")
            self.assertEqual(text, FRONT.replace("board\n", f"board\nmerge: pr\n{extra}\n"))

    def test_a_nested_value_after_a_comment_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge:\n  # policy\n\n  mode: pr\n")
        with self.assertRaisesRegex(FormatError, "nested value"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")

    def test_a_duplicate_key_is_a_format_error(self):
        before = FRONT.replace("board\n", "board\nmerge: pr\nmerge: local\n")
        with self.assertRaisesRegex(FormatError, "duplicate key 'merge'"):
            kanban_md.set_frontmatter_value(before, "merge", "pr")


class WithLinkTests(unittest.TestCase):
    def test_a_new_project_goes_last(self):
        links = [ProjectLink("p1", "One")]
        self.assertEqual(with_link(links, ProjectLink("p2", "Two")),
                         [ProjectLink("p1", "One"), ProjectLink("p2", "Two")])

    def test_a_linked_project_is_replaced_in_its_place(self):
        links = [ProjectLink("p1", "One"), ProjectLink("p2", "Two")]
        changed = ProjectLink("p1", "One", [LabelLink("l1", "repo:api")])
        self.assertEqual(with_link(links, changed), [changed, ProjectLink("p2", "Two")])


class StoreMergeTests(BoardTestCase):
    def test_set_merge_writes_and_removes_the_key(self):
        self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
        self.store.set_merge(self.repo_path, None)
        self.assertIsNone(self.store.board_meta(self.repo_path).merge)
        self.assertNotIn("merge:", self.repo_path.read_text())

    def test_set_merge_refuses_an_unknown_mode(self):
        before = self.repo_path.read_text()
        with self.assertRaisesRegex(HerdboardError, "local or pr"):
            self.store.set_merge(self.repo_path, "squash")
        self.assertEqual(self.repo_path.read_text(), before)

    def test_merge_and_links_change_in_one_write(self):
        self.store.update_links(self.repo_path, lambda links: [ProjectLink("p1", "One")])
        self.store.update_config(self.repo_path, merge="pr",
                                 change_links=lambda links: links + [ProjectLink("p2", "Two")])
        meta = self.store.board_meta(self.repo_path)
        self.assertEqual((meta.merge, [link.id for link in meta.links]), ("pr", ["p1", "p2"]))

    def test_a_failed_link_change_leaves_the_merge_mode(self):
        text = self.repo_path.read_text().replace("repo:", "other: 1\nother: 2\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "duplicate key 'other'"):
            self.store.update_config(self.repo_path, merge="pr", change_links=lambda links: [])
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_nested_merge_value_is_not_written(self):
        text = self.repo_path.read_text().replace("repo:", "merge:\n  mode: pr\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "nested value"):
            self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_change_that_makes_the_frontmatter_invalid_is_not_written(self):
        text = self.repo_path.read_text()
        with mock.patch.object(kanban_md, "set_frontmatter_value",
                               side_effect=lambda text, key, value: text.replace("repo:", "bad: [\nrepo:")):
            with self.assertRaisesRegex(FormatError, "invalid YAML"):
                self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)

    def test_a_duplicate_merge_key_is_not_written(self):
        text = self.repo_path.read_text().replace("repo:", "merge: pr\nmerge: local\nrepo:")
        self.repo_path.write_text(text)
        with self.assertRaisesRegex(FormatError, "duplicate key 'merge'"):
            self.store.set_merge(self.repo_path, "pr")
        self.assertEqual(self.repo_path.read_text(), text)


class ConfigScreenTests(BoardTestCase):
    def make_app(self, *args, resolve_link=None, **kwargs):
        app = super().make_app(*args, **kwargs)
        app.resolve_link = resolve_link
        app.config_file = self.root / "config.toml"
        return app

    def link(self, link):
        self.store.update_links(self.repo_path, lambda links: links + [link])

    async def open_config(self, pilot):
        await pilot.press("c")
        await settle(pilot)
        self.assertIsInstance(pilot.app.screen, ConfigScreen)
        return pilot.app.screen

    async def test_c_shows_current_global_display_settings(self):
        self.config.icon_style = "nerd"
        self.config.tag_style = "block"
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            icons = screen.query_one("#config-icon-style")
            tags = screen.query_one("#config-tag-style")
            self.assertEqual(icons.value, "nerd")
            self.assertEqual(tags.value, "block")
            rendered = "\n".join(strip.text for strip in screen._compositor.render_strips())
            self.assertIn("Icons (all boards)", rendered)
            self.assertIn("Tags (all boards)", rendered)

    async def test_keyboard_navigation_can_change_both_display_styles(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            await pilot.press("tab", "enter", "down", "enter")
            self.assertEqual(screen.query_one("#config-icon-style").value, "nerd")
            await pilot.press("tab", "enter", "down", "down", "enter")
            self.assertEqual(screen.query_one("#config-tag-style").value, "block")
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        config = load_config(app.config_file)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))

    async def test_display_changes_persist_and_update_cards_and_details_immediately(self):
        self.store.add(self.repo_path, "Ship it #type/bug #api", "Backlog")
        original = self.repo_path.read_bytes()
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-icon-style").value = "nerd"
            screen.query_one("#config-tag-style").value = "block"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
            self.assertEqual((self.config.icon_style, self.config.tag_style), ("nerd", "block"))
            self.assertIn("\uf188 bug  api ", app.query_one(".card").plain)
            await pilot.press("enter")
            await settle(pilot)
            self.assertIn("\uf188 bug", app.screen.query_one("#detail-meta").plain)
            self.assertEqual(app.screen.query_one("#detail-chips").plain, " api ")
        config = load_config(app.config_file)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))
        self.assertEqual(self.repo_path.read_bytes(), original)

    async def test_display_saves_run_in_the_worker_and_apply_on_the_event_loop(self):
        app = self.make_app()
        loop_thread = threading.get_ident()
        writes, applied = [], []
        original_setattr = type(self.config).__setattr__

        def save(*args, **kwargs):
            writes.append(threading.get_ident())
            save_display_config(*args, **kwargs)

        def assign(config, key, value):
            if config is self.config and key in ("icon_style", "tag_style"):
                applied.append((key, value, threading.get_ident()))
            original_setattr(config, key, value)

        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-icon-style").value = "nerd"
            screen.query_one("#config-tag-style").value = "block"
            with mock.patch("herdboard.board.save_display_config", side_effect=save), \
                    mock.patch.object(type(self.config), "__setattr__", assign):
                await pilot.press("ctrl+s")
                await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertTrue(writes)
        self.assertTrue(all(thread != loop_thread for thread in writes))
        self.assertEqual(applied, [("icon_style", "nerd", loop_thread), ("tag_style", "block", loop_thread)])

    async def test_cancel_does_not_write_or_apply_display_settings(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-icon-style").value = "nerd"
            screen.query_one("#config-tag-style").value = "block"
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual((self.config.icon_style, self.config.tag_style), ("plain", "outline"))
            self.assertFalse(app.config_file.exists())

    async def test_unchanged_display_settings_do_not_create_a_config_file(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await self.open_config(pilot)
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertFalse(app.config_file.exists())

    async def test_display_write_failure_keeps_runtime_settings_and_shows_an_error(self):
        app = self.make_app()
        app.config_file.write_text("invalid = [")
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-icon-style").value = "nerd"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("invalid TOML", screen.query_one("#config-error").plain)
            self.assertEqual(self.config.icon_style, "plain")
        self.assertEqual(app.config_file.read_text(), "invalid = [")

    async def test_partial_save_error_identifies_the_saved_board_settings(self):
        app = self.make_app()
        app.config_file.write_text("invalid = [")
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-icon-style").value = "nerd"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("Board settings saved; display settings not saved", screen.query_one("#config-error").plain)
            self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
            self.assertEqual(self.config.icon_style, "plain")

    async def test_partial_save_retry_can_restore_the_original_merge_mode(self):
        for original in (None, "local"):
            with self.subTest(original=original):
                self.store.set_merge(self.repo_path, original)
                self.config.icon_style = "plain"
                app = self.make_app()
                app.config_file.write_text("invalid = [")
                async with app.run_test(size=(80, 24)) as pilot:
                    await settle(pilot)
                    screen = await self.open_config(pilot)
                    screen.query_one("#config-merge").value = "pr"
                    screen.query_one("#config-icon-style").value = "nerd"
                    await pilot.press("ctrl+s")
                    await settle(pilot)
                    self.assertIs(app.screen, screen)
                    self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
                    app.config_file.write_text("")
                    screen.query_one("#config-merge").value = original or "default"
                    await pilot.press("ctrl+s")
                    await settle(pilot)
                    self.assertNotIsInstance(app.screen, ConfigScreen)
                    self.assertEqual(self.store.board_meta(self.repo_path).merge, original)
                    self.assertEqual(load_config(app.config_file).icon_style, "nerd")

    async def test_unchanged_partial_save_retry_keeps_saved_merge_and_links(self):
        self.link(ProjectLink("p1", "Old project"))
        app = self.make_app(resolve_link=lambda name, labels: ProjectLink("p2", name))
        app.config_file.write_text("invalid = [")
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-icon-style").value = "nerd"
            screen.query_one("#config-links").focus()
            await pilot.press("enter")
            screen.query_one("#config-project").value = "Mobile"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertEqual(self.store.board_meta(self.repo_path).links, [ProjectLink("p2", "Mobile")])
            self.assertEqual(screen.meta.merge, "pr")
            app.config_file.write_text("")
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        meta = self.store.board_meta(self.repo_path)
        self.assertEqual(meta.merge, "pr")
        self.assertEqual(meta.links, [ProjectLink("p2", "Mobile")])
        self.assertEqual(load_config(app.config_file).icon_style, "nerd")

    async def test_display_save_uses_the_environment_selected_config_path(self):
        app = self.make_app()
        app.config_file = None
        path = self.root / "environment" / "settings.toml"
        default_root = self.root / "xdg"
        with mock.patch.dict(os.environ, {"HERDBOARD_CONFIG": str(path), "XDG_CONFIG_HOME": str(default_root)}):
            async with app.run_test(size=(80, 24)) as pilot:
                await settle(pilot)
                screen = await self.open_config(pilot)
                screen.query_one("#config-icon-style").value = "nerd"
                screen.query_one("#config-tag-style").value = "block"
                await pilot.press("ctrl+s")
                await settle(pilot)
                self.assertNotIsInstance(app.screen, ConfigScreen)
        config = load_config(path)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))
        self.assertFalse((default_root / "herdboard/config.toml").exists())
        self.assertFalse((self.root / "config.toml").exists())

    async def test_menu_can_disable_icons_and_restore_round_tags(self):
        self.config.icon_style = "nerd"
        self.config.tag_style = "block"
        item = self.store.add(self.repo_path, "Ship it #type/bug #api", "Backlog")
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-icon-style").value = "plain"
            screen.query_one("#config-tag-style").value = "round"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertEqual(app.query_one(".card").plain, f"Ship it  {item.ref}\nbug \ue0b6api\ue0b4")
        config = load_config(app.config_file)
        self.assertEqual((config.icon_style, config.tag_style), ("plain", "round"))

    async def test_card_style_and_outline_tags_save_and_redraw_the_cards(self):
        self.store.add(self.repo_path, "Ship it #api", "Backlog", body=["Speed up the search"])
        self.config.tag_style = "block"
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertEqual(screen.query_one("#config-card-style").value, "full")
            rendered = "\n".join(strip.text for strip in screen._compositor.render_strips())
            self.assertIn("Cards (all boards)", rendered)
            screen.query_one("#config-card-style").value = "compact"
            screen.query_one("#config-tag-style").value = "outline"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
            widget = app.query_one(".card")
            self.assertTrue(widget.has_class("compact"))
            self.assertNotIn("Speed up", widget.plain)
        config = load_config(app.config_file)
        self.assertEqual((config.card_style, config.tag_style), ("compact", "outline"))

    async def test_display_and_board_changes_save_together(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-icon-style").value = "nerd"
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")
        self.assertEqual(load_config(app.config_file).icon_style, "nerd")

    async def test_c_shows_the_board_merge_mode_and_links(self):
        self.store.set_merge(self.repo_path, "pr")
        self.link(ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")]))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertIn(self.repo_path.stem, screen.query_one("#config-heading").plain)
            self.assertEqual(screen.query_one("#config-merge").value, "pr")
            links = screen.query_one("#config-links")
            self.assertEqual(links.option_count, 1)
            self.assertIn("Platform", str(links.get_option_at_index(0).prompt))
            self.assertIn("repo:api", str(links.get_option_at_index(0).prompt))

    async def test_saving_a_merge_mode_writes_the_frontmatter(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertEqual(screen.query_one("#config-merge").value, "default")
            screen.query_one("#config-merge").value = "pr"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(self.store.board_meta(self.repo_path).merge, "pr")

    async def test_the_config_default_removes_the_merge_key(self):
        self.store.set_merge(self.repo_path, "local")
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "default"
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertNotIn("merge:", self.repo_path.read_text())

    async def test_enter_on_a_link_marks_it_for_removal_and_save_removes_it(self):
        self.link(ProjectLink("p1", "Platform"))
        self.link(ProjectLink("p2", "Billing"))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-links").focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("remove", str(screen.query_one("#config-links").get_option_at_index(0).prompt))
            await pilot.press("ctrl+s")
            await settle(pilot)
        self.assertEqual(self.store.board_meta(self.repo_path).links, [ProjectLink("p2", "Billing")])

    async def test_enter_again_keeps_the_link(self):
        self.link(ProjectLink("p1", "Platform"))
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-links").focus()
            await pilot.press("enter", "enter", "ctrl+s")
            await settle(pilot)
        self.assertEqual(self.store.board_meta(self.repo_path).links, [ProjectLink("p1", "Platform")])

    async def test_one_save_changes_the_merge_mode_and_removes_and_adds_links(self):
        self.link(ProjectLink("p1", "Platform"))
        self.link(ProjectLink("p2", "Billing"))
        app = self.make_app(resolve_link=lambda name, labels: ProjectLink("p3", name))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-links").focus()
            await pilot.press("enter")
            screen.query_one("#config-project").value = "Mobile"
            await pilot.press("ctrl+s")
            await settle(pilot)
        meta = self.store.board_meta(self.repo_path)
        self.assertEqual(meta.merge, "pr")
        self.assertEqual(meta.links, [ProjectLink("p2", "Billing"), ProjectLink("p3", "Mobile")])

    async def test_a_failed_link_write_writes_nothing_and_shows_the_error(self):
        text = self.repo_path.read_text().replace("repo:", "other: 1\nother: 2\nrepo:")
        self.repo_path.write_text(text)
        app = self.make_app(resolve_link=lambda name, labels: ProjectLink("p3", name))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-project").value = "Mobile"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("duplicate key 'other'", screen.query_one("#config-error").plain)
            self.assertIsNone(screen.meta.merge)
        self.assertEqual(self.repo_path.read_text(), text)

    async def test_enter_in_the_project_field_moves_to_the_labels(self):
        asked = []
        app = self.make_app(resolve_link=lambda name, labels: asked.append(name) or ProjectLink("p3", name))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-project").focus()
            screen.query_one("#config-project").value = "Mobile"
            await pilot.press("enter")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIs(app.focused, screen.query_one("#config-labels"))
            self.assertEqual(asked, [])
            screen.query_one("#config-labels").value = "repo:api"
            await pilot.press("enter")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(asked, ["Mobile"])

    async def test_saving_a_project_name_links_it_with_its_labels(self):
        asked = []

        def resolve(name, labels):
            asked.append((name, labels))
            return ProjectLink("p9", "API Platform", [LabelLink("l1", "repo:api"), LabelLink("l2", "ENG/bug")])

        app = self.make_app(resolve_link=resolve)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-project").value = "API Platform"
            screen.query_one("#config-labels").value = "repo:api, ENG/bug"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(asked, [("API Platform", ["repo:api", "ENG/bug"])])
        self.assertEqual([link.id for link in self.store.board_meta(self.repo_path).links], ["p9"])

    async def test_a_failed_project_lookup_shows_in_the_modal_and_writes_nothing(self):
        def resolve(name, labels):
            raise HerdboardError(f"No Linear project named {name!r}")

        before = self.repo_path.read_text()
        app = self.make_app(resolve_link=resolve)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            screen.query_one("#config-project").value = "Nope"
            screen.query_one("#config-icon-style").value = "nerd"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("No Linear project named 'Nope'", screen.query_one("#config-error").plain)
            self.assertEqual(self.config.icon_style, "plain")
        self.assertEqual(self.repo_path.read_text(), before)
        self.assertFalse(app.config_file.exists())

    async def test_without_linear_the_add_fields_are_hidden(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertFalse(screen.query("#config-project"))

    async def test_escape_closes_without_a_write(self):
        before = self.repo_path.read_text()
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-merge").value = "pr"
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(self.repo_path.read_text(), before)

    async def test_a_long_error_stays_in_view_in_a_small_terminal(self):
        for number in range(8):
            self.link(ProjectLink(f"p{number}", f"Project {number}", [LabelLink(f"l{number}", "repo:api")]))

        def resolve(name, labels):
            raise HerdboardError("No Linear project named " + repr(name) + ". " + "Check the name. " * 12)

        app = self.make_app(resolve_link=resolve)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            screen.query_one("#config-project").value = "Nope"
            await pilot.press("ctrl+s")
            await settle(pilot)
            error = screen.query_one("#config-error")
            self.assertIn("No Linear project named 'Nope'", error.plain)
            visible = screen._compositor.visible_widgets
            self.assertIn(error, visible)
            region, clip = visible[error]
            self.assertEqual(region.intersection(clip), region)
            self.assertGreater(region.height, 1)

    async def test_c_on_a_missing_board_file_shows_the_error(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            self.repo_path.unlink()
            await pilot.press("c")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
            messages = [(n.message, n.severity) for n in app._notifications]
            self.assertTrue(any("cannot read board" in m and level == "error" for m, level in messages), messages)
            self.assertTrue(app.is_running)

    async def test_c_without_a_repository_board_can_save_global_display_settings(self):
        app = self.make_app(view="inbox", repo=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            screen = await self.open_config(pilot)
            self.assertFalse(screen.query("#config-merge"))
            self.assertFalse(screen.query("#config-project"))
            hint = screen.query_one("#config-board-hint").render().plain
            self.assertEqual(hint, "No repository board. Press i in a repository to create one.")
            self.assertEqual(screen.query_one("#config-icon-style").value, "plain")
            screen.query_one("#config-icon-style").value = "nerd"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(load_config(app.config_file).icon_style, "nerd")


def board_arguments(case, *args):
    """The keyword arguments that the `board` command gives the app."""
    seen = {}
    with mock.patch.object(HerdboardApp, "__init__", lambda self, store, view, **kw: seen.update(kw)), \
            mock.patch.object(HerdboardApp, "run"):
        case.run_cli("board", *args, directory=case.repo)
    return seen


class BoardCommandTests(CliTestCase):
    def test_without_linear_the_board_gets_no_project_lookup(self):
        self.init_repo_board()
        self.assertIsNone(board_arguments(self)["resolve_link"])

    def test_the_board_shows_agent_logos_when_the_font_is_installed(self):
        self.init_repo_board()
        for installed in (True, False):
            with self.subTest(installed=installed), \
                    mock.patch("herdboard.board.agent_font_installed", return_value=installed):
                self.assertEqual(board_arguments(self)["agent_logos"], installed)

    def test_prs_opens_the_board_on_the_pull_request_list(self):
        self.init_repo_board()
        self.assertFalse(board_arguments(self)["open_prs"])
        self.assertTrue(board_arguments(self, "--prs")["open_prs"])


class BoardCommandLinearTests(LinearCliCase):
    def test_the_board_looks_up_a_project_and_its_labels(self):
        self.board()
        resolve = board_arguments(self)["resolve_link"]
        self.assertEqual(resolve("platform", ["repo:api"]),
                         ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]))
        with self.assertRaisesRegex(HerdboardError, "No Linear project named 'Nope'"):
            resolve("Nope", [])

    def test_an_offline_board_gets_no_project_lookup(self):
        self.board()
        self.assertIsNone(board_arguments(self, "--offline")["resolve_link"])


if __name__ == "__main__":
    unittest.main()
