"""The brief an agent gets when `start` hands it a card, and the card's sidebar token."""

from __future__ import annotations

from pathlib import Path
import unicodedata

from .model import split_title
from .store import CardView

# Longest sidebar token; Herdr's sidebar is narrow.
TOKEN_WIDTH = 60


def _title(card: CardView) -> str:
    """The card's text without tags and the due date. Linear titles have neither."""
    if card.source == "linear":
        return card.title
    return split_title(card.title)[0] or card.title


def _unindent(line: str) -> str:
    """A todo detail line without the one indent level that ties it to the card."""
    if line.startswith("\t"):
        return line[1:]
    if line.startswith("    "):
        return line[4:]
    return line.lstrip(" ")


def _one_line(text: str) -> str:
    """Text without control characters, so it is safe as one shell argument."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)


def brief_text(
    card: CardView,
    *,
    notes: list[dict],
    repository: str,
    worktree: str,
    branch: str,
    command: str,
) -> str:
    """A Markdown brief: the task, where to work, and how to keep the card current."""
    linear = card.source == "linear"
    ref = card.ref
    lines = [f"# {_title(card)}", ""]
    if linear:
        lines.append(f"You are working on Linear issue `{ref}` (board `{card.board}`, column `{card.column}`).")
    else:
        lines.append(f"You are working on herdboard card `{ref}` on board `{card.board}`, column `{card.column}`.")
    lines.append("")
    facts = []
    if linear:
        facts += [f"Link: {card.url}" if card.url else "", f"State: {card.state}" if card.state else "",
                  f"Team: {card.team}" if card.team else ""]
    if card.tags:
        facts.append(("Labels: " if linear else "Tags: ") + ", ".join(card.tags))
    if card.due:
        facts.append(f"Due: {card.due}")
    facts = [fact for fact in facts if fact]
    if facts:
        lines += [f"- {fact}" for fact in facts] + [""]

    lines += [
        "## Where to work",
        "",
        f"- Repository: `{repository}`",
        f"- Worktree: `{worktree}`",
        f"- Branch: `{branch}`",
        "",
        "Work only in this worktree. Follow the repository's own agent instructions (AGENTS.md or CLAUDE.md).",
        "",
    ]

    body = list(card.body) if linear else [_unindent(line) for line in card.body]
    while body and not body[-1].strip():
        body.pop()
    lines += ["## Description" if linear else "## Details", ""]
    if any(line.strip() for line in body):
        lines += body
    else:
        lines.append("None. The title is the whole task.")
    lines.append("")

    if notes:
        lines += ["## Linked notes", ""]
        for note in notes:
            content = note["content"]
            lines += [f"### {note['name']}", ""]
            lines += [content.rstrip("\n") if content is not None else "The note was not found.", ""]

    lines += [
        "## Keep the card up to date",
        "",
        "Use the herdboard CLI for every card change. Do not edit the board files in `~/todos`.",
        "",
        f"- Read the card again: `{command} --json show {ref}`",
    ]
    if linear:
        lines.append("- Ask the user before you change the Linear issue: its state, title, description or comments.")
    else:
        lines.append(
            f'- Record progress or a blocker in the details: `{command} --json edit {ref} --body "..."`. '
            "This replaces every detail line, so read the card first and keep the lines you want."
        )
    lines += [
        f"- When the work is ready for the user to check: `{command} --json move {ref} Review`",
        "- Do not move the card to Done. The user does that after the review.",
        "",
    ]
    return "\n".join(lines)


def agent_message(card: CardView, brief: Path) -> str:
    """The agent's first message: one line, because Herdr rejects line breaks in arguments."""
    return _one_line(
        f"Start work on herdboard card {card.ref}: {_title(card)}. "
        f"Read the brief at {brief} first; it has the details and how to update the card."
    )


def card_token(card: CardView) -> str:
    """The sidebar text for the card an agent works on: `▶ title · column`."""
    mark = "✓ " if card.checked else "▶ "
    ending = f" · {card.column}"
    title = " ".join(_one_line(_title(card)).split())
    room = TOKEN_WIDTH - len(mark) - len(ending)
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return f"{mark}{title}{ending}"
