import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard.board import HerdboardApp
from herdboard.config import Config
from herdboard.identity import RepoIdentity
from herdboard.store import Store
from support.herdboard_store import render_markdown, write_markdown_board

SIZE = (80, 24)


async def settle(pilot):
    """Wait for key handling, store writes and the reload that follows."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def fill(pilot, text, submit=True):
    """Set the Add dialog text directly; typing is covered by one test."""
    pilot.app.screen.query_one("#card-title").value = text
    if submit:
        await pilot.press("enter")
        await settle(pilot)


class BoardTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # unittest runs the loop in debug mode, which slows every Textual message several times
        # over and reports slow callbacks on a loaded machine.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Keys such as `s` reach the real start code: keep its state and Herdr calls inside the test.
        env = {"HERDR_BIN_PATH": str(self.root / "no-herdr")}
        if not os.environ.get("XDG_STATE_HOME", "").startswith(tempfile.gettempdir()):
            env["XDG_STATE_HOME"] = str(self.root / "state")
        self.enterContext(mock.patch.dict(os.environ, env))
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.repo_path = self.store.init_board(self.identity)
        self.inbox_path = self.store.ensure_inbox()

    def make_app(self, view="repo", interval=60.0, repo=True):
        paths = {
            "repo": [self.repo_path],
            "inbox": [self.inbox_path],
            "all": self.store.board_paths(),
        }
        app = HerdboardApp(
            self.store,
            view,
            paths_for=lambda name: paths[name] if name != "all" else self.store.board_paths(),
            repo_board=self.repo_path if repo else None,
            reload_interval=interval,
        )
        app.animation_level = "none"
        return app

    def headers(self, app):
        return [w.plain for w in app.query(".column-header")]

    def cards(self, app):
        return [w.plain for w in app.query(".card")]

    def selected(self, app):
        return [w.plain for w in app.query(".card.selected")]

    def put_markdown(self, path, text):
        """Write a board folder from the old Markdown notation, as an outside edit would."""
        write_markdown_board(self.config.todos_dir, path.name, text)

    def markdown(self, path):
        """A board folder in the old Markdown notation."""
        return render_markdown(path)

    def refs(self, path):
        return {c.title: (c.ref, c.column) for c in self.store.list_cards([path])}
