"""The card details pane: a compact, read-only status of a card above its agents.

This module collects the data and makes the lines. It does not import Textual, so the
tests need no terminal; `herdboard details` draws the lines in a small Textual app.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.text import Text

from . import activity, attachments
from .brief import _title
from .config import Config
from .linear_cards import LINEAR_REF
from .linear_source import state_dir
from .model import HerdboardError
from .start import REPOLESS, find_record

CHECK_ITEM = re.compile(r"^\s*[-*+] \[(?P<mark>.)\] ")
PR_NUMBER = re.compile(r"/pull/(\d+)")


def details_data(store, config: Config, ref: str, linear_card=None) -> dict:
    """The card, its start record, attachments and log. `linear_card(ref)` looks up a Linear card.

    A card that cannot be read is None, so the pane shows a short line instead of an error.
    """
    card = None
    try:
        if LINEAR_REF.match(ref):
            card = linear_card(ref) if linear_card is not None else None
        else:
            card = store.show(ref, archived=True)
    except HerdboardError:
        card = None
    found = find_record(ref)
    ref = card.ref if card is not None else ref
    data = {"ref": ref, "card": card, "record": found[1] if found else None, "attachments": [], "log": []}
    if activity.LOG_REF.match(ref):
        try:
            data["attachments"] = attachments.listing(config, ref)
            data["log"] = activity.read(config, ref)
        except HerdboardError:
            pass
    return data


def watched_paths(config: Config, ref: str, board_path: Path | None) -> list[Path]:
    """The files whose change redraws the pane: the board, the log, the attachments and the starts."""
    paths = [board_path] if board_path is not None else []
    if activity.LOG_REF.match(ref):
        paths += [activity.log_path(config, ref), attachments.folder(config, ref)]
    return [*paths, state_dir() / "starts.json"]


def signature(paths: list[Path]) -> tuple:
    """The modification times of `paths`; a missing file counts as None."""
    times = []
    for path in paths:
        try:
            times.append(path.stat().st_mtime_ns)
        except OSError:
            times.append(None)
    return tuple(times)


def _cut(text: Text, width: int) -> Text:
    text.truncate(width, overflow="ellipsis")
    return text


def _card_line(card, width: int) -> Text:
    """ID, title, column pill, and the type at the right. The title gives way first."""
    column = card.state if card.source == "linear" and card.state else card.column
    pill = f" {column} "
    kind = card.type or ""
    title = " ".join(_title(card).split())
    for right in ((kind,) if kind else ()) + ("",):
        fixed = len(card.ref) + len(" │ ") + 1 + len(pill) + (1 + len(right) if right else 0)
        room = width - fixed
        if room >= min(len(title), 8) or not right:
            break
    line = Text()
    line.append(card.ref, style="bold cyan")
    line.append(" │ ", style="dim")
    shown = Text(title)
    if room < len(title):
        _cut(shown, max(room, 1))
    line.append_text(shown)
    line.append(" ")
    line.append(pill, style="bold black on yellow")
    if right:
        line.append(" " * max(width - len(line) - len(right), 1))
        line.append(right, style="dim")
    return _cut(line, width)


def _work_line(data: dict, width: int) -> Text:
    record = data["record"]
    if record is None:
        return _cut(Text(f"no start record for {data['ref']}", style="dim"), width)
    agent = " ".join(part for part in (record.get("agent"), record.get("model")) if part)
    if record.get("thinking"):
        agent += f" ({record['thinking']})"
    parts = [agent] if agent else []
    if record.get("mode") == REPOLESS:
        parts.append("no repository")
    elif record.get("branch"):
        parts.append(f"{record['branch']} → {record['base']}" if record.get("base") else record["branch"])
    number = PR_NUMBER.search(record.get("pr_url") or "")
    if number:
        parts.append(" ".join(part for part in (f"PR #{number.group(1)}", record.get("pr_state")) if part))
    return _cut(Text(" · ".join(parts), style="dim"), width)


def _files_line(card, files: list[dict], width: int) -> Text | None:
    marks = [match.group("mark") for match in map(CHECK_ITEM.match, card.body) if match]
    line = Text()
    if marks:
        line.append("☑ ", style="green")
        line.append(f"{sum(mark != ' ' for mark in marks)}/{len(marks)}")
    for entry in files:
        if line:
            line.append("  ")
        line.append(entry["name"])
    return _cut(line, width) if line else None


def _log_rule(width: int) -> Text:
    return _cut(Text("── log " + "─" * max(width - 7, 0), style="dim"), width)


def _log_lines(entries: list[dict], width: int) -> list[Text]:
    if not entries:
        return [Text("no log entries", style="dim")]
    author_width = max(len(entry["author"]) for entry in entries)
    lines = []
    for entry in entries:
        line = Text()
        line.append(entry["time"][5:], style="dim")
        line.append("  ")
        line.append(entry["author"].ljust(author_width), style="cyan")
        line.append("  ")
        line.append(entry["message"].split("\n", 1)[0])
        lines.append(_cut(line, width))
    return lines


def details_lines(data: dict, width: int, height: int) -> list[Text]:
    """The pane's lines for `width` columns and `height` rows: newest log entries at the bottom."""
    card = data["card"]
    if card is None:
        return [_cut(Text(f"no card {data['ref']}", style="dim"), width)]
    lines = [_card_line(card, width), _work_line(data, width)]
    files = _files_line(card, data["attachments"], width)
    if files is not None:
        lines.append(files)
    room = height - len(lines) - 1
    if room > 0:
        entries = data["log"][-room:]
        lines += [_log_rule(width), *_log_lines(entries, width)]
    return lines[:max(height, 1)]
