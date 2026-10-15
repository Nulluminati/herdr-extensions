"""Plan a card with an agent before the work starts.

The planner runs in a new Herdr tab in the repository's main checkout. It reads the code, asks the
user about the unknowns, and writes the plan as the card's `plan.md` attachment. The agent that later
does the work gets the plan in its brief. A background worker logs each save of the plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import time

from . import activity, attachments, start as start_module
from .agent_review import _reviewer_status, start_worker
from .brief import _one_line, _title, _unindent
from .config import THINKING_LEVELS
from .linear_cards import LINEAR_REF, issue_card
from .model import HerdboardError, NotFoundError
from .start import (Starter, StartError, _lock, _read, _write, agent_live, find_record, herdboard_command, herdr,
                    safe_key, state_dir, write_brief)
from .store import TEMP_REF

# A planning talk with the user can take hours; the worker stops watching after this.
PLAN_TIMEOUT_SECONDS = 8 * 60 * 60
POLL_SECONDS = 5
# How long the worker waits for a busy work agent before it sends the changed plan.
AGENT_WAIT_SECONDS = 30 * 60

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

    Claude keeps the user's default permission mode, because the user is in the tab to answer its prompts."""
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
                          repository_view=self.repository_view, config_file=self.config_file)
        identity = starter._repository(card, repo, issue)
        found = find_record(ref)
        local = starter._checkout(identity, checkout, found[1] if found else None)
        with _lock(".plan-" + safe_key(ref) + ".lock"):
            with _lock(wait=2):
                running = _read(_plans_path()).get(ref)
            # A planner whose pane is gone, after a worker that died, does not block a new one. The pane
            # counts, not the agent: Herdr can show no agent while Claude waits at its folder trust prompt.
            if _planner_pane(running) is not None:
                raise PlanError(f"A planner for {ref} still runs in pane {running['pane_id']}", "plan_running")
            plan_file = attachments.plan_path(self.config, ref)
            notes = []
            if card.source != "linear":
                try:
                    notes = self.store.notes_for(card) or []
                except HerdboardError:
                    notes = []
            brief = write_brief(safe_key(ref) + "-plan", brief_text(
                card, checkout=local, notes=notes, plan=attachments.plan_for_agent(self.store, ref),
                files=attachments.listing(self.config, ref), log=activity.read(self.config, ref),
                plan_file=plan_file, command=herdboard_command(self.config_file)))
            # Read before the launch: the agent can save the plan while `agent start` waits for it.
            plan_mtime = _mtime(plan_file)
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
                                "plan_mtime": plan_mtime}
                _write(_plans_path(), records)
        activity.append(self.store, ref, f"Planning started: {kind}{f' ({model})' if model else ''}.", "herdboard")
        config = ["--config", str(self.config_file)] if self.config_file else []
        log = state_dir() / "plans" / f"{safe_key(ref)}.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            start_worker([*config, "plan-wait", ref, "--pane", pane], log)
        except OSError as error:
            # The planner still works without the worker; only the log of its saves is missing.
            activity.append(self.store, ref, f"The plan saves are not logged: the worker did not start: {error}",
                            "herdboard")
        return {"ref": ref, "agent": kind, "model": model, "pane_id": pane, "checkout": str(local),
                "plan": str(plan_file)}


def _pane_exists(pane: str) -> bool:
    return herdr("pane", "get", pane, tolerated=("pane_not_found",)) is not None


def _planner_pane(record) -> str | None:
    """The live pane in a planner record. Herdr errors stay visible to the caller."""
    if not isinstance(record, dict):
        return None
    pane = record.get("pane_id")
    if not isinstance(pane, str) or not pane:
        return None
    return pane if _pane_exists(pane) else None


def running_planner(ref: str) -> str | None:
    """The pane of the card's planner while it still runs, else None."""
    with _lock(wait=2):
        running = _read(_plans_path()).get(ref)
    return _planner_pane(running)


def mark_planning(cards) -> None:
    """Set optional board-only planner status without letting state-file errors hide the cards."""
    try:
        # Writers replace the file atomically; this optional status needs no read lock.
        records = _read(_plans_path())
    except HerdboardError:
        records = {}
    for card in cards:
        card.planning = None
        record = records.get(card.ref)
        if not isinstance(record, dict) or record.get("kind") not in ("claude", "pi"):
            continue
        try:
            exists = _planner_pane(record) is not None
        except HerdboardError:
            # A failed Herdr check does not mean that planning ended; keep the recorded kind.
            exists = True
        if exists:
            card.planning = record["kind"]


def _forget(ref: str, pane: str) -> None:
    with _lock(wait=2):
        records = _read(_plans_path())
        if isinstance(records.get(ref), dict) and records[ref].get("pane_id") == pane:
            del records[ref]
            _write(_plans_path(), records)


def wait(store, ref, pane: str) -> int:
    """The background worker: log each save of the plan until the planner in `pane` ends. Returns the exit code."""
    with _lock(wait=2):
        record = _read(_plans_path()).get(ref)
    if not isinstance(record, dict):
        print(f"herdboard: no planner is recorded for {ref}", file=sys.stderr)
        return 1
    if record["pane_id"] != pane:
        # A later planner replaced this one before the worker started: that planner has its own worker.
        return 0
    last = record.get("plan_mtime")
    plan_file = attachments.plan_path(store.config, ref)
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    seen = False
    status = None
    ended = False
    saves = 0
    while True:
        try:
            status = _reviewer_status(pane)
            # Herdr can take a moment to detect a new agent, and can show none while Claude waits at its
            # folder trust prompt. So a planner not seen yet ends only when its pane is gone.
            ended = status is None and (seen or not _pane_exists(pane))
        except HerdboardError as error:
            # Herdr did not answer, so the planner can still run: ask again at the next poll.
            print(f"herdboard: {error}", file=sys.stderr)
        else:
            seen = seen or status is not None
            current = _mtime(plan_file)
            if current is not None and current != last and status in (None, "idle", "done"):
                activity.append(store, ref, f"Plan saved: {plan_file}", "herdboard")
                last = current
                saves += 1
            if ended:
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    try:
        running = not ended and (status is not None or _pane_exists(pane))
    except HerdboardError:
        # Herdr did not answer, so the planner can still run.
        running = True
    if running:
        # The record stays, so the planner still blocks a second one for the card.
        activity.append(store, ref, "The plan saves are no longer logged: the planner still runs after "
                        f"{PLAN_TIMEOUT_SECONDS // 3600} hours.", "herdboard")
        return 0
    _forget(ref, pane)
    if saves:
        message = "Planning ended."
    elif record.get("plan_mtime") is not None:
        message = "Planning ended without a change to the plan."
    else:
        message = "Planning ended without a plan."
    activity.append(store, ref, message, "herdboard")
    if saves:
        _send_plan(store, ref, plan_file)
    return 0


def _send_plan(store, ref: str, plan_file: Path) -> None:
    """Tell the card's work agent, when one runs, that the plan changed. The user decides what changes."""
    try:
        found = find_record(ref)
        if found is None or not agent_live(found[1]):
            return
        pane = found[1]["pane_id"]
        start_module.command([os.environ.get("HERDR_BIN_PATH") or "herdr", "agent", "wait", pane,
                              "--until", "idle", "--timeout", str(AGENT_WAIT_SECONDS * 1000)],
                             timeout=AGENT_WAIT_SECONDS + 60)
        herdr("agent", "prompt", pane, f"The plan for this card changed: {plan_file}. Read it. "
              "Tell the user how your work differs from the plan. Wait for the user to decide what to change.")
    except (HerdboardError, OSError, subprocess.TimeoutExpired) as failure:
        print(f"herdboard: cannot send the plan to the card's agent: {failure}", file=sys.stderr)
        return
    activity.append(store, ref, "Sent the plan to the card's agent.", "herdboard")
