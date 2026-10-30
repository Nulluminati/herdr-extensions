"""Turn Linear issues into card views."""

from __future__ import annotations

import re
from datetime import datetime

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


def type_and_labels(config: Config, labels: list[str]) -> tuple[str | None, list[str]]:
    """The first label that names a configured type, as that type, and the other labels."""
    types = {kind.casefold(): kind for kind in config.types}
    for index, label in enumerate(labels):
        if label.casefold() in types:
            return types[label.casefold()], labels[:index] + labels[index + 1 :]
    return None, labels


def has_pull_request(urls: list[str], identities: set[str]) -> bool:
    """True when a URL is a pull request of one of the `host/owner/repo` identities."""
    wanted = {i.lower() for i in identities if i.lower().startswith("github.com/")}
    for url in urls:
        match = PULL_URL.match(url)
        if match and f"github.com/{match.group(1)}/{match.group(2)}".lower() in wanted:
            return True
    return False


def attachment_links(attachments: list) -> list[dict]:
    """The title, subtitle and URL of each attachment. A cache from before titles holds only URLs."""
    links = []
    for entry in attachments:
        if isinstance(entry, str):
            entry = {"url": entry}
        links.append(
            {"title": entry.get("title") or entry["url"], "subtitle": entry.get("subtitle"), "url": entry["url"]}
        )
    return links


def issue_card(
    config: Config,
    issue: dict,
    identities: set[str],
    fetched_at: datetime,
    stale: bool,
    show_canceled: bool = False,
) -> CardView | None:
    """The card for an issue; None for a canceled issue unless `show_canceled`."""
    state = issue["state"]
    if state["type"] == "canceled" and not show_canceled:
        return None
    project = issue.get("project")
    parent = issue.get("parent")
    kind, labels = type_and_labels(config, [label["name"] for label in issue["labels"]])
    links = attachment_links(issue.get("attachments", []))
    return CardView(
        ref=issue["identifier"],
        issue_id=issue.get("id"),
        board=project["name"] if project else DEFAULT_BOARD,
        path=None,
        column=column_for(config, state),
        title=issue["title"],
        checked=state["type"] == "completed",
        due=issue.get("dueDate"),
        tags=labels,
        type=kind,
        body=(issue.get("description") or "").splitlines(),
        source="linear",
        url=issue.get("url"),
        state=state["name"],
        team=issue["team"]["key"] if issue.get("team") else None,
        pr=has_pull_request([link["url"] for link in links], identities),
        linear_attachments=links,
        sort_order=issue.get("sortOrder"),
        read_only=True,
        fetched_at=fetched_at.isoformat(),
        stale=stale,
        parent=parent["identifier"] if parent else None,
    )
