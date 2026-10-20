"""Fetch Linear data on demand, with a per-account cache for offline use."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Any, Callable

from .config import Config
from .linear import LinearClient, LinearError, LinearNetworkError
from .linear_cards import LINEAR_REF
from .model import HerdboardError


class LinearDisabledError(HerdboardError):
    code = "linear_disabled"


class ProjectNotFound(LinearError):
    """A linked project no longer exists or is not accessible."""


class CacheMiss(LinearError):
    """The cache has no data for a query."""


# Stored answers to "which state?" questions. They live beside the cached queries but are
# not a cache: dropping the cache keeps them.
STATE_CHOICES_FILE = "state-choices.json"
INVALIDATION_FILE = "cache-invalidated"
# Records of promotions in progress (see promotions.py). They are not a cache either.
PROMOTIONS_FILE = "promotions.json"


@dataclass
class Fetched:
    data: Any
    fetched_at: datetime
    stale: bool = False


def build_client(api_key: str) -> LinearClient:
    return LinearClient(api_key)


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "herdboard"


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", part) or "_"


def _write_json(path: Path, data: dict) -> bool:
    """Write a cache file. The cache is best effort, so a failure is ignored."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temp, path)
            return True
        except BaseException:
            if os.path.exists(temp):
                os.unlink(temp)
            raise
    except OSError:
        return False


# Cache files are read, compared and replaced under this lock, so that two fetches in one
# process never write in the wrong order.
_CACHE_LOCK = threading.Lock()


def _cached_time(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextmanager
def _account_cache_lock(folder: Path):
    """Serialize cache replacement and invalidation across threads and processes."""
    with _CACHE_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / ".cache.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _invalidated(path: Path, fetched_at: datetime) -> bool:
    latest = _cached_time(path.parent / INVALIDATION_FILE)
    return latest is not None and fetched_at < latest


def write_cache(path: Path, fetched_at: datetime, data: Any) -> None:
    """Write a cache entry unless it predates invalidation or a newer entry."""
    try:
        with _account_cache_lock(path.parent):
            current = _cached_time(path)
            if not _invalidated(path, fetched_at) and (current is None or current <= fetched_at):
                _write_json(path, {"fetched_at": fetched_at.isoformat(), "data": data})
    except OSError:
        pass


def _issues_of(data: Any) -> list[dict]:
    """The issues in cached data: a project, a list of issues, or one issue."""
    if isinstance(data, dict) and "issues" in data:
        return data["issues"]
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


class LinearSource:
    """Reads Linear for one API key. With `offline`, it reads only the cache."""

    def __init__(self, config: Config, offline: bool = False):
        self.settings = config.linear
        self.offline = offline
        self._client: LinearClient | None = None
        self._account: tuple[str, str] | None = None
        self._network_down = False

    def begin_refresh(self) -> None:
        """Start a refresh: Linear is contacted again even after an earlier network failure."""
        self._network_down = False

    @property
    def active(self) -> bool:
        return self.settings.active

    def require_active(self) -> None:
        if not self.active:
            raise LinearDisabledError(
                f"Linear is off: set {self.settings.key_source} and do not set "
                "linear.enabled = false"
            )

    @property
    def client(self) -> LinearClient:
        self.require_active()
        if self._client is None:
            self._client = build_client(self.settings.api_key)
        return self._client

    # Account and cache

    def _keys_file(self) -> Path:
        digest = hashlib.sha256(self.settings.api_key.encode("utf-8")).hexdigest()[:16]
        return state_dir() / "keys" / f"{digest}.json"

    def _account_ids(self) -> tuple[str, str]:
        if self._account is not None:
            return self._account
        self.require_active()
        keys_file = self._keys_file()
        failure: LinearError | None = None
        if not (self.offline or self._network_down):
            try:
                viewer = self.client.viewer()
            except LinearNetworkError as error:
                self._network_down, failure = True, error
            else:
                account = (viewer["organization"]["id"], viewer["id"])
                _write_json(keys_file, {"org": account[0], "user": account[1]})
                self._account = account
                return account
        try:
            known = json.loads(keys_file.read_text(encoding="utf-8"))
            self._account = (known["org"], known["user"])
        except (OSError, ValueError, KeyError, TypeError):
            if failure is not None:
                raise failure
            raise CacheMiss("No cached Linear data for this API key") from None
        return self._account

    def account(self) -> tuple[str, str]:
        """The workspace ID and the user ID of the API key."""
        return self._account_ids()

    def account_dir(self) -> Path:
        org, user = self._account_ids()
        return state_dir() / _safe(org) / _safe(user)

    def drop_cache(self) -> None:
        """Delete every cached query of this account, so the next read asks Linear."""
        try:
            folder = self.account_dir()
            with _account_cache_lock(folder):
                stamp = {"fetched_at": datetime.now(timezone.utc).isoformat()}
                if not _write_json(folder / INVALIDATION_FILE, stamp):
                    print("herdboard: warning: cannot record Linear cache invalidation", file=sys.stderr)
                for path in folder.glob("*.json"):
                    if path.name not in (STATE_CHOICES_FILE, PROMOTIONS_FILE):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
        except OSError:
            print("herdboard: warning: cannot record Linear cache invalidation", file=sys.stderr)

    def _cache_file(self, query: str) -> Path:
        org, user = self._account_ids()
        return state_dir() / _safe(org) / _safe(user) / f"{_safe(query)}.json"

    def _fetch(self, query: str, load: Callable[[LinearClient], Any]) -> Fetched:
        path = self._cache_file(query)
        if not (self.offline or self._network_down):
            # The request start is the data's time: a slow older fetch never looks newer.
            started = datetime.now(timezone.utc)
            try:
                data = load(self.client)
            except LinearNetworkError:
                self._network_down = True
            else:
                if data is not None:
                    write_cache(path, started, data)
                return Fetched(data, started)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if _invalidated(path, fetched_at):
                raise CacheMiss(f"No cached Linear data for {query}")
            return Fetched(cached["data"], fetched_at, True)
        except (OSError, ValueError, KeyError, TypeError):
            raise CacheMiss(f"No cached Linear data for {query}") from None

    # Queries

    def project_issues(self, project_id: str, label_ids: list[str] | None = None) -> Fetched:
        """`{"project": {...}, "issues": [...], "missing_labels": [...]}` for one project.

        Labels that do not exist or are not accessible are listed in `missing_labels`.
        The issues match the other labels.
        """
        query = f"project-{project_id}"
        if label_ids is not None:
            query += "-labels-" + hashlib.sha256(
                "\0".join(sorted(label_ids)).encode("utf-8")
            ).hexdigest()[:12]

        def load(client: LinearClient) -> dict:
            project = client.project(project_id)
            if project is None:
                raise ProjectNotFound(f"Linear project {project_id} not found or not accessible")
            valid, missing = label_ids, []
            if label_ids:
                found = {label["id"] for label in client.labels(label_ids)}
                valid = [i for i in label_ids if i in found]
                missing = [i for i in label_ids if i not in found]
            issues = client.project_issues(project_id, valid, self.settings.done_days)
            return {"project": project, "issues": issues, "missing_labels": missing}

        return self._fetch(query, load)

    def pinned_issues(self, ids: list[str]) -> Fetched:
        """The pinned issues among ids that exist and are open or recently done."""
        query = "pinned-" + hashlib.sha256("\0".join(sorted(ids)).encode("utf-8")).hexdigest()[:12]
        return self._fetch(query, lambda client: client.issues_by_ids(ids, self.settings.done_days))

    def search(self, term: str) -> list[dict]:
        """Open issues of the user's teams that match term, from Linear and never the cache.

        A term such as ENG-123 skips the search and returns that issue, from any team and in
        any state, or nothing when Linear does not know it. Each result has id, identifier,
        title, state (name, type), team (key) and project (name, or None).
        """
        self.require_active()
        if self.offline:
            raise LinearError("Linear search needs a connection")
        term = term.strip()
        if LINEAR_REF.match(term):
            found = self.client.issue(term.upper())
            if found is None:
                return []
            project = found.get("project")
            return [{"id": found["id"], "identifier": found["identifier"], "title": found["title"],
                     "state": {"name": found["state"]["name"], "type": found["state"]["type"]},
                     "team": {"key": found["team"]["key"]},
                     "project": {"name": project["name"]} if project else None}]
        team_ids = [team["id"] for team in self.client.my_teams()]
        return self.client.search_issues(term, team_ids)

    def my_issues(self) -> Fetched:
        return self._fetch("mine", lambda client: client.my_issues(self.settings.done_days))

    def issue(self, identifier: str) -> Fetched:
        """One issue by identifier; its data is None when Linear does not know it.

        Without a connection this searches every cached view, and raises CacheMiss when
        none has the issue.
        """
        identifier = identifier.upper()
        try:
            return self._fetch(f"issue-{_safe(identifier)}", lambda c: c.issue(identifier))
        except CacheMiss:
            pass
        wanted = identifier.casefold()
        newest: Fetched | None = None
        folder = self._cache_file("x").parent
        for path in folder.glob("*.json"):
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                fetched_at = datetime.fromisoformat(cached["fetched_at"])
                if _invalidated(path, fetched_at):
                    continue
                issues = _issues_of(cached["data"])
                match = next((i for i in issues if i["identifier"].casefold() == wanted), None)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if match is not None and (newest is None or fetched_at > newest.fetched_at):
                newest = Fetched(match, fetched_at, True)
        if newest is None:
            raise CacheMiss(f"No cached Linear data has {identifier}")
        return newest
