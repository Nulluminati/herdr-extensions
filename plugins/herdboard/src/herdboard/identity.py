"""Repository identity: map a working directory to one stable board."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .model import HerdboardError, frontmatter_scalars

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
        result = subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True
        )
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


def _frontmatter(path: Path) -> list[str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index]
    return None


def find_board(projects_dir: Path, identity: RepoIdentity) -> Path | None:
    if not projects_dir.is_dir():
        return None
    matches = []
    for path in sorted(projects_dir.glob("*.md")):
        lines = _frontmatter(path)
        if lines is None:
            continue
        repo = frontmatter_scalars(lines).get("repo")
        if repo is not None and _canonical_identity(repo) == _canonical_identity(identity.identity):
            matches.append(path)
    if len(matches) > 1:
        names = " and ".join(str(path) for path in matches)
        raise IdentityError(f"Two boards claim {identity.identity}: {names}")
    return matches[0] if matches else None


def new_board_path(projects_dir: Path, identity: RepoIdentity) -> Path:
    plain = projects_dir / f"{identity.board_stem}.md"
    if not plain.exists():
        return plain
    if identity.host is None:
        raise IdentityError(f"Board name is taken by another repository: {plain}")
    prefixed = projects_dir / f"{identity.host}-{identity.board_stem}.md"
    if prefixed.exists():
        raise IdentityError(f"Board name is taken by another repository: {prefixed}")
    return prefixed
