"""Merge a started card's branch into its base, and clean up after the merge.

A local merge rebases the branch onto its base in the card's worktree, then fast-forwards
the base. A conflict happens in the worktree, where the card's agent can fix it, and the
base never gets a merge commit. A pull request merge has an agent write the description,
then pushes the branch and opens a draft GitHub pull request with `gh` (see `pr_write`).
`finish` removes the worktree and the branch, and moves the card to Done.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

from . import activity, pr_write
from .linear_cards import LINEAR_REF, issue_card
from .linear_states import refuse
from .model import HerdboardError
from .start import (Starter, StartError, _lock, _read, _write, agent_live, card_changed, close_review_pane,
                    fetch_linear_card, find_record, herdr, safe_key, state_dir)


# The board reads the state of an open pull request at most this often.
PR_CHECK_SECONDS = 60


class MergeError(HerdboardError):
    def __init__(self, message, code, exit_code=1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _git(directory, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)


def _dirty(directory, untracked=True) -> bool:
    mode = "--untracked-files=normal" if untracked else "--untracked-files=no"
    return bool(_git(directory, "status", "--porcelain", mode).stdout.strip())


def _commit(directory, ref) -> str | None:
    result = _git(directory, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def _gh(directory, *args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], cwd=str(directory), capture_output=True, text=True, timeout=60)
    except FileNotFoundError as error:
        raise MergeError("The GitHub CLI `gh` is not installed", "gh_unavailable") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MergeError(f"gh failed: {error}", "pr_failed") from error


def _gh_output(directory, *args) -> str:
    result = _gh(directory, *args)
    if result.returncode:
        raise MergeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}", "pr_failed")
    return result.stdout


def push_branch(worktree, branch, base) -> None:
    """Push the branch for a pull request, and keep its upstream on the base."""
    pushed = _git(worktree, "push", "--quiet", "origin", f"{branch}:refs/heads/{branch}")
    if pushed.returncode:
        raise MergeError(f"Cannot push {branch}: {pushed.stderr.strip()}", "push_failed")
    # Hunk and `git status` compare the branch with its upstream: a push must not change it.
    _git(worktree, "branch", f"--set-upstream-to={base}", branch)


def refresh_pr_states(now: float | None = None) -> None:
    """Read the state of each open pull request whose last read is a minute old. Best effort."""
    now = time.time() if now is None else now
    path = state_dir() / "starts.json"
    try:
        records = _read(path)
    except HerdboardError:
        return
    states = {}
    for key, record in records.items():
        if not (isinstance(record, dict) and record.get("pr_url") and record.get("pr_state") == "open"
                and now - record.get("pr_checked", 0) >= PR_CHECK_SECONDS):
            continue
        try:
            result = _gh(record["checkout"], "pr", "view", record["pr_url"], "--json", "state")
            state = json.loads(result.stdout)["state"].lower() if result.returncode == 0 else "open"
        except MergeError:
            return
        except (ValueError, KeyError, TypeError, AttributeError):
            state = "open"
        states[key] = (record["pr_url"], state)
    if not states:
        return
    try:
        with _lock(wait=2):
            records = _read(path)
            for key, (url, state) in states.items():
                record = records.get(key)
                if isinstance(record, dict) and record.get("pr_url") == url:
                    record["pr_state"] = state
                    record["pr_checked"] = now
            _write(path, records)
    except HerdboardError:
        return


def checkout_of(repository, branch) -> Path | None:
    """The checkout or worktree that has `branch` checked out, if one has."""
    output = _git(repository, "worktree", "list", "--porcelain").stdout
    for block in output.split("\n\n"):
        lines = block.splitlines()
        if f"branch refs/heads/{branch}" in lines:
            path = next((l.removeprefix("worktree ") for l in lines if l.startswith("worktree ")), None)
            if path and Path(path).exists():
                return Path(path)
    return None


class Merger:
    def __init__(self, store, directory, writer=None, ask_state=refuse, config_file: Path | None = None):
        self.store = store
        self.config = store.config
        self.directory = Path(directory)
        self.writer = writer
        self.ask_state = ask_state
        # Passed on to the pull request worker, so it reads the same boards.
        self.config_file = config_file

    def _started(self, ref):
        """The card (None for Linear until needed), its reference, and its start record."""
        card = None
        if LINEAR_REF.match(ref):
            ref = ref.upper()
        else:
            card = self.store.show(ref)
            ref = card.ref
        found = find_record(ref)
        if found is None or not found[1].get("branch") or not found[1].get("checkout"):
            raise MergeError(f"{ref} has no started work; run `herdboard start {ref}` first", "not_started")
        key, record = found
        if not record.get("base"):
            # A record from before `start` recorded the base: the branch upstream is the base,
            # when it is a local branch.
            upstream = _git(record["checkout"], "rev-parse", "--abbrev-ref", f"{record['branch']}@{{u}}")
            name = upstream.stdout.strip()
            if upstream.returncode or _commit(record["checkout"], f"refs/heads/{name}") is None:
                raise MergeError(f"The start record of {ref} has no base, and its branch has no local upstream; "
                                 f"run `herdboard start {ref} --base BRANCH`", "base_unknown")
            record["base"] = name
        return card, ref, key, record

    def _card(self, card, ref):
        return card if card is not None else fetch_linear_card(self.writer, self.config, ref)

    def _persist(self, key, record):
        path = state_dir() / "starts.json"
        with _lock(wait=2):
            records = _read(path)
            records[key] = record
            _write(path, records)

    def _move(self, card, ref, column):
        """Move the card to `column` and follow the change in Herdr. A card already there stays."""
        if card is not None:
            if card.column.casefold() != column.casefold():
                card = self.store.move(ref, column)
        else:
            card = self._card(None, ref)
            if card.column.casefold() != column.casefold():
                issue = self.writer.move(ref, column, ask=self.ask_state)
                card = issue_card(self.config, issue, set(), datetime.now(timezone.utc), False, show_canceled=True)
        card_changed(card)
        return card

    def _send_conflict(self, card, ref, key, record, base, files):
        names = ", ".join(files) or "unknown files"
        text = (f"herdboard could not merge {ref}: a rebase onto `{base}` conflicts in {names}. "
                f"Run `git rebase {base}` in this worktree, fix the conflicts, commit, and move the card to Review.")
        card = self._move(card, ref, self.config.start_column)
        if agent_live(record):
            herdr("agent", "prompt", record["pane_id"], text)
        else:
            kind = record.get("agent") or self.config.start_agent
            Starter(self.store, self.directory, writer=self.writer, config_file=self.config_file).launch_agent(
                card, record, kind, self.config.start_model,
                self.config.start_thinking if kind == "pi" else None, safe_key(key),
                lambda: self._persist(key, record), linear=card.source == "linear", note=text,
                log=activity.read(self.config, ref))
        activity.append(self.store, ref, f"Rebase conflict in {names}. Sent to the agent.", "herdboard")
        return text

    def mode(self, card, record):
        """The merge mode: the board's `merge:` frontmatter, else `merge` in the config."""
        path = card.path if card is not None else next(
            (p for p in self.store.board_paths() if self.store.board_meta(p).repo == record.get("repository")), None)
        meta = self.store.board_meta(path) if path is not None else None
        return (meta.merge if meta else None) or self.config.merge

    def merge(self, ref, mode=None, force=False, rewrite=False):
        card, ref, key, record = self._started(ref)
        if card is not None and not force:
            open_children = [child.ref for child in self.store.children(ref) if not child.checked]
            if open_children:
                raise MergeError(f"Sub-cards of {ref} are not done: {', '.join(open_children)}. "
                                 "Finish them first, or use --force.", "children_open")
        mode = mode or self.mode(card, record)
        if rewrite and mode != "pr":
            raise MergeError("--rewrite works only for a pull request", "usage")
        worktree = Path(record.get("worktree_path") or "")
        if not record.get("worktree_path") or not worktree.exists():
            raise MergeError(f"The worktree of {ref} was removed; run `herdboard start {ref}` again", "not_started")
        branch, base, checkout = record["branch"], record["base"], Path(record["checkout"])
        with _lock(".start-" + safe_key(key) + ".lock"):
            if _dirty(worktree):
                raise MergeError(f"{worktree} has uncommitted changes. The agent must commit them first.",
                                 "worktree_dirty")
            ahead = _git(checkout, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
            if ahead == "0":
                raise MergeError(f"{branch} has no commits ahead of {base}", "nothing_to_merge")
            if mode == "pr":
                return {"ref": ref, "mode": "pr", "base": base,
                        **self._merge_pr(card, ref, key, record, worktree, branch, base, rewrite)}
            commit = self._merge_local(card, ref, key, record, worktree, checkout, branch, base)
            finished = self._finish(card, ref, key, record, force=False)
        return {"ref": ref, "mode": "local", "base": base, "commit": commit, **finished}

    def sync_pr(self, ref, fetch=False):
        """Send the card's written description to its open pull request (see `pr_write.sync`)."""
        return pr_write.sync(self, ref, fetch=fetch)

    def _merge_pr(self, card, ref, key, record, worktree, branch, base, rewrite):
        """Push to an open pull request, or have an agent write the description of a new one (or of the
        open one, with `rewrite`)."""
        pr_write.check_running(ref, record)
        url = pr_write.open_url(worktree, branch, record)
        if url is not None and not rewrite:
            push_branch(worktree, branch, base)
            record.update(pr_url=url, pr_state="open", pr_checked=time.time())
            self._persist(key, record)
            activity.append(self.store, ref, f"PR updated: {url}", "herdboard")
            return {"pr_url": url, "pr_action": "pushed"}
        written = pr_write.request(self, self._card(card, ref), ref, key, record, worktree, base, url,
                                   lambda: self._persist(key, record))
        return {"pr_url": url, "pr_action": "writing", **written}

    def _merge_local(self, card, ref, key, record, worktree, checkout, branch, base):
        target = checkout_of(checkout, base)
        if target is not None and _dirty(target, untracked=False):
            raise MergeError(f"{target} has uncommitted changes on {base}. Commit or stash them first.",
                             "base_dirty")
        old = _commit(checkout, base)
        if _git(worktree, "rebase", base).returncode:
            conflicted = _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()
            _git(worktree, "rebase", "--abort")
            self._send_conflict(card, ref, key, record, base, conflicted)
            raise MergeError(f"Rebase of {branch} onto {base} conflicts in {', '.join(conflicted)}. "
                             "The card's agent has the conflict.", "rebase_conflict", exit_code=2)
        head = _commit(worktree, "HEAD")
        if target is not None:
            result = _git(target, "merge", "--ff-only", "--quiet", branch)
        else:
            # The old value makes Git refuse the update if the base moved after the rebase.
            result = _git(checkout, "update-ref", f"refs/heads/{base}", head, old)
        if result.returncode:
            raise MergeError(f"Cannot fast-forward {base} to {branch}: {result.stderr.strip()}", "merge_failed")
        activity.append(self.store, ref, f"Merged into {base} at {head[:7]}.", "herdboard")
        return head

    def finish(self, ref, force=False):
        card, ref, key, record = self._started(ref)
        with _lock(".start-" + safe_key(key) + ".lock"):
            result = self._finish(card, ref, key, record, force=force)
        return {"ref": ref, **result}

    def _finish(self, card, ref, key, record, force):
        branch, base, checkout = record["branch"], record["base"], Path(record["checkout"])
        exists = _commit(checkout, f"refs/heads/{branch}") is not None
        pr_url = record.get("pr_url")
        if pr_url:
            state = json.loads(_gh_output(checkout, "pr", "view", pr_url, "--json", "state"))["state"]
            merged = state == "MERGED"
            target = f"Pull request {pr_url} is {state.lower()}"
        else:
            merged = not exists or _git(checkout, "merge-base", "--is-ancestor", branch, base).returncode == 0
            target = f"{branch} is not merged into {base}"
        if not merged and not force:
            raise MergeError(f"{target}. Merge it first, or use --force.", "not_merged")
        worktree = Path(record["worktree_path"]) if record.get("worktree_path") else None
        present = worktree is not None and worktree.exists()
        if present and _dirty(worktree):
            raise MergeError(f"{worktree} has uncommitted changes. Commit or remove them first.", "worktree_dirty")
        close_review_pane(ref)
        if present:
            self._remove_worktree(record, worktree)
        _git(checkout, "worktree", "prune")
        deleted = False
        if exists and merged and pr_url:
            # GitHub can squash or rebase the pull request, so Git does not see the branch as merged.
            deleted = _git(checkout, "branch", "-D", branch).returncode == 0
            if _git(checkout, "ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0:
                _git(checkout, "push", "--quiet", "origin", "--delete", branch)
        elif exists and merged:
            deleted = _git(checkout, "branch", "-d", branch).returncode == 0
        elif exists:
            activity.append(self.store, ref, f"Branch {branch} kept: it is not merged.", "herdboard")
        self._done(card, ref)
        activity.append(self.store, ref, "Finished.", "herdboard")
        with _lock(wait=2):
            records = _read(state_dir() / "starts.json")
            records.pop(key, None)
            _write(state_dir() / "starts.json", records)
        return {"worktree_removed": present, "branch_deleted": deleted}

    def _remove_worktree(self, record, worktree):
        try:
            herdr("worktree", "remove", "--workspace", record["workspace_id"],
                  tolerated=("workspace_not_found",))
        except StartError as error:
            if error.code == "dirty_worktree_requires_force":
                raise MergeError(f"{worktree} has uncommitted changes. Commit or remove them first.",
                                 "worktree_dirty") from error
            raise
        if worktree.exists():
            # Herdr no longer has the workspace open: remove the worktree with Git.
            removed = _git(record["checkout"], "worktree", "remove", str(worktree))
            if removed.returncode:
                raise MergeError(f"Cannot remove {worktree}: {removed.stderr.strip()}", "worktree_dirty")

    def _done(self, card, ref):
        if card is not None:
            current = self.store.show(ref)
            if not current.checked:
                card_changed(self.store.done(ref))
            return
        card = self._card(None, ref)
        # Linear's GitHub integration can move the issue to Done when its pull request merges.
        if card.column.casefold() != self.config.linear.columns["completed"].casefold():
            self.writer.done(ref, self.ask_state)
