"""Turn Linear issues into card views."""

from __future__ import annotations

from datetime import datetime
import re

from .config import Config
from .store import CardView

PULL_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/", re.IGNORECASE)
LINEAR_REF = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
DEFAULT_BOARD = "Linear"


def column_for(config: Config, state: dict) -> str:
    """The column of a workflow state: its name first, then its type."""
    names = {name.casefold(): column for name, column in config.linear.state_names.items()}
    column = names.get(state["name"].casefold())
    return column or config.linear.columns.get(state["type"]) or state["name"]


def has_pull_request(urls: list[str], identities: set[str]) -> bool:
    """True when a URL is a pull request of one of the `host/owner/repo` identities."""
    wanted = {i.lower() for i in identities if i.lower().startswith("github.com/")}
    for url in urls:
        match = PULL_URL.match(url)
        if match and f"github.com/{match.group(1)}/{match.group(2)}".lower() in wanted:
            return True
    return False


def issue_card(
    config: Config, issue: dict, identities: set[str], fetched_at: datetime, stale: bool
) -> CardView | None:
    """The card for an issue; None for a canceled issue."""
    state = issue["state"]
    if state["type"] == "canceled":
        return None
    project = issue.get("project")
    return CardView(
        ref=issue["identifier"],
        board=project["name"] if project else DEFAULT_BOARD,
        path=None,
        column=column_for(config, state),
        title=issue["title"],
        checked=state["type"] == "completed",
        due=issue.get("dueDate"),
        tags=[label["name"] for label in issue["labels"]],
        body=(issue.get("description") or "").splitlines(),
        source="linear",
        url=issue.get("url"),
        state=state["name"],
        team=issue["team"]["key"] if issue.get("team") else None,
        pr=has_pull_request(issue.get("attachments", []), identities),
        read_only=True,
        fetched_at=fetched_at.isoformat(),
        stale=stale,
    )
