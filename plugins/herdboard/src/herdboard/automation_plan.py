"""Plan an automation with an agent. The plan becomes the automation's `plan.md` attachment, which
each spawned card copies, so every unattended run gets it in its brief.

The planner of a repository board runs in a temporary worktree on `auto/<id>-plan`; that of an inbox
automation in the folder `<todos_dir>/.herdboard/work/<id>/`. A background worker logs each save of the
plan, and removes the temporary worktree and branch when the planner ends and they are clean.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from . import activity, attachments, automations
from . import plan as plan_module
from .agent_review import start_worker
from .brief import _one_line
from .config import AGENTS, thinking_levels
from .model import HerdboardError
from .plan import (
    FACTORY_LENSES_RULE,
    PLAN_FORMAT,
    VERIFICATION_RULE,
    PlanError,
    _mtime,
    _planner_pane,
    _plans_path,
    planner_arguments,
)
from .start import (
    Starter,
    StartError,
    _git,
    _lock,
    _read,
    _write,
    agent_model,
    export_config,
    herdboard_command,
    herdr,
    thinking_level,
    work_folder,
    write_brief,
)


def plan_branch(automation_id: str) -> str:
    return f"auto/{automation_id}-plan"


def brief_text(
    item: automations.Automation,
    *,
    worktree: Path,
    branch: str,
    base: str,
    repoless: bool,
    plan: str | None,
    files: list[dict],
    log: list[dict],
    plan_file: Path,
    command: str,
) -> str:
    lines = [
        f"# Plan automation: {item.title}",
        "",
        f"Plan herdboard automation `{item.id}` with the user. You do not do the work. The automation runs "
        f"unattended: at each due time (`{item.schedule}`), herdboard adds a new card to board "
        f"`{item.board}` and starts an agent on it, with your plan in its brief. That agent does not see "
        "this conversation, and no user is there to answer its questions.",
        "",
    ]
    if repoless:
        lines += [
            f"- Working folder: `{worktree}`. The automation has no repository. Ask the user what the "
            "plan needs. Do not commit anything."
        ]
    else:
        lines += [
            f"- Worktree: `{worktree}` on the temporary branch `{branch}` (from `{base}`). You may change "
            "code and run commands here to test ideas. Do not commit. herdboard removes this worktree and "
            "branch when planning ends, if they are clean. Revert every change you made before you end."
        ]
    lines += [
        f"- The plan: `{plan_file}`",
        f"- Attachments: `{plan_file.parent}`. herdboard copies every file there to each spawned card.",
        "",
    ]
    lines += ["## Details", ""]
    body = item.body.splitlines()
    lines += body if any(line.strip() for line in body) else ["None. The title is the whole task."]
    lines.append("")
    if item.may_merge_prs:
        lines += [
            "The automation allows its agent to merge pull requests whose checks pass and that need no "
            "code change, with `gh pr merge`.",
            "",
        ]
    if item.factory:
        lines += [
            "Each run of this automation is a factory run: lens reviewers check the agent's work, the agent "
            "fixes their findings, and an approver gives a verdict. Write the `Review lenses:` line in the "
            "plan.",
            "",
        ]
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
        "1. Read the automation, its attachments and the code that the work touches. Find the facts in the "
        "repository yourself; ask the user only for decisions.",
        "2. Make a list of the unknowns: unclear behavior, choices between approaches, edge cases, limits.",
        "3. Ask the user about them. Ask a few questions at a time. Give the options and recommend one. Ask a "
        "question that depends on another answer in a later round.",
        "4. Write the plan for an agent that works alone. Name each check, each command and when to stop. Say "
        "what the agent does when something is unclear: it logs the question on its card and moves the card "
        "to Review. Do not invent an answer to a required decision: write it in the plan, or tell the agent "
        "to stop at it.",
        "5. Tell the user when the plan is saved. herdboard records each save in the automation's log.",
        *([] if repoless else ["6. Revert your code changes. Check with `git status` that the worktree is clean."]),
        "",
        f"Write the plan to `{plan_file}` and to no other file. Do not change the automation: its title, "
        "details and schedule are the user's text.",
        f"Read the automation again with `{command} --json automation list`.",
        "",
        "## Plan format",
        "",
        "Write for an agent that has not seen this conversation. Be specific: name files, functions and commands.",
        "",
        PLAN_FORMAT,
    ]
    if item.factory:
        lines += [FACTORY_LENSES_RULE, "", VERIFICATION_RULE]
    return "\n".join(lines)


class AutomationPlanner:
    def __init__(self, store, config_file: Path | None = None):
        self.store = store
        self.config = store.config
        self.config_file = config_file

    def _repository_workspace(self, item: automations.Automation, label: str) -> dict:
        """A temporary worktree of the board's repository, from the main checkout's branch."""
        identity = self.store.board_meta(self.store.resolve_board(item.board)).repo
        if not identity:
            raise PlanError(f"Board {item.board} has no repository", "repo_required")
        checkout = Starter(self.store, self.config.todos_dir)._checkout(identity, None)
        base = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        if not base:
            raise PlanError(f"{checkout} has no branch checked out", "base_unknown")
        branch = plan_branch(item.id)
        _git(checkout, "worktree", "prune")
        existing = None
        for block in (_git(checkout, "worktree", "list", "--porcelain") or "").split("\n\n"):
            block_lines = block.splitlines()
            if f"branch refs/heads/{branch}" in block_lines:
                existing = next(
                    (line.removeprefix("worktree ") for line in block_lines if line.startswith("worktree ")), None
                )
        if existing:
            # A worktree kept from an earlier plan, because it had changes.
            result = herdr(
                "worktree",
                "open",
                "--cwd",
                str(checkout),
                "--path",
                existing,
                "--label",
                label,
                "--no-focus",
                "--trust-repository",
            )
        else:
            result = herdr(
                "worktree",
                "create",
                "--cwd",
                str(checkout),
                "--branch",
                branch,
                "--base",
                base,
                "--label",
                label,
                "--no-focus",
                "--trust-repository",
            )
        try:
            return {
                "workspace_id": result["workspace"]["workspace_id"],
                "pane_id": result["root_pane"]["pane_id"],
                "worktree_path": result.get("worktree", {}).get("path") or existing,
                "checkout": str(checkout),
                "branch": branch,
                "base": base,
            }
        except (KeyError, TypeError, AttributeError) as error:
            raise StartError(f"Unexpected Herdr worktree result: {result}", "herdr_error") from error

    def _folder_workspace(self, item: automations.Automation, label: str) -> dict:
        folder = work_folder(self.config, item.id)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StartError(f"{folder}: cannot create the folder: {error}") from error
        result = herdr("workspace", "create", "--cwd", str(folder), "--label", label, "--no-focus")
        try:
            return {
                "workspace_id": result["workspace"]["workspace_id"],
                "pane_id": result["root_pane"]["pane_id"],
                "worktree_path": str(folder),
            }
        except (KeyError, TypeError, AttributeError) as error:
            raise StartError(f"Unexpected Herdr workspace result: {result}", "herdr_error") from error

    def plan(self, automation_id: str, agent=None, model=None, thinking=None) -> dict:
        item = automations.get(self.config, automation_id)
        kind = agent or item.agent or self.config.start_agent
        if kind not in AGENTS:
            raise PlanError("The planner must be claude, pi or codex", "usage")
        levels = thinking_levels(kind)
        if thinking and thinking not in levels:
            raise PlanError(f"Thinking must be one of {', '.join(levels)}", "usage")
        if model is not None and (not model or any(ch.isspace() for ch in model)):
            raise PlanError("The model must be one name, such as opus or zai/glm-5.3", "usage")
        if kind == "pi":
            # One model for the launch and for its level, as `plan` does for a card.
            model = agent_model(self.config, model)
            thinking = thinking_level(self.config, model, thinking)
        elif thinking is None and self.config.start_thinking in levels:
            thinking = self.config.start_thinking
        ref = item.id
        repoless = item.board == "inbox"
        with _lock(f".plan-{ref}.lock"):
            with _lock(wait=2):
                running = _read(_plans_path()).get(ref)
            if _planner_pane(running) is not None:
                raise PlanError(f"A planner for {ref} still runs in pane {running['pane_id']}", "plan_running")
            label = f"plan {ref}"
            place = self._folder_workspace(item, label) if repoless else self._repository_workspace(item, label)
            worktree = Path(place["worktree_path"])
            pane = place["pane_id"]
            plan_file = attachments.plan_path(self.config, ref)
            brief = write_brief(
                f"{ref}-plan",
                brief_text(
                    item,
                    worktree=worktree,
                    branch=place.get("branch", ""),
                    base=place.get("base", ""),
                    repoless=repoless,
                    plan=attachments.plan_for_agent(self.store, ref),
                    files=attachments.listing(self.config, ref),
                    log=activity.read(self.config, ref),
                    plan_file=plan_file,
                    command=herdboard_command(self.config_file),
                ),
            )
            plan_mtime = _mtime(plan_file)
            prompt = _one_line(
                f"Plan herdboard automation {ref}: {item.title}. Read the brief at {brief} first; "
                "it has the automation and how to plan it with me."
            )
            export_config(pane, self.config_file)
            try:
                herdr(
                    "agent",
                    "start",
                    f"hb-{ref}-plan",
                    "--kind",
                    kind,
                    "--pane",
                    pane,
                    "--",
                    *planner_arguments(kind, prompt, plan_file.parent, model, thinking),
                    tolerated=("timeout", "agent_not_ready"),
                )
            except HerdboardError:
                try:
                    _clean_up(self.store, ref, {**place, "repoless": repoless})
                except HerdboardError:
                    pass
                raise
            with _lock(wait=2):
                records = _read(_plans_path())
                records[ref] = {
                    "pane_id": pane,
                    "kind": kind,
                    "model": model,
                    "started": time.time(),
                    "plan_mtime": plan_mtime,
                    "automation": True,
                    "repoless": repoless,
                    **place,
                }
                _write(_plans_path(), records)
        activity.append(self.store, ref, f"Planning started: {kind}{f' ({model})' if model else ''}.", "herdboard")
        config = ["--config", str(self.config_file)] if self.config_file else []
        log = automations.state_dir() / "plans" / f"{ref}.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            start_worker([*config, "automation", "plan-wait", ref, "--pane", pane], log)
        except OSError as error:
            activity.append(
                self.store,
                ref,
                f"The plan saves are not logged and the worktree stays: the worker did not start: {error}",
                "herdboard",
            )
        return {
            "id": ref,
            "agent": kind,
            "model": model,
            "pane_id": pane,
            "plan": str(plan_file),
            "workspace_id": place["workspace_id"],
            "worktree_path": str(worktree),
            "branch": place.get("branch"),
            "base": place.get("base"),
        }


def _git_status(worktree: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain"], capture_output=True, text=True)
    return None if result.returncode else result.stdout


def _clean_up(store, ref: str, record: dict) -> None:
    """Close the planner's workspace, and remove a clean temporary worktree and its branch."""
    workspace = record.get("workspace_id")
    if record.get("repoless"):
        if workspace:
            herdr("workspace", "close", workspace, tolerated=("workspace_not_found",))
        try:
            Path(record["worktree_path"]).rmdir()
        except OSError:
            # The folder keeps the planner's files.
            pass
        return
    worktree = Path(record["worktree_path"])
    checkout = record["checkout"]
    branch = record["branch"]
    if worktree.exists():
        status = _git_status(worktree)
        if status is None or status.strip():
            activity.append(
                store,
                ref,
                f"Kept the planning worktree {worktree} and branch {branch}: it has changes. Remove them by hand.",
                "herdboard",
            )
            return
        if workspace:
            herdr("worktree", "remove", "--workspace", workspace, tolerated=("workspace_not_found",))
        if worktree.exists():
            removed = subprocess.run(
                ["git", "-C", checkout, "worktree", "remove", str(worktree)], capture_output=True, text=True
            )
            if removed.returncode:
                activity.append(
                    store, ref, f"Kept the planning worktree {worktree}: {removed.stderr.strip()}", "herdboard"
                )
                return
    deleted = subprocess.run(["git", "-C", checkout, "branch", "-d", branch], capture_output=True, text=True)
    if deleted.returncode:
        activity.append(
            store, ref, f"Removed the planning worktree. Kept branch {branch}: {deleted.stderr.strip()}", "herdboard"
        )
        return
    activity.append(store, ref, "Removed the planning worktree and its branch.", "herdboard")


def wait(store, ref: str, pane: str) -> int:
    """The background worker: log each save of the plan, then clean up when the planner ends."""
    with _lock(wait=2):
        record = _read(_plans_path()).get(ref)
    if not isinstance(record, dict) or record.get("pane_id") != pane:
        return plan_module.wait(store, ref, pane)
    code = plan_module.wait(store, ref, pane)
    try:
        # The plan lock keeps a later planner from starting in the worktree while it is cleaned up.
        with _lock(f".plan-{ref}.lock", wait=30):
            with _lock(wait=2):
                still = _read(_plans_path()).get(ref)
            if isinstance(still, dict) and still.get("pane_id") == pane:
                # The planner still runs after the worker's time limit: its worktree stays.
                return code
            if isinstance(still, dict):
                # A later planner started after this one ended, in the same worktree or folder.
                if record.get("repoless") and record.get("workspace_id") not in (None, still.get("workspace_id")):
                    herdr("workspace", "close", record["workspace_id"], tolerated=("workspace_not_found",))
                activity.append(store, ref, f"A later planner uses {record['worktree_path']}; it stays.", "herdboard")
                return code
            _clean_up(store, ref, record)
    except (HerdboardError, OSError) as error:
        print(f"herdboard: cannot clean up the planning workspace: {error}", file=sys.stderr)
        activity.append(store, ref, f"Cannot clean up the planning workspace: {error}", "herdboard")
        return 1
    return code
