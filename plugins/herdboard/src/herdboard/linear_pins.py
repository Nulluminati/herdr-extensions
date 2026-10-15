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

from .linear_links import _text, frontmatter_list, replace_block

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
