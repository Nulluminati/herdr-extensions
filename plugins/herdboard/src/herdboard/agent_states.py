"""The Herdr status of each agent pane that works on a card, for the board's glyphs.

One `herdr agent list` call gives the status of every agent pane. The start and plan records name
the panes of a card's work agent, planner, agent reviewer and pull request writer. Agent status is
optional: a Herdr or state-file error shows no status and no error.
"""

from __future__ import annotations

from . import plan, start
from .model import HerdboardError


def pane_statuses() -> dict[str, str]:
    """Each agent pane's Herdr status, by pane ID. Empty when Herdr fails."""
    try:
        agents = start.herdr("agent", "list").get("agents")
    except HerdboardError:
        return {}
    if not isinstance(agents, list):
        return {}
    return {agent["pane_id"]: agent["agent_status"] for agent in agents
            if isinstance(agent, dict) and isinstance(agent.get("pane_id"), str)
            and isinstance(agent.get("agent_status"), str)}


def _records(path) -> dict:
    try:
        # Writers replace the files atomically; this optional status needs no read lock.
        return start._read(path)
    except HerdboardError:
        return {}


def _pane(record) -> str | None:
    pane = record.get("pane_id") if isinstance(record, dict) else None
    return pane if isinstance(pane, str) and pane else None


def mark(cards) -> None:
    """Set the board-only Herdr status of each agent on the cards. Run it after `start.mark_removed`."""
    starts = _records(start.state_dir() / "starts.json")
    plans = _records(plan._plans_path())
    panes = {}
    for card in cards:
        record = starts.get(card.ref)
        if not isinstance(record, dict):
            record = next((r for r in starts.values() if isinstance(r, dict) and r.get("ref") == card.ref), {})
        panes[card.ref] = (_pane(record) if card.worktree else None, _pane(plans.get(card.ref)),
                           _pane(record.get("agent_review")), _pane(record.get("pr_write")))
    # A board without agent panes does not ask Herdr.
    statuses = pane_statuses() if any(any(found) for found in panes.values()) else {}
    for card in cards:
        card.agent_status, card.planning_status, card.review_status, card.pr_write_status = (
            statuses.get(pane) for pane in panes[card.ref])
