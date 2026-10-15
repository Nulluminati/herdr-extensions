"""Single Linear issues pinned to a repository board, kept in frontmatter.

The pins live under `linear-issues` as YAML:

    linear-issues:
    - id: <issue id>
      identifier: ENG-123

The board fetches pinned issues by id, because an identifier changes when an issue
moves to another team. The identifier is for people who read the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .linear import LinearError
from .linear_links import _text, frontmatter_list, replace_block
from .model import NotFoundError

if TYPE_CHECKING:
    from .linear_source import LinearSource
    from .store import Store

KEY = "linear-issues"


@dataclass(frozen=True)
class IssuePin:
    id: str
    identifier: str

    def to_yaml(self) -> dict:
        return {"id": self.id, "identifier": self.identifier}


def parse_pins(lines: list[str]) -> list[IssuePin]:
    pins = []
    for index, item in enumerate(frontmatter_list(lines, KEY), start=1):
        where = f"entry {index}"
        pins.append(IssuePin(_text(item, where, "id", KEY), _text(item, where, "identifier", KEY)))
    return pins


def replace_pins(text: str, pins: list[IssuePin]) -> str:
    """`text` with the `linear-issues` block of the frontmatter set to `pins`."""
    return replace_block(text, KEY, [pin.to_yaml() for pin in pins])


def add_pin(store: Store, board: Path, issue: dict) -> tuple[IssuePin, bool]:
    """Pin a Linear issue (a dict with id and identifier) to a board.

    Returns the pin and whether it is new. A second pin of the same issue only refreshes
    its identifier.
    """
    pin = IssuePin(issue["id"], issue["identifier"])
    added = False

    def change(pins: list[IssuePin]) -> list[IssuePin]:
        nonlocal added
        added = not any(p.id == pin.id for p in pins)
        return [*pins, pin] if added else [pin if p.id == pin.id else p for p in pins]

    store.update_pins(board, change)
    return pin, added


def pin_issue(store: Store, source: LinearSource, board: Path, ref: str) -> tuple[IssuePin, bool]:
    """Ask Linear for the issue `ref` (such as ENG-123) and pin it to a board."""
    source.require_active()
    if source.offline:
        raise LinearError("Pinning a Linear issue needs a connection")
    issue = source.client.issue(ref.strip().upper())
    if issue is None:
        raise NotFoundError(f"Linear has no issue {ref!r}")
    return add_pin(store, board, issue)


def unpin_issue(store: Store, board: Path, ref: str) -> IssuePin:
    """Remove the pin of the issue `ref` (an identifier or an issue id) from a board.

    This works offline: it changes only the board file.
    """
    wanted = ref.strip().casefold()
    removed: list[IssuePin] = []

    def change(pins: list[IssuePin]) -> list[IssuePin]:
        removed.extend(p for p in pins if wanted in (p.identifier.casefold(), p.id.casefold()))
        if not removed:
            raise NotFoundError(f"{ref!r} is not pinned to this board")
        return [p for p in pins if p not in removed]

    store.update_pins(board, change)
    return removed[0]
