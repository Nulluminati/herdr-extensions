"""Links between a repository board and Linear projects, kept in `board.toml`.

The links are an array of tables under `linear-projects`:

    [[linear-projects]]
    id = "<project id>"
    name = "<project name>"
    labels = [{ id = "<label id>", name = "<label name>" }]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import FormatError

KEY = "linear-projects"


@dataclass(frozen=True)
class LabelLink:
    id: str
    name: str


@dataclass
class ProjectLink:
    id: str
    name: str
    labels: list[LabelLink] = field(default_factory=list)

    def to_dict(self) -> dict:
        data: dict = {"id": self.id, "name": self.name}
        if self.labels:
            data["labels"] = [{"id": label.id, "name": label.name} for label in self.labels]
        return data


def with_link(links: list[ProjectLink], link: ProjectLink) -> list[ProjectLink]:
    """`links` with `link` added last, or in the place of the link to the same project."""
    kept = [other for other in links if other.id != link.id]
    position = next((i for i, other in enumerate(links) if other.id == link.id), len(kept))
    return kept[:position] + [link] + kept[position:]


def _text(item: object, where: str, key: str, block: str = KEY) -> str:
    value = item.get(key) if isinstance(item, dict) else None
    if not isinstance(value, str) or not value:
        raise FormatError(f"{block}: {where} needs a text value for {key!r}")
    return value


def table_list(raw: object, key: str) -> list:
    """The array of tables under `key` as plain values; an empty list for None."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FormatError(f"{key} must be an array of tables")
    return raw


def parse_links(raw: list | None) -> list[ProjectLink]:
    """The project links in the `linear-projects` tables of a board."""
    raw = table_list(raw, KEY)
    links = []
    for index, item in enumerate(raw, start=1):
        where = f"entry {index}"
        labels_raw = item.get("labels") if isinstance(item, dict) else None
        if labels_raw is not None and not isinstance(labels_raw, list):
            raise FormatError(f"{KEY}: {where} labels must be a list")
        labels = [
            LabelLink(_text(label, f"{where} label", "id"), _text(label, f"{where} label", "name"))
            for label in labels_raw or []
        ]
        links.append(ProjectLink(_text(item, where, "id"), _text(item, where, "name"), labels))
    return links
