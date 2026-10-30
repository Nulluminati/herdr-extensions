import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import cli
from herdboard.board import (
    HELP_SECTIONS,
    DetailScreen,
    HerdboardApp,
    NotesScreen,
    PullRequestDetailScreen,
    PullRequestsScreen,
)
from herdboard.config import Config
from herdboard.store import Store


def binding_keys(screen_class) -> set[str]:
    """Every key of a screen's bindings, as the help writes it."""
    names = {"slash": "/", "question_mark": "?"}
    return {names.get(key, key) for binding in screen_class.BINDINGS for key in binding.key.split(",")}


def help_keys(title: str) -> set[str]:
    """Every key that a help section lists. An entry such as "left, h" lists two keys."""
    return {key for entry, _ in dict(HELP_SECTIONS)[title] for key in entry.split(", ")}


class HelpCoverageTests(unittest.TestCase):
    def test_every_key_of_a_screen_has_a_help_entry(self):
        everywhere = help_keys("Everywhere")
        for screen_class, titles in [
            (HerdboardApp, ("Board", "Cards", "Agents and branches")),
            (DetailScreen, ("Card detail",)),
            (PullRequestsScreen, ("Pull requests",)),
            (PullRequestDetailScreen, ("Pull request detail",)),
            (NotesScreen, ("Notes",)),
        ]:
            listed = everywhere.union(*(help_keys(title) for title in titles))
            with self.subTest(screen=screen_class.__name__):
                self.assertEqual(binding_keys(screen_class) - listed, set())


class CliBoardTests(unittest.TestCase):
    def test_board_command_runs_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            Store(Config(todos_dir=root / "todos")).ensure_inbox()
            with mock.patch.object(HerdboardApp, "run") as run:
                code = cli.main(["--config", str(config), "-C", str(root), "board", "--view", "all"])
            self.assertEqual(code, 0)
            run.assert_called_once_with()

    def test_board_command_default_view_is_inbox_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            seen = []
            with (
                mock.patch.object(HerdboardApp, "__init__", lambda self, store, view, **kw: seen.append(view)),
                mock.patch.object(HerdboardApp, "run"),
            ):
                cli.main(["--config", str(config), "-C", str(root), "board"])
            self.assertEqual(seen, ["inbox"])


class TerminalModeTests(unittest.TestCase):
    """Herdr reads mouse positions as cells; pixel reports (mode 1016) break its own tabs and sidebar."""

    def driver(self):
        from herdboard.board import CellMouseDriver

        driver = CellMouseDriver.__new__(CellMouseDriver)
        driver._mouse = True
        driver._mouse_pixels = False
        driver.written = []
        driver.write = driver.written.append
        driver.flush = lambda: None
        return driver

    def test_the_app_uses_the_cell_mouse_driver(self):
        from textual.app import App
        from textual.drivers.linux_driver import LinuxDriver

        from herdboard.board import CellMouseDriver

        app = HerdboardApp.__new__(HerdboardApp)
        with mock.patch.object(App, "get_driver_class", return_value=LinuxDriver):
            self.assertIs(app.get_driver_class(), CellMouseDriver)
        with mock.patch.object(App, "get_driver_class", return_value=object):
            self.assertIs(app.get_driver_class(), object)

    def test_pixel_mouse_reports_stay_off(self):
        driver = self.driver()
        driver._enable_mouse_pixels()
        self.assertNotIn("\x1b[?1016h", "".join(driver.written))
        self.assertFalse(driver._mouse_pixels)

    def test_in_band_resize_stays_off(self):
        # After an in-band resize report, Textual's parser reads every mouse position as pixels,
        # so Herdr's cell positions would land near the top-left corner.
        driver = self.driver()
        driver._query_in_band_window_resize()
        self.assertNotIn("\x1b[?2048$p", "".join(driver.written))

    def test_cell_mouse_reports_stay_on(self):
        driver = self.driver()
        driver._enable_mouse_support()
        self.assertIn("\x1b[?1000h", "".join(driver.written))
        self.assertIn("\x1b[?1006h", "".join(driver.written))


if __name__ == "__main__":
    unittest.main()
