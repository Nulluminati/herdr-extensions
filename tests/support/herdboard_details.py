import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from herdboard.config import Config
from herdboard.store import Store


class StoreCase:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.card = self.store.add(self.store.ensure_inbox(), "Fix login timeout #type/feature")

    def write_records(self, records):
        path = self.root / "state" / "herdboard" / "starts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))
