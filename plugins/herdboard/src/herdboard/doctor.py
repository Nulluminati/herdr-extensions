"""Read-only store diagnostics, with an explicit locked repair of the boards' card lists."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import card_file, linear_source
from .card_file import BoardFile, CardFile
from .config import MERGE_MODES, OBSOLETE_KEYS, Config, config_path, load_config
from .identity import IdentityError, _canonical_identity, repo_identity
from .linear import LinearAuthError, LinearError, LinearNetworkError
from .linear_links import parse_links
from .linear_pins import parse_pins
from .model import FormatError, HerdboardError, OldFormatError
from .promotions import PromotionRecords
from .start import _entries, _read
from .store import BOARD_FILE, CARD_FILE, Store, is_card_id

LEVELS = ("error", "warning", "info")


def finding(level, code, message, path=None, line=None, fix=None):
    return {
        "level": level,
        "code": code,
        "path": str(path) if path is not None else None,
        "line": line,
        "message": message,
        "fix": fix,
    }


@dataclass
class BoardRead:
    """A board folder as doctor reads it: the parsed `board.toml` and the card folders found."""

    path: Path
    file: BoardFile
    # The IDs of the card folders under `cards/` and `archive/` that hold a readable `card.md`.
    cards: dict[str, CardFile]
    archived: dict[str, CardFile]


def _card_folders(path: Path, folder: str, findings: list) -> dict[str, CardFile]:
    found: dict[str, CardFile] = {}
    try:
        entries = sorted((path / folder).iterdir())
    except FileNotFoundError:
        return found
    except OSError as error:
        findings.append(finding("error", "board_invalid", f"cannot read: {error}", path / folder))
        return found
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not is_card_id(entry.name):
            findings.append(
                finding(
                    "warning",
                    "card_folder_invalid",
                    f"{entry.name!r} is not a card ID (t and 3 letters or digits), so herdboard ignores the folder.",
                    entry,
                )
            )
            continue
        card_path = entry / CARD_FILE
        try:
            text = card_path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            findings.append(finding("warning", "card_file_missing", "Card folder has no card.md.", entry))
            continue
        except (OSError, UnicodeDecodeError) as error:
            findings.append(finding("error", "card_invalid", f"cannot read: {error}", card_path))
            continue
        try:
            found[entry.name] = card_file.parse_card(text, card_path)
        except FormatError as error:
            findings.append(finding("error", "card_invalid", str(error), card_path))
    return found


def read_boards(store):
    boards, findings = {}, []
    try:
        store.check_layout()
    except OldFormatError as error:
        return boards, [
            finding("error", "old_format", str(error), store.config.todos_dir, fix="herdboard convert-store")
        ]
    for path in store.board_paths():
        board_path = path / BOARD_FILE
        try:
            file = card_file.parse_board(board_path.read_bytes().decode("utf-8"), board_path)
        except (OSError, UnicodeDecodeError) as error:
            findings.append(finding("error", "board_invalid", f"cannot read: {error}", board_path))
            continue
        except FormatError as error:
            findings.append(finding("error", "board_invalid", str(error), board_path))
            continue
        cards = _card_folders(path, "cards", findings)
        archived = _card_folders(path, "archive", findings)
        boards[path] = BoardRead(path, file, cards, archived)
    return boards, findings


def check_board(store, board):
    findings = []
    path, file = board.path, board.file
    board_path = path / BOARD_FILE
    if file.merge not in (None, *MERGE_MODES):
        findings.append(finding("error", "board_invalid", "merge must be local or pr.", board_path))
    try:
        parse_links(file.tables("linear-projects"))
    except FormatError as error:
        findings.append(finding("error", "linear_links_invalid", str(error), board_path))
    try:
        parse_pins(file.tables("linear-issues"))
    except FormatError as error:
        findings.append(finding("error", "linear_pins_invalid", str(error), board_path))
    done = [column.name for column in file.columns if column.done]
    if len(done) > 1:
        findings.append(
            finding(
                "warning",
                "done_column_multiple",
                f"Columns {', '.join(done)} are all marked done; herdboard uses the first.",
                board_path,
            )
        )
    if not file.columns:
        # No column can show the cards, and --fix has no column to add them to.
        cards = ", ".join(board.cards) or "none"
        message = f"Board {path.name} has no columns, so it shows no cards (cards: {cards}). Add a [[column]] table."
        findings.append(finding("error", "board_no_columns", message, board_path))
        return findings
    try:
        store._done_column(file, path)
    except HerdboardError as error:
        findings.append(finding("warning", "done_column_missing", str(error), board_path))
    fix = "herdboard doctor --fix"
    listed: set[str] = set()
    for card_id in file.card_ids():
        if card_id in listed:
            findings.append(finding("warning", "card_listed_twice", f"{card_id} is listed twice.", board_path, fix=fix))
        elif card_id not in board.cards:
            findings.append(
                finding("warning", "card_missing", f"{card_id} is listed, but has no card folder.", board_path, fix=fix)
            )
        listed.add(card_id)
    for card_id in board.cards:
        if card_id not in listed:
            findings.append(
                finding(
                    "warning",
                    "card_orphan",
                    f"{card_id} is in no column; the board shows it at the bottom of the first column.",
                    path / "cards" / card_id,
                    fix=fix,
                )
            )
    return findings


def repaired_columns(board) -> list[list[str]]:
    """Each column's card list without duplicates and missing cards, orphans added to the first."""
    seen: set[str] = set()
    columns = []
    for column in board.file.columns:
        kept = []
        for card_id in column.cards:
            if card_id in board.cards and card_id not in seen:
                kept.append(card_id)
                seen.add(card_id)
        columns.append(kept)
    orphans = [card_id for card_id in board.cards if card_id not in seen]
    if columns:
        columns[0].extend(orphans)
    return columns


def fix_lists(store, before):
    """Repair the card lists of `board.toml` under the lock. Returns what changed and any errors."""
    planned = [
        path for path, board in before.items() if repaired_columns(board) != [c.cards for c in board.file.columns]
    ]
    if not planned:
        return [], []
    fixed, findings = [], []
    with store._lock():
        boards, _ = read_boards(store)
        for path in planned:
            board = boards.get(path)
            if board is None:
                continue
            old = [list(column.cards) for column in board.file.columns]
            for column, cards in zip(board.file.columns, repaired_columns(board), strict=True):
                column.cards = cards
            if old == [column.cards for column in board.file.columns]:
                continue
            try:
                store._write_file(path / BOARD_FILE, card_file.dump_board(board.file).encode("utf-8"))
            except HerdboardError as error:
                findings.append(finding("error", "board_fix_failed", str(error), path))
            else:
                fixed.append({"path": str(path / BOARD_FILE), "message": "Repaired the card lists."})
    return fixed, findings


def check_duplicate_ids(boards):
    places: dict[str, list[Path]] = {}
    for path, board in sorted(boards.items()):
        for card_id in board.cards:
            places.setdefault(card_id, []).append(path / "cards" / card_id)
        for card_id in board.archived:
            places.setdefault(card_id, []).append(path / "archive" / card_id)
    return [
        finding(
            "error",
            "duplicate_id",
            f"ID {card_id} names {len(found)} card folders: {', '.join(str(p) for p in found)}.",
            found[1],
            fix="Rename one folder to an ID that no other card has, and its ID in board.toml",
        )
        for card_id, found in sorted(places.items())
        if len(found) > 1
    ]


def check_parents(boards):
    """Sub-cards whose parent is not on their board, or is a sub-card itself."""
    findings = []
    for path, board in sorted(boards.items()):
        parents = {card_id: card.parent for card_id, card in board.cards.items()}
        for card_id, card in board.cards.items():
            parent = card.parent
            if parent is None:
                continue
            if parent not in parents:
                findings.append(
                    finding(
                        "warning",
                        "parent_missing",
                        f"{card_id} names parent {parent}, which is not on this board.",
                        path / "cards" / card_id / CARD_FILE,
                    )
                )
            elif parents[parent]:
                findings.append(
                    finding(
                        "warning",
                        "parent_nested",
                        f"{card_id} has parent {parent}, which is a sub-card itself.",
                        path / "cards" / card_id / CARD_FILE,
                    )
                )
    return findings


def check_duplicate_repos(boards):
    first, findings = {}, []
    for path, board in sorted(boards.items()):
        repo = board.file.repo
        if repo is None:
            continue
        key = _canonical_identity(repo)
        if key in first:
            findings.append(
                finding(
                    "error", "duplicate_repo", f"Repository {repo} is also claimed by {first[key]}.", path / BOARD_FILE
                )
            )
        else:
            first[key] = path
    return findings


def check_config_keys(path):
    """Settings that herdboard no longer reads."""
    try:
        with open(config_path(path), "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [
        finding(
            "warning",
            "config_key_obsolete",
            f"{key} is no longer used; herdboard ignores it.",
            config_path(path),
            fix=f"Remove {key} from the config file",
        )
        for key in OBSOLETE_KEYS
        if key in data
    ]


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
                links = parse_links(board.file.tables("linear-projects"))
            except FormatError:
                continue
            line = None
            path = path / BOARD_FILE
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
                        findings.append(
                            finding(
                                "warning",
                                "linear_project_missing",
                                f"Linear project {link.name!r} ({link.id}) is missing or inaccessible.",
                                path,
                                line,
                            )
                        )
                if not link.labels:
                    continue
                try:
                    found = {label["id"] for label in client.labels([link_label.id for link_label in link.labels])}
                except (LinearAuthError, LinearNetworkError):
                    raise
                except LinearError as error:
                    findings.append(finding("warning", "linear_check_failed", str(error), path, line))
                    return findings
                for label in link.labels:
                    if label.id not in found:
                        findings.append(
                            finding(
                                "warning",
                                "linear_label_missing",
                                f"Linear label {label.name!r} ({label.id}) is missing or inaccessible.",
                                path,
                                line,
                            )
                        )
            try:
                pins = parse_pins(board.file.tables("linear-issues"))
            except FormatError:
                continue
            if not pins:
                continue
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
                    findings.append(
                        finding(
                            "warning",
                            "linear_pin_missing",
                            f"Pinned Linear issue {pin.identifier} ({pin.id}) is missing or inaccessible.",
                            path,
                            line,
                        )
                    )
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
                    findings.append(
                        finding("info", "worktree_missing", f"Start {ref} points to missing worktree {worktree}.", path)
                    )
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
        findings.append(
            finding(
                "warning",
                "work_folder_unused",
                f"Card folder {folder} has no start record.",
                folder,
                fix=f"Delete the folder: rm -r {folder}",
            )
        )
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
                        findings.append(
                            finding(
                                "info",
                                "git_missing",
                                "git is not installed; checkout identities cannot be checked.",
                                path,
                            )
                        )
                        continue
                    if identity is None or _canonical_identity(identity.identity) != _canonical_identity(repo):
                        findings.append(
                            finding(
                                "info",
                                "checkout_stale",
                                f"Checkout {checkout} is missing or no longer repository {repo}.",
                                path,
                            )
                        )
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
                if not isinstance(issues, list) or any(
                    not isinstance(issue, dict)
                    or not isinstance(issue.get("id"), str)
                    or not isinstance(issue.get("input"), dict)
                    or not isinstance(issue.get("identifier"), (str, type(None)))
                    for issue in issues
                ):
                    raise ValueError(f"invalid promotion issues for {ref}")
                # Even confirmed issues need the final local todo update before removal.
                if owner == store:
                    findings.append(
                        finding(
                            "warning",
                            "promotion_unfinished",
                            f"Promotion {ref} is unfinished.",
                            path,
                            fix=f"herdboard promote {ref}",
                        )
                    )
            except (HerdboardError, ValueError, OSError) as error:
                findings.append(finding("error", "state_invalid", str(error), path))
    except (HerdboardError, ValueError, OSError) as error:
        findings.append(finding("error", "state_invalid", str(error), path))
    return findings


def check_automations(store):
    """The automations file parses, each schedule parses, each board exists, the IDs are unique, each factory
    automation has a plan with a herdboard-verification block, and the timer that runs them is active."""
    from . import automations

    path = automations.definitions_path(store.config)
    try:
        items = automations.load(store.config)
    except HerdboardError as error:
        return [finding("error", "automations_invalid", str(error), path)]
    findings = []
    seen = set()
    for item in items:
        if item.id in seen or not automations.AUTOMATION_ID.match(item.id):
            findings.append(
                finding(
                    "error",
                    "automation_duplicate_id",
                    f"Automation ID {item.id!r} is used twice or is not `a` and 3 letters or digits.",
                    path,
                    fix="Give each automation its own ID, such as a7k2.",
                )
            )
        seen.add(item.id)
        try:
            automations.parse_schedule(item.schedule)
        except HerdboardError as error:
            findings.append(finding("error", "automation_schedule_invalid", f"{item.id}: {error}", path))
        if item.board != "inbox":
            try:
                board = store.resolve_board(item.board)
            except HerdboardError:
                findings.append(
                    finding(
                        "error",
                        "automation_board_missing",
                        f"{item.id}: no board named {item.board!r}; its runs fail.",
                        path,
                        fix=f"herdboard automation edit {item.id} --board BOARD",
                    )
                )
            else:
                if not store.board_meta(board).repo:
                    findings.append(
                        finding(
                            "error",
                            "automation_board_no_repo",
                            f"{item.id}: board {item.board!r} has no repository; its runs fail.",
                            path,
                            fix=f"herdboard automation edit {item.id} --board BOARD",
                        )
                    )
        # An ID that is not valid names no plan folder.
        problem = automations.plan_problem(store.config, item) if automations.AUTOMATION_ID.match(item.id) else None
        if problem is not None:
            findings.append(
                finding(
                    "error",
                    "automation_plan_invalid",
                    f"Runs of automation {item.id} fail. {problem}",
                    path,
                    fix=f"herdboard automation plan {item.id}",
                )
            )
    if any(item.enabled for item in items):
        problem = automations.timer_problem()
        if problem is not None:
            findings.append(finding("warning", "automation_timer_inactive", problem, fix=automations.TIMER_INSTALL))
    return findings


def check_herdr():
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    if shutil.which(binary) is None:
        return [finding("info", "herdr_missing", f"No Herdr binary found at {binary!r}. Only start needs it.")]
    return []


def check_gh():
    if shutil.which("gh") is None:
        return [
            finding(
                "info",
                "gh_missing",
                "No GitHub CLI `gh` found. Only merge --pr, finish of a pull request and pull request reviews need it.",
            )
        ]
    return []


def check_hunk():
    """Pull request reviews show the PR in Hunk, and the user submits with its `hunk-gh-review` extension."""
    if shutil.which("hunk") is None:
        return [
            finding(
                "warning",
                "hunk_missing",
                "No `hunk` found. Only pull request reviews need it; the board works without it.",
            )
        ]
    try:
        result = subprocess.run(["hunk", "extension", "list"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [finding("warning", "hunk_check_failed", f"`hunk extension list` failed: {error}")]
    if result.returncode or not any(line.split()[:1] == ["hunk-gh-review"] for line in result.stdout.splitlines()):
        return [
            finding(
                "warning",
                "hunk_gh_review_missing",
                "The Hunk extension `hunk-gh-review` is not installed, so S in Hunk cannot submit "
                "a pull request review.",
                fix="hunk extension install phl28/hunk-gh-review",
            )
        ]
    return []


def diagnose(path=None, *, repair=False, offline=False):
    findings, fixed = [], []
    config = None
    try:
        config = load_config(path)
    except HerdboardError as error:
        findings.append(
            finding(
                "error",
                "config_invalid",
                f"{error}. Using the default config for board and Linear checks.",
                config_path(path),
            )
        )
        config = Config()
    if config is not None:
        store = Store(config)
        findings.extend(check_config_keys(path))
        boards, read_errors = read_boards(store)
        if repair:
            fixed, repair_errors = fix_lists(store, boards)
            findings.extend(repair_errors)
            boards, read_errors = read_boards(store)
        findings.extend(read_errors)
        for board_path, board in sorted(boards.items()):
            try:
                findings.extend(check_board(store, board))
            except Exception as error:
                findings.append(finding("error", "board_invalid", str(error), board_path / BOARD_FILE))
                boards[board_path] = None
        boards = {path: board for path, board in boards.items() if board is not None}
        findings.extend(check_duplicate_ids(boards))
        findings.extend(check_parents(boards))
        findings.extend(check_duplicate_repos(boards))
        findings.extend(check_linear(config, boards, offline))
        findings.extend(check_automations(store))
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
        lines.append(f"Fixed {item['path']}: {item['message']}")
    summary = result["summary"]
    lines.append(f"Summary: {summary['error']} errors, {summary['warning']} warnings, {summary['info']} info")
    return "\n".join(lines)
