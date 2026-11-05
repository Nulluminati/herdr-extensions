# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Start Herdr with the projects, tabs and panes listed in layout.toml.

default_layout.py startup   build missing projects in a fresh session, or
                            relaunch programs in a restored one
default_layout.py apply [--force]
                            rebuild the focused workspace from its project

default_layout.py apply leaves the focused tab alone when a program or an agent
runs in it. --force replaces the tab anyway, which kills that work.
"""

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

PLUGIN_ID = "nulluminati.default-layout"
AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
CLAIM_KEY = re.compile(r"[A-Za-z0-9._-]+")
CLAIM_TOKEN = re.compile(r"[0-9a-f]{32}")
SPLITS = ("down", "right")
MAX_PANES = 4

# How long a pane may take to settle at a prompt before it counts as busy.
SETTLE_SECONDS = 5.0
SETTLE_POLL_SECONDS = 0.2

# How long the startup hook waits for the server to report its first pane.
SNAPSHOT_WAIT_SECONDS = 5.0

# How many seconds one Herdr request may take, for the CLI and the socket alike.
REQUEST_TIMEOUT_SECONDS = 30.0

# How long a marker that holds no owner token, such as one an older run left
# behind, keeps a claim.
CLAIM_TTL_SECONDS = 60.0

# Open a claim marker without following a symlink that another user planted at
# its path, and without handing the descriptor to a child process.
CLAIM_OPEN_FLAGS = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)

# What a run does with a marker that it cannot open or own.
CLAIM_RETRY = "retry"  # The marker is gone; make one that this run owns.
CLAIM_FREE = "free"  # The marker cannot be removed; do not block the work.
CLAIM_BLOCK = "block"  # A young marker holds the claim until its age passes the TTL.


class HerdrError(Exception):
    pass


class LayoutError(Exception):
    def __init__(self, path, problems):
        super().__init__(f"invalid layout file {path}")
        self.problems = problems


# Herdr access
# --------


def command(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SECONDS)


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(*args: str) -> dict:
    """Run one Herdr CLI command and return its `result` object."""
    try:
        result = command([herdr_bin(), *args])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HerdrError(f"Herdr is not reachable: {error}") from error
    output = result.stdout if result.stdout.strip() else result.stderr
    try:
        data = json.loads(output)
    except (ValueError, TypeError) as error:
        raise HerdrError(f"Herdr returned no valid JSON: {output}") from error
    if not isinstance(data, dict):
        raise HerdrError(f"Unexpected Herdr response: {output}")
    if result.returncode or "error" in data:
        details = data.get("error")
        if not isinstance(details, dict):
            raise HerdrError(f"Unexpected Herdr error: {output}")
        raise HerdrError(details.get("message") or f"Herdr: {details.get('code')}: {output}")
    found = data.get("result")
    if not isinstance(found, dict):
        raise HerdrError(f"Unexpected Herdr result: {output}")
    return found


def socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config/herdr/herdr.sock")


def herdr_config_dir() -> Path:
    """The Herdr config directory, which holds the default session."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "herdr"
    return Path.home() / ".config/herdr"


def sessions() -> list[dict]:
    """The sessions Herdr knows, each with the directory that holds its files.

    `herdr session list --json` answers with bare JSON, so it does not go
    through `herdr()`, which unwraps a `result` envelope.
    """
    try:
        result = command([herdr_bin(), "session", "list", "--json"])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HerdrError(f"Herdr is not reachable: {error}") from error
    try:
        data = json.loads(result.stdout or result.stderr)
    except (ValueError, TypeError) as error:
        raise HerdrError(f"Herdr returned no valid JSON: {result.stdout or result.stderr}") from error
    found = data.get("sessions") if isinstance(data, dict) else None
    if result.returncode or not isinstance(found, list):
        raise HerdrError(f"Unexpected Herdr session list: {data}")
    return found


def session_dir() -> Path:
    """The directory that holds this session's files, its saved session included.

    A named session has its own directory below the config directory; the
    default session uses the config directory itself. `HERDR_SOCKET_PATH` can
    move the socket without moving these files, so ask Herdr for the directory.
    """
    name = os.environ.get("HERDR_SESSION") or "default"
    try:
        for session in sessions():
            if session.get("name") == name:
                return Path(session["session_dir"])
    except (HerdrError, KeyError, TypeError):
        pass
    base = herdr_config_dir()
    return base if name == "default" else base / "sessions" / name


def session_file() -> Path:
    """The saved session that Herdr restores this server from."""
    return session_dir() / "session.json"


def remaining(deadline: float) -> float:
    """How long the request may still block: the budget that the whole request shares.

    A zero timeout makes the socket non-blocking and raises BlockingIOError
    rather than TimeoutError, so an expired budget gets the shortest timeout.
    """
    return max(deadline - time.monotonic(), 0.001)


def read_line(connection: socket.socket, deadline: float) -> bytes:
    """One newline-terminated reply, read within what is left of the request's budget.

    A socket timeout covers one blocking call, so a single read under the budget
    still lets a server that drips bytes hold the call for ever. Each read gets
    only the time that remains.
    """
    line = b""
    while not line.endswith(b"\n"):
        connection.settimeout(remaining(deadline))
        chunk = connection.recv(4096)
        if not chunk:
            return line
        line += chunk
    return line


def call(method: str, params: dict) -> dict:
    """Send one newline-delimited JSON request over the socket.

    The CLI has no command for `layout.apply` or `tab.move`, so these go to the
    socket directly. The whole request carries the same limit as the CLI, so a
    server that stalls cannot hold the startup hook for ever.
    """
    request = json.dumps({"id": f"default-layout:{method}", "method": method, "params": params})
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    stage = "connect"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining(deadline))
            connection.connect(socket_path())
            stage = "write"
            with connection.makefile("rwb") as stream:
                connection.settimeout(remaining(deadline))
                stream.write(request.encode() + b"\n")
                stream.flush()
            stage = "reply"
            line = read_line(connection, deadline)
    except TimeoutError as error:
        raise HerdrError(f"{method}: Herdr did not answer within {REQUEST_TIMEOUT_SECONDS:g} s ({stage})") from error
    if not line:
        raise HerdrError(f"{method}: no response from Herdr")
    message = json.loads(line)
    if "error" in message:
        error = message["error"]
        raise HerdrError(f"{method}: {error.get('message', error) if isinstance(error, dict) else error}")
    return message["result"]


def snapshot() -> dict:
    return herdr("api", "snapshot")["snapshot"]


def workspaces() -> list[dict]:
    return herdr("workspace", "list")["workspaces"]


def wait_for_panes() -> dict:
    """Snapshot the session once it has a pane to judge."""
    deadline = time.monotonic() + SNAPSHOT_WAIT_SECONDS
    while True:
        snap = snapshot()
        if snap.get("panes") or time.monotonic() >= deadline:
            return snap
        time.sleep(SETTLE_POLL_SECONDS)


def workspace_tabs(snap: dict, workspace_id: str) -> list[dict]:
    return [tab for tab in snap["tabs"] if tab["workspace_id"] == workspace_id]


def workspace_panes(snap: dict, workspace_id: str) -> list[dict]:
    return [pane for pane in snap["panes"] if pane["workspace_id"] == workspace_id]


def tab_panes(snap: dict, tab_id: str) -> list[dict]:
    return [pane for pane in snap["panes"] if pane["tab_id"] == tab_id]


# The layout file
# --------


def layout_path() -> Path:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "layout.toml"
    return Path.home() / ".config/herdr/plugins/config" / PLUGIN_ID / "layout.toml"


def load_layout() -> dict | None:
    """The parsed layout file, or None when there is none."""
    path = layout_path()
    try:
        text = path.read_text(encoding="utf-8")
    # FileNotFoundError is an OSError, so it must come first. UnicodeDecodeError is a ValueError,
    # so it needs its own place in the tuple.
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise LayoutError(path, [str(error)]) from error
    try:
        spec = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LayoutError(path, [str(error)]) from error
    problems = validate(spec)
    if problems:
        raise LayoutError(path, problems)
    return spec


def is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def pane_problems(pane: dict, where: str, first: bool) -> list[str]:
    problems = []
    for key in ("label", "command", "agent", "working_dir"):
        if key in pane and not is_text(pane[key]):
            problems.append(f"{where}: {key} must be a non-empty string")
    if is_text(pane.get("agent")):
        if not AGENT_NAME.fullmatch(pane["agent"]):
            problems.append(f"{where}: agent must be a plain name such as claude or pi")
        if "command" in pane:
            problems.append(f"{where}: set either agent or command, not both")
    if not first and "split" in pane and pane["split"] not in SPLITS:
        problems.append(f'{where}: split must be "down" or "right"')
    if "ratio" in pane:
        ratio = pane["ratio"]
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio < 1:
            problems.append(f"{where}: ratio must be between 0 and 1")
    return problems


def tab_problems(tab, where: str) -> list[str]:
    if not isinstance(tab, dict):
        return [f"{where}: must be a table"]
    problems = []
    if is_text(tab.get("name")):
        where = f"{where.rsplit('/', 1)[0]}/{tab['name']}"
        # Herdr labels an unnamed tab with its position, so a name of only digits
        # cannot be told apart from one.
        if tab["name"].isdigit():
            problems.append(f"{where}: name must not be a bare number, which Herdr gives to an unnamed tab")
    else:
        problems.append(f"{where}: name is required")
    panes = tab.get("panes")
    if panes is None:
        return problems + pane_problems(tab, where, first=True)
    if "command" in tab or "agent" in tab:
        problems.append(f"{where}: set either command or panes, not both")
    if not isinstance(panes, list) or not all(isinstance(pane, dict) for pane in panes):
        return problems + [f"{where}: panes must be [[tabs.panes]] tables"]
    if len(panes) > MAX_PANES:
        problems.append(f"{where}: at most {MAX_PANES} panes, found {len(panes)}")
    for number, pane in enumerate(panes, 1):
        problems += pane_problems(pane, f"{where}: pane {number}", first=number == 1)
    return problems


def validate(spec: dict) -> list[str]:
    """Every problem in the layout file, so one run can report them all."""
    projects = spec.get("projects")
    if not isinstance(projects, list) or not projects:
        return ["the file needs at least one [[projects]] table"]
    problems = []
    used_labels: dict[str, int] = {}
    for number, project in enumerate(projects, 1):
        where = f"project {number}"
        if not isinstance(project, dict):
            problems.append(f"{where}: must be a table")
            continue
        if is_text(project.get("label")):
            label = project["label"]
            # Space at either end is noise, and it hides a repeat of one name.
            if label != label.strip():
                problems.append(f"{label.strip()}: label must not start or end with a space")
            where = label.strip()
            # Two workspaces with one label look alike to every later run.
            if where in used_labels:
                problems.append(f"{where}: label is already used by project {used_labels[where]}")
            else:
                used_labels[where] = number
        else:
            problems.append(f"{where}: label is required")
        if not is_text(project.get("path")):
            problems.append(f"{where}: path is required")
        tabs = project.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            problems.append(f"{where}: needs at least one [[projects.tabs]] table")
            continue
        for tab_number, tab in enumerate(tabs, 1):
            problems += tab_problems(tab, f"{where}/tab {tab_number}")
    return problems


def project_dir(project: dict) -> Path:
    return Path(project["path"]).expanduser()


# Layout trees
# --------


def pane_command(pane: dict) -> list[str] | None:
    """The pane's program, falling back to the user's shell when it quits."""
    program = pane.get("agent") or pane.get("command")
    if not program:
        return None
    return ["sh", "-c", program + "; exec ${SHELL:-/bin/sh}"]


def pane_node(pane: dict, project_path) -> dict:
    node = {"type": "pane"}
    if pane.get("label"):
        node["label"] = pane["label"]
    cwd = Path(project_path)
    if pane.get("working_dir"):
        # An absolute working_dir replaces the project path in the join.
        cwd = cwd / Path(pane["working_dir"]).expanduser()
    node["cwd"] = str(cwd)
    program = pane_command(pane)
    if program:
        node["command"] = program
    return node


def tab_tree(tab: dict, project_path) -> dict:
    """Fold a tab's pane list into a `layout.apply` tree.

    Each pane splits the whole layout built so far, so the first pane ends up
    innermost. A tab without panes is one pane built from the tab's own keys.
    """
    panes = tab.get("panes") or [tab]
    node = pane_node(panes[0], project_path)
    for pane in panes[1:]:
        node = {
            "type": "split",
            "direction": pane.get("split", "down"),
            "ratio": pane.get("ratio", 0.5),
            "first": node,
            "second": pane_node(pane, project_path),
        }
    return node


def starts_program(tab: dict) -> bool:
    return any(pane.get("agent") or pane.get("command") for pane in tab.get("panes") or [tab])


# Matching projects to workspaces
# --------


def resolve_dir(path) -> Path:
    return Path(path).expanduser().resolve()


def dir_depth(path, project: dict) -> int | None:
    """How far inside the project a directory sits: 0 at the root, more below it.

    None when the directory is outside the project. A pane cwd is the live
    shell directory, so a `cd` below the project root must still match: the
    workspace is as much in the project as it is at the root.
    """
    root = resolve_dir(project["path"])
    found = resolve_dir(path)
    if not found.is_relative_to(root):
        return None
    return len(found.parts) - len(root.parts)


def workspace_dirs(workspace: dict, snap: dict) -> list[str]:
    """The workspace's own directory first, then the live directory of each pane."""
    dirs = [pane["cwd"] for pane in workspace_panes(snap, workspace["workspace_id"]) if pane.get("cwd")]
    worktree = workspace.get("worktree") or {}
    if worktree.get("checkout_path"):
        dirs.insert(0, worktree["checkout_path"])
    return dirs


def workspace_depth(project: dict, workspace: dict, snap: dict) -> int | None:
    """The shortest distance from the project root to a directory of the workspace."""
    found = (dir_depth(cwd, project) for cwd in workspace_dirs(workspace, snap))
    return min((depth for depth in found if depth is not None), default=None)


def nearest(matches: list[tuple[int, dict]]) -> dict | None:
    """The match closest to a project root; a tie keeps the first in list order."""
    found = [(depth, candidate) for depth, candidate in matches if depth is not None]
    if not found:
        return None
    return min(found, key=lambda match: match[0])[1]


def match_project(project: dict, open_workspaces: list[dict], snap: dict) -> dict | None:
    """The workspace that holds this project: by label first, then by directory.

    A workspace whose directory is the project root wins over one that only
    holds a pane below the root, so `apply` keeps choosing the same workspace.
    """
    for workspace in open_workspaces:
        if workspace.get("label") == project["label"]:
            return workspace
    return nearest([(workspace_depth(project, workspace, snap), workspace) for workspace in open_workspaces])


def project_for_workspace(projects: list[dict], workspace: dict, snap: dict) -> dict | None:
    """The project that the workspace sits in: by label first, then by the nearest directory."""
    for project in projects:
        if workspace.get("label") == project["label"]:
            return project
    return nearest([(workspace_depth(project, workspace, snap), project) for project in projects])


# Pane state
# --------


def is_idle_shell(pane_id: str) -> bool:
    """True when a pane sits at a shell prompt with nothing running.

    Herdr reports the pane's shell pid. At a prompt the shell is the only
    foreground process, and it leads the foreground process group. A program
    in the foreground leaves both behind. A shell that is busy inside its own
    process still shows itself, so this check cannot tell it from a prompt.
    The check reads that state, not a list of shell names, so it holds for any
    shell the user starts.
    """
    try:
        info = herdr("pane", "process-info", "--pane", pane_id)["process_info"]
    except (HerdrError, KeyError):
        return False
    foreground = info.get("foreground_processes") or []
    shell_pid = info.get("shell_pid")
    if shell_pid is None or len(foreground) != 1:
        return False
    return foreground[0].get("pid") == shell_pid and info.get("foreground_process_group_id") == shell_pid


def settles_to_idle_shell(pane_id: str) -> bool:
    """Poll a pane until it is an idle shell, so a shell still reading its rc files is not busy."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        if is_idle_shell(pane_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SETTLE_POLL_SECONDS)


def in_use_reason(panes: list[dict]) -> str | None:
    """Why these panes hold work, or None when they are all idle shells.

    Herdr keeps an agent's session reference across a restore, so a restored
    agent pane can look like an idle shell for a moment. Agent metadata alone
    is reason enough to leave the pane alone.
    """
    agents = [pane["pane_id"] for pane in panes if pane.get("agent") or pane.get("agent_session")]
    if agents:
        return f"agent in {', '.join(agents)}"
    for pane in panes:
        if not settles_to_idle_shell(pane["pane_id"]):
            return f"something is running in {pane['pane_id']}"
    return None


def caller_pane() -> str | None:
    """The pane that runs this process, or None when it runs outside every pane.

    Herdr gives a managed pane and a plugin action the same `HERDR_PANE_ID`, so
    the environment alone cannot tell a manual run inside its own pane from a
    keybinding press while that pane is focused. The process ids can: a manual
    `apply` is the foreground process of its own pane, and no pane runs a plugin
    action.
    """
    pane_id = os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        return None
    try:
        info = herdr("pane", "process-info", "--pane", pane_id)["process_info"]
    except (HerdrError, KeyError):
        return None
    running = {os.getpid(), os.getppid()}
    if any(process.get("pid") in running for process in info.get("foreground_processes") or []):
        return pane_id
    return None


def tab_of_pane(snap: dict, pane_id: str | None) -> str | None:
    """The tab that holds one pane, or None when the pane is not in the snapshot."""
    return next((pane["tab_id"] for pane in snap["panes"] if pane["pane_id"] == pane_id), None)


def is_empty(snap: dict, workspace_id: str) -> bool:
    """One tab that holds one idle shell."""
    panes = workspace_panes(snap, workspace_id)
    return len(workspace_tabs(snap, workspace_id)) == 1 and len(panes) == 1 and in_use_reason(panes) is None


def is_fresh(session: Path) -> bool:
    """True when Herdr started a new session rather than restoring a saved one.

    Herdr gives a startup hook no restore flag, and a restored session with one
    workspace, one tab and one idle shell looks exactly like a fresh one, so
    the shape of the session says nothing. Herdr restores from the saved
    session file when it is there, and a fresh server writes that file seconds
    after startup. Read it at the start of the hook, before any wait.
    """
    return not session.is_file()


def session_problem(session: Path) -> str | None:
    """Why this run cannot use the saved session, or None when it can.

    Herdr keeps a file that it could not read. The plugin still counts that
    file as a restore, because Herdr restores from it, so report the file that
    gave no usable snapshot.
    """
    try:
        json.loads(session.read_text())
    except OSError as error:
        return str(error)
    except ValueError:
        return "not valid JSON"
    return None


# Claims
# --------


def claim_key(number: int, project: dict) -> str:
    """A file-name-safe key for one project's claim.

    The label cannot key the claim. Two labels can differ and still reduce to one
    file name, such as `my proj` and `my_proj`, and the file can repeat a label.
    The position in the file and the directory tell two projects apart.
    """
    return hashlib.sha256(f"{number}:{project_dir(project)}".encode()).hexdigest()


def claim_dir() -> Path:
    """Make and return the directory that holds the claim markers, and nothing else.

    Herdr names the state directory. A run with no state directory falls back to
    one private directory per user, because the shared temporary directory lets
    another user plant a marker at a path that this plugin can predict.
    """
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if configured:
        state_dir = Path(configured)
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    state_dir = Path(tempfile.gettempdir()) / f"default-layout-{os.getuid()}"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir leaves the mode of a directory that an earlier run made alone, and
    # another user may have made this one first. Only this user may write a marker.
    state_dir.chmod(0o700)
    return state_dir


def claim_path(key: str) -> Path:
    """The marker file for one claim, or ValueError when the key is not a plain file name."""
    if not CLAIM_KEY.fullmatch(key):
        raise ValueError(f"claim key must match {CLAIM_KEY.pattern}: {key!r}")
    return claim_dir() / f"building-{key}"


# The claims this run holds: the marker path and the open file that locks it.
HELD_CLAIMS: dict[Path, tuple[int, str]] = {}


def new_token() -> str:
    """A value that names one claim, so a release leaves another run's marker alone."""
    return os.urandom(16).hex()


def marker_owner(fd: int) -> str | None:
    """The owner token in an open marker, or None when it holds none."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        found = os.read(fd, 64).decode("ascii", "replace").strip()
    except OSError:
        return None
    return found if CLAIM_TOKEN.fullmatch(found) else None


def write_owner(fd: int, token: str) -> None:
    """Write this run's token into its open marker."""
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, token.encode("ascii"))
    except OSError:
        pass  # The lock decides the claim; the token is only there for the release.


def lock_marker(fd: int) -> bool | None:
    """Lock the marker for this run: True for the lock, False when another run holds it.

    None means the file system cannot lock, so the age of the marker decides.
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        return None
    return True


def marker_is_fresh(fd: int) -> bool:
    """True when the marker is young enough to hold a claim for a run that does not lock it."""
    try:
        return time.time() - os.fstat(fd).st_mtime < CLAIM_TTL_SECONDS
    except OSError:
        return False


def marker_is_ours(fd: int) -> bool:
    """True for a marker this run may write: a plain file that this user owns."""
    try:
        found = os.fstat(fd)
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode) and found.st_uid == os.getuid()


def marker_is_stale(marker: Path) -> bool:
    """True when the marker path is old enough to take over, or names no file."""
    try:
        return time.time() - marker.stat().st_mtime >= CLAIM_TTL_SECONDS
    except OSError:
        return True  # The path is gone, or its age is unknown; neither blocks the work.


def take_unreadable(marker: Path, reason: str) -> str:
    """Decide what to do with a marker that this run cannot open or own, and say why.

    A stale marker is removed, so this run can make one that it owns. A young one
    blocks the claim, which lasts no longer than CLAIM_TTL_SECONDS. A marker that
    cannot be removed must not block the work at all.
    """
    if not marker_is_stale(marker):
        print(f"default-layout: cannot read the claim marker {marker}: {reason}")
        return CLAIM_BLOCK
    try:
        marker.unlink()
    except FileNotFoundError:
        return CLAIM_RETRY  # The path is free now; make a marker that this run owns.
    except OSError:
        print(f"default-layout: cannot remove the claim marker {marker}: {reason}")
        return CLAIM_FREE
    print(f"default-layout: removed the stale claim marker {marker}: {reason}")
    return CLAIM_RETRY


def same_marker(fd: int, marker: Path) -> bool:
    """True when the open file is still the file at the marker path.

    A run releases its claim by unlinking the marker. A run that opened the file
    just before that unlink would hold a lock on a file with no name, and the
    next run would take a fresh marker beside it, so both would build.
    """
    try:
        open_file = os.fstat(fd)
        found = marker.stat()
    except OSError:
        return False
    return (open_file.st_dev, open_file.st_ino) == (found.st_dev, found.st_ino)


def claim(key: str) -> str | None:
    """Take a claim for this run and return its token, or None when another run holds it.

    The marker stays open under an exclusive lock for as long as the run builds.
    The kernel drops the lock when the run ends, so a marker that a dead run left
    behind does not block later runs, and no run can take a claim from a run that
    still lives, even past CLAIM_TTL_SECONDS. A marker that holds no token comes
    from a run that does not lock it, so its age decides. A guard that cannot
    write or lock its marker never blocks the work, and a marker that this run
    cannot read holds it for no longer than CLAIM_TTL_SECONDS.
    """
    try:
        marker = claim_path(key)
    except OSError:
        return new_token()  # No usable state directory; do not let the guard block the work.
    token = new_token()
    for _ in range(2):
        try:
            fd = os.open(marker, CLAIM_OPEN_FLAGS | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        except OSError:
            return token  # The marker cannot be written; do not let the guard block the work.
        else:
            write_owner(fd, token)  # A fresh marker never appears empty to another run.
        created = fd is not None
        if fd is None:
            try:
                fd = os.open(marker, CLAIM_OPEN_FLAGS)
            except OSError as error:
                # A dangling symlink, a directory, or a file that another user owns.
                # The age of the marker decides, so the block ends within the TTL.
                decision = take_unreadable(marker, str(error))
                if decision == CLAIM_RETRY:
                    continue  # Make a marker that this run owns.
                return token if decision == CLAIM_FREE else None
            if not marker_is_ours(fd):
                os.close(fd)
                decision = take_unreadable(marker, "it is not a plain file that this user owns")
                if decision == CLAIM_RETRY:
                    continue  # Make a marker that this run owns.
                return token if decision == CLAIM_FREE else None
        locked = lock_marker(fd)
        if locked is False or not same_marker(fd, marker):
            os.close(fd)
            if locked is False:
                return None  # Another live run holds the claim.
            continue  # The owner released it under this run; take the new marker.
        # A marker that this run did not create, and that holds no token, or one on
        # a file system that cannot lock, holds a claim only while it is young.
        if not created and (locked is None or marker_owner(fd) is None) and marker_is_fresh(fd):
            os.close(fd)
            return None  # Another run holds a claim that has not aged out.
        write_owner(fd, token)
        HELD_CLAIMS[marker] = (fd, token)
        return token
    return None  # Two runs released the claim under this one; leave the build to the next pass.


def release(key: str) -> None:
    """Drop this run's claim, and leave every other run's marker alone."""
    try:
        marker = claim_path(key)
    except OSError:
        return
    found = HELD_CLAIMS.pop(marker, None)
    if found is None:
        return
    fd, token = found
    try:
        # Unlink only the file this run opened. It must still hold this run's token,
        # or no token at all: this run holds the lock, so an empty marker is one
        # whose token write failed, and no other run can own it. A marker that
        # another run made at the same path stays.
        owner = marker_owner(fd)
        if (owner is None or owner == token) and same_marker(fd, marker):
            try:
                marker.unlink()
            except OSError:
                pass
    finally:
        # Close last: the lock stays until the marker that held it is gone.
        os.close(fd)


# Building tabs
# --------


def apply_tab(tab: dict, project: dict, *, tab_id: str | None = None, workspace_id: str | None = None) -> str:
    """Build one tab with `layout.apply`. Herdr takes either tab_id or workspace_id, not both."""
    params = {"tab_id": tab_id} if tab_id else {"workspace_id": workspace_id}
    params.update({"tab_label": tab["name"], "focus": False, "root": tab_tree(tab, project_dir(project))})
    return call("layout.apply", params)["layout"]["tab_id"]


def replace_tab(tab: dict, project: dict, tab_id: str, index: int) -> str:
    # Herdr gives the rebuilt tab a new id and appends it, so move it back.
    rebuilt = apply_tab(tab, project, tab_id=tab_id)
    call("tab.move", {"tab_id": rebuilt, "insert_index": index})
    return rebuilt


def append_tabs(tabs: list[dict], project: dict, workspace_id: str) -> None:
    for tab in tabs:
        apply_tab(tab, project, workspace_id=workspace_id)


def build_into(project: dict, tab_id: str, index: int, workspace_id: str) -> None:
    """Replace one tab with the project's first tab and append the others."""
    first, *rest = project["tabs"]
    replace_tab(first, project, tab_id, index)
    append_tabs(rest, project, workspace_id)


def target_tab(open_tabs: list[dict], preferred: list[str | None], own_tab: str | None) -> str | None:
    """The tab to rebuild: the focused one unless this run lives in it.

    `layout.apply` closes the tab that it rebuilds, so a run that starts from a
    pane must not rebuild the tab that holds that pane: the close would stop the
    run. A keybinding press reports the focused pane too, but no pane runs it,
    so `own_tab` stays None there. None means the workspace has no tab to rebuild.
    """
    tab_ids = [tab["tab_id"] for tab in open_tabs if tab["tab_id"] != own_tab]
    if not tab_ids:
        return None
    for tab_id in preferred:
        if tab_id in tab_ids:
            return tab_id
    return tab_ids[0]


def create_project(project: dict) -> None:
    created = herdr(
        "workspace", "create", "--cwd", str(project_dir(project)), "--label", project["label"], "--no-focus"
    )
    # Build into the workspace's own tab, so it keeps no extra empty tab.
    build_into(project, created["tab"]["tab_id"], 0, created["workspace"]["workspace_id"])
    print(f"default-layout: built {project['label']} ({len(project['tabs'])} tabs)")


def unnamed_tab(tab: dict, position: int) -> bool:
    """True when Herdr named the tab after its place in the tab row.

    Herdr labels a tab that has no name with its 1-based position, so the first
    tab of a workspace is "1". That name says nothing about whether the file's
    tab of that name is open, so it must not count as one.
    """
    return tab.get("label") == str(tab.get("number") or position)


def named_tab_counts(open_tabs: list[dict]) -> dict[str, int]:
    """How many tabs that carry a name of their own hold each label."""
    counts: dict[str, int] = {}
    for position, tab in enumerate(open_tabs, 1):
        if unnamed_tab(tab, position):
            continue
        label = tab.get("label")
        counts[label] = counts.get(label, 0) + 1
    return counts


def missing_tabs(project: dict, open_tabs: list[dict]) -> list[dict]:
    """The file's tabs that the workspace does not hold yet.

    Each open tab of a label answers for one file tab of that label, so a file
    may name two tabs alike: the open tab keeps its work and the file's tab
    beyond it is added.
    """
    counts = named_tab_counts(open_tabs)
    missing = []
    for tab in project["tabs"]:
        if counts.get(tab["name"], 0):
            counts[tab["name"]] -= 1
        else:
            missing.append(tab)
    return missing


def ensure_project(project: dict, open_workspaces: list[dict], snap: dict) -> None:
    """Create the project, fill its empty workspace, or add the tabs it lacks."""
    workspace = match_project(project, open_workspaces, snap)
    if workspace is None:
        create_project(project)
        return
    workspace_id = workspace["workspace_id"]
    if is_empty(snap, workspace_id):
        build_into(project, workspace_tabs(snap, workspace_id)[0]["tab_id"], 0, workspace_id)
        print(f"default-layout: built {project['label']} ({len(project['tabs'])} tabs)")
        return
    missing = missing_tabs(project, workspace_tabs(snap, workspace_id))
    if not missing:
        print(f"default-layout: {project['label']} already open")
        return
    append_tabs(missing, project, workspace_id)
    print(f"default-layout: added {len(missing)} tab(s) to {project['label']}")


def rehydrate(project: dict, workspace_id: str, snap: dict) -> int:
    """Relaunch the programs in tabs that a restore brought back as bare shells.

    Each open tab takes the first file tab of its name that no earlier open tab
    took. Two file tabs may share a name, and each rebuilt tab then gets the
    program of its own entry.
    """
    unused = list(project["tabs"])
    rebuilt = 0
    for index, open_tab in enumerate(workspace_tabs(snap, workspace_id)):
        found = next(
            ((place, entry) for place, entry in enumerate(unused) if entry["name"] == open_tab.get("label")),
            None,
        )
        if found is None:
            continue
        place, tab = found
        # A tab of shells looks the same after a restore, so it has nothing to relaunch.
        # It still answers for its file entry, so a later tab gets the next entry.
        unused.pop(place)
        if not starts_program(tab):
            continue
        where = f"{project['label']}/{tab['name']}"
        reason = in_use_reason(tab_panes(snap, open_tab["tab_id"]))
        if reason:
            print(f"default-layout: {where} left alone: {reason}")
            continue
        replace_tab(tab, project, open_tab["tab_id"], index)
        print(f"default-layout: rehydrated {where}")
        rebuilt += 1
    return rebuilt


# Entry points
# --------


def each_project(spec: dict, work) -> None:
    """Run `work` on every project whose directory exists, one failure at a time."""
    for number, project in enumerate(spec["projects"], 1):
        label = project["label"]
        if not project_dir(project).is_dir():
            print(f"default-layout: skip {label}: no such directory {project_dir(project)}")
            continue
        key = claim_key(number, project)
        # The claim itself can fail, and a lost claim must not end the pass.
        token = None
        try:
            token = claim(key)
            if token is None:
                print(f"default-layout: {label} is already being built")
                continue
            work(project)
        # A startup hook has no one to show an error to, so report it and go on.
        except Exception as error:
            print(f"default-layout: {label} failed: {error}")
        finally:
            # Release only what this run took, never another run's marker.
            if token is not None:
                release(key)


def build_missing(spec: dict, open_workspaces: list[dict], snap: dict) -> None:
    each_project(spec, lambda project: ensure_project(project, open_workspaces, snap))


def rehydrate_open(spec: dict, open_workspaces: list[dict], snap: dict) -> None:
    def work(project):
        # A project with no open workspace was closed on purpose; leave it closed.
        workspace = match_project(project, open_workspaces, snap)
        if workspace is not None:
            count = rehydrate(project, workspace["workspace_id"], snap)
            print(f"default-layout: rehydrated {count} tab(s) in {project['label']}")

    each_project(spec, work)


def cmd_startup(spec: dict) -> int:
    session = session_file()
    fresh = is_fresh(session)
    if not fresh:
        problem = session_problem(session)
        if problem:
            print(f"default-layout: cannot use the saved session at {session}: {problem}")
    snap = wait_for_panes()
    open_workspaces = workspaces()
    if fresh or not open_workspaces:
        build_missing(spec, open_workspaces, snap)
    else:
        rehydrate_open(spec, open_workspaces, snap)
    return 0


def context() -> dict:
    try:
        found = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


def cmd_apply(spec: dict, force: bool = False) -> int:
    workspace_id = os.environ.get("HERDR_WORKSPACE_ID") or context().get("workspace_id")
    snap = snapshot()
    workspace = next((w for w in workspaces() if w["workspace_id"] == workspace_id), None)
    project = workspace and project_for_workspace(spec["projects"], workspace, snap)
    if not project:
        print("default-layout: no project for this workspace")
        return 1
    if not project_dir(project).is_dir():
        print(f"default-layout: skip {project['label']}: no such directory {project_dir(project)}")
        return 1
    open_tabs = workspace_tabs(snap, workspace_id)
    if not open_tabs:
        print("default-layout: no tab in this workspace")
        return 1
    own_pane = caller_pane()
    own_tab = tab_of_pane(snap, own_pane)
    focused = target_tab(
        open_tabs,
        [os.environ.get("HERDR_TAB_ID"), context().get("tab_id"), workspace.get("active_tab_id")],
        own_tab,
    )
    if focused is None:
        own = next(tab for tab in open_tabs if tab["tab_id"] == own_tab)
        where = f"{project['label']}/{own.get('label') or own_tab}"
        print(f"default-layout: {where} runs this command; run apply from a pane in another tab")
        return 1
    # `layout.apply` closes the tab it rebuilds and cannot keep a live PTY, so a
    # tab with work in it must stay. Only --force replaces it.
    focused_tab = next(tab for tab in open_tabs if tab["tab_id"] == focused)
    panes = [pane for pane in tab_panes(snap, focused) if pane["pane_id"] != own_pane]
    reason = in_use_reason(panes)
    if reason and not force:
        where = f"{project['label']}/{focused_tab.get('label') or focused}"
        print(f"default-layout: {where} left alone: {reason}")
        print("default-layout: run apply with --force to replace it and lose that work")
        return 1
    build_into(project, focused, open_tabs.index(focused_tab), workspace_id)
    print(f"default-layout: applied {project['label']} ({len(project['tabs'])} tabs)")
    return 0


ACTION_FLAGS = {"startup": (), "apply": ("--force",)}


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    if action not in ACTION_FLAGS:
        print(__doc__, file=sys.stderr)
        return 2
    flags = argv[2:]
    unknown = [flag for flag in flags if flag not in ACTION_FLAGS[action]]
    if unknown:
        print(f"default-layout: unknown option {unknown[0]} for {action}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    try:
        spec = load_layout()
    except LayoutError as error:
        print(f"default-layout: {error}:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if spec is None:
        print(f"default-layout: no layout file at {layout_path()}")
        return 0
    try:
        if action == "startup":
            return cmd_startup(spec)
        return cmd_apply(spec, force="--force" in flags)
    except (HerdrError, OSError, ValueError) as error:
        print(f"default-layout: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
