"""Repository identity: map a working directory to one stable board."""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .model import HerdboardError

SCP_FORM = re.compile(r"^(?:[^@/:]+(?::[^@/]*)?@)?(?P<host>[^:/@]+):(?P<path>[^/].*)$")


class IdentityError(HerdboardError):
    code = "identity"


@dataclass(frozen=True)
class RepoIdentity:
    identity: str
    host: str | None
    segments: tuple[str, ...]

    @property
    def board_stem(self) -> str:
        return "-".join(self.segments)


def _segments(path: str, url: str) -> tuple[str, ...]:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = tuple(part for part in path.split("/") if part)
    if not segments:
        raise IdentityError(f"Remote URL has no repository path: {url!r}")
    return segments


def _canonical_host_path(host: str, segments: tuple[str, ...]) -> tuple[str, ...]:
    """GitHub names are case-insensitive; other hosts keep their case."""
    if host == "github.com":
        return tuple(part.lower() for part in segments)
    return segments


def _canonical_identity(identity: str) -> str:
    host, _, rest = identity.partition("/")
    if host.lower() == "github.com":
        return f"{host.lower()}/{rest.lower()}"
    return identity


def same_repo(repo: str | None, identity: RepoIdentity) -> bool:
    """True when a board's `repo:` value names the repository of `identity`."""
    return repo is not None and _canonical_identity(repo) == _canonical_identity(identity.identity)


def normalize_remote(url: str) -> tuple[str, tuple[str, ...]]:
    host, segments = _split_remote(url)
    return host, _canonical_host_path(host, segments)


def _split_remote(url: str) -> tuple[str, tuple[str, ...]]:
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme == "file" or not parts.hostname:
            raise IdentityError(f"Remote URL is a local path: {url!r}")
        try:
            parts.port  # noqa: B018  reading the port raises ValueError for a bad one
        except ValueError as error:
            raise IdentityError(f"Cannot parse remote URL: {url!r}") from error
        return parts.hostname.lower(), _segments(parts.path, url)
    match = SCP_FORM.match(text)
    if not match:
        raise IdentityError(f"Cannot parse remote URL or remote is a local path: {url!r}")
    return match.group("host").lower(), _segments(match.group("path"), url)


def _git(directory: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True)
    except FileNotFoundError as error:
        raise IdentityError("git is not installed") from error
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_identity(directory: Path) -> RepoIdentity | None:
    common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    origin = _git(directory, "remote", "get-url", "origin")
    if origin:
        try:
            host, segments = normalize_remote(origin)
        except IdentityError:
            # A local-path or unparseable origin names no hosted repository;
            # the main checkout identifies the repository instead.
            pass
        else:
            return RepoIdentity("/".join((host, *segments)), host, segments)
    common_dir = Path(common)
    main = common_dir.parent if common_dir.name == ".git" else common_dir
    name = main.name.removesuffix(".git") or main.name
    return RepoIdentity(str(main), None, (name,))


def _board_repo(board: Path) -> str | None:
    """The `repo` value of a board folder's `board.toml`, or None when it has none or cannot be read."""
    try:
        with open(board / "board.toml", "rb") as handle:
            repo = tomllib.load(handle).get("repo")
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    return repo if isinstance(repo, str) else None


def find_board(boards_dir: Path, identity: RepoIdentity) -> Path | None:
    """The board folder whose `repo` names the repository of `identity`, or None."""
    if not boards_dir.is_dir():
        return None
    matches = []
    for board in sorted(path.parent for path in boards_dir.glob("*/board.toml")):
        repo = _board_repo(board)
        if repo is not None and _canonical_identity(repo) == _canonical_identity(identity.identity):
            matches.append(board)
    if len(matches) > 1:
        names = " and ".join(str(path) for path in matches)
        raise IdentityError(f"Two boards claim {identity.identity}: {names}")
    return matches[0] if matches else None


def new_board_path(boards_dir: Path, identity: RepoIdentity) -> Path:
    """A free board folder for the repository of `identity`. The inbox name is never free."""
    plain = boards_dir / identity.board_stem
    if not plain.exists() and identity.board_stem != "inbox":
        return plain
    if identity.host is None:
        raise IdentityError(f"Board name is taken by another repository: {plain}")
    prefixed = boards_dir / f"{identity.host}-{identity.board_stem}"
    if prefixed.exists():
        raise IdentityError(f"Board name is taken by another repository: {prefixed}")
    return prefixed
