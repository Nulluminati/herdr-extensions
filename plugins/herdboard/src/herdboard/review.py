"""Open a started card's work in Hunk, next to the agent that wrote it.

The hunkdiff plugin draws the review. It reads its target from the focused pane, and it
links the review to the agent only when that pane runs an agent, so `send-review` reaches
the agent. herdboard therefore focuses the agent pane first, then invokes the action.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import activity
from .config import thinking_levels
from .linear_cards import LINEAR_REF
from .model import HerdboardError
from .start import (
    NEW_REPO,
    REPOLESS,
    Starter,
    StartError,
    _lock,
    _read,
    _write,
    agent_live,
    fetch_linear_card,
    find_record,
    herdr,
    safe_key,
    state_dir,
)

HUNK_PLUGIN = "jhochenbaum.hunkdiff"
REVIEW_NOTE = "The card is in review. Review feedback follows. Wait for it."
# How long Herdr gets to show a new agent or the Hunk pane.
PANE_WAIT_SECONDS = 10.0


class ReviewError(HerdboardError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class Reviewer:
    def __init__(self, store, directory, writer=None, config_file: Path | None = None):
        self.store = store
        self.config = store.config
        self.directory = Path(directory)
        self.writer = writer
        # Passed on to the agents and workers, so they read the same boards.
        self.config_file = config_file

    def _card(self, ref):
        """The card for a todo ID. A Linear card is fetched only when an agent must start again."""
        if LINEAR_REF.match(ref):
            return None, ref.upper()
        card = self.store.show(ref)
        return card, card.ref

    def _focus_agent(self, pane):
        deadline = time.monotonic() + PANE_WAIT_SECONDS
        while herdr("agent", "focus", pane, tolerated=("agent_not_found", "pane_not_found")) is None:
            if time.monotonic() >= deadline:
                raise ReviewError(
                    f"No agent runs in {pane}, so Hunk comments would have no target", "agent_unavailable"
                )
            time.sleep(0.2)

    def _panes(self, workspace):
        return {pane["pane_id"] for pane in herdr("pane", "list", "--workspace", workspace)["panes"]}

    def _open_hunk(self, record):
        """Invoke hunkdiff's branch review, and return the ID of its pane."""
        workspace = record["workspace_id"]
        before = self._panes(workspace)
        try:
            herdr("plugin", "action", "invoke", "review:branch", "--plugin", HUNK_PLUGIN)
        except StartError as error:
            if error.code in ("plugin_not_found", "plugin_action_not_found"):
                raise ReviewError(
                    f"Install the {HUNK_PLUGIN} Herdr plugin to review in Hunk", "hunk_unavailable"
                ) from error
            raise
        stored = record.get("review_pane")
        if stored in before:
            return stored  # hunkdiff reloads its open pane instead of opening another.
        deadline = time.monotonic() + PANE_WAIT_SECONDS
        while True:
            opened = self._panes(workspace) - before
            if opened:
                return sorted(opened)[0]
            if time.monotonic() >= deadline:
                raise ReviewError("hunkdiff opened no review pane; see `herdr plugin log list`", "hunk_unavailable")
            time.sleep(0.2)

    def _started(self, ref):
        """The card, its ID, and its start record key and record. Stops when the card has no worktree."""
        card, ref = self._card(ref)
        found = find_record(ref)
        if found is None or not found[1].get("worktree_path"):
            raise ReviewError(f"{ref} has no started work; run `herdboard start {ref}` first", "not_started")
        key, record = found
        if not Path(record["worktree_path"]).exists():
            raise ReviewError(f"The worktree of {ref} was removed; run `herdboard start {ref}` again", "not_started")
        return card, ref, key, record

    def _persister(self, key, record):
        state_path = state_dir() / "starts.json"

        def persist():
            with _lock(wait=2):
                records = _read(state_path)
                records[key] = record
                _write(state_path, records)

        return persist

    def _ready_agent(self, card, ref, key, record, persist, focus=True):
        """Open the card's workspace, and start a new agent when the old one is gone.

        With focus False the workspace opens in the background.
        Returns True when a new agent started. The caller holds the card's start lock."""
        starter = Starter(self.store, self.directory, writer=self.writer, config_file=self.config_file)
        if record.get("mode") == REPOLESS:
            folder = Path(record["worktree_path"])
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ReviewError(f"{folder}: cannot create the card folder: {error}", "io") from error
            record.update(starter._repoless_workspace(ref, folder, record, focus=focus))
        else:
            record.update(starter._workspace(Path(record["checkout"]), record["branch"], record, focus=focus))
        persist()
        if agent_live(record):
            return False
        kind = record.get("agent") or self.config.start_agent
        model = self.config.start_model if kind == self.config.start_agent else None
        thinking = self.config.start_thinking if self.config.start_thinking in thinking_levels(kind) else None
        starter.launch_agent(
            card or fetch_linear_card(self.writer, self.config, ref),
            record,
            kind,
            model,
            thinking,
            safe_key(key),
            persist,
            linear=card is None,
            note=REVIEW_NOTE,
            log=activity.read(self.config, ref),
        )
        activity.append(self.store, ref, "Agent restarted for review.", "herdboard")
        return True

    def review(self, ref):
        card, ref, key, record = self._started(ref)
        if record.get("mode") == NEW_REPO:
            raise ReviewError(
                f"{ref} has no repository yet: `herdboard start {ref}` creates it. Start the card, then review it.",
                "not_started",
            )
        if record.get("mode") == REPOLESS:
            raise ReviewError(
                f"{ref} has no repository, so Hunk has no branch to show. "
                f"Review its attachments with `herdboard review {ref} --agent`, "
                f"then run `herdboard finish {ref}`.",
                "no_repository",
            )
        persist = self._persister(key, record)
        with _lock(".start-" + safe_key(key) + ".lock"):
            restarted = self._ready_agent(card, ref, key, record, persist)
            self._focus_agent(record["pane_id"])
            record["review_pane"] = self._open_hunk(record)
            persist()
        activity.append(self.store, ref, "Review opened.", "herdboard")
        return {
            "ref": ref,
            "workspace_id": record["workspace_id"],
            "pane_id": record["pane_id"],
            "review_pane": record["review_pane"],
            "agent_restarted": restarted,
        }
