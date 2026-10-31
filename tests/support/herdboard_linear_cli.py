from pathlib import Path

from herdboard.store import Store
from support.herdboard_cli import CliTestCase
from support.herdboard_linear_source import LinearEnvTestCase


class LinearCliCase(CliTestCase, LinearEnvTestCase):
    def setUp(self):
        CliTestCase.setUp(self)
        cli_root = self.tmp
        LinearEnvTestCase.setUp(self)
        self.cli_tmp = cli_root

    def refs(self, data):
        return [card["ref"] for card in data["cards"]]

    def board(self):
        return Path(self.init_repo_board())

    def link(self, *args, expect=0):
        return self.run_json("link", *args, directory=self.repo, expect=expect)

    def store_for(self):
        from herdboard.config import load_config

        return Store(load_config(self.config))

    def relink(self, *links):
        store = self.store_for()
        store.update_links(store.boards_dir / "acme-api", lambda _: list(links))

    def repin(self, *pins):
        store = self.store_for()
        store.update_pins(store.boards_dir / "acme-api", lambda _: list(pins))

    def listing(self, *args, directory=None, expect=0):
        return self.run_json("list", *args, directory=directory or self.repo, expect=expect)
