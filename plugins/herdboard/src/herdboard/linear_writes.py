"""Write to Linear: move, finish and create issues.

A write needs a connection and is never served from the cache. A write is sent once. When
its result is unknown, the issue is fetched again to learn what happened.
"""

from __future__ import annotations

from contextlib import contextmanager
import time
import uuid

from .config import Config
from .linear import (
    LinearClient,
    LinearError,
    LinearNetworkError,
    LinearWriteUnknownError,
)
from .linear_source import LinearSource
from .linear_states import Ask, StateChoices, refuse, resolve_state
from .model import HerdboardError, NotFoundError


class LinearOfflineError(LinearError):
    code = "linear_offline"


class TeamRequiredError(HerdboardError):
    code = "team_required"


def pick_label(client, name: str, team: dict) -> dict | None:
    """The label `name` (any letter case) of `team`, else the workspace label; None when neither exists."""
    found = client.find_labels(name)
    mine = [l for l in found if l.get("team") and l["team"]["id"] == team["id"]]
    workspace = [l for l in found if not l.get("team")]
    return (mine or workspace or [None])[0]


class LinearWriter:
    def __init__(self, config: Config, source: LinearSource, wait=time.sleep):
        self.config = config
        self.source = source
        self.wait = wait

    def client(self) -> LinearClient:
        """The client for a write; fails when Linear is off or the source is offline."""
        self.source.require_active()
        if self.source.offline:
            raise LinearOfflineError(
                "Linear writes need a connection: do not use --offline, "
                "and Linear cards are read only offline"
            )
        return self.source.client

    @contextmanager
    def online(self):
        """The client; a network failure becomes `LinearOfflineError`."""
        client = self.client()
        try:
            yield client
        except LinearNetworkError as error:
            raise LinearOfflineError(
                f"Linear is not reachable, and writes need a connection: {error}"
            ) from error

    # Operations

    def move(
        self,
        identifier: str,
        column: str,
        position: str | None = None,
        ask: Ask = refuse,
        state_name: str | None = None,
    ) -> dict:
        """Move an issue to `column`; `position` is "top", "bottom" or None (no order change).

        Returns the issue as the card code reads it.
        """
        with self.online() as client:
            issue = client.issue_for_write(identifier.upper())
            if issue is None:
                raise NotFoundError(f"No card {identifier}")
            team = issue["team"]
            choices = StateChoices(self.source.account_dir())
            state = resolve_state(
                self.config, choices, team, client.team_states(team["id"]), column, ask, state_name
            )
            if issue["state"]["id"] == state["id"] and position is None:
                return self._full(client, issue["identifier"], {"labels": [], **issue})
            sort_order = self._sort_order(client, team, state, position)
            try:
                result = client.update_issue(
                    issue["id"], state_id=state["id"], sort_order=sort_order
                )
            except LinearWriteUnknownError as error:
                try:
                    result = self._reconcile_update(client, issue, state, sort_order, error)
                finally:
                    self.source.drop_cache()
            else:
                self.source.drop_cache()
            return self._full(client, issue["identifier"], {"labels": [], **issue, **result})

    def done(self, identifier: str, ask: Ask = refuse, state_name: str | None = None) -> dict:
        """Move an issue to the column that config maps completed states to."""
        return self.move(
            identifier, self.config.linear.columns["completed"], None, ask, state_name
        )

    def create(
        self,
        title: str,
        *,
        team: str | None = None,
        project: dict | None = None,
        label_ids: list[str] | tuple[str, ...] = (),
        column: str | None = None,
        ask: Ask = refuse,
        state_name: str | None = None,
        type_label: str | None = None,
    ) -> dict:
        """Create an issue assigned to you. `project` is a Linear project with its `teams`.

        `type_label` names a label that the issue's team or the workspace has.
        """
        with self.online() as client:
            found = self.resolve_team(client, team, project)
            if type_label:
                label = pick_label(client, type_label, found)
                if label is None:
                    raise HerdboardError(
                        f"No Linear label named {type_label!r} in team {found['key']} or the workspace"
                    )
                label_ids = list(dict.fromkeys([*label_ids, label["id"]]))
            states = client.team_states(found["id"])
            choices = StateChoices(self.source.account_dir())
            column = column or self.config.columns[0]
            state = resolve_state(self.config, choices, found, states, column, ask, state_name)
            issue_id = str(uuid.uuid4())
            result = self.send_create(
                client,
                issue_id,
                team_id=found["id"],
                title=title,
                state_id=state["id"],
                project_id=project["id"] if project else None,
                label_ids=list(label_ids) or None,
                assignee_id=self.source.account()[1],
            )
            known = {
                "title": title,
                "labels": [],
                "project": {"id": project["id"], "name": project["name"]} if project else None,
            }
            return self._full(client, result["identifier"], {**known, **result})

    def send_create(self, client: LinearClient, issue_id: str, **fields) -> dict:
        """Create an issue with a client-generated ID, sent once.

        When the result is unknown, look the ID up. Raises `LinearWriteUnknownError` when
        Linear does not show the issue. `fields` are the arguments of `create_issue`.
        """
        try:
            result = client.create_issue(issue_id=issue_id, **fields)
        except LinearWriteUnknownError as error:
            try:
                return self._reconcile_create(client, issue_id, fields["title"], error)
            finally:
                self.source.drop_cache()
        self.source.drop_cache()
        return result

    # Helpers

    def resolve_team(self, client: LinearClient, key: str | None, project: dict | None) -> dict:
        """The team of a new issue: the key, the project's only team, or your only team."""
        if key:
            found = client.team_by_key(key)
            if found is None:
                raise NotFoundError(f"No Linear team with key {key!r}")
            return found
        project_teams = list(project["teams"]) if project else []
        if len(project_teams) == 1:
            return project_teams[0]
        mine = client.my_teams()
        if project_teams:
            ids = {t["id"] for t in project_teams}
            mine = [t for t in mine if t["id"] in ids]
        if len(mine) == 1:
            return mine[0]
        keys = [t["key"] for t in mine or project_teams]
        listing = f"Possible teams: {', '.join(keys)}. " if keys else ""
        raise TeamRequiredError(f"{listing}Use --team KEY to choose the team")

    def _sort_order(self, client, team: dict, state: dict, position: str | None) -> float | None:
        """A sort order above or below every issue of the team in the state; None if empty."""
        if position is None:
            return None
        closed = state["type"] in ("completed", "canceled")
        orders = client.state_sort_orders(
            team["id"], state["id"], self.config.linear.done_days if closed else None
        )
        if not orders:
            return None
        return min(orders) - 1 if position == "top" else max(orders) + 1

    def _full(self, client: LinearClient, identifier: str, fallback: dict) -> dict:
        """The whole issue after a write; `fallback` when it cannot be read."""
        try:
            found = client.issue(identifier)
        except LinearError:
            found = None
        return found or fallback

    @staticmethod
    def _unknown(identifier: str) -> LinearWriteUnknownError:
        return LinearWriteUnknownError(
            f"The result of the write to {identifier} is unknown: check {identifier} in Linear"
        )

    def _verification_reads(self, client, identifier):
        """Bounded verification reads. Never sends the write again."""
        for delay in (0, 0.5, 1):
            if delay:
                self.wait(delay)
            try:
                yield client.issue_for_write(identifier)
            except LinearError:
                yield None

    def _reconcile_update(self, client, issue, state, sort_order, error) -> dict:
        """Fetch the issue after an unknown result. Never sends the write again."""
        identifier = issue["identifier"]
        for found in self._verification_reads(client, identifier):
            if found is not None and found["state"]["id"] == state["id"] and (
                sort_order is None or found["sortOrder"] == sort_order
            ):
                return found
        raise self._unknown(identifier) from error

    def _reconcile_create(self, client, issue_id: str, title: str, error) -> dict:
        for found in self._verification_reads(client, issue_id):
            if found is not None:
                return found
        unknown = self._unknown(f"the new issue {title!r} ({issue_id})")
        unknown.issue_id = issue_id
        raise unknown from error
