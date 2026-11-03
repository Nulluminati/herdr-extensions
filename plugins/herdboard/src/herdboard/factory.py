"""Run a started card as a software factory: lens reviews, builder fixes, and an approver's verdict.

`start --factory` starts the builder (the card's agent) and a background worker. Each time the builder
moves the card to Review and goes idle, the worker runs one review round: one sandboxed reviewer per
lens (security, ux, ui, code), side by side. It gives the findings to the builder, which fixes them or
logs why it disagrees, and moves the card to Review again. The loop ends when a round has no confirmed
problems, when the builder made no new commits, or at the round limit. An approver then checks the
work against the plan and writes a verdict. The card stays in Review: the user merges it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import activity, attachments
from . import start as start_module
from .agent_review import (
    REVIEW_COMMAND_RULES,
    REVIEWERS,
    _reviewer_status,
    capture_change,
    launch_reviewer,
    read_change,
    request_text,
)
from .config import LENSES
from .model import HerdboardError, NotFoundError
from .review import ReviewError
from .start import REVIEW_COLUMN, _lock, _read, _write, find_record, herdr, safe_key, state_dir
from .start import notify as _notify

POLL_SECONDS = 5
# A lens reviewer that takes longer than this fails; the round goes on without it.
REVIEW_TIMEOUT_SECONDS = 30 * 60
# The worker stops when the builder does not move the card to Review in this time.
BUILDER_TIMEOUT_SECONDS = 8 * 60 * 60
VERDICT_NAME = "factory-verdict.md"
# The statuses of a factory run that still runs.
ACTIVE = ("building", "reviewing", "fixing", "approving")
LENS_FOCUS = {
    "security": "Review this change for security problems only: injection, authentication and authorization, "
    "secrets, unsafe file and shell use, and sandbox escapes.",
    "ux": "Review this change for user experience problems only: flows, error messages, defaults, keyboard "
    "and mouse use, and the documentation that the user reads.",
    "ui": "Review this change for user interface and design problems only: layout, visual consistency with "
    "the existing interface, the card's mockup attachments (`mockup-*.png`), and themes.",
    "code": "Review this change for code problems only: bugs, missing tests, readability, and how well it "
    "matches the style of the surrounding code.",
}
LENSES_LINE = re.compile(r"^\s*(?:[-*]\s*)?Review lenses:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class FactoryError(HerdboardError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def parse_lenses(plan_text: str | None) -> tuple[str, ...]:
    """The lenses that the plan's `Review lenses:` line names. All lenses when it names none."""
    found = LENSES_LINE.search(plan_text or "")
    if found:
        names = {name.strip().strip("`.").casefold() for name in found.group(1).split(",")}
        chosen = tuple(lens for lens in LENSES if lens in names)
        if chosen:
            return chosen
    return LENSES


def lens_kind(config, lens: str) -> tuple[str, str | None, str | None]:
    """The agent kind, model and thinking level of a lens's reviewer, from `[factory.lenses]`.

    A pi reviewer without a model or level resolves them as a card review does."""
    entry = config.factory_lenses[lens]
    kind, model, thinking = entry["agent"], entry.get("model"), entry.get("thinking")
    if kind == "pi":
        model = start_module.agent_model(config, model)
        thinking = start_module.thinking_level(config, model, thinking)
    return kind, model, thinking


def approver_kind(config, builder: str) -> str:
    return config.factory_approver or REVIEWERS[builder]


def _section(text: str, heading: str) -> list[str]:
    """The `- ` lines under a `## heading`, up to the next heading."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().casefold() == heading.casefold()
            continue
        if inside and line.startswith("- "):
            lines.append(line)
    return lines


def count_confirmed(findings_text: str) -> int:
    """The findings under `## Confirmed problems`. Risks and questions do not count."""
    return len(_section(findings_text, "Confirmed problems"))


def count_questions(findings_text: str) -> int:
    return len(_section(findings_text, "Risks and questions"))


# The factory state, in the card's start record under "factory".


def factory_state(ref) -> dict | None:
    found = find_record(ref)
    return (found[1].get("factory") or None) if found else None


def factory_running(ref) -> bool:
    """True when a factory run of the card still runs: its status is active and its worker lives."""
    state = factory_state(ref)
    if not state or state.get("status") not in ACTIVE:
        return False
    pid = state.get("pid")
    if pid is None:
        return True  # The worker has not written its process ID yet.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def refuse_while_running(ref, action: str) -> None:
    if factory_running(ref):
        raise FactoryError(
            f"A factory run of {ref} still runs, so {action} must wait. Stop it with `herdboard factory {ref} --stop`.",
            "factory_running",
        )


def update(ref, folder: Path, **changes) -> dict | None:
    """Change this factory run's state and return it. None when the card or this run is gone.

    A late worker must not change a newer run's state."""
    path = state_dir() / "starts.json"
    with _lock(wait=2):
        found = find_record(ref)
        if found is None:
            return None
        key, record = found
        state = record.get("factory")
        if not state or Path(state["folder"]) != Path(folder):
            return None
        state.update(changes)
        records = _read(path)
        records[key] = record
        _write(path, records)
        return dict(state)


def new_state(key: str, rounds: int) -> dict:
    """The state of a new factory run, with its folder created."""
    folder = state_dir() / "factory" / f"{safe_key(key)}-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise FactoryError(f"{folder}: cannot create the factory folder: {error}", "io") from error
    return {
        "rounds": rounds,
        "round": 0,
        "folder": str(folder),
        "lenses": [],
        "status": "building",
        "heads": [],
        "panes": [],
    }


def stop(store, ref) -> dict:
    """`factory --stop`: mark the run stopped and close its reviewer panes. The worker then ends."""
    state = factory_state(ref)
    if not state or state.get("status") not in ACTIVE:
        raise FactoryError(f"No factory run of {ref} runs", "not_running")
    update(ref, Path(state["folder"]), status="stopped")
    _close(state.get("panes") or [])
    activity.append(store, ref, "Factory stopped by the user.", "herdboard")
    return {"ref": ref, "status": "stopped", "round": state.get("round"), "rounds": state.get("rounds")}


def _close(panes) -> None:
    for pane in panes:
        try:
            herdr("pane", "close", pane, tolerated=("pane_not_found",))
        except HerdboardError as error:
            print(f"herdboard: cannot close the reviewer pane {pane}: {error}", file=sys.stderr)


# One review round.


def history_text(folder: Path, done: int) -> str:
    """The earlier rounds' findings and the builder's answers, for the next round's reviewers."""
    if done < 1:
        return ""
    lines = [
        "## Earlier rounds",
        "",
        "Earlier review rounds of this factory run sent findings to the card's agent, and it answered each one:",
    ]
    for number in range(1, done + 1):
        round_folder = folder / f"round-{number}"
        lines.append(
            f"- Round {number}: findings `{round_folder / 'findings.md'}`, answers `{round_folder / 'responses.md'}`"
        )
    lines += [
        "",
        "Read them first. Do not raise a finding again that the agent fixed, unless the fix is wrong. "
        "Do not raise a finding again that the agent disagreed with, unless you have new evidence: "
        "then give the evidence.",
    ]
    return "\n".join(lines)


def run_round(store, ref, record, card, number: int) -> dict:
    """Capture the change once and start one reviewer per lens. Returns the round: its folder and lenses."""
    state = record["factory"]
    folder = Path(state["folder"])
    round_folder = folder / f"round-{number}"
    worktree = Path(record["worktree_path"])
    base = record.get("base") or "@{upstream}"
    untracked, head = capture_change(worktree, base, round_folder)
    lenses = parse_lenses(attachments.plan_text(store.config, ref))
    files = attachments.listing(store.config, ref)
    history = history_text(folder, number - 1)
    reviewers, split_from, direction = [], record["pane_id"], "right"
    for lens in lenses:
        kind, model, thinking = lens_kind(store.config, lens)
        lens_folder = round_folder / lens
        lens_folder.mkdir(parents=True, exist_ok=True)
        for name in ("branch.patch", "uncommitted.patch"):
            shutil.copyfile(round_folder / name, lens_folder / name)
        request = request_text(
            card, record, base, lens_folder, untracked, files, browser=True, focus=LENS_FOCUS[lens], history=history
        )
        pane = launch_reviewer(
            worktree, kind, model, thinking, request, lens_folder, split_from, direction, f"hb-{ref.lower()}-{lens}"
        )
        reviewers.append({"lens": lens, "kind": kind, "model": model, "pane_id": pane})
        update(ref, folder, panes=[entry["pane_id"] for entry in reviewers])
        split_from, direction = pane, "down"
    update(ref, folder, round=number, lenses=list(lenses), heads=[*state.get("heads", []), head], status="reviewing")
    return {"folder": round_folder, "reviewers": reviewers, "head": head}


def wait_round(ref, state_folder: Path, review: dict) -> dict:
    """Wait for every lens's findings, close the reviewer panes, and write the round's `findings.md`.

    A lens that fails gets a section that says so; it does not stop the round. Returns the counts,
    or None in "stopped" when the user stopped the run."""
    round_folder, reviewers = review["folder"], review["reviewers"]
    deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
    pending = {entry["lens"]: entry for entry in reviewers}
    seen, failures = set(), {}
    while pending:
        state = factory_state(ref)
        if not state or Path(state["folder"]) != state_folder or state.get("status") == "stopped":
            _close([entry["pane_id"] for entry in reviewers])
            return {"stopped": True}
        for lens, entry in list(pending.items()):
            findings = round_folder / lens / "findings.md"
            status = _reviewer_status(entry["pane_id"])
            # Herdr can take a moment to detect a new agent, so only a reviewer that was seen can stop.
            if status is not None:
                seen.add(lens)
            if findings.exists() and status in (None, "idle", "done"):
                del pending[lens]
            elif lens in seen and status is None:
                failures[lens] = f"The {entry['kind']} reviewer stopped before it wrote its findings."
                del pending[lens]
        if pending and time.monotonic() >= deadline:
            for lens, entry in pending.items():
                failures[lens] = (
                    f"The {entry['kind']} reviewer did not finish in {REVIEW_TIMEOUT_SECONDS // 60} minutes."
                )
            pending = {}
        if pending:
            time.sleep(POLL_SECONDS)
    _close([entry["pane_id"] for entry in reviewers])
    update(ref, state_folder, panes=[])
    sections, counts = [], {}
    for entry in reviewers:
        lens = entry["lens"]
        if lens in failures:
            sections += [f"# {lens}", "", f"This lens failed: {failures[lens]}", ""]
            continue
        text = (round_folder / lens / "findings.md").read_text(encoding="utf-8").strip()
        counts[lens] = (count_confirmed(text), count_questions(text))
        sections += [f"# {lens} ({entry['kind']})", "", text, ""]
    (round_folder / "findings.md").write_text("\n".join(sections), encoding="utf-8")
    return {
        "stopped": False,
        "counts": counts,
        "failures": failures,
        "confirmed": sum(confirmed for confirmed, _ in counts.values()),
        "questions": sum(questions for _, questions in counts.values()),
    }


def round_summary(number: int, rounds: int, result: dict) -> str:
    detail = ", ".join(f"{lens} {confirmed}" for lens, (confirmed, _) in result["counts"].items() if confirmed)
    text = (
        f"Factory round {number} of {rounds}: {result['confirmed']} confirmed problems"
        f"{f' ({detail})' if detail else ''}, {result['questions']} questions."
    )
    if result["failures"]:
        text += f" Failed lenses: {', '.join(result['failures'])}."
    return text


def builder_message(ref, round_folder: Path, command: str) -> str:
    """The builder's prompt for one round. One line: Herdr rejects line breaks in agent arguments."""
    return (
        f"Factory review round findings for {ref} are ready: {round_folder / 'findings.md'}. Read them. "
        "For each finding, fix it, or record why you disagree with "
        f'{command} log {ref} "..." and do not change the code for it. '
        "Answer the risks and questions too. "
        f"Then write {round_folder / 'responses.md'} as one Markdown table: "
        "| Lens | # | Finding | Agree/Disagree | What you did |, one row for each finding. "
        f"Commit your fixes. Then run {command} --json move {ref} Review."
    )


def send_to_builder(store, ref, record, round_folder: Path, config_file: Path | None) -> None:
    """Move the card to In progress, wait for the builder to be idle, then prompt it with the round."""
    start_module.card_changed(store.move(ref, store.config.start_column))
    timeout_ms = REVIEW_TIMEOUT_SECONDS * 1000
    result = start_module.command(
        [
            os.environ.get("HERDR_BIN_PATH") or "herdr",
            "agent",
            "wait",
            record["pane_id"],
            "--until",
            "idle",
            "--until",
            "done",
            "--timeout",
            str(timeout_ms),
        ],
        timeout=REVIEW_TIMEOUT_SECONDS + 60,
    )
    if result.returncode:
        raise FactoryError(f"Waiting for the card's agent failed: {result.stderr or result.stdout}", "delivery_failed")
    herdr(
        "agent",
        "prompt",
        record["pane_id"],
        builder_message(ref, round_folder, start_module.herdboard_command(config_file)),
    )


# The approver.


def approver_request(card, record, folder: Path, approver_folder: Path, plan: Path | None, rounds: int) -> str:
    round_lines = (
        "".join(
            f"- Round {number}: findings `{folder / f'round-{number}' / 'findings.md'}`, "
            f"answers `{folder / f'round-{number}' / 'responses.md'}`\n"
            for number in range(1, rounds + 1)
        )
        or "- (none)\n"
    )
    plan_line = f"- The plan that the user agreed on: `{plan}`" if plan else "- The card has no plan."
    body = "\n".join(card.body).strip() if card is not None else ""
    return f"""# Factory approval

Card {record["ref"]}: {card.title if card is not None else record["ref"]}

{body or "(no description)"}

- Repository: `{record["worktree_path"]}`
- Branch `{record["branch"]}`, based on `{record.get("base") or "@{upstream}"}`
- The full branch diff: `{approver_folder / "branch.patch"}`
- Uncommitted changes: `{approver_folder / "uncommitted.patch"}`
{plan_line}
- The card's activity log: `{approver_folder / "log.md"}`
- The review rounds:
{round_lines}
You are the approver. Do not review the code line by line again: the review rounds did that.
Check that the change does every step of the plan, and that the card's agent dealt with each finding.
Do not change any file in the repository.
{REVIEW_COMMAND_RULES}
Write your verdict to `{approver_folder / "verdict.md"}` in this format:

    Verdict: approve

    ## Plan steps not done
    - Step: what is missing.

    ## Disagreements
    - Lens, finding: the reviewer's point and the agent's reason.

    ## Findings left at the round limit
    - Lens, finding.

    ## Testing
    What the log shows was tested.

The first line is `Verdict: approve` or `Verdict: changes needed`. Write "None." in an empty section.
Finish writing the complete verdict file before you end your turn. Do not wait for another prompt.
"""


def verdict_of(text: str) -> str:
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    found = re.match(r"verdict:\s*(.+)", first, re.IGNORECASE)
    return found.group(1).strip().rstrip(".") if found else "unknown"


def run_approver(store, ref, record, card, writer=None) -> dict:
    """Start the approver, wait for its verdict, and attach it to the card. Returns the verdict."""
    state = record["factory"]
    folder = Path(state["folder"])
    approver_folder = folder / "approver"
    worktree = Path(record["worktree_path"])
    base = record.get("base") or "@{upstream}"
    capture_change(worktree, base, approver_folder)
    log = activity.read(store.config, ref)
    (approver_folder / "log.md").write_text(
        "".join(f"- {entry['time']} · {entry['author']} · {entry['message']}\n" for entry in log), encoding="utf-8"
    )
    plan = attachments.plan_path(store.config, ref)
    kind = approver_kind(store.config, record.get("agent") or store.config.start_agent)
    model = thinking = None
    if kind == "pi":
        model = start_module.agent_model(store.config, None)
        thinking = start_module.thinking_level(store.config, model)
    request = approver_request(
        card, record, folder, approver_folder, plan if plan.exists() else None, state.get("round", 0)
    )
    pane = launch_reviewer(
        worktree,
        kind,
        model,
        thinking,
        request,
        approver_folder,
        record["pane_id"],
        "right",
        f"hb-{ref.lower()}-approver",
    )
    update(ref, folder, status="approving", panes=[pane])
    verdict_file = approver_folder / "verdict.md"
    deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
    seen, error = False, None
    while True:
        current = factory_state(ref)
        if not current or Path(current["folder"]) != folder or current.get("status") == "stopped":
            _close([pane])
            return {"stopped": True}
        status = _reviewer_status(pane)
        seen = seen or status is not None
        if verdict_file.exists() and status in (None, "idle", "done"):
            break
        if seen and status is None:
            error = f"The {kind} approver stopped before it wrote its verdict."
            break
        if time.monotonic() >= deadline:
            error = f"The {kind} approver did not finish in {REVIEW_TIMEOUT_SECONDS // 60} minutes."
            break
        time.sleep(POLL_SECONDS)
    _close([pane])
    update(ref, folder, panes=[])
    if error is not None:
        return {"stopped": False, "error": error}
    attachments.add(store, ref, verdict_file, name=VERDICT_NAME)
    return {"stopped": False, "verdict": verdict_of(verdict_file.read_text(encoding="utf-8"))}


# The worker.


def _builder_status(record) -> str | None:
    return _reviewer_status(record["pane_id"])


def _end(store, ref, folder: Path, status: str, message: str, title: str) -> int:
    state = factory_state(ref)
    if state and Path(state["folder"]) == folder:
        _close(state.get("panes") or [])
    update(ref, folder, status=status, panes=[])
    try:
        activity.append(store, ref, message, "herdboard")
    except HerdboardError as error:
        print(f"herdboard: cannot log: {error}", file=sys.stderr)
    _notify(title, message)
    return 0 if status == "done" else 1


def _stop_reason(store, ref, folder: Path) -> tuple[str | None, dict | None, object]:
    """Why the run must stop now, else None, with the start record and the card."""
    found = find_record(ref)
    if found is None:
        return "Factory stopped: the card was finished.", None, None
    record = found[1]
    state = record.get("factory")
    if not state or Path(state["folder"]) != folder:
        return "Factory stopped: a newer factory run replaced it.", None, None
    if state.get("status") == "stopped":
        return "stopped", record, None
    try:
        card = store.show(ref)
    except NotFoundError:
        return "Factory stopped: the card left the board.", record, None
    if card.checked or card.archived:
        return "Factory stopped: the card was finished.", record, card
    if not Path(record["worktree_path"]).exists():
        return "Factory stopped: the worktree was removed.", record, card
    return None, record, card


def wait(store, ref, folder: Path, writer=None, config_file: Path | None = None) -> int:
    """The background worker: drive the factory run of `ref` until a verdict, a stop or a limit."""
    folder = Path(folder)
    update(ref, folder, pid=os.getpid())
    waited_since = time.monotonic()
    while True:
        reason, record, card = _stop_reason(store, ref, folder)
        if reason == "stopped":
            _close((record.get("factory") or {}).get("panes") or [])
            return 1
        if reason is not None:
            if record is None:
                _notify(f"Factory of {ref} stopped", reason)
                try:
                    activity.append(store, ref, reason, "herdboard")
                except HerdboardError:
                    pass
                return 1
            return _end(store, ref, folder, "stopped", reason, f"Factory of {ref} stopped")
        builder = _builder_status(record)
        if builder is None:
            return _end(
                store, ref, folder, "stopped", "Factory stopped: the card's agent is gone.", f"Factory of {ref} stopped"
            )
        if card.column.casefold() != REVIEW_COLUMN.casefold() or builder not in ("idle", "done"):
            if time.monotonic() - waited_since >= BUILDER_TIMEOUT_SECONDS:
                return _end(
                    store,
                    ref,
                    folder,
                    "stopped",
                    f"Factory stopped: the card's agent did not move the card to Review in "
                    f"{BUILDER_TIMEOUT_SECONDS // 3600} hours.",
                    f"Factory of {ref} stopped",
                )
            time.sleep(POLL_SECONDS)
            continue
        try:
            code = _step(store, ref, folder, record, card, config_file, writer)
        except (HerdboardError, OSError, subprocess.TimeoutExpired) as error:
            print(f"herdboard: {error}", file=sys.stderr)
            return _end(store, ref, folder, "stopped", f"Factory stopped: {error}", f"Factory of {ref} failed")
        if code is not None:
            return code
        waited_since = time.monotonic()


def _step(store, ref, folder: Path, record, card, config_file, writer) -> int | None:
    """One decision with the card in Review and the builder idle. Returns an exit code when the run ends."""
    state = record["factory"]
    number, rounds = state.get("round", 0), state["rounds"]
    worktree = Path(record["worktree_path"])
    base = record.get("base") or "@{upstream}"
    try:
        head = read_change(worktree, base)[3]
    except ReviewError as error:
        if error.code != "nothing_to_review":
            raise
        return _end(store, ref, folder, "done", "Factory: no change to review.", f"Factory of {ref} done")
    if number:
        last = folder / f"round-{number}" / "findings.md"
        confirmed = count_confirmed(last.read_text(encoding="utf-8")) if last.exists() else 0
        reason = None
        if head == state["heads"][-1]:
            reason = f"Factory: no new commits since round {number}, approver next."
        elif number >= rounds:
            reason = f"Factory: round {number} of {rounds} was the last, approver next."
        elif not confirmed:
            reason = f"Factory: round {number} had no confirmed problems, approver next."
        if reason:
            activity.append(store, ref, reason, "herdboard")
            return _approve(store, ref, folder, record, card, writer)
    number += 1
    review = run_round(store, ref, record, card, number)
    activity.append(
        store,
        ref,
        f"Factory round {number} of {rounds} started: "
        + ", ".join(f"{entry['lens']} ({entry['kind']})" for entry in review["reviewers"])
        + ".",
        "herdboard",
    )
    result = wait_round(ref, folder, review)
    if result["stopped"]:
        return 1
    activity.append(store, ref, round_summary(number, rounds, result), "herdboard")
    if not result["confirmed"] and not result["questions"] and not result["failures"]:
        activity.append(store, ref, f"Factory: round {number} had no findings, approver next.", "herdboard")
        found = find_record(ref)
        return _approve(store, ref, folder, found[1] if found else record, card, writer)
    found = find_record(ref)
    record = found[1] if found else record
    update(ref, folder, status="fixing")
    send_to_builder(store, ref, record, review["folder"], config_file)
    return None


def _approve(store, ref, folder: Path, record, card, writer) -> int:
    found = find_record(ref)
    record = found[1] if found else record
    result = run_approver(store, ref, record, card, writer)
    if result["stopped"]:
        return 1
    if "error" in result:
        return _end(
            store, ref, folder, "stopped", f"Factory approver failed: {result['error']}", f"Factory of {ref} failed"
        )
    return _end(store, ref, folder, "done", f"Factory done: {result['verdict']}.", f"Factory of {ref} done")


def summary(state: dict | None) -> dict | None:
    """The factory state for `show --json`."""
    if not state:
        return None
    return {key: state.get(key) for key in ("round", "rounds", "status", "lenses", "folder")}
