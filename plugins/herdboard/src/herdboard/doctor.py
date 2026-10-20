"""Read-only store diagnostics, with an explicit locked duplicate-ID repair."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess

from .config import Config, config_path, load_config
from .identity import IdentityError, _canonical_identity, repo_identity
from . import linear_links, linear_source
from .linear import LinearAuthError, LinearError, LinearNetworkError
from .linear_links import parse_links
from .linear_pins import parse_pins
from .model import BLOCK_ID, Board, Card, Column, ConflictError, FormatError, HerdboardError, parent_of
from .promotions import PromotionRecords
from .start import _entries, _read
from .store import Store

LEVELS = ("error", "warning", "info")


def finding(level, code, message, path=None, line=None, fix=None):
    return {"level": level, "code": code, "path": str(path) if path is not None else None,
            "line": line, "message": message, "fix": fix}


@dataclass
class CardLocation:
    path: Path
    column: Column
    card: Card
    line: int
    number: int


def locations(path, board):
    """Locate parsed cards by counting preserved source lines, not by parsing again."""
    line = len(board.frontmatter) + len(board.preamble) + 1
    number = 0
    columns = list(board.columns)
    for column in columns + ([board.archive] if board.archive is not None else []):
        if column.archive:
            line += len(board.archive_lines)
        line += 1  # the heading
        for item in column.items:
            if isinstance(item, Card):
                number += 1
                yield CardLocation(path, column, item, line, number)
                line += len(item.lines)
            else:
                line += 1


def read_boards(store):
    boards, findings = {}, []
    for path in sorted(store.board_paths()):
        try:
            boards[path] = store._read(path)
        except HerdboardError as error:
            findings.append(finding("error", "board_invalid", str(error), path))
    return boards, findings


def check_board(store, path, board):
    findings = []
    if len(board.frontmatter) < 2 or board.frontmatter_value("kanban-plugin") != "board":
        findings.append(finding("error", "board_invalid",
                                "Frontmatter must be closed and contain kanban-plugin: board. The board is read only.",
                                path, 1))
    try:
        parse_links(board.frontmatter)
    except FormatError as error:
        line = next((i for i, text in enumerate(board.frontmatter, 1)
                     if text.startswith("linear-projects:")), 1)
        if linear_links._yaml() is None:
            findings.append(finding("warning", "pyyaml_missing",
                                    "PyYAML is not installed; Linear links cannot be checked.", path, line))
        else:
            findings.append(finding("error", "linear_links_invalid", str(error), path, line))
    try:
        parse_pins(board.frontmatter)
    except FormatError as error:
        line = next((i for i, text in enumerate(board.frontmatter, 1)
                     if text.startswith("linear-issues:")), 1)
        if linear_links._yaml() is None:
            findings.append(finding("warning", "pyyaml_missing",
                                    "PyYAML is not installed; pinned Linear issues cannot be checked.", path, line))
        else:
            findings.append(finding("error", "linear_pins_invalid", str(error), path, line))
    try:
        store._done_column(board, path)
    except HerdboardError as error:
        findings.append(finding("warning", "done_column_missing", str(error), path))
    for place in locations(path, board):
        if place.card.block_id is None:
            findings.append(finding("info", "card_id_missing",
                                    "Card has no ID. The next write assigns IDs to column cards; archive cards keep their IDs.",
                                    path, place.line))
    return findings


def duplicates(boards):
    first = {}
    for path, board in sorted(boards.items()):
        for place in locations(path, board):
            block_id = place.card.block_id
            if block_id is None:
                continue
            if block_id in first:
                yield first[block_id], place
            else:
                first[block_id] = place


def check_duplicate_ids(boards):
    return [finding("error", "duplicate_id",
                    f"ID {place.card.block_id} duplicates {first.path}:{first.line}.",
                    place.path, place.line, "herdboard doctor --fix-ids")
            for first, place in duplicates(boards)]


def check_parents(boards):
    """Sub-cards whose parent is not on their board, or is a sub-card itself."""
    findings = []
    for path, board in sorted(boards.items()):
        parents = {card.block_id: parent_of(card.title) for _, card in board.all_cards() if card.block_id}
        for _, card in board.all_cards():
            parent = parent_of(card.title)
            if parent is None:
                continue
            ref = card.block_id or card.title
            if parent not in parents:
                findings.append(finding("warning", "parent_missing",
                                        f"{ref} links to parent {parent}, which is not on this board.", path))
            elif parents[parent]:
                findings.append(finding("warning", "parent_nested",
                                        f"{ref} has parent {parent}, which is a sub-card itself.", path))
    return findings


def check_duplicate_repos(boards):
    first, findings = {}, []
    for path, board in sorted(boards.items()):
        repo = board.frontmatter_value("repo")
        if repo is None:
            continue
        key = _canonical_identity(repo)
        if key in first:
            line = next((i for i, text in enumerate(board.frontmatter, 1)
                         if text.startswith("repo:")), None)
            findings.append(finding("error", "duplicate_repo",
                                    f"Repository {repo} is also claimed by {first[key]}.", path, line))
        else:
            first[key] = path
    return findings


def fix_ids(store, before):
    """Repair writable duplicates, preserving source lines and checking under the lock."""
    def writable_duplicates(boards):
        places = []
        for _, place in duplicates(boards):
            try:
                store._require_writable(place.path, boards[place.path])
            except FormatError:
                continue  # The checks below report why this board is read only.
            places.append(place)
        return places

    planned = writable_duplicates(before)
    if not planned:
        return [], []
    fixed, findings = [], []
    with store._lock():
        boards, _ = read_boards(store)
        current_duplicates = writable_duplicates(boards)
        def key(place):
            return place.path, place.number, place.card.block_id
        if {key(place) for place in current_duplicates} != {key(place) for place in planned}:
            raise ConflictError("Duplicate cards changed on disk; retry")
        # Verify each duplicate card's lines and column, while keeping other edits.
        for old in planned:
            board = boards.get(old.path)
            places = list(locations(old.path, board)) if board is not None else []
            current = next((p for p in places if p.number == old.number), None)
            if current is None or current.card.lines != old.card.lines or current.column.title != old.column.title:
                raise ConflictError(f"{old.path}: card changed on disk; retry")
        taken = {p.card.block_id for path, board in boards.items()
                 for p in locations(path, board) if p.card.block_id}
        changed = {}
        for place in current_duplicates:
            old_id = place.card.block_id
            new_id = store._new_id(taken)
            place.card.lines[0] = BLOCK_ID.sub(lambda match: f" ^{new_id}", place.card.first_line)
            changed.setdefault(place.path, []).append({"old_id": old_id, "new_id": new_id,
                                                       "path": str(place.path), "line": place.line})
        for path in sorted(changed):
            board = boards[path]
            try:
                # Normal first-write rules assign IDs to all unaddressed column cards.
                for _, card in board.all_cards():
                    if card.block_id is None:
                        card.set_block_id(store._new_id(taken))
                store._write(path, board)
            except HerdboardError as error:
                findings.append(finding("error", "board_fix_failed", str(error), path))
            else:
                fixed.extend(changed[path])
    return fixed, findings


def check_linear(config, boards, offline):
    settings = config.linear
    enabled = settings.key_configured if settings.enabled is None else settings.enabled
    if offline or not enabled:
        return []
    if not settings.key_configured:
        return [finding("error", "linear_auth", f"Linear is enabled but {settings.key_source} is not set.")]
    findings = []
    try:
        client = linear_source.build_client(settings.api_key)
        viewer = client.viewer()
        if not isinstance(viewer, dict) or not viewer.get("id"):
            raise LinearAuthError("Linear did not return a viewer. Check the API key.")
        for path, board in sorted(boards.items()):
            try:
                links = parse_links(board.frontmatter)
            except FormatError:
                continue
            line = next((i for i, text in enumerate(board.frontmatter, 1)
                         if text.startswith("linear-projects:")), None)
            for link in links:
                try:
                    project = client.project(link.id)
                except (LinearAuthError, LinearNetworkError):
                    raise
                except LinearError as error:
                    findings.append(finding("warning", "linear_check_failed", str(error), path, line))
                    return findings
                else:
                    if project is None:
                        findings.append(finding("warning", "linear_project_missing",
                                                f"Linear project {link.name!r} ({link.id}) is missing or inaccessible.", path, line))
                if not link.labels:
                    continue
                try:
                    found = {label["id"] for label in client.labels([l.id for l in link.labels])}
                except (LinearAuthError, LinearNetworkError):
                    raise
                except LinearError as error:
                    findings.append(finding("warning", "linear_check_failed", str(error), path, line))
                    return findings
                for label in link.labels:
                    if label.id not in found:
                        findings.append(finding("warning", "linear_label_missing",
                                                f"Linear label {label.name!r} ({label.id}) is missing or inaccessible.", path, line))
            try:
                pins = parse_pins(board.frontmatter)
            except FormatError:
                continue
            if not pins:
                continue
            line = next((i for i, text in enumerate(board.frontmatter, 1)
                         if text.startswith("linear-issues:")), None)
            try:
                # Every state: a done pin still exists, and only a missing one is a finding.
                found = {issue["id"] for issue in client.issues_by_ids([pin.id for pin in pins], done_days=None)}
            except (LinearAuthError, LinearNetworkError):
                raise
            except LinearError as error:
                findings.append(finding("warning", "linear_check_failed", str(error), path, line))
                return findings
            for pin in pins:
                if pin.id not in found:
                    findings.append(finding("warning", "linear_pin_missing",
                                            f"Pinned Linear issue {pin.identifier} ({pin.id}) is missing or inaccessible.",
                                            path, line))
    except LinearNetworkError as error:
        findings.append(finding("warning", "linear_unreachable", str(error)))
    except LinearAuthError as error:
        findings.append(finding("error", "linear_auth", str(error)))
    except LinearError as error:
        findings.append(finding("warning", "linear_check_failed", str(error)))
    return findings


def check_starts(path):
    findings = []
    try:
        records = _read(path)
        for ref, record in records.items():
            try:
                if not isinstance(record, dict):
                    findings.append(finding("warning", "state_invalid", f"invalid start record {ref}", path))
                    continue
                worktree = record.get("worktree_path")
                if worktree is not None and (not isinstance(worktree, str) or not worktree):
                    raise ValueError(f"invalid worktree path for {ref}")
                if "steps_done" in record and not isinstance(record["steps_done"], list):
                    findings.append(finding("warning", "state_invalid", f"invalid steps for {ref}", path))
                if worktree and not Path(worktree).exists():
                    findings.append(finding("info", "worktree_missing", f"Start {ref} points to missing worktree {worktree}.", path))
            except (HerdboardError, ValueError, OSError) as error:
                findings.append(finding("error", "state_invalid", str(error), path))
    except (HerdboardError, ValueError, OSError) as error:
        findings.append(finding("error", "state_invalid", str(error), path))
    return findings


def check_work_folders(config, starts_path):
    """Card folders of repoless cards, and the ones no start record names."""
    base = config.todos_dir / ".herdboard" / "work"
    try:
        folders = [path for path in base.iterdir() if path.is_dir()]
    except FileNotFoundError:
        return []
    except OSError as error:
        return [finding("error", "state_invalid", f"{base}: cannot read the card folders: {error}")]
    try:
        records = _read(starts_path)
    except HerdboardError:
        records = {}
    recorded = set()
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        recorded.add(key)
        if isinstance(record.get("ref"), str):
            recorded.add(record["ref"])
        path = record.get("worktree_path")
        if isinstance(path, str) and path:
            recorded.add(Path(path).name)
    findings = []
    for folder in sorted(folders):
        if folder.name in recorded:
            continue
        findings.append(finding("warning", "work_folder_unused",
                                f"Card folder {folder} has no start record.", folder,
                                fix=f"Delete the folder: rm -r {folder}"))
    return findings


def check_checkouts(path):
    findings = []
    try:
        records = _read(path)
        git_missing = False
        for repo, entries in records.items():
            valid = _entries(entries)
            if not isinstance(entries, list) or len(valid) != len(entries):
                findings.append(finding("error", "state_invalid", f"invalid checkout entries for {repo}", path))
            for entry in valid:
                try:
                    checkout = Path(entry["path"])
                    exists = checkout.exists()
                    if exists and git_missing:
                        continue
                    try:
                        identity = repo_identity(checkout) if exists else None
                    except IdentityError as error:
                        if str(error) != "git is not installed":
                            raise
                        git_missing = True
                        findings.append(finding("info", "git_missing",
                                                "git is not installed; checkout identities cannot be checked.", path))
                        continue
                    if identity is None or _canonical_identity(identity.identity) != _canonical_identity(repo):
                        findings.append(finding("info", "checkout_stale",
                                                f"Checkout {checkout} is missing or no longer repository {repo}.", path))
                except (HerdboardError, ValueError, OSError) as error:
                    findings.append(finding("error", "state_invalid", str(error), path))
    except (HerdboardError, ValueError, OSError) as error:
        findings.append(finding("error", "state_invalid", str(error), path))
    return findings


def check_promotions(path, store):
    findings = []
    try:
        records = PromotionRecords(path.parent, store)._read()
        for key, record in records.items():
            try:
                owner, separator, ref = key.rpartition("::")
                if not isinstance(record, dict) or not separator or not ref or record.get("store") != owner:
                    raise ValueError(f"invalid promotion record {key}")
                issues = record.get("issues")
                if not isinstance(issues, list) or any(not isinstance(issue, dict) or
                        not isinstance(issue.get("id"), str) or not isinstance(issue.get("input"), dict) or
                        not isinstance(issue.get("identifier"), (str, type(None))) for issue in issues):
                    raise ValueError(f"invalid promotion issues for {ref}")
                # Even confirmed issues need the final local todo update before removal.
                if owner == store:
                    findings.append(finding("warning", "promotion_unfinished", f"Promotion {ref} is unfinished.",
                                            path, fix=f"herdboard promote {ref}"))
            except (HerdboardError, ValueError, OSError) as error:
                findings.append(finding("error", "state_invalid", str(error), path))
    except (HerdboardError, ValueError, OSError) as error:
        findings.append(finding("error", "state_invalid", str(error), path))
    return findings


def check_herdr():
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    if shutil.which(binary) is None:
        return [finding("info", "herdr_missing", f"No Herdr binary found at {binary!r}. Only start needs it.")]
    return []


def check_gh():
    if shutil.which("gh") is None:
        return [finding("info", "gh_missing",
                        "No GitHub CLI `gh` found. Only merge --pr, finish of a pull request and pull request reviews need it.")]
    return []


def check_hunk():
    """Pull request reviews show the PR in Hunk, and the user submits with its `hunk-gh-review` extension."""
    if shutil.which("hunk") is None:
        return [finding("warning", "hunk_missing",
                        "No `hunk` found. Only pull request reviews need it; the board works without it.")]
    try:
        result = subprocess.run(["hunk", "extension", "list"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [finding("warning", "hunk_check_failed", f"`hunk extension list` failed: {error}")]
    if result.returncode or not any(line.split()[:1] == ["hunk-gh-review"] for line in result.stdout.splitlines()):
        return [finding("warning", "hunk_gh_review_missing",
                        "The Hunk extension `hunk-gh-review` is not installed, so S in Hunk cannot submit "
                        "a pull request review.", fix="hunk extension install phl28/hunk-gh-review")]
    return []


def diagnose(path=None, *, repair_ids=False, offline=False):
    findings, fixed = [], []
    config = None
    try:
        config = load_config(path)
    except HerdboardError as error:
        findings.append(finding("error", "config_invalid",
                                f"{error}. Using the default config for board and Linear checks.", config_path(path)))
        config = Config()
    if config is not None:
        store = Store(config)
        boards, read_errors = read_boards(store)
        if repair_ids:
            fixed, repair_errors = fix_ids(store, boards)
            findings.extend(repair_errors)
            boards, read_errors = read_boards(store)
        findings.extend(read_errors)
        for board_path, board in sorted(boards.items()):
            try:
                findings.extend(check_board(store, board_path, board))
            except Exception as error:
                findings.append(finding("error", "board_invalid", str(error), board_path))
                boards[board_path] = None
        boards = {path: board for path, board in boards.items() if board is not None}
        findings.extend(check_duplicate_ids(boards))
        findings.extend(check_parents(boards))
        findings.extend(check_duplicate_repos(boards))
        findings.extend(check_linear(config, boards, offline))
    state = linear_source.state_dir()
    findings.extend(check_starts(state / "starts.json"))
    if config is not None:
        findings.extend(check_work_folders(config, state / "starts.json"))
    findings.extend(check_checkouts(state / "checkouts.json"))
    store_path = str(config.todos_dir.resolve()) if config is not None else None
    for promotion_path in sorted(state.glob("*/*/promotions.json")):
        findings.extend(check_promotions(promotion_path, store_path))
    findings.extend(check_herdr())
    findings.extend(check_gh())
    findings.extend(check_hunk())
    summary = {level: sum(f["level"] == level for f in findings) for level in LEVELS}
    return {"findings": findings, "fixed": fixed, "summary": summary}


def render(result):
    lines = []
    for level in LEVELS:
        found = [f for f in result["findings"] if f["level"] == level]
        if not found:
            continue
        lines.append(level.capitalize())
        for item in found:
            location = item["path"] or ""
            if item["line"] is not None:
                location += f":{item['line']}"
            lines.append(f"  {item['code']}: {location + ': ' if location else ''}{item['message']}")
            if item["fix"]:
                lines.append(f"    Fix: {item['fix']}")
    for item in result["fixed"]:
        lines.append(f"Fixed {item['path']}:{item['line']}: {item['old_id']} -> {item['new_id']}")
    summary = result["summary"]
    lines.append(f"Summary: {summary['error']} errors, {summary['warning']} warnings, {summary['info']} info")
    return "\n".join(lines)
