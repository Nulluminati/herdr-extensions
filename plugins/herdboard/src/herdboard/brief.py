"""The brief an agent gets when `start` hands it a card, and the card's sidebar token."""

from __future__ import annotations

import json
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


def width(text: str) -> int:
    """The text's size on screen in cells: 2 for wide characters, 0 for combining ones."""
    return sum(
        0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


FACTORY_RULE = (
    "- This card runs as a factory. When you move it to Review, the card stays in In progress: reviewers "
    "check your work and send you their findings. herdboard moves the card to Review when every round "
    "is done. Move it to Review only when the work is complete and committed."
)


def evidence_text(evidence: dict, command: str, ref: str) -> list[str]:
    """How a factory builder records the evidence for the plan's verification contract.

    `evidence` has the run's `folder` (the evidence folder), `run` (its identifier) and `contract_digest`."""
    folder = evidence["folder"]
    example = json.dumps(
        {
            "version": 1,
            "run": evidence["run"],
            "contract_digest": evidence["contract_digest"],
            "commit": "<the full output of git rev-parse HEAD>",
            "results": [
                {
                    "check": "<check id from the contract>",
                    "status": "passed",
                    "action": "<what you ran or did>",
                    "observed": "<what you saw, against the check's expected result>",
                    "started": "2026-01-01T10:00:00+00:00",
                    "finished": "2026-01-01T10:01:00+00:00",
                    "exit_code": 0,
                    "evidence": ["<check id>.txt"],
                }
            ],
        },
        indent=2,
    )
    return [
        "## Verification evidence",
        "",
        "This factory run follows the `herdboard-verification` contract in the plan. The approver cannot approve "
        "the work without current evidence for each check of the contract.",
        "",
        "Before each move to Review:",
        "",
        "1. Commit all your work. The worktree must be clean: no uncommitted changes and no untracked files.",
        "2. Run every check of the contract on the final commit, as its `action` says, on its `surface`. Follow "
        "its `isolation` and `cleanup`. Clean up only what you created.",
        f"3. Save the real output of each check as a file in `{folder}`, such as `unit.txt`. Remove secrets, "
        "such as tokens, before you save output. Do not put evidence in the repository.",
        f"4. Write `{folder}/verification.json`, with one result for each check:",
        "",
        *(f"    {line}" for line in example.splitlines()),
        "",
        "- `status` is passed, failed, blocked or not_applicable. `started` and `finished` are ISO 8601 times "
        "with a time zone.",
        "- For a command, record its exit code in `exit_code`. An exit code of 0 alone does not prove the check: "
        "`observed` must show the check's `expected` result.",
        "- `evidence` names files in the evidence folder. A passed check needs at least one.",
        "- A passing result describes the final commit. Keep earlier output, such as a failing test before the "
        "fix or a reproduction of a defect, as more evidence files, but not as the final result.",
        "- You cannot change the contract or exempt a check. For a check that cannot run here, write status "
        "blocked and say why in `observed`. The approver decides.",
        "- herdboard checks that the report is complete, current and consistent. It does not prove that you ran "
        "a command, so record exactly what happened.",
        f'- When the plan must change, stop and log why: `{command} --json log {ref} "..."`. A changed plan '
        "stops approval: the user restarts the factory with the new plan.",
        "",
    ]


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
    evidence: dict | None = None,
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
    if factory and evidence:
        lines += evidence_text(evidence, command, ref)
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
    column_room = TOKEN_WIDTH - width(mark) - width(separator) - min(width(title), TOKEN_WIDTH // 2)
    ending = separator + _cut(card.column, column_room)
    title = _cut(title, TOKEN_WIDTH - width(mark) - width(ending))
    return f"{mark}{title}{ending}"


def _cut(text: str, room: int) -> str:
    """`text`, cut to `room` cells with an `…` when it is longer."""
    if width(text) <= room:
        return text
    kept = ""
    filled = 0
    for ch in text:
        cells = width(ch)
        if filled + cells > room - 1:
            break
        kept += ch
        filled += cells
    return kept.rstrip() + "…"
