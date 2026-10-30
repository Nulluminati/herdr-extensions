import threading
from herdboard.board import HerdboardApp
from herdboard.linear_links import ProjectLink
from herdboard.linear_source import LinearSource
from herdboard.linear_writes import LinearWriter
from herdboard.views import Views
from support.herdboard_board import BoardTestCase
from support.herdboard_linear_source import LinearEnvTestCase


class LinearBoardCase(BoardTestCase, LinearEnvTestCase):
    def setUp(self):
        LinearEnvTestCase.setUp(self)
        BoardTestCase.setUp(self)
        self.gate = None
        self.store.update_links(self.repo_path, lambda _: [ProjectLink("p-plat", "Platform")])
        self.store.add(self.repo_path, "local todo", "Todo")

    def make_linear_app(self, view="repo", active=True, linear_interval=300.0, project=None, **kwargs):
        source = self.source_in_use = LinearSource(self.config)
        views = Views(self.store, self.config, source, self.identity, project)
        self.fetches = self.running = self.most_running = 0
        lock = threading.Lock()

        def fetch(name):
            with lock:
                self.fetches += 1
                self.running += 1
                self.most_running = max(self.most_running, self.running)
            try:
                if self.gate is not None:
                    self.gate.wait(5)
                return views.linear(name)
            finally:
                with lock:
                    self.running -= 1

        app = HerdboardApp(
            self.store, view, paths_for=views.paths, repo_board=self.repo_path,
            reload_interval=60.0, linear_for=fetch, linear_active=active,
            available=views.available, linear_interval=linear_interval,
            linear_writer=LinearWriter(self.config, source), **kwargs,
        )
        app.animation_level = "none"
        return app

    def title(self, app):
        """The left part of the header: the board names and the Linear state."""
        return app.query_one("#header-bar").place

    def notifications(self, app):
        return [(n.message, n.severity) for n in app._notifications]

    def select(self, app, ref):
        for col, data in enumerate(app.columns):
            for row, card in enumerate(data.cards):
                if card.ref == ref:
                    app.col, app.row = col, row
                    app._show_selection()
                    return
        self.fail(f"{ref} is not on the board")
