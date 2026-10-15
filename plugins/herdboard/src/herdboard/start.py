"""Resume card starts through Herdr without importing the board UI."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import attachments
from .brief import agent_message, brief_text, card_token
from .config import THINKING_LEVELS, default_config_path
from .identity import _git, repo_identity
from .linear_cards import LINEAR_REF, issue_card
from .linear_source import state_dir
from .linear_states import StateChoices, refuse, resolve_state
from .model import ConflictError, HerdboardError, IoError, NotFoundError
from .store import TEMP_REF, CardView

# A card's Hunk review pane stays open only while the card is in this column.
REVIEW_COLUMN = "Review"

# The pane token that shows a started card in the Herdr sidebar: `$herdboard_card`.
CARD_TOKEN = "herdboard_card"
TOKEN_SOURCE = "user:herdboard"


class StartError(HerdboardError):
    def __init__(self, message, code="start_failed"):
        super().__init__(message)
        self.code = code
        self.steps_done = []


def command(argv: list[str], timeout: int = 120):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def herdr(*args, tolerated=()):
    try:
        result = command([os.environ.get("HERDR_BIN_PATH") or "herdr", *args])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StartError(f"Herdr is not reachable: {error}", "herdr_unavailable") from error
    output = result.stdout if result.stdout.strip() else result.stderr
    try:
        data = json.loads(output)
    except (ValueError, TypeError) as error:
        raise StartError(f"Herdr returned no valid JSON: {output}", "herdr_error") from error
    if not isinstance(data, dict):
        raise StartError(f"Unexpected Herdr response: {output}", "herdr_error")
    if result.returncode or "error" in data:
        details = data.get("error", {})
        if not isinstance(details, dict):
            raise StartError(f"Unexpected Herdr error: {output}", "herdr_error")
        code = details.get("code", "herdr_error")
        if code in tolerated:
            return None
        if code in ("server_unavailable", "server_not_running"):
            code = "herdr_unavailable"
        raise StartError(details.get("message", f"Herdr: {code}: {output}"), code)
    found = data.get("result")
    if not isinstance(found, dict):
        raise StartError(f"Unexpected Herdr result: {output}", "herdr_error")
    operation = tuple(args[:2])
    lists = {("workspace", "list"): ("workspaces", "workspace_id"),
             ("worktree", "list"): ("worktrees", "path"),
             ("pane", "list"): ("panes", "pane_id")}
    if operation in lists:
        field, key = lists[operation]
        entries = found.get(field)
        if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get(key), str)
                                               or not e[key] for e in entries):
            raise StartError(f"Unexpected Herdr {field}: {output}", "herdr_error")
    if operation == ("worktree", "list") and any(
            e.get("open_workspace_id") is not None and not isinstance(e["open_workspace_id"], str)
            for e in found["worktrees"]):
        raise StartError(f"Unexpected Herdr workspace ID: {output}", "herdr_error")
    if operation == ("workspace", "get"):
        root = found.get("root_pane")
        if root is not None and (not isinstance(root, dict) or not isinstance(root.get("pane_id"), str)):
            raise StartError(f"Unexpected Herdr root pane: {output}", "herdr_error")
    if operation == ("tab", "create"):
        root = found.get("root_pane")
        if not isinstance(root, dict) or not isinstance(root.get("pane_id"), str):
            raise StartError(f"Unexpected Herdr root pane: {output}", "herdr_error")
    if operation == ("agent", "get") and not isinstance(found.get("agent"), (dict, type(None))):
        raise StartError(f"Unexpected Herdr agent: {output}", "herdr_error")
    return found


def herdboard_command(config_file: Path | None = None) -> str:
    """How an agent runs this herdboard: the installed command, else this Python with `-m`.

    A `herdboard` inside this Python's own environment is on the path only while
    `uv run` runs it, so the agent's shell would not find it. A config file that is not the
    default one is named, so the agent reads the same boards. That includes one from
    HERDBOARD_CONFIG: an agent that Herdr starts may not have the variable.
    """
    found = shutil.which("herdboard")
    if found and not Path(found).resolve().is_relative_to(Path(sys.prefix).resolve()):
        program = "herdboard"
    else:
        program = f"{shlex.quote(sys.executable)} -m herdboard"
    if config_file is not None and Path(config_file) != default_config_path():
        program += f" --config {shlex.quote(str(config_file))}"
    return program


def pi_model_choices(config) -> tuple[str | None, list[str]]:
    """pi's default model as `provider/model, thinking`, and the models to offer.

    The models come from `pi_models` in the config, else from pi's `enabledModels`.
    """
    try:
        settings = json.loads((Path.home() / ".pi" / "agent" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    default = None
    if isinstance(settings.get("defaultModel"), str):
        default = settings["defaultModel"]
        if isinstance(settings.get("defaultProvider"), str):
            default = f"{settings['defaultProvider']}/{default}"
        if isinstance(settings.get("defaultThinkingLevel"), str):
            default += f", {settings['defaultThinkingLevel']}"
    models = config.pi_models
    if models is None:
        found = settings.get("enabledModels")
        models = [m for m in found if isinstance(m, str) and m] if isinstance(found, list) else []
    return default, list(models)


def agent_arguments(agent: str, message: str, model: str | None, thinking: str | None) -> list[str]:
    """The agent's command-line arguments after Herdr's `--`."""
    if agent == "pi":
        options = (["--model", model] if model else []) + (["--thinking", thinking] if thinking else [])
        return [*options, "--", message]
    # Claude takes its prompt first: options after it cannot swallow the prompt.
    return [message, *(["--model", model] if model else [])]


def export_config(pane_id: str, config_file: Path | None) -> None:
    """Set HERDBOARD_CONFIG in the pane's shell when the config file is not the default one.

    The agent that starts in the pane then reads the same boards, also with commands that are
    not in its brief. Herdr cannot set the environment of a worktree's pane, so the shell exports
    the variable. `export` works in bash, zsh and fish. Best effort: the brief names the file too.
    """
    if config_file is None or Path(config_file) == default_config_path():
        return
    try:
        command([os.environ.get("HERDR_BIN_PATH") or "herdr", "pane", "run", pane_id,
                 f"export HERDBOARD_CONFIG={shlex.quote(str(config_file))}"])
    except (OSError, subprocess.TimeoutExpired):
        pass


def write_brief(name: str, text: str) -> Path:
    """Write an agent brief to the state folder and return its path."""
    path = state_dir() / "briefs" / f"{name}.md"
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
        temp = None
    except OSError as error:
        raise IoError(f"{path}: cannot write the agent brief: {error}") from error
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
    return path


def report_card_token(pane_id: str, token: str | None) -> None:
    """Set or clear the sidebar card on an agent's pane. Best effort: the sidebar is display only."""
    change = ["--token", f"{CARD_TOKEN}={token}"] if token else ["--clear-token", CARD_TOKEN]
    try:
        command([os.environ.get("HERDR_BIN_PATH") or "herdr", "pane", "report-metadata", pane_id,
                 "--source", TOKEN_SOURCE, "--seq", str(time.time_ns()), *change])
    except (OSError, subprocess.TimeoutExpired):
        pass


def update_card_token(card: CardView, removed: bool = False) -> None:
    """Show a started card's current column in its agent's sidebar row, or clear it."""
    try:
        records = _read(state_dir() / "starts.json")
    except HerdboardError:
        return
    for key, record in records.items():
        if not isinstance(record, dict) or card.ref not in (key, record.get("ref")):
            continue
        if record.get("agent_name") and isinstance(record.get("pane_id"), str):
            report_card_token(record["pane_id"], None if removed else card_token(card))


def find_record(ref):
    """The start record of a card as (key, record), or None. Linear records are keyed by issue ID."""
    records = _read(state_dir() / "starts.json")
    record = records.get(ref)
    if isinstance(record, dict):
        return ref, record
    for key, record in records.items():
        if isinstance(record, dict) and record.get("ref") == ref:
            return key, record
    return None


def safe_key(key):
    """A record key that is safe in a file name: for the per-card lock and the brief."""
    return key if re.fullmatch(r"[a-zA-Z0-9_-]+", key) else hashlib.sha256(key.encode()).hexdigest()


def close_review_pane(ref) -> None:
    """Close a card's Hunk review pane and forget it. Best effort: the pane may be gone already."""
    path = state_dir() / "starts.json"
    try:
        with _lock(wait=2):
            records = _read(path)
            found = find_record(ref)
            if found is None or not found[1].get("review_pane"):
                return
            key, record = found
            pane = record.pop("review_pane")
            records[key] = record
            _write(path, records)
    except HerdboardError:
        return
    try:
        command([os.environ.get("HERDR_BIN_PATH") or "herdr", "plugin", "pane", "close", pane])
    except (OSError, subprocess.TimeoutExpired):
        pass


def card_changed(card: CardView, removed: bool = False) -> None:
    """Follow a card change in Herdr: the sidebar row, and the review pane once the card leaves Review."""
    update_card_token(card, removed=removed)
    if removed or card.column.casefold() != REVIEW_COLUMN.casefold():
        close_review_pane(card.ref)


def fetch_linear_card(writer, config, ref) -> CardView:
    """A Linear issue as a card, read from Linear. Needs a connection."""
    if writer is None:
        raise StartError("Linear writes are unavailable", "linear_disabled")
    with writer.online() as client:
        issue = client.issue(ref)
    if issue is None:
        raise NotFoundError(f"No card {ref}")
    return issue_card(config, issue, set(), datetime.now(timezone.utc), False, show_canceled=True)


def agent_live(record) -> bool:
    """True when the agent that `start` recorded still runs, in its pane or under its name."""
    if not record.get("agent_name"):
        return False
    tolerated = ("agent_not_found", "pane_not_found")
    pane_live = herdr("agent", "get", record["pane_id"], tolerated=tolerated)
    named_live = herdr("agent", "get", record["agent_name"], tolerated=tolerated)
    live = named_live or pane_live
    return bool(live and live.get("agent"))


def archive_blocker(ref) -> str | None:
    """Why a card must stay on the board: its planner still runs, its worktree still exists, or its agent still runs."""
    # `plan` imports this module, so it loads here.
    from .plan import running_planner

    pane = running_planner(ref)
    if pane is not None:
        return f"its planner still runs in pane {pane}"
    found = find_record(ref)
    if found is None:
        return None
    record = found[1]
    path = record.get("worktree_path")
    if path and Path(path).exists():
        return f"its worktree still exists at {path}. Finish the card first"
    if agent_live(record):
        return "its agent still runs"
    return None


def card_agent_panes() -> set[str]:
    """The panes where `start` started an agent for a card."""
    try:
        records = _read(state_dir() / "starts.json")
    except HerdboardError:
        return set()
    return {record["pane_id"] for record in records.values()
            if isinstance(record, dict) and record.get("agent_name") and isinstance(record.get("pane_id"), str)}


def _read(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data
    except (OSError, ValueError) as error:
        raise IoError(f"{path}: cannot read start state: {error}") from error


def _write(path, data):
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as error:
        raise IoError(f"{path}: cannot write start state: {error}") from error
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def _lock(name=".starts.lock", wait=0):
    folder = state_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle = (folder / name).open("a")
    except OSError as error:
        raise IoError(f"{folder}: cannot open start lock: {error}") from error
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConflictError("Another start or checkout update is in progress; retry") from None
                time.sleep(0.01)
        yield
    finally:
        handle.close()


def _main_checkout(directory):
    common = _git(Path(directory), "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    path = Path(common)
    main = path.parent if path.name == ".git" else path
    top = _git(main, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == main.resolve():
        return main
    current = _git(Path(directory), "rev-parse", "--show-toplevel")
    return Path(current) if current else None


def _remember(directory, expected=None):
    identity = repo_identity(Path(directory))
    if identity is None or (expected is not None and identity.identity != expected):
        raise StartError(f"{directory}: checkout does not match repository {expected}", "checkout_unknown")
    main = _main_checkout(directory)
    if main is None:
        raise StartError(f"{directory}: not a working checkout", "checkout_unknown")
    path = state_dir() / "checkouts.json"
    with _lock(wait=0.2):
        data = _read(path)
        entries = [e for e in _entries(data.get(identity.identity)) if Path(e["path"]).resolve() != main.resolve()]
        entries.append({"path": str(main), "last_seen": time.time()})
        data[identity.identity] = entries
        _write(path, data)
    return main


def _entries(entries):
    return [e for e in entries if isinstance(e, dict)
            and isinstance(e.get("path"), str) and e["path"]
            and isinstance(e.get("last_seen"), (int, float))] if isinstance(entries, list) else []


def register_checkout(directory):
    try:
        if repo_identity(Path(directory)) is not None:
            _remember(directory)
    except (HerdboardError, OSError, ValueError, TypeError):
        # Registration must not prevent read-only commands or agent updates.
        pass


def todo_branch(card):
    text = re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)|https?://\S+", "", card.title)
    text = re.sub(r"#[\w/-]+|📅\s*\d{4}-\d{2}-\d{2}|\^\w+", "", text)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (card.ref + ("-" + slug if slug else ""))[:50].rstrip("-")


def validate_branch(branch):
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise StartError(f"Invalid branch: {branch!r}", "invalid_branch")
    result = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True, text=True)
    # @{-1} is accepted by Git in a checkout, but is not a literal branch name.
    if result.returncode or branch.startswith("@{"):
        raise StartError(f"Invalid branch: {branch!r}", "invalid_branch")


def mark_removed(cards):
    """Set the board-only start status of cards: a removed worktree, a worktree left after Done, the PR state,
    a running agent review, and a pull request description that an agent writes."""
    records = _read(state_dir() / "starts.json")
    for card in cards:
        record = records.get(card.ref)
        if not isinstance(record, dict):
            record = next((r for r in records.values() if isinstance(r, dict) and r.get("ref") == card.ref), {})
        path = record.get("worktree_path")
        card.worktree_removed = bool(path and not Path(path).exists())
        card.worktree_left = bool(path and card.checked and Path(path).exists())
        card.pr_state = record.get("pr_state") if record.get("pr_url") else None
        review = record.get("agent_review")
        card.agent_review = review.get("kind") if isinstance(review, dict) else None
        card.pr_writing = isinstance(record.get("pr_write"), dict)


class Starter:
    def __init__(self, store, directory, writer=None, ask_state=refuse,
                 prompt_checkout=None, repository_view=True, config_file: Path | None = None):
        self.store = store
        self.config = store.config
        self.directory = Path(directory)
        self.writer = writer
        self.ask_state = ask_state
        self.prompt_checkout = prompt_checkout
        self.repository_view = repository_view
        # Named in the agent's brief, so the agent reads the same boards.
        self.config_file = config_file

    def _repository(self, card, repo, issue=None):
        if card.path is not None:
            own = self.store.board_meta(card.path).repo
            if own:
                return own
        if repo:
            value = str(repo)
            if "/" not in value and not value.startswith(("~", ".")):
                try:
                    meta = self.store.board_meta(self.store.resolve_board(value))
                except NotFoundError:
                    pass
                else:
                    if meta.repo:
                        return meta.repo
                    raise StartError(f"Board {value} has no repository", "repo_required")
            path = Path(repo).expanduser()
            if path.is_file():
                meta = self.store.board_meta(path)
                if meta.repo:
                    return meta.repo
            if path.exists():
                identity = repo_identity(path)
                if identity:
                    _remember(path)
                    return identity.identity
                raise StartError(f"{path}: not a checkout", "checkout_unknown")
            if any(self.store.board_meta(p).repo == value for p in self.store.board_paths()):
                return value
        matches = []
        if card.source == "linear":
            for path in self.store.board_paths():
                meta = self.store.board_meta(path)
                project = (issue or {}).get("project") or {}
                labels = {label["id"] for label in (issue or {}).get("labels", [])}
                if meta.repo and any(link.id == project.get("id") and
                                     (not link.labels or any(label.id in labels for label in link.labels))
                                     for link in meta.links):
                    matches.append(meta.repo)
            if not repo and self.repository_view:
                identity = repo_identity(self.directory)
                if identity and identity.identity in matches:
                    return identity.identity
        listing = f" Linked repositories: {', '.join(sorted(set(matches)))}." if matches else ""
        raise StartError("Choose a repository with --repo BOARD_OR_PATH." + listing, "repo_required")

    def _checkout(self, identity, explicit, record=None):
        if explicit:
            return _remember(Path(explicit).expanduser(), identity)
        stored = (record or {}).get("checkout")
        if stored:
            path = Path(stored)
            found = repo_identity(path) if path.exists() else None
            if found and found.identity == identity:
                return path
        entries = _entries(_read(state_dir() / "checkouts.json").get(identity))
        for entry in sorted(entries, key=lambda e: e["last_seen"], reverse=True):
            path = Path(entry["path"])
            found = repo_identity(path) if path.exists() else None
            if found and found.identity == identity:
                return path
        if self.prompt_checkout:
            answer = self.prompt_checkout(identity)
            if answer:
                return _remember(Path(answer).expanduser(), identity)
        raise StartError(f"No local checkout for {identity}; use --checkout PATH", "checkout_unknown")

    def _parent_branch(self, card, repo, checkout):
        """A sub-card starts from its parent's branch. A parent without one starts first, with no agent."""
        found = find_record(card.parent)
        if found is None or not found[1].get("branch"):
            self.start(card.parent, no_agent=True, repo=repo, checkout=checkout)
            found = find_record(card.parent)
        return found[1]["branch"]

    def _base(self, checkout, wanted, branch):
        """The local branch that the card branch starts from and merges back into."""
        if wanted is None:
            wanted = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
            if not wanted:
                raise StartError(f"{checkout} has no branch checked out; use --base BRANCH", "base_unknown")
        if wanted == branch or _git(checkout, "rev-parse", "--verify", "--quiet", f"refs/heads/{wanted}") is None:
            raise StartError(f"No local branch {wanted!r} to start from; use --base with a local branch",
                             "base_unknown")
        return wanted

    def _workspace(self, checkout, branch, record):
        listed = herdr("worktree", "list", "--cwd", str(checkout), "--trust-repository")
        worktrees = listed.get("worktrees", [])
        stored = record.get("worktree_path")
        path = stored if stored and Path(stored).exists() else None
        if path is None:
            output = _git(checkout, "worktree", "list", "--porcelain") or ""
            for block in output.split("\n\n"):
                lines = block.splitlines()
                if f"branch refs/heads/{branch}" in lines and not any(l.startswith("prunable") for l in lines):
                    candidate = next((l.removeprefix("worktree ") for l in lines if l.startswith("worktree ")), None)
                    if candidate and Path(candidate).exists():
                        path = candidate
                        break
        if path:
            entry = next((w for w in worktrees if Path(w["path"]).resolve() == Path(path).resolve()), None)
            workspace = entry.get("open_workspace_id") if entry else None
            if workspace:
                herdr("workspace", "focus", workspace)
                info = herdr("workspace", "get", workspace)
                root = (info.get("root_pane") or {}).get("pane_id")
                if root is None:
                    panes = herdr("pane", "list", "--workspace", workspace).get("panes", [])
                    root = record.get("pane_id")
                    if not any(p["pane_id"] == root for p in panes):
                        root = panes[0]["pane_id"] if panes else None
                if not root:
                    raise StartError(f"Workspace {workspace} has no root pane")
                return {"workspace_id": workspace, "pane_id": root, "worktree_path": str(path)}
            result = herdr("worktree", "open", "--cwd", str(checkout), "--path", str(path), "--focus", "--trust-repository")
        else:
            if _git(checkout, "worktree", "prune") is None:
                raise StartError(f"{checkout}: cannot prune worktrees")
            result = herdr("worktree", "create", "--cwd", str(checkout), "--branch", branch,
                           "--base", record["base"], "--focus", "--trust-repository")
        try:
            workspace = result["workspace"]["workspace_id"]
            pane = result["root_pane"]["pane_id"]
            worktree = result.get("worktree", {}).get("path", path)
            if not all(isinstance(v, str) and v for v in (workspace, pane, worktree)):
                raise ValueError("missing workspace, root pane or worktree path")
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise StartError(f"Unexpected Herdr worktree result: {result}", "herdr_error") from error
        return {"workspace_id": workspace, "pane_id": pane, "worktree_path": worktree}

    def launch_agent(self, card, record, agent, model, thinking, lock_key, persist, linear=False,
                     note=None, log=()):
        """Start an agent in the record's pane, with a brief file for the card."""
        name = "hb-" + card.ref.lower()
        # Herdr rejects line breaks in agent arguments, so the details
        # go to a brief file and the message is one line that names it.
        notes = []
        if not linear:
            try:
                notes = self.store.notes_for(card)
            except HerdboardError:
                notes = []
        brief = write_brief(lock_key, brief_text(
            card, notes=notes, repository=record["repository"],
            worktree=record["worktree_path"], branch=record["branch"], base=record["base"],
            command=herdboard_command(self.config_file), note=note, log=log,
            plan=attachments.plan_for_agent(self.store, card.ref),
            attachments=attachments.listing(self.config, card.ref),
        ))
        message = agent_message(card, brief)
        record["agent_name"] = name
        record["agent"] = agent
        # Record intent before Herdr can return an unknown startup result.
        persist()
        args = agent_arguments(agent, message, model, thinking)
        export_config(record["pane_id"], self.config_file)
        herdr("agent", "start", name, "--kind", agent, "--pane", record["pane_id"], "--", *args,
              tolerated=("timeout", "agent_not_ready"))

    def start(self, ref, agent=None, no_agent=False, repo=None, checkout=None, model=None, thinking=None,
              base=None):
        agent = agent or self.config.start_agent
        if agent not in ("claude", "pi"):
            raise StartError("Agent must be claude or pi", "usage")
        if thinking and agent != "pi":
            raise StartError("--thinking works only with pi", "usage")
        if thinking and thinking not in THINKING_LEVELS:
            raise StartError(f"Thinking must be one of {', '.join(THINKING_LEVELS)}", "usage")
        if model is not None and (not model or any(ch.isspace() for ch in model)):
            raise StartError("The model must be one name, such as zai/glm-5.3", "usage")
        model = model or self.config.start_model
        if agent == "pi":
            thinking = thinking or self.config.start_thinking
        state_path = state_dir() / "starts.json"
        issue = None
        if LINEAR_REF.match(ref):
            if self.writer is None:
                raise StartError("Linear writes are unavailable", "linear_disabled")
            with self.writer.online() as client:
                issue = client.issue(ref.upper())
            if issue is None:
                raise NotFoundError(f"No card {ref}")
            card = issue_card(self.config, issue, set(), datetime.now(timezone.utc), False, show_canceled=True)
            key = issue["id"]
        else:
            card = self.store.show(ref)
            if TEMP_REF.match(card.ref):
                card = self.store.ensure_id(card.ref)
            key = card.ref
        lock_key = safe_key(key)
        with _lock(".start-" + lock_key + ".lock"):
            record = {"steps_done": []}
            try:
                herdr("workspace", "list")
                with _lock(wait=2):
                    record = _read(state_path).get(key, {"steps_done": []})
                if not isinstance(record, dict) or not isinstance(record.get("steps_done"), list):
                    record = {"steps_done": []}
                    raise IoError(f"{state_path}: invalid start record")
                if record.get("worktree_path") and not Path(record["worktree_path"]).exists():
                    # The worktree is gone; keep where the card's repository lives.
                    record = {"steps_done": [], **{k: record[k] for k in ("repository", "checkout") if k in record}}
                record["ref"] = card.ref

                def save(step):
                    if step not in record["steps_done"]:
                        record["steps_done"].append(step)
                    persist()

                def persist():
                    with _lock(wait=2):
                        records = _read(state_path)
                        records[key] = record
                        _write(state_path, records)

                identity = self._repository(card, repo, issue)
                if record.get("repository") not in (None, identity):
                    record = {"steps_done": [], "ref": card.ref}
                record["repository"] = identity
                save("repository")
                local = self._checkout(identity, checkout, record)
                record["checkout"] = str(local)
                save("checkout")
                branch = record.get("branch") or (issue.get("branchName") if issue else todo_branch(card))
                validate_branch(branch)
                record["branch"] = branch
                save("branch")
                column = self.config.start_column
                state = None
                if issue:
                    with self.writer.online() as client:
                        team = issue["team"]
                        state = resolve_state(self.config, StateChoices(self.writer.source.account_dir()),
                                              team, client.team_states(team["id"]), column, self.ask_state)
                elif not any(c.title.casefold() == column.casefold() for c in self.store.snapshot(card.path).columns):
                    raise StartError(f"Board {card.board} has no column {column!r}", "not_found")
                # A Linear sub-issue can belong to another repository, so only todos start from a parent.
                if not base and not record.get("base") and card.parent and not issue:
                    base = self._parent_branch(card, repo, checkout)
                record["base"] = self._base(local, base or record.get("base"), branch)
                save("base")
                record.update(self._workspace(local, branch, record))
                save("worktree")
                # Hunk and `git status` compare a branch with its upstream, so the base is the upstream.
                if _git(local, "branch", f"--set-upstream-to={record['base']}", branch) is None:
                    raise StartError(f"Cannot set {record['base']} as the upstream of {branch}", "upstream_failed")
                save("upstream")
                if "move" not in record["steps_done"] and card.column.casefold() != column.casefold():
                    if issue:
                        self.writer.move(card.ref, column, ask=self.ask_state, state_name=state["name"])
                    else:
                        card = self.store.move(card.ref, column)
                save("move")
                if not no_agent:
                    if not agent_live(record):
                        self.launch_agent(replace(card, column=column), record, agent, model, thinking,
                                          lock_key, persist, linear=bool(issue))
                    save("agent")
                    report_card_token(record["pane_id"], card_token(replace(card, column=column)))
                result = {k: record.get(k) for k in ("ref", "steps_done", "workspace_id", "pane_id", "worktree_path", "branch", "base", "agent_name")}
                record["steps_done"] = []
                persist()
                return result
            except HerdboardError as error:
                error.steps_done = list(record.get("steps_done", []))
                raise
