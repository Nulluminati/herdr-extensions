# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Give each Herdr tab a title from its panes.

The title is the agent's session name, else other agent context, else the name
of the program in the pane. A herdboard planner tab gets the `Planning: `
prefix. The plugin renames only tabs it owns: a default number label, a
herdboard `plan <ref>` label, the label it wrote last, or a tab that the reset
action handed back. Any other label is the user's.

Herdr sends no event when a pane is renamed or a token changes, so a background
poller (one per Herdr server) runs a pass every few seconds.
"""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import tomllib
import traceback
import unicodedata


PLUGIN_ID = "nulluminati.auto-title"
DEFAULTS = {"max_length": 40, "poll_seconds": 2.0}
SESSION_TOKENS = ("claude_session_name", "pi_session_name")
CARD_TOKEN = "herdboard_card"
PLAN_LABEL = re.compile(r"plan [0-9a-z]+")
PLANNING_PREFIX = "Planning: "
# Error text of a Herdr call when the server is gone, not just one failed call.
SERVER_GONE = ("server_not_running", "server_unavailable", "Required program not found")
SERVER_GONE_SECONDS = 30
LOG_LIMIT = 100 * 1024
STOP_WAIT_SECONDS = 2


class TitleError(RuntimeError):
    """A failure that the script can explain without a traceback."""


def command(argv: list[str], timeout: int = 30):
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise TitleError(f"Required program not found: {argv[0]}") from error
    except subprocess.CalledProcessError as error:
        raise TitleError(error.stderr.strip() or f"{argv[0]} failed") from error
    except subprocess.TimeoutExpired as error:
        raise TitleError(f"{argv[0]} did not finish within {timeout} seconds") from error


def herdr(*arguments: str) -> dict:
    output = command([os.environ.get("HERDR_BIN_PATH") or "herdr", *arguments]).stdout
    return json.loads(output or "{}").get("result", {})


def notify(body: str) -> None:
    herdr("notification", "show", "Auto title", "--body", body)


# Directories, config, state and log

def state_directory() -> Path:
    explicit = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if explicit:
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(state) / "herdr-auto-title"


def config_directory() -> Path:
    explicit = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config) / "herdr-auto-title"


def log(state_dir: Path, message: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "auto-title.log"
    mode = "w" if path.exists() and path.stat().st_size > LOG_LIMIT else "a"
    with path.open(mode) as output:
        output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def load_config(config_dir: Path, state_dir: Path) -> dict:
    config = dict(DEFAULTS)
    path = config_dir / "config.toml"
    try:
        values = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return config
    except (OSError, tomllib.TOMLDecodeError) as error:
        log(state_dir, f"{path}: {error}; using the defaults")
        return config
    max_length = values.get("max_length", config["max_length"])
    # A title needs room for at least one character and the ellipsis.
    if isinstance(max_length, int) and not isinstance(max_length, bool) and max_length >= 2:
        config["max_length"] = max_length
    else:
        log(state_dir, f"{path}: bad max_length {max_length!r}; using {DEFAULTS['max_length']}")
    poll_seconds = values.get("poll_seconds", config["poll_seconds"])
    if isinstance(poll_seconds, (int, float)) and not isinstance(poll_seconds, bool) and poll_seconds > 0:
        config["poll_seconds"] = float(poll_seconds)
    else:
        log(state_dir, f"{path}: bad poll_seconds {poll_seconds!r}; using {DEFAULTS['poll_seconds']}")
    return config


def empty_state() -> dict:
    return {"tabs": {}, "reset": []}


def load_state(state_dir: Path) -> dict:
    try:
        state = json.loads((state_dir / "tabs.json").read_text())
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(state, dict) or not isinstance(state.get("tabs"), dict) \
            or not isinstance(state.get("reset"), list):
        return empty_state()
    return state


@contextlib.contextmanager
def state_lock(state_dir: Path):
    """Hold while you load, change and save the state: the poller and an action can run at once."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary = state_dir / "tabs.json.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, state_dir / "tabs.json")


# Naming (no Herdr calls)

def columns(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def clean(text: str, max_length: int) -> str:
    """One line of text, at most max_length terminal columns, cut with an ellipsis."""
    text = " ".join(text.split())
    text = "".join(character for character in text if unicodedata.category(character) != "Cc")
    if sum(columns(character) for character in text) <= max_length:
        return text
    kept, width = [], 0
    for character in text:
        if width + columns(character) > max_length - 1:
            break
        kept.append(character)
        width += columns(character)
    return "".join(kept).rstrip() + "…"


def card_title(token: str | None) -> str:
    """The card title from a herdboard_card token such as `▶ Title · Column`."""
    text = (token or "").strip()
    if text[:2] in ("▶ ", "✓ "):
        text = text[2:]
    title, separator, _ = text.rpartition(" · ")
    return (title if separator else text).strip()


def session_name(pane: dict) -> tuple[str, str] | None:
    tokens = pane.get("tokens") or {}
    for token in SESSION_TOKENS:
        if (tokens.get(token) or "").strip():
            return token, tokens[token]
    return None


def context_title(pane: dict) -> tuple[int, str, str] | None:
    """Sources 1 to 3 as (number, name, text): session name, agent pane label, card title."""
    session = session_name(pane)
    if session:
        return 1, session[0], session[1]
    if pane.get("agent") and (pane.get("label") or "").strip():
        return 2, "pane label", pane["label"]
    card = card_title((pane.get("tokens") or {}).get(CARD_TOKEN))
    if card:
        return 3, CARD_TOKEN, card
    return None


def pane_title(pane: dict, program: str | None) -> str:
    found = context_title(pane)
    return found[2] if found else (program or "")


def context_rank(pane: dict) -> int:
    # The main agent of a herdboard card holds the card token; a review or PR
    # split next to it holds at most a session name.
    tokens = pane.get("tokens") or {}
    if (tokens.get(CARD_TOKEN) or "").strip():
        return 2
    return 1 if session_name(pane) else 0


def source_pane(panes: list[dict]) -> dict | None:
    """The pane that names the tab: the best agent pane, else the focused pane, else the first."""
    candidates = [pane for pane in panes if pane.get("agent")]
    if candidates:
        best = max(context_rank(pane) for pane in candidates)
        candidates = [pane for pane in candidates if context_rank(pane) == best]
    else:
        candidates = panes
    for pane in candidates:
        if pane.get("focused"):
            return pane
    return candidates[0] if candidates else None


def owned(tab: dict, state: dict) -> str | None:
    """Why the plugin may rename the tab, or None when the label is the user's."""
    label = tab.get("label") or ""
    if label.isdigit():
        return "default number label"
    if PLAN_LABEL.fullmatch(label):
        return "herdboard plan label"
    if tab["tab_id"] in state["reset"]:
        return "reset"
    if label == (state["tabs"].get(tab["tab_id"]) or {}).get("written"):
        return "label the plugin wrote"
    return None


def planning(tab: dict, state: dict, workspaces: dict[str, str]) -> bool:
    """A herdboard planner tab: a card planner has a `plan <ref>` tab label, which the plugin
    replaces, so the state remembers it; an automation planner has a `plan <ref>` workspace."""
    if PLAN_LABEL.fullmatch(tab.get("label") or ""):
        return True
    if PLAN_LABEL.fullmatch(workspaces.get(tab.get("workspace_id"), "")):
        return True
    return bool((state["tabs"].get(tab["tab_id"]) or {}).get("planning"))


# Herdr passes

def snapshot() -> tuple[list[dict], dict[str, list[dict]], dict[str, str]]:
    """The tabs, the panes of each tab and the label of each workspace."""
    tabs = herdr("tab", "list").get("tabs", [])
    panes: dict[str, list[dict]] = {}
    for pane in herdr("pane", "list").get("panes", []):
        panes.setdefault(pane["tab_id"], []).append(pane)
    workspaces = {workspace["workspace_id"]: workspace.get("label") or ""
                  for workspace in herdr("workspace", "list").get("workspaces", [])}
    return tabs, panes, workspaces


def program_name(pane_id: str) -> str | None:
    try:
        info = herdr("pane", "process-info", "--pane", pane_id).get("process_info", {})
    except TitleError:
        return None
    processes = info.get("foreground_processes") or []
    return processes[0].get("name") if processes else None


def choose_title(panes: list[dict], config: dict, prefix: str = "") -> dict | None:
    """The source pane, the source used and the title for a tab, or None without a source."""
    pane = source_pane(panes)
    if pane is None:
        return None
    found = context_title(pane)
    if found is None:
        # Only a pane with no agent context costs a process-info call.
        found = 4, "program", program_name(pane["pane_id"]) or ""
    number, name, text = found
    return {"pane": pane["pane_id"], "number": number, "name": name,
            "title": clean(prefix + text if text else "", config["max_length"])}


def reconcile(state: dict, config: dict) -> bool:
    """Rename every tab the plugin owns. Return True when the state changed."""
    tabs, panes, workspaces = snapshot()
    changed = False
    # Herdr can reuse a tab ID, so forget tabs that are gone.
    live = {tab["tab_id"] for tab in tabs}
    for tab_id in [tab_id for tab_id in state["tabs"] if tab_id not in live]:
        del state["tabs"][tab_id]
        changed = True
    reset = [tab_id for tab_id in state["reset"] if tab_id in live]
    if reset != state["reset"]:
        state["reset"] = reset
        changed = True

    for tab in tabs:
        tab_id = tab["tab_id"]
        if not owned(tab, state):
            continue
        planner = planning(tab, state, workspaces)
        choice = choose_title(panes.get(tab_id, []), config, PLANNING_PREFIX if planner else "")
        if not choice or not choice["title"]:
            continue
        title = choice["title"]
        if title != tab.get("label"):
            herdr("tab", "rename", tab_id, title)
        record = {"written": title, "planning": True} if planner else {"written": title}
        if state["tabs"].get(tab_id) != record or tab_id in state["reset"]:
            state["tabs"][tab_id] = record
            state["reset"] = [other for other in state["reset"] if other != tab_id]
            changed = True
    return changed


def tick(state_dir: Path, config: dict) -> None:
    if (state_dir / "paused").exists():
        # Still ask Herdr, so a paused poller sees the server go away.
        herdr("tab", "list")
        return
    with state_lock(state_dir):
        state = load_state(state_dir)
        if reconcile(state, config):
            save_state(state_dir, state)


# Poller

def server_gone(error: TitleError) -> bool:
    return any(code in str(error) for code in SERVER_GONE)


def run(state_dir: Path, config: dict) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "poller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        (state_dir / "poller.pid").write_text(str(os.getpid()))
        return poll(state_dir, config)


def plugin_enabled() -> bool:
    plugins = herdr("plugin", "list", "--plugin", PLUGIN_ID, "--json").get("plugins", [])
    return any(plugin.get("plugin_id") == PLUGIN_ID and plugin.get("enabled") for plugin in plugins)


def poll(state_dir: Path, config: dict) -> int:
    gone_since = None
    while True:
        try:
            # Herdr does not stop the poller when the plugin is disabled or removed.
            if not plugin_enabled():
                return 0
            tick(state_dir, config)
            gone_since = None
        except TitleError as error:
            if not server_gone(error):
                log(state_dir, f"pass failed: {error}")
            else:
                # Exit when the server stays gone, so the poller does not outlive it.
                gone_since = time.monotonic() if gone_since is None else gone_since
                if time.monotonic() - gone_since >= SERVER_GONE_SECONDS:
                    return 0
        except Exception:
            log(state_dir, f"pass failed:\n{traceback.format_exc()}")
        time.sleep(config["poll_seconds"])


def poller_running(state_dir: Path) -> bool:
    path = state_dir / "poller.lock"
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def start(state_dir: Path) -> None:
    # Detach so the startup command returns at once; the child exits if a poller runs.
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "auto-title.log").open("a") as output:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=output,
            start_new_session=True,
        )


def stop(state_dir: Path) -> None:
    if not poller_running(state_dir):
        return
    try:
        os.kill(int((state_dir / "poller.pid").read_text()), signal.SIGTERM)
    except (OSError, ValueError) as error:
        raise TitleError(f"Cannot stop the poller: {error}") from error
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while poller_running(state_dir) and time.monotonic() < deadline:
        time.sleep(0.1)


# Actions

def focused_tab_id() -> str:
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    tab_id = context.get("focused_tab_id") or context.get("tab_id")
    if tab_id:
        return tab_id
    pane_id = context.get("focused_pane_id") or context.get("pane_id")
    if pane_id:
        for pane in herdr("pane", "list").get("panes", []):
            if pane["pane_id"] == pane_id:
                return pane["tab_id"]
    for tab in herdr("tab", "list").get("tabs", []):
        if tab.get("focused"):
            return tab["tab_id"]
    raise TitleError("No focused tab")


def reset(state_dir: Path, config: dict) -> None:
    tab_id = focused_tab_id()
    with state_lock(state_dir):
        state = load_state(state_dir)
        if tab_id not in state["reset"]:
            state["reset"].append(tab_id)
        reconcile(state, config)
        save_state(state_dir, state)
    label = (state["tabs"].get(tab_id) or {}).get("written") or tab_id
    notify(f"Tab {label} is automatic again")


def doctor(state_dir: Path, config: dict) -> str:
    tab_id = focused_tab_id()
    tabs, panes, workspaces = snapshot()
    tab = next((tab for tab in tabs if tab["tab_id"] == tab_id), None)
    if tab is None:
        raise TitleError(f"Tab {tab_id} not found")
    state = load_state(state_dir)
    reason = owned(tab, state)
    prefix = PLANNING_PREFIX if planning(tab, state, workspaces) else ""
    choice = choose_title(panes.get(tab_id, []), config, prefix)
    poller = "running" if poller_running(state_dir) else "not running"
    if (state_dir / "paused").exists():
        poller += ", paused"
    lines = [
        f"Label: {tab.get('label')}",
        f"Owner: plugin ({reason})" if reason else "Owner: user (manual label)",
        f"Source: {choice['pane']}, {choice['name']} ({choice['number']})" if choice else "Source: none",
        f"Title: {choice['title']}" if choice and choice["title"] else "Title: none",
        f"Poller: {poller}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="start the poller in the background")
    commands.add_parser("run", help=argparse.SUPPRESS)
    commands.add_parser("restart", help="stop the poller, then start it")
    commands.add_parser("reset", help="make the focused tab automatic again")
    commands.add_parser("doctor", help="explain the title of the focused tab")
    commands.add_parser("pause", help="stop renaming tabs")
    commands.add_parser("resume", help="rename tabs again")
    arguments = parser.parse_args(argv)

    state_dir = state_directory()
    config = load_config(config_directory(), state_dir)
    try:
        if arguments.command == "start":
            start(state_dir)
        elif arguments.command == "run":
            return run(state_dir, config)
        elif arguments.command == "restart":
            stop(state_dir)
            start(state_dir)
            notify("Poller restarted")
        elif arguments.command == "reset":
            reset(state_dir, config)
        elif arguments.command == "doctor":
            report = doctor(state_dir, config)
            print(report)
            notify(report)
        elif arguments.command == "pause":
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "paused").touch()
            notify("Paused: tabs keep their labels")
        elif arguments.command == "resume":
            (state_dir / "paused").unlink(missing_ok=True)
            start(state_dir)
            notify("Resumed")
    except TitleError as error:
        print(f"auto-title: {error}", file=sys.stderr)
        if arguments.command != "run":
            notify_failure(str(error))
        return 1
    return 0


def notify_failure(message: str) -> None:
    """Show a failed action in Herdr: a plugin action's error output goes only to the plugin log."""
    try:
        herdr("notification", "show", "Auto title failed", "--body", message)
    except TitleError as error:
        print(f"auto-title: cannot show a notification: {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
