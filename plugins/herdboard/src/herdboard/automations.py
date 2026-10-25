"""Automations: a task text and a schedule. Each due time spawns a normal card and starts an agent on it.

The definitions live in `<todos_dir>/.herdboard/automations.toml`, the run state in the state folder.
`herdboard automation tick`, which an OS timer runs every few minutes, spawns and starts the due runs.
A spawned card is an ordinary todo: the user reviews, merges and finishes it as any other.
"""

from __future__ import annotations

import calendar
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as clock, timedelta
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

from . import activity, attachments, start as start_module
from .config import THINKING_LEVELS, Config, default_config_path
from .model import ConflictError, HerdboardError, IoError, NotFoundError, join_title, with_type
from .start import Starter, _lock, _read, _write, notify, state_dir
from .store import ID_ALPHABET, CardView

AUTOMATION_ID = re.compile(r"^a[0-9a-z]{3}$")
# Two ticks must not run the same due time twice.
TICK_LOCK = ".automations-tick.lock"
DEFINITIONS_LOCK = ".automations.lock"
DEFAULT_TIME = (9, 0)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The search for a due time stops after this many days; the longest period is `every N weeks`.
SEARCH_DAYS = 366 * 3
FORMS = ("every day", "every weekday", "every week on monday[, thursday]", "every month on the 1st",
         "every N days", "every N weeks", "each with an optional `at HH:MM` (default 09:00)")


class AutomationError(HerdboardError):
    def __init__(self, message, code="usage"):
        super().__init__(message)
        self.code = code


# Schedules


@dataclass(frozen=True)
class Schedule:
    """When an automation is due, in local time. `anchor` is the first day of `every N days/weeks`."""

    kind: str
    hour: int
    minute: int
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    day: int = 1
    anchor: date | None = None

    def occurs_on(self, day: date) -> bool:
        if self.kind == "day":
            return self.interval == 1 or (day - self.anchor).days % self.interval == 0
        if self.kind == "weekday":
            return day.weekday() < 5
        if self.kind == "week":
            if self.weekdays:
                return day.weekday() in self.weekdays
            return (day - self.anchor).days % (7 * self.interval) == 0
        # A month without the day runs on its last day.
        return day.day == min(self.day, calendar.monthrange(day.year, day.month)[1])

    def _at(self, day: date) -> datetime:
        return datetime.combine(day, clock(self.hour, self.minute)).astimezone()

    def next_after(self, moment: datetime) -> datetime:
        """The first due time after `moment`."""
        day = moment.astimezone().date()
        for _ in range(SEARCH_DAYS):
            if self.occurs_on(day) and self._at(day) > moment:
                return self._at(day)
            day += timedelta(days=1)
        raise AutomationError(f"No due time within {SEARCH_DAYS} days", "bad_schedule")

    def latest_at_or_before(self, moment: datetime) -> datetime | None:
        """The last due time at or before `moment`, or None when there is none within the search."""
        day = moment.astimezone().date()
        for _ in range(SEARCH_DAYS):
            if self.occurs_on(day) and self._at(day) <= moment:
                return self._at(day)
            day -= timedelta(days=1)
        return None

    def count_between(self, after: datetime, before: datetime) -> int:
        """The number of due times strictly between `after` and `before`."""
        count = 0
        moment = self.next_after(after)
        while moment < before:
            count += 1
            moment = self.next_after(moment)
        return count


def _bad(text: str) -> AutomationError:
    return AutomationError(f"Unknown schedule {text!r}. Use: {'; '.join(FORMS)}", "bad_schedule")


def _weekday(name: str) -> int | None:
    name = name.strip()
    for number, full in enumerate(WEEKDAYS):
        if name == full or (len(name) >= 3 and full.startswith(name)):
            return number
    return None


def parse_schedule(text: str, anchor: date | None = None) -> Schedule:
    """Read a schedule such as `every weekday at 07:30`. `anchor` defaults to today."""
    words = " ".join(str(text).casefold().split())
    hour, minute = DEFAULT_TIME
    found = re.fullmatch(r"(?P<rest>.*?) at (?P<hour>\d{1,2}):(?P<minute>\d{2})", words)
    if found:
        hour, minute = int(found["hour"]), int(found["minute"])
        if hour > 23 or minute > 59:
            raise _bad(text)
        words = found["rest"]
    anchor = anchor or date.today()
    if words == "every day":
        return Schedule("day", hour, minute, anchor=anchor)
    if words == "every weekday":
        return Schedule("weekday", hour, minute)
    if found := re.fullmatch(r"every (\d+) days?", words):
        interval = int(found[1])
        if interval < 1:
            raise _bad(text)
        return Schedule("day", hour, minute, interval=interval, anchor=anchor)
    if words == "every week":
        return Schedule("week", hour, minute, anchor=anchor)
    if found := re.fullmatch(r"every (\d+) weeks?", words):
        interval = int(found[1])
        if interval < 1:
            raise _bad(text)
        return Schedule("week", hour, minute, interval=interval, anchor=anchor)
    if found := re.fullmatch(r"every week on (.+)", words):
        days = [_weekday(part) for part in re.split(r",| and ", found[1]) if part.strip()]
        if not days or None in days:
            raise _bad(text)
        return Schedule("week", hour, minute, weekdays=tuple(sorted(set(days))), anchor=anchor)
    if found := re.fullmatch(r"every month on the (\d{1,2})(st|nd|rd|th)", words):
        day = int(found[1])
        if not 1 <= day <= 31:
            raise _bad(text)
        return Schedule("month", hour, minute, day=day)
    raise _bad(text)


# Definitions


@dataclass
class Automation:
    id: str
    title: str
    schedule: str
    board: str = "inbox"
    body: str = ""
    type: str | None = None
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    agent: str | None = None
    model: str | None = None
    thinking: str | None = None
    may_merge_prs: bool = False
    # When the automation was added: the first run is the first due time after it.
    created: str | None = None

    def parsed_schedule(self) -> Schedule:
        return parse_schedule(self.schedule, self.anchor())

    def anchor(self) -> date | None:
        created = _time(self.created)
        return created.date() if created else None


STRING_FIELDS = ("id", "title", "schedule", "board", "body", "type", "agent", "model", "thinking", "created")
BOOL_FIELDS = ("enabled", "may_merge_prs")


def definitions_path(config: Config) -> Path:
    return config.todos_dir / ".herdboard" / "automations.toml"


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def load(config: Config) -> list[Automation]:
    """The automations in the definitions file, in file order."""
    path = definitions_path(config)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read the automations: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise AutomationError(f"{path}: invalid TOML: {error}", "format") from error
    entries = data.get("automation", [])
    if not isinstance(entries, list):
        raise AutomationError(f"{path}: `automation` must be a list of tables", "format")
    found = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise AutomationError(f"{path}: automation {number} is not a table", "format")
        values = {}
        for key in STRING_FIELDS:
            if key in entry:
                if not isinstance(entry[key], str):
                    raise AutomationError(f"{path}: automation {number}: {key} must be a string", "format")
                values[key] = entry[key]
        for key in BOOL_FIELDS:
            if key in entry:
                if not isinstance(entry[key], bool):
                    raise AutomationError(f"{path}: automation {number}: {key} must be true or false", "format")
                values[key] = entry[key]
        tags = entry.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise AutomationError(f"{path}: automation {number}: tags must be a list of strings", "format")
        values["tags"] = list(tags)
        for key in ("id", "title", "schedule"):
            if not values.get(key):
                raise AutomationError(f"{path}: automation {number} has no {key}", "format")
        found.append(Automation(**values))
    return found


def _toml_string(value: str) -> str:
    # A JSON string is a TOML basic string, except that TOML also escapes DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml(automations: list[Automation]) -> str:
    """The definitions file: one `[[automation]]` table per automation."""
    parts = ["# herdboard automations. Change them with `herdboard automation` or the board's Automations tab.\n"]
    for item in automations:
        lines = ["", "[[automation]]"]
        for key in STRING_FIELDS:
            value = getattr(item, key)
            if value is not None:
                lines.append(f"{key} = {_toml_string(value)}")
        lines.append(f"tags = [{', '.join(_toml_string(tag) for tag in item.tags)}]")
        for key in BOOL_FIELDS:
            lines.append(f"{key} = {'true' if getattr(item, key) else 'false'}")
        parts.append("\n".join(lines) + "\n")
    return "".join(parts)


def save(config: Config, automations: list[Automation]) -> None:
    path = definitions_path(config)
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_toml(automations))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    except OSError as error:
        raise IoError(f"{path}: cannot write the automations: {error}") from error
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)


def get(config: Config, automation_id: str) -> Automation:
    for item in load(config):
        if item.id == automation_id:
            return item
    raise NotFoundError(f"No automation {automation_id}")


def _new_id(config: Config, taken: set[str]) -> str:
    while True:
        candidate = "a" + "".join(secrets.choice(ID_ALPHABET) for _ in range(3))
        # A removed automation keeps its attachments and log, so its ID stays taken.
        if candidate not in taken and not attachments.folder(config, candidate).exists() \
                and not activity.log_path(config, candidate).exists():
            return candidate


def _check(store, item: Automation) -> Automation:
    """Check every field before a write. Returns the automation with the type's configured name."""
    config = store.config
    title = " ".join(item.title.split())
    if not title:
        raise AutomationError("An automation needs a title")
    for tag in item.tags:
        if not tag or any(ch.isspace() or ch == "#" for ch in tag):
            raise AutomationError(f"Tag {tag!r} must be one word without #")
    kind = config.type_named(item.type) if item.type else None
    if item.board != "inbox":
        try:
            store.resolve_board(item.board)
        except NotFoundError as error:
            raise AutomationError(f"No board named {item.board!r}", "unknown_board") from error
    parse_schedule(item.schedule)
    if item.agent not in (None, "claude", "pi"):
        raise AutomationError("The agent must be claude or pi")
    agent = item.agent or config.start_agent
    if item.thinking and agent != "pi":
        raise AutomationError("Thinking works only with pi")
    if item.thinking and item.thinking not in THINKING_LEVELS:
        raise AutomationError(f"Thinking must be one of {', '.join(THINKING_LEVELS)}")
    if item.model is not None and (not item.model or any(ch.isspace() for ch in item.model)):
        raise AutomationError("The model must be one name, such as zai/glm-5.3")
    # Card text is one line; the title and tags make it.
    join_title(title, item.tags, None)
    return replace(item, title=title, type=kind, tags=list(item.tags))


def add(store, *, title: str, schedule: str, board: str = "inbox", body: str = "", type: str | None = None,
        tags: list[str] = (), agent: str | None = None, model: str | None = None, thinking: str | None = None,
        may_merge_prs: bool = False, enabled: bool = True, created: datetime | None = None) -> Automation:
    created = (created or datetime.now()).astimezone()
    with _lock(DEFINITIONS_LOCK, wait=2):
        current = load(store.config)
        item = _check(store, Automation(
            id=_new_id(store.config, {a.id for a in current}), title=title, schedule=schedule, board=board,
            body=body, type=type, tags=list(tags), enabled=enabled, agent=agent, model=model, thinking=thinking,
            may_merge_prs=may_merge_prs, created=created.isoformat(timespec="seconds")))
        save(store.config, [*current, item])
    return item


def _change(store, automation_id: str, change) -> tuple[Automation, Automation]:
    """Apply `change(item) -> item` to one automation. Returns (before, after)."""
    with _lock(DEFINITIONS_LOCK, wait=2):
        current = load(store.config)
        for index, item in enumerate(current):
            if item.id == automation_id:
                changed = _check(store, change(item))
                current[index] = changed
                save(store.config, current)
                return item, changed
    raise NotFoundError(f"No automation {automation_id}")


def edit(store, automation_id: str, now: datetime | None = None, **changes) -> Automation:
    """Change fields of an automation. A changed schedule starts with its next due time."""
    unknown = set(changes) - set(Automation.__dataclass_fields__) | ({"id", "created"} & set(changes))
    if unknown:
        raise AutomationError(f"Cannot change {', '.join(sorted(unknown))}")
    before, after = _change(store, automation_id, lambda item: replace(item, **changes))
    if after.schedule != before.schedule:
        _restart(automation_id, now)
    return after


def set_enabled(store, automation_id: str, enabled: bool, now: datetime | None = None) -> Automation:
    """Pause or resume. A resumed automation starts with its next due time: it does not catch up."""
    before, after = _change(store, automation_id, lambda item: replace(item, enabled=enabled))
    if enabled and not before.enabled:
        _restart(automation_id, now)
    return after


def remove(store, automation_id: str) -> Automation:
    with _lock(DEFINITIONS_LOCK, wait=2):
        current = load(store.config)
        found = next((item for item in current if item.id == automation_id), None)
        if found is None:
            raise NotFoundError(f"No automation {automation_id}")
        save(store.config, [item for item in current if item.id != automation_id])
    with _lock(wait=2):
        state = read_state()
        if state.pop(automation_id, None) is not None:
            _write(_state_path(), state)
    return found


# Run state


def _state_path() -> Path:
    return state_dir() / "automations.json"


def read_state() -> dict:
    return _read(_state_path())


def _update_state(automation_id: str, **values) -> None:
    with _lock(wait=2):
        state = read_state()
        entry = state.get(automation_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(values)
        state[automation_id] = entry
        _write(_state_path(), state)


def _restart(automation_id: str, now: datetime | None) -> None:
    """Count due times from `now`, so earlier ones do not run."""
    moment = (now or datetime.now()).astimezone()
    _update_state(automation_id, last_due=moment.isoformat(timespec="seconds"))


# Runs


def _stamp(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def spawn(store, automation: Automation, due: datetime) -> CardView:
    """Add the automation's card to the first column of its board, with a copy of its attachments."""
    config = store.config
    path = store.ensure_inbox() if automation.board == "inbox" else store.resolve_board(automation.board)
    text = join_title(automation.title, automation.tags, None)
    if automation.type:
        text = with_type(text, config.type_named(automation.type))
    body = automation.body.splitlines() if automation.body.strip() else None
    card = store.add(path, text, body=body)
    for entry in attachments.listing(config, automation.id):
        attachments.add(store, card.ref, Path(entry["path"]))
    activity.append(store, card.ref, f"Spawned by automation {automation.id} for {_stamp(due)}.", "herdboard")
    return card


def run(store, automation: Automation, due: datetime, *, skipped: int = 0,
        config_file: Path | None = None) -> dict:
    """Spawn one card and start its agent. Failures are logged, recorded and notified, never retried."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        card = spawn(store, automation, due)
    except HerdboardError as error:
        result = f"spawn_failed: {error}"
        _update_state(automation.id, last_run=now, last_card=None, last_result=result)
        notify(f"herdboard: automation {automation.id} failed", f"{automation.title}: {error}")
        return {"id": automation.id, "card": None, "result": result, "skipped": skipped}
    if skipped:
        activity.append(store, card.ref, f"Missed runs: skipped {skipped} earlier due "
                        f"time{'' if skipped == 1 else 's'}.", "herdboard")
    starter = Starter(store, store.config.todos_dir, config_file=config_file)
    try:
        starter.start(card.ref, agent=automation.agent, model=automation.model, thinking=automation.thinking,
                      no_repo=automation.board == "inbox", may_merge_prs=automation.may_merge_prs)
    except HerdboardError as error:
        result = f"start_failed: {error}"
        activity.append(store, card.ref, f"Auto start failed: {error}", "herdboard")
        notify(f"herdboard: {automation.title} did not start ({card.ref})", str(error))
    else:
        result = "started"
        notify(f"herdboard: {automation.title} started ({card.ref})", f"Automation {automation.id}")
    _update_state(automation.id, last_run=now, last_card=card.ref, last_result=result)
    return {"id": automation.id, "card": card.ref, "result": result, "skipped": skipped}


def tick(store, now: datetime | None = None, *, config_file: Path | None = None) -> dict:
    """Run each enabled automation once for its latest due time, when that is new. A tick that finds
    another tick running does nothing."""
    now = (now or datetime.now()).astimezone()
    runs, errors = [], []
    with ExitStack() as stack:
        try:
            stack.enter_context(_lock(TICK_LOCK))
        except ConflictError:
            return {"busy": True, "runs": [], "errors": []}
        state = read_state()
        for item in load(store.config):
            if not item.enabled:
                continue
            try:
                schedule = item.parsed_schedule()
            except HerdboardError as error:
                errors.append({"id": item.id, "message": str(error)})
                continue
            due = schedule.latest_at_or_before(now)
            if due is None:
                continue
            entry = state.get(item.id) if isinstance(state.get(item.id), dict) else {}
            floor = _time(entry.get("last_due")) or _time(item.created)
            if floor is None:
                # Without a creation time, the automation starts with its next due time.
                _update_state(item.id, last_due=due.isoformat(timespec="seconds"))
                continue
            if due <= floor:
                continue
            skipped = schedule.count_between(floor, due)
            # Recorded before the run, so a run that fails or crashes is never repeated.
            _update_state(item.id, last_due=due.isoformat(timespec="seconds"))
            runs.append(run(store, item, due, skipped=skipped, config_file=config_file))
    return {"busy": False, "runs": runs, "errors": errors}


def listing(store, now: datetime | None = None) -> list[dict]:
    """Each automation with its next due time and its last run, for the CLI and the board."""
    now = (now or datetime.now()).astimezone()
    state = read_state()
    found = []
    for item in load(store.config):
        entry = state.get(item.id) if isinstance(state.get(item.id), dict) else {}
        try:
            next_due = item.parsed_schedule().next_after(max(now, _time(entry.get("last_due")) or now))
            next_text = next_due.isoformat(timespec="seconds") if item.enabled else None
            problem = None
        except HerdboardError as error:
            next_text, problem = None, str(error)
        try:
            files = attachments.listing(store.config, item.id)
        except HerdboardError:
            files = []
        found.append({**_fields(item), "next_due": next_text, "schedule_error": problem,
                      "last_run": entry.get("last_run"), "last_card": entry.get("last_card"),
                      "last_result": entry.get("last_result"), "attachments": files})
    return found


def _fields(item: Automation) -> dict:
    return {key: getattr(item, key) for key in Automation.__dataclass_fields__}


# The OS timer that runs `automation tick`


SERVICE = "herdboard-automations"
LAUNCH_LABEL = "com.nulluminati.herdboard-automations"
TICK_SECONDS = 300
# The tools that a started agent needs: background jobs get a minimal PATH.
PATH_TOOLS = ("herdr", "gh", "git", "uv", "claude", "pi")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def _tick_program(config_file: Path | None) -> list[str]:
    """The tick command as absolute arguments."""
    argv = shlex.split(start_module.herdboard_command(config_file))
    if "/" not in argv[0]:
        found = shutil.which(argv[0])
        if found is None:
            raise AutomationError(f"{argv[0]} is not on the PATH", "herdboard_not_found")
        argv[0] = str(Path(found).absolute())
    return [*argv, "automation", "tick"]


def _environment(config_file: Path | None) -> dict[str, str]:
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not herdr:
        raise AutomationError("Cannot find the herdr binary: put it on the PATH or set HERDR_BIN_PATH",
                              "herdr_not_found")
    folders = []
    for tool in PATH_TOOLS:
        found = herdr if tool == "herdr" else shutil.which(tool)
        if found and str(Path(found).absolute().parent) not in folders:
            folders.append(str(Path(found).absolute().parent))
    folders += [folder for folder in SYSTEM_PATH if folder not in folders]
    env = {"PATH": ":".join(folders), "HERDR_BIN_PATH": str(Path(herdr).absolute())}
    if config_file is not None and Path(config_file) != default_config_path():
        env["HERDBOARD_CONFIG"] = str(config_file)
    return env


def _system(argv: list[str], tolerate: bool = False):
    try:
        result = start_module.command(argv)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutomationError(f"{argv[0]} failed: {error}", "timer_failed") from error
    if result.returncode and not tolerate:
        raise AutomationError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout).strip()}",
                              "timer_failed")
    return result


def _systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _systemd_units() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "systemd" / "user"


def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def _write_file(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as error:
        raise IoError(f"{path}: cannot write: {error}") from error


def _systemd(action: str, config_file: Path | None) -> dict:
    units = _systemd_units()
    service, timer_file = units / f"{SERVICE}.service", units / f"{SERVICE}.timer"
    if action == "install":
        program = _tick_program(config_file)
        env = _environment(config_file)
        lines = ["[Unit]", "Description=herdboard automations: spawn and start due cards", "",
                 "[Service]", "Type=oneshot",
                 "ExecStart=" + " ".join(_systemd_quote(arg) for arg in program),
                 *(f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in env.items()), ""]
        _write_file(service, "\n".join(lines).encode("utf-8"))
        _write_file(timer_file, "\n".join([
            "[Unit]", "Description=Run herdboard automations every 5 minutes", "",
            "[Timer]", "OnCalendar=*:0/5", "Persistent=true", "",
            "[Install]", "WantedBy=timers.target", ""]).encode("utf-8"))
        _system(["systemctl", "--user", "daemon-reload"])
        _system(["systemctl", "--user", "enable", "--now", f"{SERVICE}.timer"])
    elif action == "remove":
        _system(["systemctl", "--user", "disable", "--now", f"{SERVICE}.timer"], tolerate=True)
        for path in (service, timer_file):
            path.unlink(missing_ok=True)
        _system(["systemctl", "--user", "daemon-reload"], tolerate=True)
    state = _system(["systemctl", "--user", "is-active", f"{SERVICE}.timer"], tolerate=True).stdout.strip()
    return {"backend": "systemd", "active": state == "active", "state": state or "unknown",
            "installed": service.exists() and timer_file.exists(), "files": [str(service), str(timer_file)]}


def _launchd(action: str, config_file: Path | None) -> dict:
    path = _launch_agent()
    domain = f"gui/{os.getuid()}"
    if action == "install":
        log = str(state_dir() / "automations.log")
        try:
            state_dir().mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise IoError(f"{state_dir()}: cannot create: {error}") from error
        _write_file(path, plistlib.dumps({
            "Label": LAUNCH_LABEL, "ProgramArguments": _tick_program(config_file),
            "EnvironmentVariables": _environment(config_file), "StartInterval": TICK_SECONDS,
            "RunAtLoad": True, "StandardOutPath": log, "StandardErrorPath": log}))
        # A loaded job keeps its old definition until it is booted out.
        _system(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], tolerate=True)
        _system(["launchctl", "bootstrap", domain, str(path)])
    elif action == "remove":
        _system(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], tolerate=True)
        path.unlink(missing_ok=True)
    loaded = _system(["launchctl", "print", f"{domain}/{LAUNCH_LABEL}"], tolerate=True).returncode == 0
    return {"backend": "launchd", "active": loaded, "state": "loaded" if loaded else "not loaded",
            "installed": path.exists(), "files": [str(path)]}


def timer(action: str, config_file: Path | None = None) -> dict:
    """Install, remove or report the OS timer that runs `automation tick` every 5 minutes.

    A second install writes the files again, so it also refreshes the paths in them.
    """
    if action not in ("install", "remove", "status"):
        raise AutomationError("The timer action must be install, remove or status")
    if sys.platform.startswith("linux"):
        return _systemd(action, config_file)
    if sys.platform == "darwin":
        return _launchd(action, config_file)
    raise AutomationError(f"Automations need systemd (Linux) or launchd (macOS), not {sys.platform}",
                          "unsupported_platform")
