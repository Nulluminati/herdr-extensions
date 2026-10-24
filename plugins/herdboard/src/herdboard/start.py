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
import tomllib

from . import attachments
from .activity import LOG_REF
from .brief import agent_message, brief_text, card_token
from .config import AGENTS, default_config_path, thinking_levels
from .identity import RepoIdentity, _canonical_identity, _git, repo_identity
from .linear_cards import LINEAR_REF, issue_card
from .linear_pins import add_pin
from .linear_source import state_dir
from .linear_states import StateChoices, refuse, resolve_state
from .model import ConflictError, HerdboardError, IoError, NotFoundError, commit_type
from .store import TEMP_REF, CardView

# A card's Hunk review pane stays open only while the card is in this column.
REVIEW_COLUMN = "Review"

# The pane token that shows a started card in the Herdr sidebar: `$herdboard_card`.
CARD_TOKEN = "herdboard_card"
TOKEN_SOURCE = "user:herdboard"

# How a card without a repository runs: in a plain folder (`repoless`), or in a repository that
# `start` creates first (`new_repo`). A record without a mode is a repository start.
REPO = "repo"
REPOLESS = "repoless"
NEW_REPO = "new_repo"


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
        if code == "agent_name_not_found" and args[:2] == ("agent", "start"):
            kind = args[args.index("--kind") + 1] if "--kind" in args else None
            pane = args[args.index("--pane") + 1] if "--pane" in args else None
            if kind == "codex" and pane:
                # Herdr can clear Codex's launch name while the same agent still runs.
                found = herdr("agent", "get", pane, tolerated=("agent_not_found", "pane_not_found"))
                if ((found or {}).get("agent") or {}).get("agent") == kind:
                    return found
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


def pi_settings() -> dict:
    """pi's own settings, or an empty dict when there is no file or it is not valid."""
    try:
        settings = json.loads((Path.home() / ".pi" / "agent" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def pi_default_model(settings: dict | None = None) -> str | None:
    """pi's default model as `provider/model`, or None when pi names no default."""
    settings = pi_settings() if settings is None else settings
    if not isinstance(settings.get("defaultModel"), str):
        return None
    provider = settings.get("defaultProvider")
    return f"{provider}/{settings['defaultModel']}" if isinstance(provider, str) else settings["defaultModel"]


def pi_model_choices(config) -> tuple[str | None, list[str]]:
    """pi's default model as `provider/model, thinking`, and the models to offer.

    The models come from `pi_models` in the config, else from pi's `enabledModels`.
    """
    settings = pi_settings()
    default = pi_default_model(settings)
    if default and isinstance(settings.get("defaultThinkingLevel"), str):
        default += f", {settings['defaultThinkingLevel']}"
    models = config.pi_models
    if models is None:
        found = settings.get("enabledModels")
        models = [m for m in found if isinstance(m, str) and m] if isinstance(found, list) else []
    return default, list(models)


def agent_model(config, model: str | None) -> str | None:
    """The pi model that an unnamed `model` means: `start_model`, else pi's own default.

    Every path that names no model resolves it here, so that the model that runs and the
    `[thinking]` row that its level comes from are always the same model.
    """
    return model or config.start_model or pi_default_model()


def thinking_level(config, model: str | None, thinking: str | None = None) -> str | None:
    """The thinking level to give pi: the one asked for, else the last one for its model, else the default.

    `model` None means the model that `agent_model` resolves.
    """
    if thinking:
        return thinking
    return remembered_thinking(config, model) or config.start_thinking


def remembered_thinking(config, model: str | None) -> str | None:
    """The thinking level that the user last chose for `model`, or None. None is `agent_model`'s model."""
    return config.thinking.get(agent_model(config, model) or "")


def codex_model_choices(config) -> tuple[str | None, list[str]]:
    """Codex's default model and effort, and its visible cached or configured models."""
    folder = Path.home() / ".codex"
    try:
        settings = tomllib.loads((folder / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    default = settings.get("model")
    if not isinstance(default, str) or not default:
        default = None
    elif isinstance(settings.get("model_reasoning_effort"), str):
        default += f", {settings['model_reasoning_effort']}"
    models = config.codex_models
    if models is None:
        try:
            cache = json.loads((folder / "models_cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        found = cache.get("models") if isinstance(cache, dict) else None
        models = [entry["slug"] for entry in found
                  if isinstance(entry, dict) and entry.get("visibility") == "list"
                  and isinstance(entry.get("slug"), str) and entry["slug"]] if isinstance(found, list) else []
    return default, list(models)


def agent_arguments(agent: str, message: str, model: str | None, thinking: str | None) -> list[str]:
    """The agent's command-line arguments after Herdr's `--`."""
    if agent == "pi":
        options = (["--model", model] if model else []) + (["--thinking", thinking] if thinking else [])
        return [*options, "--", message]
    if agent == "codex":
        options = (["--model", model] if model else []) + (
            ["-c", f'model_reasoning_effort="{thinking}"'] if thinking else [])
        return [*options, message]
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


def go_to_workspace(ref):
    """Focus the Herdr workspace of a started card's worktree. Stops when Herdr has none open."""
    found = find_record(ref)
    record = found[1] if found else {}
    if record.get("mode") in (REPOLESS, NEW_REPO):
        path = record.get("worktree_path")
        workspace = record.get("workspace_id")
        if not path or not workspace or not Path(path).exists():
            raise StartError(f"{ref} has no work folder; start the card first", "not_started")
        if herdr("workspace", "focus", workspace, tolerated=("workspace_not_found",)) is None:
            raise StartError(f"Herdr has no open workspace for {ref}; start the card to open it",
                             "workspace_closed")
        return {"ref": ref, "workspace_id": workspace}
    path = record.get("worktree_path")
    if not path or not record.get("checkout") or not Path(path).exists():
        raise StartError(f"{ref} has no worktree; start the card first", "not_started")
    listed = herdr("worktree", "list", "--cwd", record["checkout"], "--trust-repository")
    entry = next((w for w in listed.get("worktrees", []) if Path(w["path"]).resolve() == Path(path).resolve()), None)
    workspace = entry.get("open_workspace_id") if entry else None
    if not workspace:
        raise StartError(f"Herdr has no open workspace for {ref}; start the card to open it", "workspace_closed")
    herdr("workspace", "focus", workspace)
    return {"ref": ref, "workspace_id": workspace}


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
        if record.get("mode") in (REPOLESS, NEW_REPO):
            return f"its work folder still exists at {path}. Finish the card first"
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


def _links_show(meta, issue) -> bool:
    """Whether a linked project of a board shows the Linear issue (with a label of the link, if any)."""
    project = (issue or {}).get("project") or {}
    labels = {label["id"] for label in (issue or {}).get("labels", [])}
    return any(link.id == project.get("id") and
               (not link.labels or any(label.id in labels for label in link.labels))
               for link in meta.links)


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


def work_folder(config, ref) -> Path:
    """The plain folder where a card without a repository runs, and where its planner runs."""
    if not isinstance(ref, str) or not LOG_REF.match(ref):
        raise StartError(f"{ref!r} is not a card ID: use a todo ID", "usage")
    return config.todos_dir / ".herdboard" / "work" / ref


def _slug(text):
    """A card title as a branch or folder name part."""
    text = re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)|https?://\S+", "", text)
    text = re.sub(r"#[\w/-]+|📅\s*\d{4}-\d{2}-\d{2}|\^\w+", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def todo_branch(card):
    slug = _slug(card.title)
    # A typed card's branch starts with its commit type, such as `fix/t3m9-login-timeout`.
    prefix = f"{commit_type(card.type)}/" if card.type else ""
    return (prefix + card.ref + ("-" + slug if slug else ""))[:50].rstrip("-")


def repo_slug(card):
    """The folder name for a new repository: the card title, without the ID and the type."""
    return (_slug(card.title) or card.ref)[:50].rstrip("-")


def _repository_folder_usable(path) -> bool:
    """True for a folder that a stopped start may reuse: empty, or only an unborn `.git`."""
    try:
        entries = sorted(entry.name for entry in path.iterdir())
    except OSError as error:
        raise StartError(f"{path}: cannot read the folder: {error}", "io") from error
    if not entries:
        return True
    if entries != [".git"] or not (path / ".git").is_dir():
        return False
    # `git init` ran in a start that stopped before the first commit.
    return _git(path, "rev-parse", "--verify", "--quiet", "HEAD") is None


def create_repository(path) -> RepoIdentity:
    """Create a local Git repository with one empty commit, and return its identity.

    A folder that a stopped start left with only an unborn repository is reused, so the
    next start can make the first commit.
    """
    path = Path(path).expanduser()
    if path.exists():
        if not path.is_dir() or not _repository_folder_usable(path):
            raise StartError(f"{path} exists and is not an empty folder", "path_exists")
    else:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StartError(f"{path}: cannot create the folder: {error}", "io") from error
    if _git(path, "init", "-b", "main") is None:
        raise StartError(f"{path}: cannot create the Git repository", "new_repo_failed")
    if _git(path, "commit", "--allow-empty", "-m", "chore: initial commit") is None:
        raise StartError(f"{path}: cannot make the first commit. Set git user.name and user.email",
                         "new_repo_failed")
    identity = repo_identity(path)
    if identity is None:
        raise StartError(f"{path}: the new repository has no identity", "new_repo_failed")
    return identity


def validate_branch(branch):
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise StartError(f"Invalid branch: {branch!r}", "invalid_branch")
    result = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True, text=True)
    # @{-1} is accepted by Git in a checkout, but is not a literal branch name.
    if result.returncode or branch.startswith("@{"):
        raise StartError(f"Invalid branch: {branch!r}", "invalid_branch")


def mark_removed(cards):
    """Set the board-only start status of cards: a removed or existing worktree and its agent, a worktree left after Done,
    the PR state, a running agent review, a pull request description that an agent writes, and the last agent review."""
    records = _read(state_dir() / "starts.json")
    for card in cards:
        record = records.get(card.ref)
        if not isinstance(record, dict):
            record = next((r for r in records.values() if isinstance(r, dict) and r.get("ref") == card.ref), {})
        path = record.get("worktree_path")
        card.repoless = record.get("mode") == REPOLESS and bool(path)
        card.worktree_removed = bool(path and not Path(path).exists())
        card.worktree = bool(path and Path(path).exists())
        # The agent that `start` started in the worktree, while the worktree exists.
        card.agent = record.get("agent") if card.worktree else None
        card.worktree_left = bool(path and card.checked and Path(path).exists())
        card.pr_state = record.get("pr_state") if record.get("pr_url") else None
        review = record.get("agent_review")
        card.agent_review = review.get("kind") if isinstance(review, dict) else None
        card.pr_writing = isinstance(record.get("pr_write"), dict)
        card.reviewed = _reviewed(record) if card.worktree else None


def _reviewed(record) -> str | None:
    """The last agent review: `reviewed` while the worktree HEAD is its commit, `new commits` after it."""
    saved = record.get("agent_reviewed")
    if not isinstance(saved, dict) or not saved.get("head"):
        return None
    head = _git(Path(record["worktree_path"]), "rev-parse", "HEAD")
    if not head:
        return None
    return "reviewed" if head == saved["head"] else "new commits"


class Starter:
    def __init__(self, store, directory, writer=None, ask_state=refuse,
                 prompt_checkout=None, repository_view=True, config_file: Path | None = None,
                 prompt_target=None):
        self.store = store
        self.config = store.config
        self.directory = Path(directory)
        self.writer = writer
        self.ask_state = ask_state
        self.prompt_checkout = prompt_checkout
        self.repository_view = repository_view
        # Asks where a card without a repository runs: `(card) -> (mode, value)`, or None.
        self.prompt_target = prompt_target
        # Named in the agent's brief, so the agent reads the same boards.
        self.config_file = config_file

    def default_repo_path(self, card) -> Path:
        """Where `--new-repo` without a path creates the repository."""
        return Path(self.config.projects_dir) / repo_slug(card)

    def _refuse_new_repo_family(self, card):
        if card.parent or self.store.children(card.ref):
            raise StartError(f"{card.ref} has a parent or sub-cards; their cards must stay on one board. "
                             "Run it without a repository with --no-repo", "new_repo_family")

    def _new_repo_path(self, card, new_repo, issue):
        """Resolve and check the path of a new repository: it must be a todo on a board without one."""
        if issue is not None:
            raise StartError("A Linear issue cannot create a repository; use --no-repo", "usage")
        if card.path is not None and self.store.board_meta(card.path).repo:
            raise StartError(f"{card.board} already has a repository; use --repo to pick another one", "usage")
        self._refuse_new_repo_family(card)
        if new_repo is True or new_repo is None:
            path = self.default_repo_path(card)
        else:
            path = Path(new_repo).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
        return path.absolute()

    def _refuse_over_live_worktree(self, card, option):
        """Stop an option that would replace a repository start whose worktree still exists."""
        found = find_record(card.ref)
        record = found[1] if found else {}
        path = record.get("worktree_path")
        if record.get("mode") not in (None, REPO) or not path or not Path(path).exists():
            return
        raise StartError(f"{card.ref} has a worktree at {path}. Finish the card with "
                         f"`herdboard finish {card.ref}` first, then use {option}.", "usage")

    def _on_own_new_repo_board(self, card, record) -> bool:
        """True when the card already sits on the board of the repository that its start created."""
        repository = record.get("repository")
        if card.path is None or not repository or not record.get("new_repo_path"):
            return False
        own = self.store.board_meta(card.path).repo
        return bool(own) and _canonical_identity(own) == _canonical_identity(repository)

    def _target(self, card, repo, no_repo, new_repo, issue, checkout=None, base=None):
        """Where the card runs: `("repo", identity)`, `("new_repo", path)` or `("repoless", None)`.

        The explicit flags win, then the choice a record keeps, then the repository of the card.
        Only when no repository is found does `prompt_target` ask.
        """
        if new_repo is not None and (no_repo or repo):
            raise StartError("--new-repo cannot be used with --no-repo or --repo", "usage")
        if no_repo or new_repo is not None:
            if checkout is not None:
                raise StartError("--checkout works only with a repository that the card already has",
                                 "usage")
            if base:
                raise StartError("--base works only with a repository that the card already has", "usage")
        if new_repo is not None:
            self._refuse_over_live_worktree(card, "--new-repo")
            return NEW_REPO, self._new_repo_path(card, new_repo, issue)
        if no_repo:
            self._refuse_over_live_worktree(card, "--no-repo")
            return REPOLESS, None
        if repo:
            return REPO, self._repository(card, repo, issue)
        found = find_record(card.ref)
        stored = found[1] if found else {}
        if stored.get("mode") == REPOLESS:
            return REPOLESS, None
        if stored.get("mode") == NEW_REPO:
            if self._on_own_new_repo_board(card, stored):
                # A start stopped after the board move, before it saved the new mode: resume there.
                self._refuse_new_repo_family(card)
                return NEW_REPO, Path(stored["new_repo_path"]).expanduser()
            path = stored.get("new_repo_path")
            return NEW_REPO, self._new_repo_path(card, path or True, issue)
        # A recorded existing repository, unless the card's own board names a repository.
        recorded = None
        if stored.get("repository") and (card.path is None or not self.store.board_meta(card.path).repo):
            recorded = stored["repository"]
        try:
            return REPO, self._repository(card, recorded, issue)
        except StartError as error:
            if error.code != "repo_required" or self.prompt_target is None:
                raise
            answer = self.prompt_target(card)
            if answer is None:
                raise
            mode, value = answer
            if mode == NEW_REPO:
                self._refuse_over_live_worktree(card, "--new-repo")
                return NEW_REPO, self._new_repo_path(card, value, issue)
            if mode == REPOLESS:
                self._refuse_over_live_worktree(card, "--no-repo")
                return REPOLESS, None
            return REPO, self._repository(card, value, issue)

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
            issue_id = (issue or {}).get("id")
            for path in self.store.board_paths():
                meta = self.store.board_meta(path)
                if meta.repo and (_links_show(meta, issue) or any(pin.id == issue_id for pin in meta.pins)):
                    matches.append(meta.repo)
            if not repo and self.repository_view:
                identity = repo_identity(self.directory)
                # GitHub names ignore case, so a board's `repo:` can differ from the checkout's.
                if identity and _canonical_identity(identity.identity) in {_canonical_identity(m) for m in matches}:
                    return identity.identity
        listing = f" Linked repositories: {', '.join(sorted(set(matches)))}." if matches else ""
        raise StartError("Choose a repository with --repo BOARD_OR_PATH, run the card without one with "
                         "--no-repo, or create one with --new-repo [PATH]." + listing, "repo_required")

    def _pin(self, identity, issue):
        """Pin a started Linear issue to the board of its repository.

        Nothing happens when the repository has no board or a linked project of the board
        already shows the issue. A failure is only a warning: the start goes on.
        """
        board = None
        try:
            wanted = _canonical_identity(identity)
            board = next((p for p in self.store.board_paths()
                          if (repo := self.store.board_meta(p).repo) and _canonical_identity(repo) == wanted), None)
            if board is None or _links_show(self.store.board_meta(board), issue):
                return
            add_pin(self.store, board, issue)
        except HerdboardError as error:
            where = board or f"the board of {identity}"
            print(f"herdboard: warning: cannot pin {issue['identifier']} to {where}: {error}", file=sys.stderr)

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

    def _parent_branch(self, card, repo, checkout, move_parent=True, check_column=True):
        """A sub-card starts from its parent's branch. A parent without one starts first, with no agent."""
        found = find_record(card.parent)
        if found is None or not found[1].get("branch"):
            if move_parent:
                self.start(card.parent, no_agent=True, repo=repo, checkout=checkout)
            else:
                # The caller only prepares, so the parent is prepared too: no move and no agent.
                parent = self.store.show(card.parent)
                if TEMP_REF.match(parent.ref):
                    parent = self.store.ensure_id(parent.ref)
                self.prepare(parent, None, parent.ref, repo=repo, checkout=checkout, move_parent=False,
                             check_column=check_column)
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

    def _workspace(self, checkout, branch, record, focus=True):
        """Open the card's worktree workspace. With focus False it opens in the background."""
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
                if focus:
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
            result = herdr("worktree", "open", "--cwd", str(checkout), "--path", str(path),
                           "--focus" if focus else "--no-focus", "--trust-repository")
        else:
            if _git(checkout, "worktree", "prune") is None:
                raise StartError(f"{checkout}: cannot prune worktrees")
            result = herdr("worktree", "create", "--cwd", str(checkout), "--branch", branch,
                           "--base", record["base"], "--focus" if focus else "--no-focus", "--trust-repository")
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
            card, notes=notes, repository=record.get("repository") or "",
            worktree=record["worktree_path"], branch=record.get("branch") or "",
            base=record.get("base") or "",
            command=herdboard_command(self.config_file), note=note, log=log,
            plan=attachments.plan_for_agent(self.store, card.ref),
            attachments=attachments.listing(self.config, card.ref),
            repoless=record.get("mode") == REPOLESS,
            attachments_dir=str(attachments.folder(self.config, card.ref)),
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

    def _repoless_workspace(self, ref, folder, record, focus=True):
        """Open the card folder as a Herdr workspace, reusing the open one that the record names."""
        stored = record.get("workspace_id")
        if stored and folder.exists():
            info = herdr("workspace", "get", stored, tolerated=("workspace_not_found",))
            if info is not None:
                root = (info.get("root_pane") or {}).get("pane_id")
                if not root:
                    panes = herdr("pane", "list", "--workspace", stored).get("panes", [])
                    root = record.get("pane_id")
                    if not any(p["pane_id"] == root for p in panes):
                        root = panes[0]["pane_id"] if panes else None
                if root:
                    if focus:
                        herdr("workspace", "focus", stored)
                    return {"workspace_id": stored, "pane_id": root, "worktree_path": str(folder)}
        result = herdr("workspace", "create", "--cwd", str(folder), "--label", ref,
                       "--focus" if focus else "--no-focus")
        try:
            workspace = result["workspace"]["workspace_id"]
            pane = result["root_pane"]["pane_id"]
            if not all(isinstance(value, str) and value for value in (workspace, pane)):
                raise ValueError("missing workspace or root pane")
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise StartError(f"Unexpected Herdr workspace result: {result}", "herdr_error") from error
        return {"workspace_id": workspace, "pane_id": pane, "worktree_path": str(folder)}

    def prepare(self, card, issue, key, repo=None, checkout=None, base=None, move_parent=True,
                check_column=True, no_repo=False, new_repo=None):
        """Set a card up for work: repository, pin, checkout, branch, base, worktree and upstream.

        It writes the card's `starts.json` record and takes the card's start lock. It moves no card
        and starts no agent: `start` calls it and then moves the card and starts the agent, and
        `plan` calls it with `move_parent=False`, so a sub-card's parent is only prepared. The
        finished steps stay in the record, so a failed run resumes at the first failed step.
        With `check_column`, the start column must exist (and have a Linear state); only a caller
        that moves the card needs that, so `plan` passes false. Returns the record.

        A card without a repository gets only a work folder: `no_repo` asks for one, and
        `new_repo` names where a later `start` creates the repository.
        """
        with _lock(".start-" + safe_key(key) + ".lock"):
            mode, target = self._target(card, repo, no_repo, new_repo, issue, checkout, base)
            if mode == REPO:
                return self._prepare(card, issue, key, repo, checkout, base, move_parent, check_column,
                                     identity=target)
            return self._prepare_workspace(card, key, mode, target)

    def _prepare_workspace(self, card, key, mode, path=None, focus=False):
        """Create the card folder and open its workspace, and keep the choice in the start record."""
        state_path = state_dir() / "starts.json"
        folder = work_folder(self.config, card.ref)
        record = {"steps_done": []}
        herdr("workspace", "list")
        with _lock(wait=2):
            record = _read(state_path).get(key, {"steps_done": []})
        if not isinstance(record, dict) or not isinstance(record.get("steps_done"), list):
            record = {"steps_done": []}
            raise IoError(f"{state_path}: invalid start record")
        if record.get("mode") != mode or (mode == NEW_REPO and record.get("new_repo_path") != str(path)):
            record = {"steps_done": []}
        record["ref"] = card.ref
        record["mode"] = mode
        if mode == NEW_REPO:
            record["new_repo_path"] = str(path)

        def persist():
            with _lock(wait=2):
                records = _read(state_path)
                records[key] = record
                _write(state_path, records)

        def save(step):
            if step not in record["steps_done"]:
                record["steps_done"].append(step)
            persist()

        try:
            # The target is the card folder: there is no repository to record.
            save("repository")
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise StartError(f"{folder}: cannot create the card folder: {error}") from error
            record.update(self._repoless_workspace(card.ref, folder, record, focus=focus))
            save("worktree")
            return record
        except HerdboardError as error:
            error.steps_done = list(record.get("steps_done", []))
            raise

    def _create_new_repo(self, card, key, path):
        """Create the repository and its board, move the card to the board, then prepare a normal start.

        Each sub-step is saved, so a second `start` continues where the first stopped.
        """
        state_path = state_dir() / "starts.json"
        record = {"steps_done": []}
        herdr("workspace", "list")
        with _lock(wait=2):
            record = _read(state_path).get(key, {"steps_done": []})
        if not isinstance(record, dict) or not isinstance(record.get("steps_done"), list):
            record = {"steps_done": []}
            raise IoError(f"{state_path}: invalid start record")
        if record.get("mode") != NEW_REPO or record.get("new_repo_path") != str(path):
            record = {"steps_done": []}
        record["ref"] = card.ref
        record["mode"] = NEW_REPO
        record["new_repo_path"] = str(path)

        def persist():
            with _lock(wait=2):
                records = _read(state_path)
                records[key] = record
                _write(state_path, records)

        def save(step):
            if step not in record["steps_done"]:
                record["steps_done"].append(step)
            persist()

        try:
            # Save the choice first: a failed `git init` or first commit then resumes at this path.
            persist()
            if "new_repo" not in record["steps_done"]:
                identity = create_repository(path)
                record["repository"] = identity.identity
                save("new_repo")
            else:
                identity = repo_identity(Path(path))
                if identity is None:
                    raise StartError(f"{path} is no longer a Git repository", "new_repo_failed")
                record["repository"] = identity.identity
            # Register the checkout only after the step is saved: a failure here is retried.
            _remember(path)
            board = self.store.board_for_repo(identity)
            if board is None:
                board = self.store.init_board(identity, merge="local")
            save("board")
            current = self.store.show(card.ref)
            if current.path is None or current.path.resolve() != board.resolve():
                self.store.move_to_board(card.ref, board)
            save("board_move")
            # Planning left the card folder: the work happens in a worktree now.
            self._forget_card_folder(card.ref, record)
            record["mode"] = REPO
            record.pop("new_repo_path", None)
            persist()
        except HerdboardError as error:
            error.steps_done = list(record.get("steps_done", []))
            raise
        return self._prepare(self.store.show(card.ref), None, key, None, None, None, True, True,
                             identity=identity.identity)

    def _forget_card_folder(self, ref, record):
        """Forget a planning session's folder and workspace in the start record.

        The folder is removed when no planner still needs it: a planner that runs keeps it.
        """
        from .plan import running_planner

        record.pop("worktree_path", None)
        workspace = record.pop("workspace_id", None)
        record.pop("pane_id", None)
        try:
            if running_planner(ref) is not None:
                return
        except HerdboardError:
            pass
        if workspace:
            try:
                herdr("workspace", "close", workspace, tolerated=("workspace_not_found",))
            except HerdboardError:
                pass
        try:
            work_folder(self.config, ref).rmdir()
        except OSError:
            pass

    def _prepare(self, card, issue, key, repo, checkout, base, move_parent, check_column,
                 identity=None):
        state_path = state_dir() / "starts.json"
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
            elif record.get("mode") not in (None, REPO):
                # The card ran without a repository, and now runs in one: the folder is not a worktree.
                record = {"steps_done": []}
            record["mode"] = REPO
            record.pop("new_repo_path", None)
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

            if identity is None:
                identity = self._repository(card, repo, issue)
            if record.get("repository") not in (None, identity):
                record = {"steps_done": [], "ref": card.ref}
            record["repository"] = identity
            save("repository")
            if issue:
                self._pin(identity, issue)
            local = self._checkout(identity, checkout, record)
            record["checkout"] = str(local)
            save("checkout")
            branch = record.get("branch") or (issue.get("branchName") if issue else todo_branch(card))
            validate_branch(branch)
            record["branch"] = branch
            save("branch")
            # Only a caller that moves the card needs the start column: `plan` prepares without
            # moving, so it works even when the column has no state or the board has no column.
            state = None
            if check_column:
                column = self.config.start_column
                if issue:
                    with self.writer.online() as client:
                        team = issue["team"]
                        state = resolve_state(self.config, StateChoices(self.writer.source.account_dir()),
                                              team, client.team_states(team["id"]), column, self.ask_state)
                elif not any(c.title.casefold() == column.casefold() for c in self.store.snapshot(card.path).columns):
                    raise StartError(f"Board {card.board} has no column {column!r}", "not_found")
            # A Linear sub-issue can belong to another repository, so only todos start from a parent.
            if not base and not record.get("base") and card.parent and not issue:
                base = self._parent_branch(card, repo, checkout, move_parent=move_parent,
                                           check_column=check_column)
            record["base"] = self._base(local, base or record.get("base"), branch)
            save("base")
            record.update(self._workspace(local, branch, record, focus=False))
            save("worktree")
            # Hunk and `git status` compare a branch with its upstream, so the base is the upstream.
            if _git(local, "branch", f"--set-upstream-to={record['base']}", branch) is None:
                raise StartError(f"Cannot set {record['base']} as the upstream of {branch}", "upstream_failed")
            save("upstream")
            if state is not None:
                # Transient, and set after the last save, so it never reaches the record on disk:
                # `start` hands the resolved state to the move instead of resolving it again.
                record["_state_name"] = state["name"]
            return record
        except HerdboardError as error:
            error.steps_done = list(record.get("steps_done", []))
            raise

    def start(self, ref, agent=None, no_agent=False, repo=None, checkout=None, model=None, thinking=None,
              base=None, no_repo=False, new_repo=None):
        agent = agent or self.config.start_agent
        if agent not in AGENTS:
            raise StartError("Agent must be claude, pi or codex", "usage")
        levels = thinking_levels(agent)
        if thinking is not None and not levels:
            raise StartError("--thinking works only with pi or codex", "usage")
        if thinking is not None and thinking not in levels:
            raise StartError(f"Thinking must be one of {', '.join(levels)}", "usage")
        if model is not None and (not model or any(ch.isspace() for ch in model)):
            raise StartError("The model must be one name, such as zai/glm-5.3", "usage")
        model = model or self.config.start_model
        if agent == "pi":
            thinking = thinking_level(self.config, model, thinking)
        elif thinking is None and self.config.start_thinking in levels:
            thinking = self.config.start_thinking
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
            mode, target = self._target(card, repo, no_repo, new_repo, issue, checkout, base)
            if mode == REPOLESS:
                # The card has no worktree, so its folder workspace opens in front of the user.
                record = self._prepare_workspace(card, key, REPOLESS, focus=True)
            elif mode == NEW_REPO:
                record = self._create_new_repo(card, key, target)
                # The card is on the new board now, and its brief names that board.
                card = self.store.show(card.ref)
            else:
                record = self._prepare(card, issue, key, repo, checkout, base, move_parent=True,
                                       check_column=True, identity=target)
            state_name = record.pop("_state_name", None)
            try:
                def persist():
                    with _lock(wait=2):
                        records = _read(state_path)
                        records[key] = record
                        _write(state_path, records)

                def save(step):
                    if step not in record["steps_done"]:
                        record["steps_done"].append(step)
                    persist()

                column = self.config.start_column
                if "move" not in record["steps_done"] and card.column.casefold() != column.casefold():
                    if issue:
                        self.writer.move(card.ref, column, ask=self.ask_state, state_name=state_name)
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
                result["mode"] = record.get("mode") or REPO
                record["steps_done"] = []
                persist()
                return result
            except HerdboardError as error:
                error.steps_done = list(record.get("steps_done", []))
                raise
