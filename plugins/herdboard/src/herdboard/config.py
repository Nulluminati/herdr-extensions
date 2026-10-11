"""Load herdboard settings from a TOML file."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

from .model import HerdboardError, IoError

DEFAULT_COLUMNS = ["Backlog", "Todo", "In progress", "Review", "Done"]


@dataclass
class Config:
    todos_dir: Path = field(default_factory=lambda: Path("~/todos").expanduser())
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    complete_markers: list[str] = field(default_factory=lambda: ["Complete"])
    vault_root: Path | None = None
    archive_titles: list[str] = field(default_factory=lambda: ["Archive"])


def config_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    explicit = os.environ.get("HERDBOARD_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
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
        config.todos_dir = Path(todos_dir).expanduser()
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
    return config
