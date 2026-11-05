"""Automations: a task text and a schedule. Each due time spawns a normal card and starts an agent on it.

The definitions live in `<todos_dir>/.herdboard/automations.toml`, the run state in the state folder.
`herdboard automation tick`, which an OS timer runs every few minutes, spawns and starts the due runs.
A spawned card is an ordinary todo: the user reviews, merges and finishes it as any other.
"""

from __future__ import annotations

import calendar
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

from . import activity, attachments
from . import start as start_module
from .config import AGENTS, Config, thinking_levels
from .model import ConflictError, HerdboardError, IoError, NotFoundError, automation_title
from .paths import card_dir
from .start import Starter, _lock, _read, _write, notify, state_dir
from .store import ID_ALPHABET, CardView

AUTOMATION_ID = re.compile(r"^a[0-9a-z]{3}$")
# Two ticks must not run the same due time twice.
TICK_LOCK = ".automations-tick.lock"
DEFINITIONS_LOCK = ".automations.lock"
DEFAULT_TIME = (9, 0)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# The longest periods, so a due time is always within the search.
MAX_DAYS = 365
MAX_WEEKS = 52
# The search for a due time stops after this many days: longer than any allowed period.
SEARCH_DAYS = 366 * 3
FORMS = (
    "every day",
    "every weekday",
    "every week on monday[, thursday]",
    "every month on the 1st",
    f"every N days (N up to {MAX_DAYS})",
    f"every N weeks (N up to {MAX_WEEKS})",
    "each with an optional `at HH:MM` (default 09:00)",
)


class AutomationError(HerdboardError):
    def __init__(self, message, code="usage"):
        super().__init__(message)
        self.code = code


class SpawnError(AutomationError):
    """A spawn that failed after it added its card."""

    def __init__(self, message, card: CardView):
        super().__init__(message, "spawn_failed")
        self.card = card


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
        if not 1 <= interval <= MAX_DAYS:
            raise _bad(text)
        return Schedule("day", hour, minute, interval=interval, anchor=anchor)
    if words == "every week":
        return Schedule("week", hour, minute, anchor=anchor)
    if found := re.fullmatch(r"every (\d+) weeks?", words):
        interval = int(found[1])
        if not 1 <= interval <= MAX_WEEKS:
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
    # Each spawned card starts as a factory run (`start --factory`). Needs a repository board.
    factory: bool = False
    factory_preset: str | None = None
    # When the automation was added: the first run is the first due time after it.
    created: str | None = None

    def parsed_schedule(self) -> Schedule:
        return parse_schedule(self.schedule, self.anchor())

    def anchor(self) -> date | None:
        created = _time(self.created)
        return created.date() if created else None


STRING_FIELDS = (
    "id",
    "title",
    "schedule",
    "board",
    "body",
    "type",
    "agent",
    "model",
    "thinking",
    "factory_preset",
    "created",
)
BOOL_FIELDS = ("enabled", "may_merge_prs", "factory")


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
        # A removed automation keeps its folder (attachments and log), so its ID stays taken.
        if candidate not in taken and not card_dir(config, candidate).exists():
            return candidate


def _check(store, item: Automation, *, check_preset: bool = True) -> Automation:
    """Check every field before a write. Returns the automation with the type's configured name."""
    config = store.config
    title = " ".join(item.title.split())
    if not title:
        raise AutomationError("An automation needs a title")
    for tag in item.tags:
        if not tag or any(ch.isspace() or ch == "#" for ch in tag):
            raise AutomationError(f"Tag {tag!r} must be one word without #")
    kind = config.type_named(item.type) if item.type else None
    if item.factory_preset is not None:
        if not item.factory:
            raise AutomationError("factory_preset needs factory = true", "usage")
        if any(value is not None for value in (item.agent, item.model, item.thinking)):
            raise AutomationError("factory_preset cannot be combined with agent, model or thinking", "usage")
        if check_preset and item.factory_preset not in config.factory_presets:
            raise AutomationError(f"Unknown factory preset {item.factory_preset!r}", "usage")
    if item.factory and item.board == "inbox":
        raise AutomationError("A factory automation needs a repository board.", "usage")
    if item.board != "inbox":
        try:
            store.resolve_board(item.board)
        except NotFoundError as error:
            raise AutomationError(f"No board named {item.board!r}", "unknown_board") from error
    parse_schedule(item.schedule)
    if item.agent is not None and item.agent not in AGENTS:
        raise AutomationError("The agent must be claude, pi or codex")
    agent = item.agent or config.start_agent
    levels = thinking_levels(agent)
    if item.thinking and item.thinking not in levels:
        raise AutomationError(f"Thinking must be one of {', '.join(levels)}")
    if item.model is not None and (not item.model or any(ch.isspace() for ch in item.model)):
        raise AutomationError("The model must be one name, such as zai/glm-5.3")
    return replace(item, title=title, type=kind, tags=list(item.tags))


def add(
    store,
    *,
    title: str,
    schedule: str,
    board: str = "inbox",
    body: str = "",
    type: str | None = None,
    tags: list[str] = (),
    agent: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    may_merge_prs: bool = False,
    factory: bool = False,
    factory_preset: str | None = None,
    enabled: bool = True,
    created: datetime | None = None,
) -> Automation:
    created = (created or datetime.now()).astimezone()
    with _lock(DEFINITIONS_LOCK, wait=2):
        current = load(store.config)
        item = _check(
            store,
            Automation(
                id=_new_id(store.config, {a.id for a in current}),
                title=title,
                schedule=schedule,
                board=board,
                body=body,
                type=type,
                tags=list(tags),
                enabled=enabled,
                agent=agent,
                model=model,
                thinking=thinking,
                may_merge_prs=may_merge_prs,
                factory=factory,
                factory_preset=factory_preset,
                created=created.isoformat(timespec="seconds"),
            ),
        )
        save(store.config, [*current, item])
    return item


def _change(store, automation_id: str, change, saved=None) -> tuple[Automation, Automation]:
    """Apply `change(item) -> item` to one automation. Returns (before, after). `saved(before, after)`
    runs under the same lock, so a tick never sees the change without it."""
    with _lock(DEFINITIONS_LOCK, wait=2):
        current = load(store.config)
        for index, item in enumerate(current):
            if item.id == automation_id:
                changed = change(item)
                changed = _check(store, changed, check_preset=changed.factory_preset != item.factory_preset)
                current[index] = changed
                save(store.config, current)
                if saved is not None:
                    saved(item, changed)
                return item, changed
    raise NotFoundError(f"No automation {automation_id}")


def edit(store, automation_id: str, now: datetime | None = None, **changes) -> Automation:
    """Change fields of an automation. A changed schedule starts with its next due time."""
    if changes.get("factory") is False:
        changes.setdefault("factory_preset", None)
    unknown = set(changes) - set(Automation.__dataclass_fields__) | ({"id", "created"} & set(changes))
    if unknown:
        raise AutomationError(f"Cannot change {', '.join(sorted(unknown))}")

    def saved(before: Automation, after: Automation) -> None:
        if after.schedule != before.schedule:
            _restart(automation_id, now)

    return _change(store, automation_id, lambda item: replace(item, **changes), saved)[1]


def set_enabled(store, automation_id: str, enabled: bool, now: datetime | None = None) -> Automation:
    """Pause or resume. A resumed automation starts with its next due time: it does not catch up."""

    def saved(before: Automation, after: Automation) -> None:
        if enabled and not before.enabled:
            _restart(automation_id, now)

    return _change(store, automation_id, lambda item: replace(item, enabled=enabled), saved)[1]


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
    """Add the automation's card to the first column of its board, with a copy of its attachments.
    The card's title starts with `Automation: `, so the board and the user see where it came from."""
    config = store.config
    path = store.ensure_inbox() if automation.board == "inbox" else store.resolve_board(automation.board)
    kind = config.type_named(automation.type) if automation.type else None
    body = automation.body.splitlines() if automation.body.strip() else None
    card = store.add(path, automation_title(automation.title), body=body, tags=automation.tags, type=kind)
    activity.append(store, card.ref, f"Spawned by automation {automation.id} for {_stamp(due)}.", "herdboard")
    try:
        for entry in attachments.listing(config, automation.id):
            attachments.add(store, card.ref, Path(entry["path"]))
    except HerdboardError as error:
        activity.append(store, card.ref, f"Cannot copy the automation's attachments: {error}", "herdboard")
        raise SpawnError(str(error), card) from error
    return card


def run(store, automation: Automation, due: datetime, *, skipped: int = 0, config_file: Path | None = None) -> dict:
    """Spawn one card and start its agent. Failures are logged, recorded and notified, never retried."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        card = spawn(store, automation, due)
    except HerdboardError as error:
        result = f"spawn_failed: {error}"
        # A card that was added stays on its board, linked to the automation.
        ref = error.card.ref if isinstance(error, SpawnError) else None
        _update_state(automation.id, last_run=now, last_card=ref, last_result=result)
        notify(f"herdboard: automation {automation.id} failed", f"{automation.title}: {error}")
        return {"id": automation.id, "card": ref, "result": result, "skipped": skipped}
    if skipped:
        activity.append(
            store,
            card.ref,
            f"Missed runs: skipped {skipped} earlier due time{'' if skipped == 1 else 's'}.",
            "herdboard",
        )
    starter = Starter(store, store.config.todos_dir, config_file=config_file)
    try:
        from . import factory

        run = (
            factory.resolve_run(
                store.config, automation.factory_preset, automation.agent, automation.model, automation.thinking
            )
            if automation.factory
            else None
        )
        starter.start(
            card.ref,
            agent=automation.agent,
            model=automation.model,
            thinking=automation.thinking,
            no_repo=automation.board == "inbox",
            may_merge_prs=automation.may_merge_prs,
            factory=automation.factory,
            factory_run=run,
        )
    except HerdboardError as error:
        result = f"start_failed: {error}"
        activity.append(store, card.ref, f"Auto start failed: {error}", "herdboard")
        _undo_move(store, card)
        notify(f"herdboard: {automation.title} did not start ({card.ref})", str(error))
    else:
        result = "started"
        notify(f"herdboard: {automation.title} started ({card.ref})", f"Automation {automation.id}")
    _update_state(automation.id, last_run=now, last_card=card.ref, last_result=result)
    return {"id": automation.id, "card": card.ref, "result": result, "skipped": skipped}


def _undo_move(store, card: CardView) -> None:
    """Put a card whose start failed after the move back in its first column, and forget the move in its
    start record, so a later start by hand moves it again. Best effort: the log already has the failure."""
    try:
        if store.show(card.ref).column != card.column:
            store.move(card.ref, card.column)
        path = state_dir() / "starts.json"
        with _lock(wait=2):
            records = _read(path)
            record = records.get(card.ref)
            if isinstance(record, dict) and "move" in record.get("steps_done", []):
                record["steps_done"] = [step for step in record["steps_done"] if step != "move"]
                _write(path, records)
    except HerdboardError as error:
        activity.append(store, card.ref, f"Cannot move the card back to {card.column}: {error}", "herdboard")


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
        for listed in load(store.config):
            # One broken automation does not stop the others.
            try:
                # The automation and its state are read again under the definitions lock, so a pause,
                # removal, edit or resume during an earlier run of this tick counts.
                with _lock(DEFINITIONS_LOCK, wait=2):
                    item = next((found for found in load(store.config) if found.id == listed.id), None)
                    if item is None or not item.enabled:
                        continue
                    found = _due_run(item, read_state(), now)
            except HerdboardError as error:
                errors.append({"id": listed.id, "message": str(error)})
                continue
            if found is not None:
                due, skipped = found
                runs.append(run(store, item, due, skipped=skipped, config_file=config_file))
    return {"busy": False, "runs": runs, "errors": errors}


def _due_run(item: Automation, state: dict, now: datetime) -> tuple[datetime, int] | None:
    """The due time to run now and the number of due times it skips, or None. A due time is recorded
    before the run, so a run that fails or crashes is never repeated."""
    schedule = item.parsed_schedule()
    due = schedule.latest_at_or_before(now)
    if due is None:
        return None
    entry = state.get(item.id) if isinstance(state.get(item.id), dict) else {}
    floor = _time(entry.get("last_due")) or _time(item.created)
    if floor is None:
        # Without a creation time, the automation starts with its next due time.
        _update_state(item.id, last_due=due.isoformat(timespec="seconds"))
        return None
    if due <= floor:
        return None
    skipped = schedule.count_between(floor, due)
    _update_state(item.id, last_due=due.isoformat(timespec="seconds"))
    return due, skipped


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
        found.append(
            {
                **_fields(item),
                "next_due": next_text,
                "schedule_error": problem,
                "last_run": entry.get("last_run"),
                "last_card": entry.get("last_card"),
                "last_result": entry.get("last_result"),
                "attachments": files,
            }
        )
    return found


def _fields(item: Automation) -> dict:
    return {key: getattr(item, key) for key in Automation.__dataclass_fields__}


# The OS timer that runs `automation tick`


SERVICE = "herdboard-automations"
LAUNCH_LABEL = "com.nulluminati.herdboard-automations"
TICK_SECONDS = 300
# The tools that a started agent needs: background jobs get a minimal PATH.
PATH_TOOLS = ("herdr", "gh", "git", "uv", "claude", "pi", "codex")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def _tick_program(config_file: Path | None) -> list[str]:
    """The tick command as absolute arguments."""
    # The config file is always named: the job's default can differ from this shell's.
    argv = shlex.split(start_module.herdboard_command())
    if config_file is not None:
        argv += ["--config", str(config_file)]
    if "/" not in argv[0]:
        found = shutil.which(argv[0])
        if found is None:
            raise AutomationError(f"{argv[0]} is not on the PATH", "herdboard_not_found")
        argv[0] = str(Path(found).absolute())
    return [*argv, "automation", "tick"]


def _environment(config_file: Path | None) -> dict[str, str]:
    herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not herdr:
        raise AutomationError(
            "Cannot find the herdr binary: put it on the PATH or set HERDR_BIN_PATH", "herdr_not_found"
        )
    folders = []
    for tool in PATH_TOOLS:
        found = herdr if tool == "herdr" else shutil.which(tool)
        if found and str(Path(found).absolute().parent) not in folders:
            folders.append(str(Path(found).absolute().parent))
    folders += [folder for folder in SYSTEM_PATH if folder not in folders]
    env = {"PATH": ":".join(folders), "HERDR_BIN_PATH": str(Path(herdr).absolute())}
    if config_file is not None:
        env["HERDBOARD_CONFIG"] = str(config_file)
    # A job does not get the shell's folders: without them it reads other config and state folders.
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        if os.environ.get(name):
            env[name] = str(Path(os.environ[name]).expanduser().absolute())
    return env


def _system(argv: list[str], tolerate: bool = False):
    try:
        result = start_module.command(argv)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutomationError(f"{argv[0]} failed: {error}", "timer_failed") from error
    if result.returncode and not tolerate:
        raise AutomationError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout).strip()}", "timer_failed")
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
        lines = [
            "[Unit]",
            "Description=herdboard automations: spawn and start due cards",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=" + " ".join(_systemd_quote(arg) for arg in program),
            *(f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in env.items()),
            "",
        ]
        _write_file(service, "\n".join(lines).encode("utf-8"))
        _write_file(
            timer_file,
            "\n".join(
                [
                    "[Unit]",
                    "Description=Run herdboard automations every 5 minutes",
                    "",
                    "[Timer]",
                    "OnCalendar=*:0/5",
                    "Persistent=true",
                    "",
                    "[Install]",
                    "WantedBy=timers.target",
                    "",
                ]
            ).encode("utf-8"),
        )
        _system(["systemctl", "--user", "daemon-reload"])
        _system(["systemctl", "--user", "enable", "--now", f"{SERVICE}.timer"])
    elif action == "remove":
        _system(["systemctl", "--user", "disable", "--now", f"{SERVICE}.timer"], tolerate=True)
        for path in (service, timer_file):
            path.unlink(missing_ok=True)
        _system(["systemctl", "--user", "daemon-reload"], tolerate=True)
    state = _system(["systemctl", "--user", "is-active", f"{SERVICE}.timer"], tolerate=True).stdout.strip()
    return {
        "backend": "systemd",
        "active": state == "active",
        "state": state or "unknown",
        "installed": service.exists() and timer_file.exists(),
        "files": [str(service), str(timer_file)],
    }


def _launchd(action: str, config_file: Path | None) -> dict:
    path = _launch_agent()
    domain = f"gui/{os.getuid()}"
    if action == "install":
        log = str(state_dir() / "automations.log")
        try:
            state_dir().mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise IoError(f"{state_dir()}: cannot create: {error}") from error
        _write_file(
            path,
            plistlib.dumps(
                {
                    "Label": LAUNCH_LABEL,
                    "ProgramArguments": _tick_program(config_file),
                    "EnvironmentVariables": _environment(config_file),
                    "StartInterval": TICK_SECONDS,
                    "RunAtLoad": True,
                    "StandardOutPath": log,
                    "StandardErrorPath": log,
                }
            ),
        )
        # A loaded job keeps its old definition until it is booted out.
        _system(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], tolerate=True)
        _system(["launchctl", "bootstrap", domain, str(path)])
    elif action == "remove":
        _system(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"], tolerate=True)
        path.unlink(missing_ok=True)
    loaded = _system(["launchctl", "print", f"{domain}/{LAUNCH_LABEL}"], tolerate=True).returncode == 0
    return {
        "backend": "launchd",
        "active": loaded,
        "state": "loaded" if loaded else "not loaded",
        "installed": path.exists(),
        "files": [str(path)],
    }


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
    raise AutomationError(
        f"Automations need systemd (Linux) or launchd (macOS), not {sys.platform}", "unsupported_platform"
    )


# The notice that the board and `doctor` show when automations are on but the timer does not run.
TIMER_INSTALL = "herdboard automation timer install"


def timer_problem() -> str | None:
    """Why automations do not run on their own, or None when the timer is active. The board's Automations
    tab and `doctor` both show this text, so the two stay the same."""
    try:
        status = timer("status")
    except HerdboardError as error:
        return f"Automations do not run on their own: {error}"
    if not status["active"]:
        return f"Automations do not run on their own: the {status['backend']} timer is {status['state']}."
    return None
