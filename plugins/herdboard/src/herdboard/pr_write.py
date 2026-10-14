"""Have an agent write a card's pull request title and description, then open the pull request as a draft.

The card's agent writes when it still runs, since it knows the work. Otherwise a read-only writer agent
starts in a split. A background worker waits for the text, pushes the branch and runs `gh`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from . import activity, merge, start as start_module
from .agent_review import _notify, _reviewer_status, reviewer_arguments, start_worker
from .brief import _unindent
from .model import HerdboardError, split_title
from .start import Starter, _lock, _read, _write, agent_live, find_record, herdr, safe_key, state_dir

# Writing a description takes minutes; the worker gives up after this.
WRITE_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 5
# The folders where GitHub looks for pull request templates; "" is the repository root.
TEMPLATE_FOLDERS = ("", ".github", "docs")

DEFAULT_FORMAT = """    <title>

    ## Why
    One to three sentences: what was wrong or missing, and who it affected.

    ## What changed
    The approach and the decisions, not a list of changed files.

    ## Trade-offs
    What was given up, or the risks. Leave this section out when there are none.

    ## Testing
    How the change was checked.
"""

TITLE_GUIDE = """    <type>(<scope>): <summary>

    - Type: one of `feat` (a new feature), `fix` (a bug fix), `docs` (documentation only),
      `refactor` (no change in behavior), `perf` (faster), `test` (tests only), `build` (build or
      dependencies), `ci` (continuous integration), `chore` (other upkeep).
    - Scope: optional. The part of the code that changed, in one lower case word, such as `auth`.
    - Summary: imperative mood ("add", not "added" or "adds"), lower case first word, no period.
    - The whole title is at most 72 characters.
    - A breaking change puts `!` before the colon: `feat(api)!: remove the v1 routes`.
"""


def find_templates(worktree: Path) -> list[Path]:
    """The pull request templates in the places GitHub reads, with names in any case."""
    found = []
    for folder in TEMPLATE_FOLDERS:
        base = worktree / folder if folder else worktree
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            name = entry.name.lower()
            if entry.is_file() and name == "pull_request_template.md":
                found.append(entry)
            elif entry.is_dir() and name == "pull_request_template":
                found += sorted(p for p in entry.iterdir() if p.is_file() and p.suffix.lower() == ".md")
    return found


def card_text(card) -> tuple[str, str]:
    """The card's title and description, without board markup."""
    if card.source == "linear":
        return card.title, "\n".join(card.body).strip()
    return split_title(card.title)[0] or card.title, "\n".join(_unindent(line) for line in card.body).strip()


def request_text(card, data: dict, folder: Path, templates: list[Path], entries: list[dict]) -> str:
    title, description = card_text(card)
    log = "\n".join(f"- {e['time']} · {e['author']} · {e['message']}" for e in entries) or "- (none)"
    if len(templates) == 1:
        template = f"Follow the template `{templates[0]}`: the same headings in the same order."
    elif templates:
        listed = "\n".join(f"- `{path}`" for path in templates)
        template = f"Pick the template that fits, and follow it: the same headings in the same order.\n{listed}"
    else:
        template = f"The repository has no template. Use this format:\n\n{DEFAULT_FORMAT}"
    return f"""# Pull request description

Write the title and description of a pull request for this work.

Task: {title}

{description or "(no description)"}

- Repository: `{data['worktree']}`
- Branch `{data['branch']}`, into `{data['base']}`
- The change: `{folder / 'branch.patch'}`

Notes recorded during the work:
{log}

Write `{folder / 'pr.md'}`. Its first line is the title. The rest is the description.

- The title follows this guide, whatever the style of the repository:

{TITLE_GUIDE}
- {template}
- Keep it short. Write plain prose. Use the `humanizer` skill if you have it.
- Explain why the change exists, not only what changed. Do not list the changed files.
- Describe only the testing that the notes or the change show. Do not claim checks you did not run or see.
  When nothing shows testing, say that the change has no tests.
- Do not name the tools, the agent or the task card. Do not add a Linear link.
- Do not run `gh`, push, or change any file other than `{folder / 'pr.md'}`.
"""


def prompt_text(folder: Path) -> str:
    return (f"Write the pull request title and description for this work. Read {folder / 'request.md'} "
            f"and follow it. Write the result to {folder / 'pr.md'} and nowhere else. "
            "Do not run gh, push, or change code.")


def check_running(ref: str, record: dict) -> None:
    """Stop when an agent still writes this card's description. A gone or timed-out writer does not count."""
    running = record.get("pr_write")
    if not isinstance(running, dict):
        return
    if time.time() - running.get("started", 0) > WRITE_TIMEOUT_SECONDS + 60:
        return
    if _reviewer_status(running["pane_id"]) is not None:
        raise merge.MergeError(f"An agent still writes the pull request description of {ref} "
                               f"in pane {running['pane_id']}", "pr_writing")


def request(merger, card, ref, key, record, worktree: Path, base: str, rewrite_url: str | None,
            persist) -> dict:
    """Ask an agent for the description, store the request, and start the worker. The caller holds the lock."""
    config = merger.config
    data = {"worktree": str(worktree), "branch": record["branch"], "base": base}
    entries = activity.read(config, ref)
    requests = state_dir() / "prs"
    try:
        patch = merge._git(worktree, "diff", f"{base}...HEAD").stdout
        requests.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"{safe_key(key)}-{time.strftime('%Y%m%d-%H%M%S')}-", dir=requests))
        (folder / "branch.patch").write_text(patch, encoding="utf-8")
        (folder / "request.md").write_text(request_text(card, data, folder, find_templates(worktree), entries),
                                           encoding="utf-8")
    except OSError as error:
        raise merge.MergeError(f"{requests}: cannot write the pull request request: {error}", "io") from error
    starter = Starter(merger.store, merger.directory, writer=merger.writer, config_file=merger.config_file)
    record.update(starter._workspace(Path(record["checkout"]), record["branch"], record))
    persist()
    kind = record.get("agent") or config.start_agent
    if agent_live(record):
        pane, own, writer = record["pane_id"], False, "the card's agent"
    else:
        pane = herdr("pane", "split", record["pane_id"], "--direction", "right", "--cwd", str(worktree),
                     "--no-focus")["pane"]["pane_id"]
        thinking = config.start_thinking if kind == "pi" else None
        # A startup prompt waits for the user, and a busy writer never looks ready: the worker watches it.
        herdr("agent", "start", f"hb-{ref.lower()}-pr", "--kind", kind, "--pane", pane, "--",
              *reviewer_arguments(kind, prompt_text(folder), folder, config.start_model, thinking),
              tolerated=("timeout", "agent_not_ready"))
        own, writer = True, f"a {kind} writer agent"
    data.update(folder=str(folder), pane_id=pane, kind=kind, own_pane=own, rewrite_url=rewrite_url,
                linear_url=card.url if card.source == "linear" else None, started=time.time())
    record["pr_write"] = data
    persist()
    # The worker reads the request from here: the start record is gone once the card is finished.
    (folder / "pr_write.json").write_text(json.dumps(data), encoding="utf-8")
    activity.append(merger.store, ref, f"PR description requested from {writer}.", "herdboard")
    config_file = ["--config", str(merger.config_file)] if merger.config_file else []
    try:
        start_worker([*config_file, "pr-wait", ref, "--folder", str(folder)], folder / "worker.log")
    except OSError as error:
        # Without the worker nobody closes the writer, and its record would block the next request.
        if own:
            herdr("pane", "close", pane, tolerated=("pane_not_found",))
        _settle(ref)
        activity.append(merger.store, ref, f"PR not opened: the worker did not start: {error}", "herdboard")
        raise merge.MergeError(f"The pull request worker did not start: {error}", "worker_failed") from error
    return {"writer": writer, "pane_id": pane, "folder": str(folder)}


def _wait_for_text(path: Path, pane: str, who: str) -> None:
    deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
    seen = False
    while True:
        status = _reviewer_status(pane)
        # Herdr can take a moment to detect a new agent, so only a writer that was seen can stop.
        seen = seen or status is not None
        if path.exists() and status in (None, "idle", "done"):
            return
        if seen and status is None:
            raise merge.MergeError(f"The {who} stopped before it wrote {path}", "pr_failed")
        if time.monotonic() >= deadline:
            raise merge.MergeError(f"The {who} did not finish in {WRITE_TIMEOUT_SECONDS // 60} minutes",
                                   "pr_failed")
        time.sleep(POLL_SECONDS)


def parse(text: str, linear_url: str | None) -> tuple[str, str]:
    """The title (the first line, without a heading mark) and the body, with the Linear link at its end."""
    lines = text.strip().splitlines()
    title = lines[0].lstrip("#").strip() if lines else ""
    if not title:
        raise merge.MergeError("pr.md has no title on its first line", "pr_failed")
    body = "\n".join(lines[1:]).strip()
    if linear_url:
        body = f"{body}\n\nLinear: {linear_url}".strip()
    return title, body + "\n" if body else ""


def _settle(ref: str, **updates) -> dict | None:
    """Remove the request from the card's start record and apply `updates`. None when the record is gone."""
    path = state_dir() / "starts.json"
    with _lock(wait=2):
        found = find_record(ref)
        if found is None:
            return None
        key, record = found
        records = _read(path)
        record.pop("pr_write", None)
        record.update(updates)
        records[key] = record
        _write(path, records)
        return record


def _open(data: dict, folder: Path) -> tuple[str, str]:
    """Push the branch and open or edit the pull request. Returns its URL and the log text."""
    title, body = parse((folder / "pr.md").read_text(encoding="utf-8"), data.get("linear_url"))
    worktree, branch, base = Path(data["worktree"]), data["branch"], data["base"]
    merge.push_branch(worktree, branch, base)
    body_file = folder / "body.md"
    body_file.write_text(body, encoding="utf-8")
    if data.get("rewrite_url"):
        merge._gh_output(worktree, "pr", "edit", data["rewrite_url"], "--title", title,
                         "--body-file", str(body_file))
        return data["rewrite_url"], "PR description rewritten"
    created = merge._gh_output(worktree, "pr", "create", "--draft", "--base", base, "--head", branch,
                               "--title", title, "--body-file", str(body_file))
    return created.strip().splitlines()[-1], "PR opened"


def wait(store, ref, folder: Path, writer=None) -> int:
    """The background worker: wait for the description and open the pull request. Returns the exit code."""
    folder = Path(folder)
    try:
        data = json.loads((folder / "pr_write.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"herdboard: cannot read the pull request request in {folder}: {error}", file=sys.stderr)
        return 1
    pane = data["pane_id"]
    who = f"{data['kind']} writer agent" if data["own_pane"] else "card's agent"
    try:
        if not data["own_pane"]:
            start_module.command([os.environ.get("HERDR_BIN_PATH") or "herdr", "agent", "wait", pane,
                                  "--until", "idle", "--timeout", str(WRITE_TIMEOUT_SECONDS * 1000)],
                                 timeout=WRITE_TIMEOUT_SECONDS + 60)
            herdr("agent", "prompt", pane, prompt_text(folder))
        _wait_for_text(folder / "pr.md", pane, who)
        error = None
    except (HerdboardError, OSError, subprocess.TimeoutExpired) as failure:
        error = failure
    if data["own_pane"]:
        try:
            herdr("pane", "close", pane, tolerated=("pane_not_found",))
        except HerdboardError as failure:
            print(f"herdboard: cannot close the writer pane {pane}: {failure}", file=sys.stderr)
    if find_record(ref) is None:
        return 0  # The card was finished while the agent wrote.
    if error is None:
        try:
            url, done = _open(data, folder)
        except (HerdboardError, OSError) as failure:
            error = failure
    if error is not None:
        print(f"herdboard: {error}", file=sys.stderr)
        _settle(ref)
        activity.append(store, ref, f"PR not opened: {error}", "herdboard")
        _notify(f"PR for {ref} not opened", str(error))
        return 1
    _settle(ref, pr_url=url, pr_state="open", pr_checked=time.time())
    activity.append(store, ref, f"{done}: {url}", "herdboard")
    _notify(f"PR for {ref} {'opened' if done == 'PR opened' else 'rewritten'}", url)
    return 0
