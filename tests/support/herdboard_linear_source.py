import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import linear_source
from herdboard.config import Config
from herdboard.linear import LinearClient
from herdboard.linear_source import LinearSource
from support import linear_data
from support.linear_fake import FakeLinear

DEAD = "http://127.0.0.1:1/graphql"


class LinearEnvTestCase(unittest.TestCase):
    """A fake Linear server, a private state directory and a controlled environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        env = {
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.state),
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
            "HERDBOARD_CONFIG": "",
            "LINEAR_API_KEY": "test-key",
        }
        self.enterContext(mock.patch.dict(os.environ, env))
        self.fixtures = linear_data.fixtures()
        self.fake = self.enterContext(FakeLinear(self.fixtures, page_size=50))
        self.endpoint = self.fake.endpoint
        self.enterContext(mock.patch.object(linear_source, "build_client", self.build_client))

    def build_client(self, key):
        return LinearClient(key, endpoint=self.endpoint, timeout=2.0, sleep=lambda _: None)

    def source(self, **kwargs):
        return LinearSource(Config(todos_dir=self.root / "todos"), **kwargs)

    def requests(self, operation):
        return [r for r in self.fake.requests if r["operationName"] == operation]
