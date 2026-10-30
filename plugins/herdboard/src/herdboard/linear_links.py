"""Links between a repository board and Linear projects, kept in frontmatter.

The links live under `linear-projects` as YAML:

    linear-projects:
    - id: <project id>
      name: <project name>
      labels:
      - {id: <label id>, name: <label name>}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import FormatError

KEY = "linear-projects"
TOP_LEVEL_KEY = re.compile(r"^([^\s#\-][^:]*):(\s|$)")


@dataclass(frozen=True)
class LabelLink:
    id: str
    name: str


@dataclass
class ProjectLink:
    id: str
    name: str
    labels: list[LabelLink] = field(default_factory=list)

    def to_yaml(self) -> dict:
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


def _key_line(key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(key)}:(\s|$)")


def _yaml():
    """The PyYAML module, or None when it is not installed.

    PyYAML loads only when links are read or written, so the local commands work without it.
    """
    try:
        import yaml
    except ImportError:
        return None
    return yaml


def _frontmatter_data(yaml, lines: list[str]) -> dict:
    """The mapping in frontmatter lines (with both `---` fences); empty when unclosed."""
    if len(lines) < 2:
        return {}
    try:
        data = yaml.safe_load("\n".join(lines[1:-1]))
    except (yaml.YAMLError, ValueError) as error:
        raise FormatError(f"invalid YAML in frontmatter: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise FormatError("frontmatter must be a YAML mapping")
    return data


def frontmatter_list(lines: list[str], key: str) -> list:
    """The YAML list under `key` in frontmatter lines; empty when the key is not there."""
    yaml = _yaml()
    if yaml is None:
        if any(_key_line(key).match(line) for line in lines):
            raise FormatError(f"PyYAML is needed to read {key}: install it with the plugin")
        return []
    raw = _frontmatter_data(yaml, lines).get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FormatError(f"{key} must be a list")
    return raw


def parse_links(lines: list[str]) -> list[ProjectLink]:
    raw = frontmatter_list(lines, KEY)
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


def _block_range(body: list[str], key: str) -> tuple[int, int] | None:
    """Where the block of `key` sits in the frontmatter lines, or None.

    The block is the key line and the lines after it that are indented, blank or list
    items. Blank lines at its end belong to what follows.
    """
    key_line = _key_line(key)
    start = next((i for i, line in enumerate(body) if key_line.match(line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(body) and (
        not body[end].strip() or body[end][0] in " \t" or body[end].startswith("-")
    ):
        end += 1
    while end > start + 1 and not body[end - 1].strip():
        end -= 1
    return start, end


def _require_unique_keys(body: list[str]) -> None:
    seen: set[str] = set()
    for line in body:
        match = TOP_LEVEL_KEY.match(line)
        if match:
            key = match.group(1).strip()
            if key in seen:
                raise FormatError(f"duplicate key {key!r} in frontmatter")
            seen.add(key)


def replace_links(text: str, links: list[ProjectLink]) -> str:
    """`text` with the `linear-projects` block of the frontmatter set to `links`."""
    return replace_block(text, KEY, [link.to_yaml() for link in links])


def replace_block(text: str, key: str, items: list[dict]) -> str:
    """`text` with the block of `key` in the frontmatter set to the YAML list `items`.

    Every other line of the text keeps its bytes and its order. Without a block, the new
    one goes before the closing `---` (before a blank line that ends the frontmatter).
    An empty list removes the block.
    """
    yaml = _yaml()
    if yaml is None:
        raise FormatError(f"PyYAML is needed to write {key}: install it with the plugin")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if not lines or lines[0] != "---" or end is None:
        raise FormatError("frontmatter must be closed")
    _frontmatter_data(yaml, lines[: end + 1])
    body = lines[1:end]
    _require_unique_keys(body)
    block: list[str] = []
    if items:
        dumped = yaml.safe_dump({key: items}, sort_keys=False, allow_unicode=True)
        block = dumped.rstrip("\n").split("\n")
    found = _block_range(body, key)
    if found is not None:
        start, stop = found
    else:
        start = stop = len(body)
        while start > 0 and not body[start - 1].strip():
            start -= 1
        stop = start
    body[start:stop] = block
    return newline.join([lines[0], *body, *lines[end:]])
