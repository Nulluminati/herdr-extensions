"""Plan a card with an agent before the work starts.

The planner runs in a new Herdr tab in the repository's main checkout. It reads the code, asks the
user about the unknowns, and writes the plan as the card's `plan.md` attachment. The agent that later
does the work gets the plan in its brief. A background worker logs each save of the plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from . import activity, attachments
from .agent_review import _reviewer_status, start_worker
from .brief import _one_line, _title, _unindent
from .config import THINKING_LEVELS
from .linear_cards import LINEAR_REF, issue_card
from .model import HerdboardError, NotFoundError
from .start import (Starter, StartError, _lock, _read, _write, find_record, herdboard_command, herdr, safe_key,
                    state_dir, write_brief)
from .store import TEMP_REF

# A planning talk with the user can take hours; the worker stops watching after this.
PLAN_TIMEOUT_SECONDS = 8 * 60 * 60
POLL_SECONDS = 5

PLAN_FORMAT = """    # Plan: <title>

    ## Goal
    What the work achieves, and for whom. Two or three sentences.

    ## Decisions
    - Each decision the user made, and why.

    ## Steps
    1. One bounded change per step, with the files or modules it touches.

    ## Testing
    The tests to write, and how to check the change by hand.

    ## Out of scope
    What the work does not do.

    ## Open questions
    What is still unknown. Write "None." when nothing is.
"""


class PlanError(HerdboardError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def planner_arguments(kind: str, prompt: str, folder: Path, model: str | None,
                      thinking: str | None) -> list[str]:
    """The planner's arguments after Herdr's `--`: it reads the repository and writes only the attachments.

    Claude keeps its default permission mode, because the user is in the tab to answer its prompts."""
    if kind == "pi":
        options = (["--model", model] if model else []) + (["--thinking", thinking] if thinking else [])
        return ["--tools", "read,grep,find,ls,write,edit", *options, "--", prompt]
    # The prompt goes first, and --allowedTools last, because --allowedTools takes every value after it.
    return [prompt, *(["--model", model] if model else []),
            "--allowedTools", "Read", "Grep", "Glob", f"Edit(/{folder}/**)"]


def brief_text(card, *, checkout: Path, notes: list[dict], plan: str | None, files: list[dict],
               log: list[dict], plan_file: Path, command: str) -> str:
    linear = card.source == "linear"
    lines = [f"# Plan: {_title(card)}", "",
             f"Plan {'Linear issue' if linear else 'herdboard card'} `{card.ref}` (board `{card.board}`, "
             f"column `{card.column}`) with the user. You do not do the work. A later agent does the work "
             "from your plan, and it does not see this conversation.", "",
             f"- Repository checkout: `{checkout}`. Read it. Do not change the code.",
             f"- The plan: `{plan_file}`", ""]
    if linear and card.url:
        lines += [f"- Link: {card.url}", ""]
    body = list(card.body) if linear else [_unindent(line) for line in card.body]
    while body and not body[-1].strip():
        body.pop()
    lines += ["## Description" if linear else "## Details", ""]
    lines += body if any(line.strip() for line in body) else ["None. The title is the whole task."]
    lines.append("")
    for note in notes:
        content = note["content"]
        lines += [f"## Linked note: {note['name']}", "",
                  content.rstrip("\n") if content is not None else "The note was not found.", ""]
    if plan and plan.strip():
        lines += ["## The current plan", "", "A plan exists. Revise it with the user.", "", plan.strip("\n"), ""]
    others = [entry for entry in files if entry["name"] != attachments.PLAN_NAME]
    if others:
        lines += ["## Attachments", "", *(f"- `{entry['path']}`" for entry in others), ""]
    if log:
        lines += ["## Activity log", ""]
        for entry in log:
            first, *rest = entry["message"].split("\n")
            lines.append(f"- {entry['time']} · {entry['author']} · {first}")
            lines += [f"\t{line}" for line in rest]
        lines.append("")
    lines += [
        "## How to plan",
        "",
        "1. Read the card, its attachments and the code that the work touches.",
        "2. Make a list of the unknowns: unclear behavior, choices between approaches, edge cases, limits.",
        "3. Ask the user about them. Ask a few questions at a time. Give the options and recommend one.",
        "4. Write the plan when the main questions have answers. Write it again each time a decision changes.",
        "5. Tell the user when the plan is saved. herdboard records each save in the card's log.",
        "",
        f"Write the plan to `{plan_file}` and to no other file. Do not change the code. "
        "Do not change the card title or details: they are the user's text.",
        f"Read the card again with `{command} --json show {card.ref}`.",
        "",
        "## Plan format",
        "",
        "Write for an agent that has not seen this conversation. Be specific: name files, functions and commands.",
        "",
        PLAN_FORMAT,
    ]
    return "\n".join(lines)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _plans_path() -> Path:
    return state_dir() / "plans.json"


class Planner:
    def __init__(self, store, directory, writer=None, prompt_checkout=None, repository_view=True,
                 config_file: Path | None = None):
        self.store = store
        self.config = store.config
        self.directory = Path(directory)
        self.writer = writer
        self.prompt_checkout = prompt_checkout
        self.repository_view = repository_view
        self.config_file = config_file

    def _card(self, ref):
        """The card, and the Linear issue when it is one. A Linear card needs a connection."""
        if LINEAR_REF.match(ref):
            if self.writer is None:
                raise StartError("Linear writes are unavailable", "linear_disabled")
            with self.writer.online() as client:
                issue = client.issue(ref.upper())
            if issue is None:
                raise NotFoundError(f"No card {ref}")
            return issue_card(self.config, issue, set(), datetime.now(timezone.utc), False, show_canceled=True), issue
        card = self.store.show(ref)
        if TEMP_REF.match(card.ref):
            card = self.store.ensure_id(card.ref)
        return card, None

    def plan(self, ref, agent=None, model=None, thinking=None, repo=None, checkout=None):
        kind = agent or self.config.start_agent
        if kind not in ("claude", "pi"):
            raise PlanError("The planner must be claude or pi", "usage")
        if thinking and kind != "pi":
            raise PlanError("--thinking works only with pi", "usage")
        if thinking and thinking not in THINKING_LEVELS:
            raise PlanError(f"Thinking must be one of {', '.join(THINKING_LEVELS)}", "usage")
        if model is not None and (not model or any(ch.isspace() for ch in model)):
            raise PlanError("The model must be one name, such as opus or zai/glm-5.3", "usage")
        if kind == "pi":
            thinking = thinking or self.config.start_thinking
        card, issue = self._card(ref)
        ref = card.ref
        starter = Starter(self.store, self.directory, writer=self.writer, prompt_checkout=self.prompt_checkout,
                          repository_view=self.repository_view)
        identity = starter._repository(card, repo, issue)
        found = find_record(ref)
        local = starter._checkout(identity, checkout, found[1] if found else None)
        with _lock(".plan-" + safe_key(ref) + ".lock"):
            with _lock(wait=2):
                running = _read(_plans_path()).get(ref)
            # A planner whose pane is gone, after a worker that died, does not block a new one.
            if isinstance(running, dict) and _reviewer_status(running["pane_id"]) is not None:
                raise PlanError(f"A planner for {ref} still runs in pane {running['pane_id']}", "plan_running")
            plan_file = attachments.plan_path(self.config, ref)
            notes = []
            if card.source != "linear":
                try:
                    notes = self.store.notes_for(card) or []
                except HerdboardError:
                    notes = []
            brief = write_brief(safe_key(ref) + "-plan", brief_text(
                card, checkout=local, notes=notes, plan=attachments.plan_text(self.config, ref),
                files=attachments.listing(self.config, ref), log=activity.read(self.config, ref),
                plan_file=plan_file, command=herdboard_command()))
            prompt = _one_line(f"Plan herdboard card {ref}: {_title(card)}. Read the brief at {brief} first; "
                               "it has the card and how to plan it with me.")
            pane = herdr("tab", "create", "--cwd", str(local), "--label", f"plan {ref}",
                         "--focus")["root_pane"]["pane_id"]
            # A startup prompt (such as Claude's folder trust check) waits for the user: the worker watches it.
            try:
                herdr("agent", "start", f"hb-{ref.lower()}-plan", "--kind", kind, "--pane", pane, "--",
                      *planner_arguments(kind, prompt, plan_file.parent, model, thinking),
                      tolerated=("timeout", "agent_not_ready"))
            except HerdboardError:
                herdr("pane", "close", pane, tolerated=("pane_not_found",))
                raise
            with _lock(wait=2):
                records = _read(_plans_path())
                records[ref] = {"pane_id": pane, "kind": kind, "model": model, "started": time.time(),
                                "plan_mtime": _mtime(plan_file)}
                _write(_plans_path(), records)
        activity.append(self.store, ref, f"Planning started: {kind}{f' ({model})' if model else ''}.", "herdboard")
        config = ["--config", str(self.config_file)] if self.config_file else []
        log = state_dir() / "plans" / f"{safe_key(ref)}.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            start_worker([*config, "plan-wait", ref], log)
        except OSError as error:
            # The planner still works without the worker; only the log of its saves is missing.
            activity.append(self.store, ref, f"The plan saves are not logged: the worker did not start: {error}",
                            "herdboard")
        return {"ref": ref, "agent": kind, "model": model, "pane_id": pane, "checkout": str(local),
                "plan": str(plan_file)}


def _forget(ref: str, pane: str) -> None:
    with _lock(wait=2):
        records = _read(_plans_path())
        if isinstance(records.get(ref), dict) and records[ref].get("pane_id") == pane:
            del records[ref]
            _write(_plans_path(), records)


def wait(store, ref) -> int:
    """The background worker: log each save of the plan until the planner ends. Returns the exit code."""
    with _lock(wait=2):
        record = _read(_plans_path()).get(ref)
    if not isinstance(record, dict):
        print(f"herdboard: no planner is recorded for {ref}", file=sys.stderr)
        return 1
    pane, last = record["pane_id"], record.get("plan_mtime")
    plan_file = attachments.plan_path(store.config, ref)
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    seen = False
    saves = 0
    while True:
        try:
            status = _reviewer_status(pane)
        except HerdboardError as error:
            print(f"herdboard: {error}", file=sys.stderr)
            status = None
        # Herdr can take a moment to detect a new agent, so only a planner that was seen can end.
        seen = seen or status is not None
        current = _mtime(plan_file)
        if current is not None and current != last and status in (None, "idle", "done"):
            activity.append(store, ref, f"Plan saved: {plan_file}", "herdboard")
            last = current
            saves += 1
        if (seen and status is None) or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    _forget(ref, pane)
    if saves:
        ended = "Planning ended."
    elif record.get("plan_mtime") is not None:
        ended = "Planning ended without a change to the plan."
    else:
        ended = "Planning ended without a plan."
    activity.append(store, ref, ended, "herdboard")
    return 0
