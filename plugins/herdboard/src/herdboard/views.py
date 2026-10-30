"""Views shared by `list` and `board`: which boards and which Linear issues each shows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .identity import RepoIdentity
from .linear_cards import issue_card
from .linear_links import ProjectLink
from .linear_source import CacheMiss, Fetched, LinearSource, ProjectNotFound
from .model import HerdboardError
from .store import CardView, Store

VIEW_NAMES = ["repo", "inbox", "all", "mine", "project"]
INIT_MESSAGE = "Repository board is unavailable. Run the command again to retry automatic setup."


@dataclass
class LinearResult:
    cards: list[CardView] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None
    stale: bool = False

    @property
    def info(self) -> dict | None:
        """The `linear` JSON object; None when nothing was fetched."""
        if self.fetched_at is None:
            return None
        return {"fetched_at": self.fetched_at.isoformat(), "stale": self.stale}


class Views:
    def __init__(
        self,
        store: Store,
        config: Config,
        source: LinearSource,
        identity: RepoIdentity | None,
        project: str | None = None,
    ):
        self.store = store
        self.config = config
        self.source = source
        self.identity = identity
        self.project = project
        # The id of `project` when it is not linked to a board: Linear is asked once.
        self._resolved_project: str | None = None

    # Boards

    def repo_board(self) -> Path | None:
        return self.store.board_for_repo(self.identity) if self.identity else None

    def available(self, view: str) -> bool:
        if view == "repo":
            return self.repo_board() is not None
        if view == "mine":
            return self.source.active
        if view == "project":
            return self.source.active and self.project is not None
        return True

    def _linked(self) -> list[tuple[Path, list[ProjectLink]]]:
        return [(path, self.store.board_meta(path).links) for path in self.store.board_paths()]

    def paths(self, view: str) -> list[Path]:
        """Board files that a view shows."""
        if view == "repo":
            board = self.repo_board()
            if board is None:
                raise HerdboardError(INIT_MESSAGE)
            return [board]
        if view == "inbox":
            inbox = self.store.inbox_path
            return [inbox] if inbox.is_file() else []
        if view == "all":
            return self.store.board_paths()
        if view == "mine":
            self.source.require_active()
            return []
        if view == "project":
            project_id = self._project_id(ask_linear=False)
            if project_id is None:
                return []
            return [p for p, links in self._linked() if any(link.id == project_id for link in links)]
        raise HerdboardError(f"Unknown view {view!r}")

    # Linear

    def resolve_project(self) -> None:
        """Find the id of the project view's project. Linear is asked at most once."""
        self._project_id(ask_linear=True)

    def _project_id(self, ask_linear: bool) -> str | None:
        """The id of the project view's project; None until `resolve_project` found it.

        Only `ask_linear` reaches the network, so board code on the event loop passes False.
        """
        self.source.require_active()
        if not self.project:
            raise HerdboardError("The project view needs a project name: use --project NAME")
        wanted = self.project.casefold()
        ids = {
            link.id
            for _, links in self._linked()
            for link in links
            if link.name.casefold() == wanted or link.id == self.project
        }
        if len(ids) > 1:
            raise HerdboardError(f"Several linked projects are named {self.project!r}")
        if ids:
            return ids.pop()
        if self._resolved_project is not None:
            return self._resolved_project
        if self.source.offline:
            raise HerdboardError(
                f"{self.project!r} is not linked to a board: cannot find it offline"
            )
        if not ask_linear:
            return None
        found = self.source.client.find_projects(self.project)
        if len(found) != 1:
            raise HerdboardError(
                f"No Linear project named {self.project!r}"
                if not found
                else f"{len(found)} Linear projects are named {self.project!r}"
            )
        self._resolved_project = found[0]["id"]
        return self._resolved_project

    def _jobs(self, view: str) -> tuple[list[tuple[str, Callable[[], Fetched]]], set[str], list[str]]:
        """Fetch jobs (a label and a function), repository identities, errors."""
        source = self.source
        jobs: list[tuple[str, Callable[[], Fetched]]] = []
        identities: set[str] = set()
        errors: list[str] = []

        def add_project(link: ProjectLink, use_labels: bool) -> None:
            label_ids = [label.id for label in link.labels] if use_labels and link.labels else None
            where = f"Linear project {link.name!r}"
            names = {label.id: label.name for label in link.labels}

            def fetch() -> Fetched:
                fetched = source.project_issues(link.id, label_ids)
                for missing in fetched.data.get("missing_labels", []):
                    errors.append(
                        f"{where}: label {names.get(missing, missing)!r} not found or not accessible"
                    )
                return fetched

            jobs.append((where, fetch))

        if view == "repo":
            meta = self.store.board_meta(self.paths("repo")[0])
            if meta.error:
                errors.append(meta.error)
            if meta.repo:
                identities.add(meta.repo)
            for link in meta.links:
                add_project(link, True)
            if meta.pins:
                pin_ids = [pin.id for pin in meta.pins]
                jobs.append(("Linear pinned issues", lambda: source.pinned_issues(pin_ids)))
        elif view == "project":
            project_id = self._project_id(ask_linear=True)
            names = {}
            for path in self.paths("project"):
                meta = self.store.board_meta(path)
                if meta.repo:
                    identities.add(meta.repo)
                names.update({link.id: link.name for link in meta.links})
            jobs.append(
                (
                    f"Linear project {names.get(project_id, self.project)!r}",
                    lambda: source.project_issues(project_id),
                )
            )
        elif view in ("mine", "all"):
            for path in self.paths(view):
                meta = self.store.board_meta(path)
                if meta.repo:
                    identities.add(meta.repo)
            if view == "mine" and self.identity:
                identities.add(self.identity.identity)
            jobs.append(("Linear issues assigned to you", source.my_issues))
        return jobs, identities, errors

    def linear(self, view: str) -> LinearResult:
        """Linear cards of a view. Without a usable Linear the result is empty."""
        source = self.source
        result = LinearResult()
        if view == "inbox" or not source.active:
            if view in ("mine", "project"):
                self.source.require_active()
            return result
        source.begin_refresh()
        jobs, identities, result.errors = self._jobs(view)
        seen: set[str] = set()
        for label, job in jobs:
            try:
                fetched = job()
            except (ProjectNotFound, CacheMiss) as error:
                if view in ("mine", "all"):
                    raise
                result.errors.append(f"{label}: {error}")
                continue
            if result.fetched_at is None or fetched.fetched_at < result.fetched_at:
                result.fetched_at = fetched.fetched_at
            result.stale = result.stale or fetched.stale
            data = fetched.data
            issues = data["issues"] if isinstance(data, dict) else data
            for issue in issues:
                if issue["id"] in seen:
                    continue
                seen.add(issue["id"])
                card = issue_card(self.config, issue, identities, fetched.fetched_at, fetched.stale)
                if card is not None:
                    result.cards.append(card)
        # Linear's manual order. The sort is stable, so equal or missing keys keep fetch order.
        result.cards.sort(key=lambda c: (c.sort_order is None, c.sort_order or 0.0))
        return result
