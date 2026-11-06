"""The brief an agent gets when `start` hands it a card, and the card's sidebar token."""

from __future__ import annotations

import unicodedata
from fnmatch import fnmatch
from pathlib import Path

from .attachments import PLAN_NAME
from .model import commit_type
from .store import CardView

# Longest sidebar token; Herdr's sidebar is narrow.
TOKEN_WIDTH = 60


def _one_line(text: str) -> str:
    """Text without control characters, so it is safe as one shell argument."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)


FACTORY_RULE = (
    "- This card runs as a factory. When you move it to Review, the card stays in In progress: reviewers "
    "check your work and send you their findings. herdboard moves the card to Review when every round "
    "is done. Move it to Review only when the work is complete and committed."
)


def brief_text(
    card: CardView,
    *,
    repository: str,
    worktree: str,
    branch: str,
    base: str,
    command: str,
    note: str | None = None,
    log: list[dict] = (),
    plan: str | None = None,
    attachments: list[dict] = (),
    repoless: bool = False,
    attachments_dir: str | None = None,
    may_merge_prs: bool = False,
    factory: bool = False,
) -> str:
    """A Markdown brief: the task, where to work, and how to keep the card current.

    Without a repository (`repoless`), the card folder is the whole workspace and its output
    goes in the card's attachments: herdboard deletes the folder at finish. With `may_merge_prs`
    (an automation that allows it), the agent may merge other pull requests that need no change.
    """
    linear = card.source == "linear"
    ref = card.ref
    lines = [f"# {card.title}", ""]
    if linear:
        lines.append(f"You are working on Linear issue `{ref}` (board `{card.board}`, column `{card.column}`).")
    else:
        lines.append(f"You are working on herdboard card `{ref}` on board `{card.board}`, column `{card.column}`.")
    lines.append("")
    if note:
        lines += [note, ""]
    facts = []
    if linear:
        facts += [
            f"Link: {card.url}" if card.url else "",
            f"State: {card.state}" if card.state else "",
            f"Team: {card.team}" if card.team else "",
        ]
    if card.type:
        facts.append(f"Type: {card.type}")
    if card.tags:
        facts.append(("Labels: " if linear else "Tags: ") + ", ".join(card.tags))
    if card.due:
        facts.append(f"Due: {card.due}")
    facts = [fact for fact in facts if fact]
    if facts:
        lines += [f"- {fact}" for fact in facts] + [""]

    if repoless:
        lines += [
            "## Where to work",
            "",
            f"- Folder: `{worktree}`",
            f"- Attachments: `{attachments_dir or worktree}`",
            "",
            "This card has no repository: this folder is the whole workspace. There is no branch and no "
            "checkout. herdboard deletes the folder when the card is finished, so copy everything worth "
            "keeping into the attachments folder.",
            "",
        ]
    else:
        lines += [
            "## Where to work",
            "",
            f"- Repository: `{repository}`",
            f"- Worktree: `{worktree}`",
            f"- Branch: `{branch}`",
            f"- Base: `{base}`. herdboard merges the branch into it after the review.",
            "",
            "Work only in this worktree. Follow the repository's own agent instructions (AGENTS.md or CLAUDE.md).",
            "",
        ]

    body = list(card.body)
    while body and not body[-1].strip():
        body.pop()
    lines += ["## Description" if linear else "## Details", ""]
    if any(line.strip() for line in body):
        lines += body
    else:
        lines.append("None. The title is the whole task.")
    lines.append("")

    if plan and plan.strip():
        lines += [
            "## Plan",
            "",
            "You and the user agreed on this plan before the work started. Follow it. Log each change to it.",
            "",
            plan.strip("\n"),
            "",
        ]
    # The plan is in the brief already, so only the other attachments are listed.
    others = [entry for entry in attachments if entry["name"] != PLAN_NAME]
    if others:
        lines += ["## Attachments", "", "Read them when you need them.", ""]
        lines += [f"- `{entry['path']}`" for entry in others] + [""]
        if any(fnmatch(entry["name"], "mockup-*.png") for entry in others):
            lines += [
                "Files named `mockup-*.png` are UI mockups that the user approved. Open them before you build the UI.",
                "",
            ]

    if log:
        lines += ["## Activity log", ""]
        for entry in log:
            first, *rest = entry["message"].split("\n")
            lines.append(f"- {entry['time']} · {entry['author']} · {first}")
            lines += [f"\t{line}" for line in rest]
        lines.append("")
    if may_merge_prs:
        lines += [
            "## GitHub pull requests",
            "",
            "This task allows you to merge pull requests with `gh pr merge`. Merge a pull request only when "
            "all its checks pass and it needs no code change. Leave every other pull request open: one that "
            "needs a code change, an answer to a review comment, or a conflict fix.",
            "",
            f'Log each pull request that you merged or left, and why: `{command} --json log {ref} "..."`',
            "",
        ]
    lines += [
        "## Keep the card up to date",
        "",
        "Use the herdboard CLI for every card change. Do not edit the board files in `~/todos`.",
        "",
        f"- Read the card again: `{command} --json show {ref}`",
    ]
    lines.append(
        f'- Record progress, decisions and blockers in the card\'s activity log: `{command} --json log {ref} "..."`'
    )
    lines.append("- Do not change the card title or details. They are the user's text.")
    if linear:
        lines.append("- Ask the user before you change the Linear issue: its state, title, description or comments.")
    if repoless:
        lines += [
            "",
            "## Finish the work",
            "",
            f"- Write the result to the attachments folder: `{attachments_dir or worktree}`. "
            "Name each file clearly, for example `findings.md`.",
            f'- Record what you did with `{command} --json log {ref} "..."`.',
            f"- When the work is ready for the user to check, run `{command} --json move {ref} Review`",
            f'- When review feedback arrives, run `{command} --json move {ref} "In progress"`. '
            "Fix, and move the card back to Review.",
            "- Do not move the card to Done. The user does that after the review.",
            "",
        ]
        return "\n".join(lines)
    lines += [
        "",
        "## Finish the work",
        "",
        f"- Commit every change on the branch `{branch}`. Use conventional commit messages.",
        *(
            [
                f"- The card is a {card.type}: use `{commit_type(card.type)}` as the conventional commit type "
                "of the main change."
            ]
            if card.type
            else []
        ),
        f"- When the work is ready for the user to check, commit it, then run `{command} --json move {ref} Review`",
        *([FACTORY_RULE] if factory else []),
        f'- When review feedback arrives, run `{command} --json move {ref} "In progress"`. '
        "Fix, commit, and move the card back to Review.",
        (
            "- Do not push or merge your branch, rebase it onto another branch, or remove the worktree. "
            "herdboard does that."
            if may_merge_prs
            else "- Do not push, merge, rebase onto another branch, or remove the worktree. herdboard does that."
        ),
        "- Do not move the card to Done. The user does that after the review.",
        "",
    ]
    return "\n".join(lines)


def agent_message(card: CardView, brief: Path) -> str:
    """The agent's first message: one line, because Herdr rejects line breaks in arguments."""
    return _one_line(
        f"Start work on herdboard card {card.ref}: {card.title}. "
        f"Read the brief at {brief} first; it has the details and how to update the card."
    )


def card_token(card: CardView) -> str:
    """The sidebar text for the card an agent works on: `▶ title · column`."""
    mark = "✓ " if card.checked else "▶ "
    separator = " · "
    title = " ".join(_one_line(card.title).split())
    # The title keeps up to half the token, so a long column name cannot push it out.
    column_room = TOKEN_WIDTH - len(mark) - len(separator) - min(len(title), TOKEN_WIDTH // 2)
    ending = separator + _cut(card.column, column_room)
    title = _cut(title, TOKEN_WIDTH - len(mark) - len(ending))
    return f"{mark}{title}{ending}"


def _cut(text: str, room: int) -> str:
    """`text`, cut to `room` cells with an `…` when it is longer."""
    if len(text) <= room:
        return text
    return text[: room - 1].rstrip() + "…"
