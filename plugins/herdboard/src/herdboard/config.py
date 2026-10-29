"""Load herdboard settings from a TOML file."""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import os
import re
import shlex
import subprocess
from pathlib import Path
import tempfile
import threading
import tomllib

from .linear import LinearAuthError
from .model import HerdboardError, IoError, UnknownTypeError

DEFAULT_COLUMNS = ["Backlog", "Todo", "In progress", "Review", "Done"]
DEFAULT_LINEAR_COLUMNS = {
    "backlog": "Backlog",
    "triage": "Backlog",
    "unstarted": "Todo",
    "started": "In progress",
    "completed": "Done",
    "canceled": "Done",
}


DEFAULT_DONE_DAYS = 14
AGENTS = ("claude", "pi", "codex")
# pi's thinking levels (`pi --thinking`).
PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
CODEX_THINKING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
# Claude Code's effort levels (`claude --effort`).
CLAUDE_THINKING_LEVELS = ("low", "medium", "high", "xhigh", "max")
THINKING_LEVELS = tuple(dict.fromkeys((*PI_THINKING_LEVELS, *CODEX_THINKING_LEVELS, *CLAUDE_THINKING_LEVELS)))

MERGE_MODES = ("local", "pr")
# The names that a card's `#type/<name>` tag takes in `add --type`, `edit --type` and the board.
DEFAULT_TYPES = ["bug", "feature", "chore", "docs", "refactor"]
TAG_STYLES = ("outline", "round", "block")
CARD_STYLES = ("full", "compact")


KEY_COMMAND_TIMEOUT = 30
# The share of the agent pane's height that the details pane takes. Herdr splits panes by ratio.
DETAILS_PANE_RATIOS = (0.1, 0.6)


def thinking_levels(agent: str) -> tuple[str, ...]:
    """The thinking levels accepted by an agent's command line."""
    return {"pi": PI_THINKING_LEVELS, "codex": CODEX_THINKING_LEVELS, "claude": CLAUDE_THINKING_LEVELS}.get(agent, ())


def _run_key_command(argv: list[str]) -> str:
    name = shlex.join(argv)
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=KEY_COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise LinearAuthError(f"Linear key command {name} timed out after {KEY_COMMAND_TIMEOUT} seconds") from None
    except UnicodeDecodeError:
        raise LinearAuthError(f"Linear key command {name} returned undecodable output") from None
    except ValueError:
        raise LinearAuthError(f"Linear key command {name} has invalid arguments") from None
    except OSError as error:
        raise LinearAuthError(f"Linear key command {name} could not run: {error}") from None
    if result.returncode:
        raise LinearAuthError(f"Linear key command {name} exited with status {result.returncode}")
    key = result.stdout.strip()
    if not key:
        raise LinearAuthError(f"Linear key command {name} returned empty stdout")
    return key


@dataclass
class LinearConfig:
    """`[linear]` settings. The key comes from a command or environment, never the file itself."""

    enabled: bool | None = None  # None: on when a key source is configured
    api_key_env: str = "LINEAR_API_KEY"
    api_key_command: list[str] | None = None
    _command_key: str | None = field(default=None, init=False, repr=False, compare=False)
    _command_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LINEAR_COLUMNS))
    state_names: dict[str, str] = field(default_factory=lambda: {"In Review": "Review"})
    # Days a completed issue stays visible. The client also accepts None (show every
    # completed issue), but TOML has no null, so a config file cannot set it.
    done_days: int = DEFAULT_DONE_DAYS

    @property
    def api_key(self) -> str | None:
        if self.api_key_command is not None:
            with self._command_lock:
                if self._command_key is None:
                    self._command_key = _run_key_command(self.api_key_command)
                return self._command_key
        return os.environ.get(self.api_key_env) or None

    @property
    def key_configured(self) -> bool:
        """Check for a key source without running the command."""
        return self.api_key_command is not None or bool(os.environ.get(self.api_key_env))

    @property
    def key_source(self) -> str:
        return "linear.api_key_command" if self.api_key_command is not None else self.api_key_env

    @property
    def active(self) -> bool:
        """True when Linear is on and a key source is configured."""
        wanted = self.key_configured if self.enabled is None else self.enabled
        return wanted and self.key_configured


@dataclass
class Config:
    todos_dir: Path = field(default_factory=lambda: Path("~/todos").expanduser())
    # Where `start --new-repo` creates a new repository, under a slug of the card title.
    projects_dir: Path = field(default_factory=lambda: Path("~/Projects").expanduser())
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    complete_markers: list[str] = field(default_factory=lambda: ["Complete"])
    vault_root: Path | None = None
    archive_titles: list[str] = field(default_factory=lambda: ["Archive"])
    linear: LinearConfig = field(default_factory=LinearConfig)
    start_agent: str = "claude"
    # The model and thinking level `start` gives an agent; None keeps the agent's own default.
    start_model: str | None = None
    start_thinking: str | None = None
    # The Codex model that `mockup` generates images with. The user's Codex config model does not
    # work for image generation on a ChatGPT account, so herdboard always names one.
    mockup_model: str = "gpt-6.1-sol"
    # The thinking level that the user last chose for each model, by model name.
    thinking: dict[str, str] = field(default_factory=dict)
    # Models the board offers for pi; None reads pi's own `enabledModels`.
    pi_models: list[str] | None = None
    # Models the board offers for Codex; None reads Codex's visible cached models.
    codex_models: list[str] | None = None
    start_column: str | None = None
    # How `merge` merges a card branch when neither the command nor the board says: "local" or "pr".
    merge: str = "local"
    # A Textual theme name. The ANSI themes use the terminal's own colors.
    theme: str = "ansi-dark"
    # "outline" draws tag text in its color on a faint tint; "round" draws tag ends with Nerd Font
    # half circles; "block" needs no special font.
    tag_style: str = "outline"
    # "full" draws each card in its own border with a description line; "compact" leaves both out.
    card_style: str = "full"
    # "plain" uses text labels; "nerd" adds Nerd Font icons beside them.
    icon_style: str = "plain"
    types: list[str] = field(default_factory=lambda: list(DEFAULT_TYPES))
    # Open a read-only card details pane on top of each card agent and planner.
    details_pane: bool = True
    details_pane_ratio: float = 0.25

    def __post_init__(self):
        if self.start_column is None:
            self.start_column = self.linear.columns["started"]

    def type_named(self, name: str) -> str:
        """The configured type that `name` names, in any letter case."""
        for kind in self.types:
            if kind.casefold() == name.strip().casefold():
                return kind
        raise UnknownTypeError(f"Unknown type {name!r}: use one of {', '.join(self.types)}")


def config_path(path: Path | None = None) -> Path:
    # Absolute, because agents and workers that get the path run in other folders.
    if path is not None:
        return Path(path).expanduser().absolute()
    explicit = os.environ.get("HERDBOARD_CONFIG")
    if explicit:
        return Path(explicit).expanduser().absolute()
    return default_config_path()


def default_config_path() -> Path:
    """The config file when neither --config nor HERDBOARD_CONFIG names one."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "herdboard" / "config.toml"


def save_display_config(path: Path | None = None, *, icon_style: str | None = None,
                        tag_style: str | None = None, card_style: str | None = None) -> None:
    """Save only changed display settings, with comments, permissions and other values intact."""
    changes = {key: value for key, value in (("icon_style", icon_style), ("tag_style", tag_style),
                                             ("card_style", card_style)) if value is not None}
    save_global_config(path, **changes)


THINKING_TABLE = "thinking"


def check_thinking_levels(model: str | None, level: str | None) -> None:
    """Raise when `model` or `level` cannot go in the thinking table. None level removes the model."""
    if not isinstance(model, str) or not model or any(ch.isspace() for ch in model):
        raise HerdboardError("The model must be one name, such as zai/glm-5.3")
    if level is not None and level not in THINKING_LEVELS:
        raise HerdboardError(f"A thinking level must be one of {', '.join(THINKING_LEVELS)}")


def save_thinking_levels(path: Path | None = None, model: str = "", level: str | None = None) -> None:
    """Remember `model`'s thinking level for the next start, plan or review. None forgets the model.

    The level lives in the `[thinking]` table of the config file, so a person can read and change it.
    """
    check_thinking_levels(model, level)
    path = config_path(path)
    if level is None and not path.exists():
        return

    def change(document) -> bool:
        from tomlkit import table as new_table

        found = document.get(THINKING_TABLE)
        if level is None:
            if found is None or model not in found:
                return False
            del found[model]
            if not found:
                # An emptied table would keep an empty `[thinking]` header.
                del document[THINKING_TABLE]
            return True
        if not isinstance(found, dict):
            if found is not None:
                raise HerdboardError(f"{path}: thinking must be a table of model names and thinking levels")
            found = new_table()
            document[THINKING_TABLE] = found
        if found.get(model) == level:
            return False
        found[model] = level
        return True

    _rewrite_config(path, change)


class _Unchanged:
    def __repr__(self) -> str:
        return "UNCHANGED"


# A `save_global_config` argument that leaves its setting as it is. None removes an optional setting.
UNCHANGED = _Unchanged()
# The settings that may be absent: None removes them, so the agent's own default applies.
_OPTIONAL_SETTINGS = ("start_model", "start_thinking")


def _check_global(key: str, value) -> None:
    """Raise when `value` is not a valid value of the global setting `key`."""
    if value is None and key in _OPTIONAL_SETTINGS:
        return
    if key == "details_pane":
        if not isinstance(value, bool):
            raise HerdboardError("details_pane must be true or false")
        return
    if key == "details_pane_ratio":
        _check_details_ratio(value, "details_pane_ratio")
        return
    choices = {"start_agent": AGENTS, "start_thinking": THINKING_LEVELS, "merge": MERGE_MODES,
               "icon_style": ("plain", "nerd"), "tag_style": TAG_STYLES, "card_style": CARD_STYLES}.get(key)
    if choices is not None:
        if value not in choices:
            raise HerdboardError(f"{key} must be {', '.join(choices[:-1])} or {choices[-1]}")
    elif not isinstance(value, str) or not value:
        raise HerdboardError(f"{key} must be a non-empty string")
    elif key == "start_model" and any(ch.isspace() for ch in value):
        raise HerdboardError("start_model must be one model name, such as zai/glm-5.3")


def _check_details_ratio(value, label: str) -> None:
    low, high = DETAILS_PANE_RATIOS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise HerdboardError(f"{label} must be a number from {low} to {high}")


def check_global_config(**settings) -> None:
    """Raise when a value is not valid for `save_global_config`, before any write."""
    for key, value in settings.items():
        _check_global(key, value)


def save_global_config(path: Path | None = None, *, theme=UNCHANGED, start_agent=UNCHANGED,
                       start_model=UNCHANGED, start_thinking=UNCHANGED, merge=UNCHANGED,
                       icon_style=UNCHANGED, tag_style=UNCHANGED, card_style=UNCHANGED,
                       details_pane=UNCHANGED, details_pane_ratio=UNCHANGED) -> None:
    """Save only the given settings, with comments, permissions and other values intact.

    None removes `start_model` or `start_thinking`. Every value is checked before any write.
    """
    changes = {key: value for key, value in (
        ("theme", theme), ("start_agent", start_agent), ("start_model", start_model),
        ("start_thinking", start_thinking), ("merge", merge), ("icon_style", icon_style),
        ("tag_style", tag_style), ("card_style", card_style), ("details_pane", details_pane),
        ("details_pane_ratio", details_pane_ratio)) if value is not UNCHANGED}
    check_global_config(**changes)
    if not changes:
        return
    path = config_path(path)
    if all(value is None for value in changes.values()) and not path.exists():
        return

    def change(document) -> bool:
        for key, value in changes.items():
            if value is not None:
                document[key] = value
            elif key in document:
                del document[key]
        return True

    _rewrite_config(path, change)


def _rewrite_config(path: Path, change) -> None:
    """Read the config file, let `change` edit the TOML document, and replace the file in one step.

    `change` returns False when the document stays as it is. Comments, permissions and a symlink
    survive. Core config reads need only the standard library; the board installs its TOML writer.
    """
    # Core config reads need only the standard library; the board installs its TOML writer.
    import tomlkit
    from tomlkit.exceptions import ParseError

    path = path.resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(f".{path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                text = path.read_bytes().decode("utf-8")
                mode = path.stat().st_mode & 0o777
            except FileNotFoundError:
                text, mode = "", 0o600
            document = tomlkit.parse(text)
            if not change(document):
                return
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                    temp = Path(handle.name)
                    os.fchmod(handle.fileno(), mode)
                    handle.write(tomlkit.dumps(document).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
    except ParseError as error:
        raise HerdboardError(f"{path}: invalid TOML: {error}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot save settings: {error}") from error


def _string(path: Path, data: dict, key: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or not value:
        raise HerdboardError(f"{path}: {key} must be a non-empty string")
    return value


def _string_list(path: Path, data: dict, key: str) -> list[str] | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise HerdboardError(f"{path}: {key} must be a list of non-empty strings")
    return list(value)


def _string_map(path: Path, data: dict, key: str, label: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in value.items()
    ):
        raise HerdboardError(f"{path}: {label} must be a table of non-empty strings")
    return dict(value)


def _load_linear(path: Path, data: dict) -> LinearConfig:
    table = data.get("linear", {})
    if not isinstance(table, dict):
        raise HerdboardError(f"{path}: linear must be a table")
    if "api_key" in table:
        raise HerdboardError(
            f"{path}: linear.api_key is not read from the config file; "
            "set linear.api_key_command, or the environment variable named by linear.api_key_env"
        )
    linear = LinearConfig()
    if "enabled" in table:
        if not isinstance(table["enabled"], bool):
            raise HerdboardError(f"{path}: linear.enabled must be true or false")
        linear.enabled = table["enabled"]
    api_key_env = _string(path, table, "api_key_env")
    if api_key_env is not None:
        linear.api_key_env = api_key_env
    if "api_key_command" in table:
        command = table["api_key_command"]
        if isinstance(command, str):
            try:
                command = shlex.split(command)
            except ValueError as error:
                raise HerdboardError(f"{path}: invalid linear.api_key_command: {error}") from error
        if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
            raise HerdboardError(f"{path}: linear.api_key_command must be a string or list of non-empty strings")
        if not command:
            raise HerdboardError(f"{path}: linear.api_key_command must not be empty")
        linear.api_key_command = [os.path.expanduser(command[0]), *command[1:]]
    columns = _string_map(path, table, "columns", "linear.columns")
    unknown = sorted(set(columns) - set(DEFAULT_LINEAR_COLUMNS))
    if unknown:
        raise HerdboardError(f"{path}: unknown state type in linear.columns: {', '.join(unknown)}")
    linear.columns.update(columns)
    linear.state_names.update(_string_map(path, table, "state_names", "linear.state_names"))
    done_days = table.get("done_days", 0)
    if isinstance(done_days, bool) or not isinstance(done_days, int) or done_days < 0:
        raise HerdboardError(f"{path}: linear.done_days must be an integer of 0 or more")
    # 0 means the default.
    linear.done_days = done_days or DEFAULT_DONE_DAYS
    return linear


def _load_thinking(path: Path, data: dict) -> dict[str, str]:
    """The `[thinking]` table: the thinking level that the user last chose for each model."""
    table = data.get(THINKING_TABLE, {})
    if not isinstance(table, dict):
        raise HerdboardError(f"{path}: thinking must be a table of model names and thinking levels")
    for model, level in table.items():
        if not isinstance(model, str) or not model:
            raise HerdboardError(f"{path}: thinking must be a table of model names and thinking levels")
        if level not in THINKING_LEVELS:
            raise HerdboardError(f"{path}: the thinking level of {model} must be one of "
                                 f"{', '.join(THINKING_LEVELS)}")
    return dict(table)


def load_config(path: Path | None = None) -> Config:
    path = config_path(path)
    config = Config()
    if not path.is_file():
        return config
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise HerdboardError(f"{path}: invalid TOML: {error}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read config: {error}") from error

    todos_dir = _string(path, data, "todos_dir")
    if todos_dir is not None:
        try:
            # Absolute, because agents that read the attachments run in other folders.
            config.todos_dir = Path(todos_dir).expanduser().absolute()
        except RuntimeError as error:
            raise HerdboardError(f"{path}: invalid todos_dir: {error}") from error
    projects_dir = _string(path, data, "projects_dir")
    if projects_dir is not None:
        try:
            config.projects_dir = Path(projects_dir).expanduser().absolute()
        except RuntimeError as error:
            raise HerdboardError(f"{path}: invalid projects_dir: {error}") from error
    columns = _string_list(path, data, "columns")
    if columns is not None:
        if not columns:
            raise HerdboardError(f"{path}: columns must not be empty")
        config.columns = columns
    markers = _string_list(path, data, "complete_markers")
    if markers is not None:
        config.complete_markers = markers
    archive_titles = _string_list(path, data, "archive_titles")
    if archive_titles is not None:
        config.archive_titles = ["Archive"] + [t for t in archive_titles if t != "Archive"]
    vault_root = _string(path, data, "vault_root")
    if vault_root is not None:
        config.vault_root = Path(vault_root).expanduser()
    config.linear = _load_linear(path, data)
    start_agent = _string(path, data, "start_agent")
    if start_agent is not None:
        if start_agent not in AGENTS:
            raise HerdboardError(f"{path}: start_agent must be claude, pi or codex")
        config.start_agent = start_agent
    start_model = _string(path, data, "start_model")
    if start_model is not None:
        if any(ch.isspace() for ch in start_model):
            raise HerdboardError(f"{path}: start_model must be one model name, such as zai/glm-5.3")
        config.start_model = start_model
    start_thinking = _string(path, data, "start_thinking")
    if start_thinking is not None:
        if start_thinking not in THINKING_LEVELS:
            raise HerdboardError(f"{path}: start_thinking must be one of {', '.join(THINKING_LEVELS)}")
        config.start_thinking = start_thinking
    mockup_model = _string(path, data, "mockup_model")
    if mockup_model is not None:
        if any(ch.isspace() for ch in mockup_model):
            raise HerdboardError(f"{path}: mockup_model must be one model name, such as gpt-6.1-sol")
        config.mockup_model = mockup_model
    config.pi_models = _string_list(path, data, "pi_models")
    config.thinking = _load_thinking(path, data)
    config.codex_models = _string_list(path, data, "codex_models")
    config.start_column = _string(path, data, "start_column") or config.linear.columns["started"]
    config.theme = _string(path, data, "theme") or config.theme
    merge = _string(path, data, "merge")
    if merge is not None:
        if merge not in MERGE_MODES:
            raise HerdboardError(f"{path}: merge must be local or pr")
        config.merge = merge
    tag_style = _string(path, data, "tag_style")
    if tag_style is not None:
        if tag_style not in TAG_STYLES:
            raise HerdboardError(f"{path}: tag_style must be outline, round or block")
        config.tag_style = tag_style
    card_style = _string(path, data, "card_style")
    if card_style is not None:
        if card_style not in CARD_STYLES:
            raise HerdboardError(f"{path}: card_style must be full or compact")
        config.card_style = card_style
    icon_style = _string(path, data, "icon_style")
    if icon_style is not None:
        if icon_style not in ("plain", "nerd"):
            raise HerdboardError(f"{path}: icon_style must be plain or nerd")
        config.icon_style = icon_style
    types = _string_list(path, data, "types")
    if types is not None:
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", kind) for kind in types):
            raise HerdboardError(f"{path}: types must be single words, such as bug or feature")
        config.types = types
    if "details_pane" in data:
        if not isinstance(data["details_pane"], bool):
            raise HerdboardError(f"{path}: details_pane must be true or false")
        config.details_pane = data["details_pane"]
    if "details_pane_ratio" in data:
        _check_details_ratio(data["details_pane_ratio"], f"{path}: details_pane_ratio")
        config.details_pane_ratio = float(data["details_pane_ratio"])
    return config
