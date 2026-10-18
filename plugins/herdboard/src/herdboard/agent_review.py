"""Have a second agent review a started card's work, then give its findings to the card's agent.

The reviewer runs in a split next to the card's agent. It can read the repository and write
only its findings. A background worker waits for the findings, closes the reviewer pane,
logs the result and prompts the card's agent, which answers and waits for the user.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from . import activity, attachments, start as start_module
from .config import THINKING_LEVELS
from .model import HerdboardError
from .review import Reviewer, ReviewError
from .start import _lock, _read, _write, find_record, herdr, safe_key, state_dir

REVIEWERS = {"claude": "pi", "pi": "claude"}
# A review of a large change can take long; the worker gives up after this.
REVIEW_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 5


def start_worker(arguments: list[str], log: Path) -> None:
    """Run herdboard detached, so the caller (the CLI or the board) returns at once."""
    # A linked checkout can differ from the installed package. Workers must use the caller's code.
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, environment.get("PYTHONPATH"))))
    with log.open("a") as output:
        subprocess.Popen([sys.executable, "-m", "herdboard", *arguments], stdin=subprocess.DEVNULL,
                         stdout=output, stderr=subprocess.STDOUT, start_new_session=True, env=environment)


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True)
    if result.returncode:
        raise ReviewError(f"git {' '.join(args)} failed in {worktree}: {result.stderr.strip()}", "git_failed")
    return result.stdout


def reviewer_arguments(kind: str, prompt: str, folder: Path, model: str | None,
                       thinking: str | None) -> list[str]:
    """The reviewer's arguments after Herdr's `--`: it reads the repository and writes only its findings."""
    if kind == "pi":
        options = (["--model", model] if model else []) + (["--thinking", thinking] if thinking else [])
        return ["--tools", "read,grep,find,ls,write", *options, "--", prompt]
    # The prompt goes first, and --allowedTools last, because --allowedTools takes every value after it.
    return [prompt, "--permission-mode", "dontAsk", *(["--model", model] if model else []),
            "--allowedTools", "Read", "Grep", "Glob", f"Edit(/{folder}/**)"]


def request_text(card, record, base: str, folder: Path, untracked: list[str], files: list[dict] = ()) -> str:
    untracked_list = "\n".join(f"- {path}" for path in untracked) or "- (none)"
    plan = next((entry for entry in files if entry["name"] == attachments.PLAN_NAME), None)
    others = [entry for entry in files if entry is not plan]
    card_files = ""
    if plan:
        card_files += f"Check that the change follows the plan that the user agreed on: `{plan['path']}`\n"
    if others:
        card_files += "The card's other attachments:\n" + "".join(f"- `{entry['path']}`\n" for entry in others)
    if card_files:
        card_files += "\n"
    body = "\n".join(card.body).strip() if card is not None else ""
    return f"""# Review request

Card {record['ref']}: {card.title if card is not None else record['ref']}

{body or "(no description)"}

- Repository: `{record['worktree_path']}`
- Branch `{record['branch']}`, based on `{base}`
- Commits since the base: `{folder / 'branch.patch'}`
- Uncommitted changes: `{folder / 'uncommitted.patch'}`
- New untracked files (read them from the repository):
{untracked_list}

{card_files}Review this change for bugs, security problems and missing tests.
Check that it does what the card asks.
Read the surrounding code in the repository when you need context.
Do not change any file in the repository.

Write your findings to `{folder / 'findings.md'}` in this format:

    ## Confirmed problems
    - [high|medium|low] path:line: problem. Suggested fix.

    ## Risks and questions
    - path:line: concern.

If you find nothing, write "No findings."
Finish writing the complete findings file before you end your turn.
Herdboard automatically delivers the findings path to the card's requesting agent after your turn ends.
No command execution or notification from you is required. Do not wait for another prompt.
If you cannot run tests or inspect live screens, state those limits in the findings.
"""


class AgentReviewer(Reviewer):
    def __init__(self, store, directory, writer=None, config_file: Path | None = None):
        super().__init__(store, directory, writer=writer, config_file=config_file)

    def request(self, ref, reviewer=None, model=None, thinking=None, then_hunk=False):
        if reviewer is not None and reviewer not in REVIEWERS:
            raise ReviewError("The reviewer must be claude or pi", "usage")
        if model is not None and (not model or any(ch.isspace() for ch in model)):
            raise ReviewError("The model must be one name, such as sonnet or zai/glm-5.3", "usage")
        if thinking is not None and thinking not in THINKING_LEVELS:
            raise ReviewError(f"Thinking must be one of {', '.join(THINKING_LEVELS)}", "usage")
        card, ref, key, record = self._started(ref)
        kind = reviewer or REVIEWERS[record.get("agent") or self.config.start_agent]
        if thinking and kind != "pi":
            raise ReviewError("--thinking works only with a pi reviewer", "usage")
        persist = self._persister(key, record)
        with _lock(".start-" + safe_key(key) + ".lock"):
            running = record.get("agent_review")
            # A review whose reviewer is gone, after a worker that died, does not block a new one.
            if running and _reviewer_status(running["pane_id"]) is not None:
                raise ReviewError(f"An agent review of {ref} still runs in pane {running['pane_id']}",
                                  "review_running")
            worktree = Path(record["worktree_path"])
            # A record from before bases were recorded compares with the branch upstream, as merge does.
            base = record.get("base") or "@{upstream}"
            branch_diff = _git(worktree, "diff", f"{base}...HEAD")
            uncommitted = _git(worktree, "diff", "HEAD")
            untracked = _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
            if not (branch_diff or uncommitted or untracked):
                raise ReviewError(f"{ref} has no commits since {base} and no uncommitted changes",
                                  "nothing_to_review")
            # Read before the review folder exists, so a failure leaves no folder behind.
            files = attachments.listing(self.config, ref)
            # The review runs in the background: the user stays where they are.
            self._ready_agent(card, ref, key, record, persist, focus=False)
            reviews = state_dir() / "reviews"
            try:
                reviews.mkdir(parents=True, exist_ok=True)
                folder = Path(tempfile.mkdtemp(prefix=f"{safe_key(key)}-{time.strftime('%Y%m%d-%H%M%S')}-",
                                               dir=reviews))
                (folder / "branch.patch").write_text(branch_diff, encoding="utf-8")
                (folder / "uncommitted.patch").write_text(uncommitted, encoding="utf-8")
                (folder / "request.md").write_text(request_text(card, record, base, folder, untracked, files),
                                                   encoding="utf-8")
            except OSError as error:
                raise ReviewError(f"{reviews}: cannot write the review request: {error}", "io") from error
            pane = herdr("pane", "split", record["pane_id"], "--direction", "right", "--cwd", str(worktree),
                         "--no-focus")["pane"]["pane_id"]
            prompt = (f"Read {folder / 'request.md'} and do the review it asks for. "
                      f"Write your findings to {folder / 'findings.md'} and nowhere else.")
            # A startup prompt (such as Claude's folder trust check) waits for the user,
            # and a reviewer busy with its first message never looks ready: the worker watches it.
            herdr("agent", "start", f"hb-{ref.lower()}-review", "--kind", kind, "--pane", pane, "--",
                  *reviewer_arguments(kind, prompt, folder, model, thinking),
                  tolerated=("timeout", "agent_not_ready"))
            record["agent_review"] = {"folder": str(folder), "pane_id": pane, "kind": kind, "model": model,
                                      "then_hunk": then_hunk}
            persist()
            # The worker reads the review from here: the start record is gone once the card is finished.
            (folder / "review.json").write_text(json.dumps(record["agent_review"]), encoding="utf-8")
        activity.append(self.store, ref, f"Agent review started: {kind}{f' ({model})' if model else ''}.",
                        "herdboard")
        config = ["--config", str(self.config_file)] if self.config_file else []
        try:
            start_worker([*config, "review-wait", ref, "--folder", str(folder)], folder / "worker.log")
        except OSError as error:
            # Without the worker nobody closes the reviewer, and its record would block the next review.
            herdr("pane", "close", pane, tolerated=("pane_not_found",))
            _forget(ref, folder)
            activity.append(self.store, ref, f"Agent review failed: the worker did not start: {error}", "herdboard")
            raise ReviewError(f"The agent review worker did not start: {error}", "worker_failed") from error
        return {"ref": ref, "reviewer": kind, "model": model, "pane_id": pane, "folder": str(folder),
                "then_hunk": then_hunk}


def _reviewer_status(pane: str) -> str | None:
    found = herdr("agent", "get", pane, tolerated=("agent_not_found", "pane_not_found"))
    return found["agent"].get("agent_status") if found else None


def _wait_for_findings(findings: Path, pane: str, kind: str) -> None:
    deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
    seen = False
    while True:
        status = _reviewer_status(pane)
        # Herdr can take a moment to detect a new agent, so only a reviewer that was seen can stop.
        seen = seen or status is not None
        if findings.exists() and status in (None, "idle", "done"):
            return
        if seen and status is None:
            raise ReviewError(f"The {kind} reviewer stopped before it wrote {findings}", "review_failed")
        if time.monotonic() >= deadline:
            raise ReviewError(f"The {kind} review did not finish in {REVIEW_TIMEOUT_SECONDS // 60} minutes",
                              "review_failed")
        time.sleep(POLL_SECONDS)


def _count_findings(findings: Path) -> int:
    return sum(1 for line in findings.read_text(encoding="utf-8").splitlines() if line.startswith("- "))


def _notify(title: str, body: str) -> None:
    try:
        herdr("notification", "show", title, "--body", body)
    except HerdboardError as error:
        print(f"herdboard: cannot show a notification: {error}", file=sys.stderr)


def _forget(ref: str, folder: Path) -> dict | None:
    """Remove only this review, returning the start record (None when it is gone).

    A late worker must not clear a retry's review from the card's start record.
    """
    path = state_dir() / "starts.json"
    with _lock(wait=2):
        found = find_record(ref)
        if found is None:
            return None
        key, record = found
        running = record.get("agent_review")
        if not running or Path(running["folder"]) != folder:
            return record
        records = _read(path)
        record.pop("agent_review", None)
        records[key] = record
        _write(path, records)
        return record


def wait(store, ref, folder: Path, writer=None, config_file: Path | None = None) -> int:
    """The background worker: wait for the findings and deliver them. Returns the exit code."""
    try:
        review = json.loads((Path(folder) / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"herdboard: cannot read the agent review in {folder}: {error}", file=sys.stderr)
        return 1
    folder, pane, kind = Path(folder), review["pane_id"], review["kind"]
    findings = folder / "findings.md"
    try:
        _wait_for_findings(findings, pane, kind)
        error = None
    except HerdboardError as failure:
        error = failure
    try:
        herdr("pane", "close", pane, tolerated=("pane_not_found",))
    except HerdboardError as failure:
        print(f"herdboard: cannot close the reviewer pane {pane}: {failure}", file=sys.stderr)
    record = _forget(ref, folder)
    if record is None:
        return 0  # The card was finished during the review.
    delivery_error = None
    if error is None:
        activity.append(store, ref, f"Agent review by {kind} done: {_count_findings(findings)} findings. {findings}",
                        "herdboard")
        message = (f"An agent review of this card by {kind} is ready: {findings}. Read it. "
                   "For each finding, say if you agree or disagree, and why. "
                   f"Record your answer with {start_module.herdboard_command(config_file)} log {ref}. "
                   "Do not change any code. Wait for the user to decide what to fix.")
        timeout_ms = REVIEW_TIMEOUT_SECONDS * 1000
        try:
            result = start_module.command(
                [os.environ.get("HERDR_BIN_PATH") or "herdr", "agent", "wait", record["pane_id"],
                 "--until", "idle", "--until", "done", "--timeout", str(timeout_ms)],
                timeout=REVIEW_TIMEOUT_SECONDS + 60)
            if result.returncode:
                raise ReviewError(f"Waiting for the card's agent failed: {result.stderr or result.stdout}",
                                  "delivery_failed")
            herdr("agent", "prompt", record["pane_id"], message)
        except (HerdboardError, OSError, subprocess.TimeoutExpired) as failure:
            print(f"herdboard: cannot prompt the card's agent: {failure}", file=sys.stderr)
            delivery_error = failure
            activity.append(store, ref, f"Agent review delivery failed: {findings}. {failure}", "herdboard")
    else:
        print(f"herdboard: {error}", file=sys.stderr)
        activity.append(store, ref, f"Agent review failed: {error}", "herdboard")
    if review.get("then_hunk"):
        try:
            Reviewer(store, record["checkout"], writer=writer, config_file=config_file).review(ref)
        except HerdboardError as failure:
            print(f"herdboard: cannot open Hunk: {failure}", file=sys.stderr)
            activity.append(store, ref, f"Hunk did not open after the agent review: {failure}", "herdboard")
    if delivery_error is not None:
        _notify(f"Agent review of {ref} delivery failed", f"{findings}. {delivery_error}")
        return 1
    if error is None:
        _notify(f"Agent review of {ref} done", f"{_count_findings(findings)} findings. {findings}")
        return 0
    _notify(f"Agent review of {ref} failed", str(error))
    return 1
