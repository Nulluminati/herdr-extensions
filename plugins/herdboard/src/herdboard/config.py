"""Load herdboard settings from a TOML file."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

from .model import HerdboardError, IoError

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
# pi's thinking levels (`pi --thinking`).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
MERGE_MODES = ("local", "pr")


@dataclass
class LinearConfig:
    """`[linear]` settings. The API key comes from the environment, never from the file."""

    enabled: bool | None = None  # None: on when the API key variable is set
    api_key_env: str = "LINEAR_API_KEY"
    columns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LINEAR_COLUMNS))
    state_names: dict[str, str] = field(default_factory=lambda: {"In Review": "Review"})
    # Days a completed issue stays visible. The client also accepts None (show every
    # completed issue), but TOML has no null, so a config file cannot set it.
    done_days: int = DEFAULT_DONE_DAYS

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None

    @property
    def active(self) -> bool:
        """True when Linear is on and a key is available."""
        wanted = self.api_key is not None if self.enabled is None else self.enabled
        return wanted and self.api_key is not None


@dataclass
class Config:
    todos_dir: Path = field(default_factory=lambda: Path("~/todos").expanduser())
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    complete_markers: list[str] = field(default_factory=lambda: ["Complete"])
    vault_root: Path | None = None
    archive_titles: list[str] = field(default_factory=lambda: ["Archive"])
    linear: LinearConfig = field(default_factory=LinearConfig)
    start_agent: str = "claude"
    # The model and thinking level `start` gives an agent; None keeps the agent's own default.
    start_model: str | None = None
    start_thinking: str | None = None
    # Models the board offers for pi; None reads pi's own `enabledModels`.
    pi_models: list[str] | None = None
    start_column: str | None = None
    # How `merge` merges a card branch when neither the command nor the board says: "local" or "pr".
    merge: str = "local"
    # A Textual theme name. The ANSI themes use the terminal's own colors.
    theme: str = "ansi-dark"
    # "round" draws tag ends with Nerd Font half circles; "block" needs no special font.
    tag_style: str = "round"

    def __post_init__(self):
        if self.start_column is None:
            self.start_column = self.linear.columns["started"]


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
            "set the environment variable named by linear.api_key_env"
        )
    linear = LinearConfig()
    if "enabled" in table:
        if not isinstance(table["enabled"], bool):
            raise HerdboardError(f"{path}: linear.enabled must be true or false")
        linear.enabled = table["enabled"]
    api_key_env = _string(path, table, "api_key_env")
    if api_key_env is not None:
        linear.api_key_env = api_key_env
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
        if start_agent not in ("claude", "pi"):
            raise HerdboardError(f"{path}: start_agent must be claude or pi")
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
    config.pi_models = _string_list(path, data, "pi_models")
    config.start_column = _string(path, data, "start_column") or config.linear.columns["started"]
    config.theme = _string(path, data, "theme") or config.theme
    merge = _string(path, data, "merge")
    if merge is not None:
        if merge not in MERGE_MODES:
            raise HerdboardError(f"{path}: merge must be local or pr")
        config.merge = merge
    tag_style = _string(path, data, "tag_style")
    if tag_style is not None:
        if tag_style not in ("round", "block"):
            raise HerdboardError(f"{path}: tag_style must be round or block")
        config.tag_style = tag_style
    return config
