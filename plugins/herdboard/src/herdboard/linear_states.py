"""Resolve a board column to a workflow state of one Linear team."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .linear_cards import column_for
from .linear_source import STATE_CHOICES_FILE, _write_json
from .model import HerdboardError

# A question about which of several states to use: (team key, column, candidates) -> the
# chosen candidate, or None when the person cancels.
Ask = Callable[[str, str, list[dict]], "dict | None"]


class NoStateError(HerdboardError):
    code = "no_state"


class AmbiguousStateError(HerdboardError):
    """Several states match a column and nobody answered which one to use."""

    code = "ambiguous_state"

    def __init__(self, message: str, candidates: list[str]):
        super().__init__(message)
        self.candidates = candidates


class StateCancelledError(HerdboardError):
    code = "cancelled"


def candidates(
    config: Config, states: list[dict], column: str, only_type: str | None = None
) -> list[dict]:
    """The states of one team that a move to `column` may use, in workflow order.

    A canceled state counts only when no other state matches, and a triage state only
    when no backlog state matches. With `only_type`, states of other types never count.
    """
    wanted = column.casefold()
    found = [s for s in states if column_for(config, s).casefold() == wanted]
    if only_type is not None:
        found = [s for s in found if s["type"] == only_type]
    if any(s["type"] != "canceled" for s in found):
        found = [s for s in found if s["type"] != "canceled"]
    if any(s["type"] == "backlog" for s in found):
        found = [s for s in found if s["type"] != "triage"]
    return sorted(found, key=lambda s: s.get("position", 0))


def refuse(team_key: str, column: str, found: list[dict]) -> dict:
    """The question handler when nobody can answer: fail and list the candidates."""
    names = [s["name"] for s in found]
    raise AmbiguousStateError(
        f"Several states of team {team_key} match column {column!r}: {', '.join(names)}. "
        "Use --state NAME to choose one",
        names,
    )


_LOCK = threading.Lock()


class StateChoices:
    """Stored answers, by team ID and column, in the account folder of the cache."""

    def __init__(self, folder: Path):
        self.path = folder / STATE_CHOICES_FILE

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, team_id: str, column: str) -> str | None:
        teams = self._read().get(team_id)
        found = teams.get(column.casefold()) if isinstance(teams, dict) else None
        return found if isinstance(found, str) else None

    def set(self, team_id: str, column: str, state_id: str | None) -> None:
        """Store an answer; with None, forget it."""
        with _LOCK:
            data = self._read()
            teams = data.get(team_id)
            teams = dict(teams) if isinstance(teams, dict) else {}
            if state_id is None:
                teams.pop(column.casefold(), None)
            else:
                teams[column.casefold()] = state_id
            if teams:
                data[team_id] = teams
            else:
                data.pop(team_id, None)
            _write_json(self.path, data)


def resolve_state(
    config: Config,
    choices: StateChoices,
    team: dict,
    states: list[dict],
    column: str,
    ask: Ask,
    state_name: str | None = None,
    only_type: str | None = None,
) -> dict:
    """The state of `team` for `column`.

    One candidate is used. With several, `state_name` answers, else the stored choice if
    its state is still a candidate, else `ask` does. An answer is stored.
    """
    found = candidates(config, states, column, only_type)
    if not found:
        raise NoStateError(f"Team {team['key']} has no workflow state for column {column!r}")
    names = [s["name"] for s in found]
    if state_name is not None:
        wanted = state_name.casefold()
        chosen = next((s for s in found if s["name"].casefold() == wanted), None)
        if chosen is None:
            raise AmbiguousStateError(
                f"Team {team['key']} has no state {state_name!r} for column {column!r}. "
                f"Candidates: {', '.join(names)}",
                names,
            )
    elif len(found) == 1:
        return found[0]
    else:
        stored = choices.get(team["id"], column)
        chosen = next((s for s in found if s["id"] == stored), None)
        if chosen is not None:
            return chosen
        if stored is not None:
            choices.set(team["id"], column, None)
        chosen = ask(team["key"], column, found)
        if chosen is None:
            raise StateCancelledError("No state chosen")
    if len(found) > 1:
        choices.set(team["id"], column, chosen["id"])
    return chosen
