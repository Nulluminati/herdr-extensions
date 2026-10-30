"""Records of promotions in progress, so a rerun finishes a promotion and creates nothing twice.

The file lives in the account folder of the Linear cache, because the client UUIDs in it
mean something only in that workspace. A cache drop keeps it.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .linear_source import PROMOTIONS_FILE
from .model import ConflictError, IoError

LOCK_FILE = "promotions.lock"


class PromotionRecords:
    """Records by todo store and todo ID. Each holds the issues of one promotion, parent first.

    The store is the resolved `todos_dir`: two stores may have the same card ID.

    An issue is `{"id": client UUID, "input": create arguments, "identifier": str | None}`;
    `identifier` is set once Linear confirmed the issue.
    """

    def __init__(self, folder: Path, store: str):
        self.folder = folder
        self.store = store
        self.path = folder / PROMOTIONS_FILE

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise IoError(f"{self.path}: cannot read promotion records: {error}") from error
        if not isinstance(data, dict):
            raise IoError(f"{self.path}: promotion records are not a JSON object")
        return data

    def _write(self, data: dict) -> None:
        """Write the whole file atomically. A failure is an error: the record must exist."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self.folder, prefix=f".{PROMOTIONS_FILE}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
            except BaseException:
                if os.path.exists(temp):
                    os.unlink(temp)
                raise
        except OSError as error:
            raise IoError(f"{self.path}: cannot write promotion records: {error}") from error

    def _key(self, todo_id: str) -> str:
        return f"{self.store}::{todo_id}"

    def get(self, todo_id: str) -> dict | None:
        record = self._read().get(self._key(todo_id))
        if isinstance(record, dict) and record.get("store") == self.store:
            return record
        return None

    def put(self, todo_id: str, record: dict) -> None:
        data = self._read()
        data[self._key(todo_id)] = {**record, "store": self.store}
        self._write(data)

    def delete(self, todo_id: str) -> None:
        data = self._read()
        if data.pop(self._key(todo_id), None) is not None:
            self._write(data)

    @contextmanager
    def lock(self):
        """One promotion at a time per account; a second one fails instead of waiting."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            handle = open(self.folder / LOCK_FILE, "a")
        except OSError as error:
            raise IoError(f"{self.folder / LOCK_FILE}: cannot open lock: {error}") from error
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConflictError("Another promote is running; wait for it to finish") from None
            yield
        finally:
            handle.close()
