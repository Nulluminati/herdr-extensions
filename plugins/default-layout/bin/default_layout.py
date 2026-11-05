# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Start Herdr with the projects, tabs and panes listed in layout.toml.

default_layout.py startup   build missing projects in a fresh session, or
                            relaunch programs in a restored one
default_layout.py apply     rebuild the focused workspace from its project
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

PLUGIN_ID = "nulluminati.default-layout"
AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SPLITS = ("down", "right")
MAX_PANES = 4

# How long a pane may take to settle at a prompt before it counts as busy.
SETTLE_SECONDS = 5.0
SETTLE_POLL_SECONDS = 0.2

# How long the startup hook waits for the server to report its first pane.
SNAPSHOT_WAIT_SECONDS = 5.0

# How long one run's claim on building a project stays valid.
CLAIM_TTL_SECONDS = 60.0


class HerdrError(Exception):
    pass


class LayoutError(Exception):
    def __init__(self, path, problems):
        super().__init__(f"invalid layout file {path}")
        self.problems = problems


# Herdr access
# --------


def command(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


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


def call(method: str, params: dict) -> dict:
    """Send one newline-delimited JSON request over the socket.

    The CLI has no command for `layout.apply` or `tab.move`, so these go to the
    socket directly.
    """
    request = json.dumps({"id": f"default-layout:{method}", "method": method, "params": params})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path())
        stream = connection.makefile("rwb")
        stream.write(request.encode() + b"\n")
        stream.flush()
        line = stream.readline()
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
        text = path.read_text()
    except FileNotFoundError:
        return None
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
    for number, project in enumerate(projects, 1):
        where = f"project {number}"
        if not isinstance(project, dict):
            problems.append(f"{where}: must be a table")
            continue
        if is_text(project.get("label")):
            where = project["label"]
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


def claim_path(key: str) -> Path:
    state_dir = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp")
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"building-{re.sub(r'[^A-Za-z0-9._-]', '_', key)}"


def claim(key: str) -> bool:
    """Take a short-lived claim, so two runs cannot build the same project.

    A claim ages out after CLAIM_TTL_SECONDS, so a run that died holding one
    does not block later runs. A marker that vanishes during the check belonged
    to a run that has released it, so the claim is free: create the marker
    again. A guard that cannot write or read its marker never blocks the work.
    """
    try:
        marker = claim_path(key)
    except OSError:
        return True  # No usable state directory; do not let the guard block the work.
    for _ in range(2):
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            pass
        except OSError:
            return True  # The marker cannot be written; do not let the guard block the work.
        try:
            if time.time() - marker.stat().st_mtime < CLAIM_TTL_SECONDS:
                return False  # Another run holds a claim that has not aged out.
            marker.touch()
            return True
        except FileNotFoundError:
            continue  # The owner released it; take the claim with a fresh marker.
        except OSError:
            return True  # The marker cannot be read or touched; do not block the work.
    return False  # Two runs released the claim under us; leave the build to the next pass.


def release(key: str) -> None:
    try:
        claim_path(key).unlink()
    except OSError:
        pass


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


def create_project(project: dict) -> None:
    created = herdr(
        "workspace", "create", "--cwd", str(project_dir(project)), "--label", project["label"], "--no-focus"
    )
    # Build into the workspace's own tab, so it keeps no extra empty tab.
    build_into(project, created["tab"]["tab_id"], 0, created["workspace"]["workspace_id"])
    print(f"default-layout: built {project['label']} ({len(project['tabs'])} tabs)")


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
    present = {tab.get("label") for tab in workspace_tabs(snap, workspace_id)}
    missing = [tab for tab in project["tabs"] if tab["name"] not in present]
    if not missing:
        print(f"default-layout: {project['label']} already open")
        return
    append_tabs(missing, project, workspace_id)
    print(f"default-layout: added {len(missing)} tab(s) to {project['label']}")


def rehydrate(project: dict, workspace_id: str, snap: dict) -> int:
    """Relaunch the programs in tabs that a restore brought back as bare shells."""
    by_name = {tab["name"]: tab for tab in project["tabs"]}
    rebuilt = 0
    for index, open_tab in enumerate(workspace_tabs(snap, workspace_id)):
        tab = by_name.get(open_tab.get("label"))
        # A tab of shells looks the same after a restore, so it has nothing to relaunch.
        if tab is None or not starts_program(tab):
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
    for project in spec["projects"]:
        label = project["label"]
        if not project_dir(project).is_dir():
            print(f"default-layout: skip {label}: no such directory {project_dir(project)}")
            continue
        # The claim itself can fail, and a lost claim must not end the pass.
        held = False
        try:
            if not claim(label):
                print(f"default-layout: {label} is already being built")
                continue
            held = True
            work(project)
        # A startup hook has no one to show an error to, so report it and go on.
        except Exception as error:
            print(f"default-layout: {label} failed: {error}")
        finally:
            # Release only what this run took, never another run's marker.
            if held:
                release(label)


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


def cmd_apply(spec: dict) -> int:
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
    tab_ids = [tab["tab_id"] for tab in workspace_tabs(snap, workspace_id)]
    focused = next(
        (
            tab_id
            for tab_id in (os.environ.get("HERDR_TAB_ID"), context().get("tab_id"), workspace.get("active_tab_id"))
            if tab_id in tab_ids
        ),
        tab_ids[0],
    )
    build_into(project, focused, tab_ids.index(focused), workspace_id)
    print(f"default-layout: applied {project['label']} ({len(project['tabs'])} tabs)")
    return 0


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else ""
    if action not in ("startup", "apply"):
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
        return cmd_startup(spec) if action == "startup" else cmd_apply(spec)
    except (HerdrError, OSError, ValueError) as error:
        print(f"default-layout: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
