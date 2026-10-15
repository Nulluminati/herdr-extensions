"""Command-line interface for herdboard."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Callable

from . import activity, agent_review, attachments, plan, pr_write
from .agent_review import AgentReviewer
from .plan import Planner
from .config import THINKING_LEVELS, config_path, load_config
from .identity import repo_identity
from .linear_cards import LINEAR_REF, issue_card
from .linear_links import LabelLink, ProjectLink, with_link
from .linear_source import LinearSource
from .linear_states import refuse
from .linear_writes import LinearWriter
from .migrate import build_preview, preview_text
from .migrate_convert import convert
from .model import HerdboardError, IoError, NotFoundError, with_parent, with_type
from .merge import Merger
from .promote import Promoter
from .review import Reviewer
from .store import CardView, Store
from .start import Starter, card_agent_panes, register_checkout
from .views import INIT_MESSAGE, VIEW_NAMES, LinearResult, Views

INIT_HINT = "Run `herdboard init` to create a board for this repository."


class UsageError(HerdboardError):
    code = "usage"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise UsageError(message)


def _add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", type=Path, default=default, help="config file")
    parser.add_argument(
        "-C", dest="directory", type=Path, default=default, metavar="DIR",
        help="act as if run in DIR",
    )
    parser.add_argument(
        "--json", action="store_true", default=default, help="machine output on stdout"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="herdboard", description="Kanban boards for repositories.")
    _add_global_options(parser, suppress=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help)
        _add_global_options(sub, suppress=True)
        return sub

    def view_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--view", choices=VIEW_NAMES)
        sub.add_argument("--project", help="Linear project name (for the project view)")
        sub.add_argument("--offline", action="store_true", help="read Linear from the cache only")

    sub = command("list", "list cards")
    view_options(sub)
    sub.add_argument("--column")
    sub.add_argument("--board", help="board name: inbox or a project name")
    sub.add_argument("--source", choices=["todo", "linear"])
    sub.add_argument("--archived", action="store_true", help="list the archived todos instead")

    def write_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--state", help="Linear workflow state, when several match the column")
        sub.add_argument("--offline", action="store_true",
                         help="Linear writes need a connection: this makes them fail")

    sub = command("add", "add a card, or with --linear a Linear issue assigned to you")
    sub.add_argument("text")
    sub.add_argument("--parent", help="make the todo a sub-card of this todo, on its board")
    sub.add_argument("--type", help="the card's type, such as bug or feature (the `types` setting)")
    sub.add_argument("--column")
    sub.add_argument("--position", choices=["top", "bottom"], default="bottom")
    sub.add_argument("--board", help="board name: inbox or a project name")
    sub.add_argument("--linear", action="store_true", help="create a Linear issue")
    sub.add_argument("--team", help="Linear team key (with --linear)")
    sub.add_argument("--project", help="Linear project name (with --linear)")
    sub.add_argument("--label", action="append", default=[],
                     help="Linear label name or TEAM/label (with --linear)")
    write_options(sub)

    sub = command("show", "show one card")
    sub.add_argument("ref")
    sub.add_argument("--log", action="store_true", help="show every activity log entry, not the last 10")
    sub.add_argument("--offline", action="store_true", help="read Linear from the cache only")

    sub = command("move", "move a card to a column, or a todo to another board")
    sub.add_argument("ref")
    sub.add_argument("column", nargs="?", help="optional with --board: the card keeps its column name")
    sub.add_argument("--board", help="move a todo to this board: inbox or a project name")
    order = sub.add_mutually_exclusive_group()
    order.add_argument("--up", action="store_true", help="swap the todo with the card above it")
    order.add_argument("--down", action="store_true", help="swap the todo with the card below it")
    sub.add_argument(
        "--position", choices=["top", "bottom"],
        help="place the card at the top or bottom of the column (todos default to the bottom; "
        "a Linear issue keeps Linear's order unless you ask)",
    )
    write_options(sub)

    sub = command("done", "move a card to the done column")
    sub.add_argument("ref")
    write_options(sub)

    sub = command("edit", "change a todo's text, its indented lines, or both")
    sub.add_argument("ref")
    sub.add_argument("--title", help="the card's new first line, with #tags and 📅 date")
    sub.add_argument("--body", help="the card's new indented lines, one per line; empty clears them")
    sub.add_argument("--body-file", type=Path, help="read the new indented lines from a file")
    parent = sub.add_mutually_exclusive_group()
    parent.add_argument("--parent", help="make the todo a sub-card of this todo, on the same board")
    parent.add_argument("--no-parent", action="store_true", help="make the sub-card a plain todo again")
    kind = sub.add_mutually_exclusive_group()
    kind.add_argument("--type", help="set the card's type, such as bug or feature (the `types` setting)")
    kind.add_argument("--no-type", action="store_true", help="remove the card's type")

    sub = command("log", "add an entry to a card's activity log")
    sub.add_argument("ref")
    sub.add_argument("message")

    sub = command("attach", "add a file to a card's attachments, or remove one")
    sub.add_argument("ref")
    sub.add_argument("file", nargs="?", type=Path)
    sub.add_argument("--name", help="the attachment's name; default: the file's name")
    sub.add_argument("--remove", metavar="NAME", help="remove the attachment with this name")

    sub = command("delete", "remove a todo and its indented lines")
    sub.add_argument("ref")
    sub.add_argument("--yes", action="store_true", help="delete without asking")

    sub = command("archive", "move a todo to its board's archive, or with --done every card in the done column")
    sub.add_argument("ref", nargs="?")
    sub.add_argument("--done", action="store_true", help="archive every card in the board's done column")
    sub.add_argument("--board", help="with --done: board name, inbox or a project name; default: this repository's")

    sub = command("unarchive", "move an archived todo to its board's done column")
    sub.add_argument("ref", help="a todo ID, or an archive reference such as acme-api:archive-2")
    sub.add_argument("--yes", action="store_true", help="also unarchive an archived parent without asking")

    sub = command("promote", "turn a todo into a Linear issue, its checklist into sub-issues")
    sub.add_argument("ref")
    sub.add_argument("--team", help="Linear team key")
    sub.add_argument("--project", help="Linear project name")
    write_options(sub)

    sub = command("start", "start work on a card in Herdr")
    sub.add_argument("ref")
    sub.add_argument("--agent", choices=["claude", "pi"])
    sub.add_argument("--no-agent", action="store_true")
    sub.add_argument("--model", help="the agent's model, such as openai-codex/gpt-6.1-sol or opus")
    sub.add_argument("--thinking", choices=THINKING_LEVELS, help="pi's thinking level")
    sub.add_argument("--repo", help="repository board name or checkout path")
    sub.add_argument("--checkout", type=Path)
    sub.add_argument("--base", help="the local branch to start from and merge into; default: the checkout's branch")

    sub = command("plan", "plan a card with an agent in a new tab; the plan becomes the card's plan.md attachment")
    sub.add_argument("ref")
    sub.add_argument("--agent", choices=["claude", "pi"])
    sub.add_argument("--model", help="the planner's model, such as opus or zai/glm-5.3")
    sub.add_argument("--thinking", choices=THINKING_LEVELS, help="pi's thinking level")
    sub.add_argument("--repo", help="repository board name or checkout path")
    sub.add_argument("--checkout", type=Path)

    sub = command("plan-wait", "log each save of the plan until the planner ends (run by plan)")
    sub.add_argument("ref")
    sub.add_argument("--pane", required=True, help="the planner's pane")

    sub = command("review", "review a started card's work: in Hunk next to its agent, by another agent, or both")
    sub.add_argument("ref")
    kind = sub.add_mutually_exclusive_group()
    kind.add_argument("--human", dest="kind", action="store_const", const="human",
                      help="open the work in Hunk next to the card's agent (the default)")
    kind.add_argument("--agent", dest="kind", action="store_const", const="agent",
                      help="have another agent review the work; the card's agent gets the findings")
    kind.add_argument("--both", dest="kind", action="store_const", const="both",
                      help="an agent review first, then Hunk")
    sub.add_argument("--reviewer", choices=["claude", "pi"],
                     help="the reviewing agent; default: the other kind than the card's agent")
    sub.add_argument("--model", help="the reviewer's model, such as sonnet or zai/glm-5.3")
    sub.add_argument("--thinking", choices=THINKING_LEVELS, help="a pi reviewer's thinking level")

    sub = command("review-wait", "wait for an agent review and deliver it (run by review --agent)")
    sub.add_argument("ref")
    sub.add_argument("--folder", type=Path, required=True)

    sub = command("merge", "merge a started card's branch into its base, then finish the card")
    sub.add_argument("ref")
    mode = sub.add_mutually_exclusive_group()
    mode.add_argument("--local", dest="mode", action="store_const", const="local",
                      help="rebase onto the base and fast-forward it")
    mode.add_argument("--pr", dest="mode", action="store_const", const="pr",
                      help="have an agent write the description, then open a draft GitHub pull request")
    sub.add_argument("--rewrite", action="store_true",
                     help="have an agent rewrite the description of the open pull request, not only push")
    sub.add_argument("--force", action="store_true", help="merge a parent card whose sub-cards are not done")

    sub = command("pr-wait", "wait for a pull request description and open the PR (run by merge --pr)")
    sub.add_argument("ref")
    sub.add_argument("--folder", type=Path, required=True)

    sub = command("finish", "remove a merged card's worktree and branch, and move the card to Done")
    sub.add_argument("ref")
    sub.add_argument("--force", action="store_true",
                     help="finish an unmerged card; its branch is kept, and uncommitted work still stops it")

    sub = command("doctor", "check boards, configuration, Linear links and state")
    sub.add_argument("--fix-ids", action="store_true", help="repair duplicate card IDs")
    sub.add_argument("--offline", action="store_true", help="skip Linear checks")

    command("init", "create the board for this repository")
    sub = command("migrate", "convert a free-form TODO.md into cards")
    sub.add_argument("file", type=Path)
    sub.add_argument("--board", help="board name: inbox or a project name")
    sub.add_argument("--yes", action="store_true", help="write without asking")
    sub = command("link", "link this repository's board to a Linear project")
    sub.add_argument("project")
    sub.add_argument("--label", action="append", default=[], help="label name or TEAM/label")
    sub.add_argument("--remove", action="store_true", help="remove the link")
    sub = command("board", "show the board view")
    view_options(sub)
    return parser


def _card_dict(card: CardView) -> dict:
    data = asdict(card)
    # The list order already carries the sort order, and JSON consumers keep their schema.
    del data["sort_order"]
    # Board-only status; preserve the CLI card schema.
    del data["worktree_removed"], data["worktree_left"], data["pr_state"], data["parent_title"], data["sub_cards"], data["agent_review"]
    del data["pr_writing"]
    data["path"] = str(card.path) if card.path is not None else None
    return data


def _emit_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _card_line(card: CardView) -> str:
    return f"{card.ref} → {card.column} ({card.board})"


def _card_text(card: CardView) -> str:
    # A Linear issue keeps its parent outside the title, so its title text stays as it is.
    title = card.title if card.source == "linear" else with_parent(card.title, None)
    line = f"  {card.ref}  {title}"
    if card.source == "linear":
        if card.state and card.state.casefold() != card.column.casefold():
            line += f"  [{card.state}]"
        if card.pr:
            line += "  PR"
    return line


def _list_text(cards: list[CardView]) -> str:
    if not cards:
        return "No cards."
    boards = list(dict.fromkeys(card.board for card in cards))
    lines: list[str] = []
    for board in boards:
        if lines:
            lines.append("")
        if len(boards) > 1:
            lines.append(f"# {board}")
        limits = {c.column: c.column_limit for c in cards if c.board == board}
        for index, column in enumerate(limits):
            if index:
                lines.append("")
            limit = limits[column]
            lines.append(f"## {column}" if limit is None else f"## {column} ({limit})")
            for card in cards:
                if card.board == board and card.column == column:
                    lines.append(_card_text(card))
    return "\n".join(lines)


def _show_linear_text(card: CardView) -> str:
    lines = [card.title, f"{card.board} / {card.column}", f"ref: {card.ref}"]
    lines.append(f"state: {card.state}")
    if card.team:
        lines.append(f"team: {card.team}")
    if card.url:
        lines.append(f"url: {card.url}")
    if card.tags:
        lines.append("labels: " + ", ".join(card.tags))
    if card.due:
        lines.append(f"due: {card.due}")
    if card.pr:
        lines.append("pull request: yes")
    lines.extend(card.body)
    return "\n".join(lines)


def _log_text(entries: list[dict]) -> list[str]:
    if not entries:
        return []
    lines = ["--- log ---"]
    for entry in entries:
        first, *rest = entry["message"].split("\n")
        lines.append(f"{entry['time']} · {entry['author']} · {first}")
        lines.extend("\t" + line for line in rest)
    return lines


def _attachments_text(files: list[dict]) -> list[str]:
    return ["--- attachments ---", *(entry["path"] for entry in files)] if files else []


def _show_text(card: CardView, notes: list[dict] | None = None, log: list[dict] | None = None,
               files: list[dict] | None = None) -> str:
    if card.source == "linear":
        return "\n".join([_show_linear_text(card), *_attachments_text(files or []), *_log_text(log or [])])
    lines = [card.title, f"{card.board} / {card.column}", f"ref: {card.ref}"]
    if card.parent:
        lines.append(f"parent: {card.parent}")
    if card.type:
        lines.append(f"type: {card.type}")
    if card.due:
        lines.append(f"due: {card.due}")
    if card.tags:
        lines.append("tags: " + ", ".join(card.tags))
    lines.extend(card.body)
    for note in notes or []:
        lines.append(f"--- {note['name']} ---")
        lines.append("(note not found)" if note["content"] is None else note["content"].rstrip("\n"))
    lines.extend(_attachments_text(files or []))
    lines.extend(_log_text(log or []))
    return "\n".join(lines)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _prompt_state(team_key: str, column: str, found: list[dict]) -> dict | None:
    """Ask on the terminal which of several states to use; None when input ends."""
    print(f"Which state of team {team_key} for column {column!r}?", file=sys.stderr)
    for number, state in enumerate(found, start=1):
        print(f"  {number}) {state['name']}", file=sys.stderr)
    while True:
        sys.stderr.write(f"Choose 1-{len(found)}: ")
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer:
            return None
        if answer.strip().isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]


class _Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config = load_config(args.config)
        self.store = Store(self.config)
        self.directory = args.directory or Path.cwd()
        register_checkout(self.directory)
        self._identity = False
        self._views: Views | None = None

    @property
    def identity(self):
        if self._identity is False:
            self._identity = repo_identity(self.directory)
        return self._identity

    def repo_board(self) -> Path | None:
        return self.store.board_for_repo(self.identity) if self.identity else None

    @property
    def views(self) -> Views:
        if self._views is None:
            project = getattr(self.args, "project", None)
            source = LinearSource(self.config, offline=getattr(self.args, "offline", False))
            self._views = Views(self.store, self.config, source, self.identity, project)
        return self._views

    @property
    def writer(self) -> LinearWriter:
        return LinearWriter(self.config, self.views.source)

    def ask(self):
        """The question handler for an ambiguous state: a prompt on a terminal, else an error."""
        return _prompt_state if not self.args.json and _interactive() else refuse

    def linear_card(self, issue: dict) -> CardView:
        identities = {self.identity.identity} if self.identity else set()
        now = datetime.now(timezone.utc)
        return issue_card(self.config, issue, identities, now, False, show_canceled=True)

    def default_link(self) -> ProjectLink | None:
        """The only Linear project linked to this repository's board, if there is one."""
        board = self.repo_board()
        links = self.store.board_meta(board).links if board else []
        return links[0] if len(links) == 1 else None

    def todo_link(self, view) -> ProjectLink | None:
        """The only Linear project linked to the board of a todo; an inbox todo has none."""
        if view.path is None or view.path == self.store.inbox_path:
            return None
        links = self.store.board_meta(view.path).links
        return links[0] if len(links) == 1 else None

    def chosen_view(self) -> str:
        """The `--view` option, or the default for the current directory."""
        args = self.args
        if args.project and args.view not in (None, "project"):
            raise UsageError("--project works only with --view project")
        if args.view == "project" and not args.project:
            raise UsageError("--view project needs --project NAME")
        if args.view:
            return args.view
        if args.project:
            return "project"
        return "repo" if self.repo_board() else "inbox"


def _cmd_list(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    result = LinearResult()
    if args.archived:
        if args.source == "linear" or args.column:
            raise UsageError("--archived lists todos: it does not work with --source linear or --column")
        paths = [store.resolve_board(args.board)] if args.board else ctx.views.paths(ctx.chosen_view())
        return {"cards": store.archived_cards(paths), "linear": result, "boards": []}
    if args.board:
        paths = [store.resolve_board(args.board)]
        cards = store.list_cards(paths)
    else:
        view = ctx.chosen_view()
        if view == "project":
            ctx.views.resolve_project()
        paths = ctx.views.paths(view)
        cards = store.list_cards(paths)
        if args.source != "todo":
            result = ctx.views.linear(view)
            cards += result.cards
    if args.source:
        cards = [card for card in cards if card.source == args.source]
    if args.column:
        wanted = args.column.casefold()
        cards = [card for card in cards if card.column.casefold() == wanted]
    # Every column of each listed board, including empty ones, so a caller can pick a move target.
    boards = [] if args.source == "linear" else [store.snapshot(path) for path in paths]
    return {"cards": cards, "linear": result, "boards": boards}


def _cmd_add(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    if args.linear:
        return _cmd_add_linear(ctx)
    if args.team or args.project or args.label or args.state:
        raise UsageError("--team, --project, --label and --state work only with --linear")
    if args.type is not None:
        args.text = with_type(args.text, ctx.config.type_named(args.type))
    hint = None
    if args.parent is not None:
        if LINEAR_REF.match(args.parent):
            raise UsageError("A Linear issue's sub-issues are set in Linear; --parent takes a todo")
        parent = store.show(args.parent)
        path = store.ensure_inbox() if args.board == "inbox" else (
            store.resolve_board(args.board) if args.board else parent.path)
        return {"card": store.add(path, args.text, args.column, args.position, parent=parent.ref), "hint": None}
    if args.board == "inbox":
        path = store.ensure_inbox()
    elif args.board:
        path = store.resolve_board(args.board)
    else:
        path = ctx.repo_board()
        if path is None:
            path = store.ensure_inbox()
            if ctx.identity is not None:
                hint = INIT_HINT
    card = store.add(path, args.text, args.column, args.position)
    return {"card": card, "hint": hint}


def _find_linear_card(ctx: _Context, ref: str) -> tuple[CardView, LinearResult]:
    """Look the issue up in Linear; without a connection, search every cached view."""
    fetched = ctx.views.source.issue(ref)
    card = None
    if fetched.data is not None:
        identities = {ctx.identity.identity} if ctx.identity else set()
        card = issue_card(ctx.config, fetched.data, identities, fetched.fetched_at, fetched.stale)
    if card is None:
        raise NotFoundError(f"No card {ref}")
    return card, LinearResult(fetched_at=fetched.fetched_at, stale=fetched.stale)


SHOWN_LOG_ENTRIES = 10


def _card_log(ctx: _Context, card: CardView) -> list[dict]:
    """The card's activity log; a card with only a temporary reference has none."""
    if not activity.LOG_REF.match(card.ref):
        return []
    return activity.read(ctx.config, card.ref, None if ctx.args.log else SHOWN_LOG_ENTRIES)


def _card_attachments(ctx: _Context, card: CardView) -> list[dict]:
    """The card's attachments; a card with only a temporary reference has none."""
    if not activity.LOG_REF.match(card.ref):
        return []
    return attachments.listing(ctx.config, card.ref)


def _cmd_show(ctx: _Context) -> dict:
    ref = ctx.args.ref
    if LINEAR_REF.match(ref):
        ctx.views.source.require_active()
        card, result = _find_linear_card(ctx, ref)
        return {"card": card, "linear": result, "log": _card_log(ctx, card),
                "attachments": _card_attachments(ctx, card)}
    card = ctx.store.show(ref, archived=True)
    return {"card": card, "notes": ctx.store.notes_for(card), "log": _card_log(ctx, card),
            "attachments": _card_attachments(ctx, card)}


def _cmd_log(ctx: _Context) -> dict:
    args = ctx.args
    if not args.message.strip():
        raise UsageError("The log message is empty")
    if LINEAR_REF.match(args.ref):
        ref = args.ref.upper()
    else:
        ref = ctx.store.ensure_id(args.ref, archived=True).ref
    return {"ref": ref, "entry": activity.append(ctx.store, ref, args.message, activity.author())}


def _cmd_attach(ctx: _Context) -> dict:
    args = ctx.args
    if (args.file is None) == (args.remove is None):
        raise UsageError("Give a file to attach, or --remove NAME")
    if args.remove is not None and args.name is not None:
        raise UsageError("--name works only when you attach a file")
    ref = args.ref.upper() if LINEAR_REF.match(args.ref) else ctx.store.ensure_id(args.ref).ref
    if args.remove is not None:
        removed = attachments.remove(ctx.store, ref, args.remove)
        activity.append(ctx.store, ref, f"Removed the attachment {removed['name']}.", activity.author())
        return {"ref": ref, "removed": removed}
    path = args.file if args.file.is_absolute() else ctx.directory / args.file
    added = attachments.add(ctx.store, ref, path, args.name)
    activity.append(ctx.store, ref, f"Attached {added['name']}.", activity.author())
    return {"ref": ref, "attachment": added}


def _require_todo_options(args: argparse.Namespace) -> None:
    if args.state:
        raise UsageError("--state works only with Linear issues")


def _cmd_move(ctx: _Context) -> dict:
    args = ctx.args
    if args.up or args.down:
        if args.column or args.board or args.position or args.state:
            raise UsageError("--up and --down take no column, --board, --position or --state")
        if LINEAR_REF.match(args.ref):
            raise UsageError("Change the order of Linear issues in Linear")
        return {"card": ctx.store.shift(args.ref, -1 if args.up else 1)}
    if args.board is not None:
        if LINEAR_REF.match(args.ref):
            raise UsageError("Linear issues stay in Linear; --board moves only todos")
        if args.position or args.state:
            raise UsageError("--position and --state do not work with --board")
        target = ctx.store.ensure_inbox() if args.board == "inbox" else ctx.store.resolve_board(args.board)
        return {"card": ctx.store.move_to_board(args.ref, target, args.column)}
    if args.column is None:
        raise UsageError("Give a column, or --board to move the todo to another board")
    if LINEAR_REF.match(args.ref):
        issue = ctx.writer.move(args.ref, args.column, args.position, ctx.ask(), args.state)
        return {"card": ctx.linear_card(issue)}
    _require_todo_options(args)
    return {"card": ctx.store.move(args.ref, args.column, args.position or "bottom")}


def _cmd_done(ctx: _Context) -> dict:
    args = ctx.args
    if LINEAR_REF.match(args.ref):
        return {"card": ctx.linear_card(ctx.writer.done(args.ref, ctx.ask(), args.state))}
    _require_todo_options(args)
    return {"card": ctx.store.done(args.ref)}


class LinearEditError(HerdboardError):
    code = "linear_edit"


class AgentEditDenied(HerdboardError):
    code = "agent_edit_denied"


def _cmd_edit(ctx: _Context) -> dict:
    args = ctx.args
    if LINEAR_REF.match(args.ref):
        raise LinearEditError("Edit Linear issues in Linear; herdboard edits only todos.")
    if args.body is not None and args.body_file is not None:
        raise UsageError("Use --body or --body-file, not both")
    body = args.body
    if args.body_file is not None:
        path = args.body_file if args.body_file.is_absolute() else ctx.directory / args.body_file
        body = _read_todo_file(path)
    if (args.title is None and body is None and args.parent is None and not args.no_parent
            and args.type is None and not args.no_type):
        raise UsageError(
            "Nothing to change: give --title, --body, --body-file, --parent, --no-parent, --type or --no-type"
        )
    kind = ctx.config.type_named(args.type) if args.type is not None else None
    # A guard against mistakes, not a security boundary: the card text is the user's.
    pane = os.environ.get("HERDR_PANE_ID")
    if pane and pane in card_agent_panes():
        raise AgentEditDenied(
            "Agents do not change the card title or details. "
            f'Record progress with `herdboard log {args.ref} "..."` instead.'
        )
    lines = body.splitlines() if body is not None else None
    title = args.title
    if args.type is not None or args.no_type:
        title = with_type(title if title is not None else ctx.store.show(args.ref).title, kind)
    card = None
    if title is not None or lines is not None:
        card = ctx.store.edit(args.ref, title=title, body=lines)
    if args.parent is not None or args.no_parent:
        if args.parent is not None and LINEAR_REF.match(args.parent):
            raise UsageError("A Linear issue's sub-issues are set in Linear; --parent takes a todo")
        parent = ctx.store.show(args.parent).ref if args.parent is not None else None
        card = ctx.store.set_parent(card.ref if card else args.ref, parent)
    return {"card": card, "edited": True}


class ConfirmationRequired(HerdboardError):
    code = "confirmation_required"


class LinearDeleteError(HerdboardError):
    code = "linear_delete"


def _update_sidebar(command: str, result: dict) -> None:
    """Show a started card's change in Herdr: its agent's sidebar row and its review pane."""
    from .start import card_changed

    if command in ("move", "done", "edit") and result.get("card") is not None:
        card_changed(result["card"])
    elif command == "delete" and result.get("deleted") is not None:
        card_changed(result["deleted"], removed=True)


def _cmd_delete(ctx: _Context) -> dict:
    args = ctx.args
    if LINEAR_REF.match(args.ref):
        raise LinearDeleteError("Delete Linear issues in Linear; herdboard deletes only todos.")
    card = ctx.store.show(args.ref)
    if args.yes:
        return {"deleted": ctx.store.delete(args.ref)}
    if args.json or not _interactive():
        raise ConfirmationRequired(f"Deleting {card.ref} needs --yes without a terminal.")
    if not _confirm(f"Delete {card.ref}: {card.title} ({card.board} / {card.column})? [y/N] "):
        return {"deleted": None}
    return {"deleted": ctx.store.delete(args.ref)}


class LinearArchiveError(HerdboardError):
    code = "linear_archive"


def _cmd_archive(ctx: _Context) -> dict:
    from . import start as start_module

    args, store = ctx.args, ctx.store
    if args.done == (args.ref is not None):
        raise UsageError("Give a card to archive, or --done")
    if args.board and not args.done:
        raise UsageError("--board works only with --done")
    if args.done:
        if args.board == "inbox":
            path = store.ensure_inbox()
        elif args.board:
            path = store.resolve_board(args.board)
        else:
            path = ctx.repo_board()
            if path is None and ctx.identity is not None:
                # A bulk archive of the inbox needs a clear request.
                raise UsageError(f"No board for this repository. {INIT_HINT} "
                                 "Use --board inbox to archive the inbox's done column.")
            path = path or store.ensure_inbox()
        archived, skipped = store.archive_done(path, blocked=start_module.archive_blocker)
        for card in archived:
            activity.append(store, card.ref, "Archived.", activity.author())
        return {"board": store.board_name(path), "archived": archived, "skipped": skipped}
    if LINEAR_REF.match(args.ref):
        raise LinearArchiveError("herdboard archives only todos. Linear hides completed issues after done_days.")
    card = store.archive(args.ref, blocked=start_module.archive_blocker)
    activity.append(store, card.ref, "Archived.", activity.author())
    return {"card": card}


def _cmd_unarchive(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    if LINEAR_REF.match(args.ref):
        raise LinearArchiveError("herdboard archives only todos.")
    parent = store.archived_parent(args.ref)
    if parent is not None and not args.yes:
        question = f"The parent {parent.ref} of {args.ref} is archived too, and unarchives with it."
        if args.json or not _interactive():
            raise ConfirmationRequired(f"{question} Run again with --yes to unarchive both.")
        if not _confirm(f"{question} Unarchive both? [y/N] "):
            return {"card": None}
    card = store.unarchive(args.ref, with_parent=parent is not None)
    hint = None
    if parent is not None:
        activity.append(store, parent.ref, "Unarchived.", activity.author())
        hint = f"Also unarchived the parent {parent.ref}."
    activity.append(store, card.ref, "Unarchived.", activity.author())
    return {"card": card, "hint": hint}


def _prompt_checkout(identity: str) -> str | None:
    print(f"Local checkout path for {identity}: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip() or None


def _cmd_start(ctx: _Context) -> dict:
    args = ctx.args
    starter = Starter(
        ctx.store, ctx.directory,
        writer=ctx.writer if LINEAR_REF.match(args.ref) else None,
        ask_state=ctx.ask(),
        prompt_checkout=_prompt_checkout if not args.json and _interactive() else None,
        config_file=config_path(args.config),
    )
    return starter.start(args.ref, agent=args.agent, no_agent=args.no_agent,
                         repo=args.repo, checkout=args.checkout, model=args.model, thinking=args.thinking,
                         base=args.base)


def _cmd_plan(ctx: _Context) -> dict:
    args = ctx.args
    planner = Planner(ctx.store, ctx.directory, writer=ctx.writer if LINEAR_REF.match(args.ref) else None,
                      prompt_checkout=_prompt_checkout if not args.json and _interactive() else None,
                      config_file=config_path(args.config))
    return planner.plan(args.ref, agent=args.agent, model=args.model, thinking=args.thinking, repo=args.repo,
                        checkout=args.checkout)


def _cmd_plan_wait(ctx: _Context) -> dict:
    ref = ctx.args.ref
    return {"ref": ref, "exit_code": plan.wait(ctx.store, ref, ctx.args.pane)}


def _cmd_review(ctx: _Context) -> dict:
    args = ctx.args
    ref = args.ref
    writer = ctx.writer if LINEAR_REF.match(ref) else None
    kind = args.kind or "human"
    if kind == "human":
        if args.reviewer or args.model or args.thinking:
            raise UsageError("--reviewer, --model and --thinking need --agent or --both")
        return Reviewer(ctx.store, ctx.directory, writer=writer, config_file=config_path(args.config)).review(ref)
    reviewer = AgentReviewer(ctx.store, ctx.directory, writer=writer, config_file=config_path(args.config))
    return {"kind": kind, **reviewer.request(ref, reviewer=args.reviewer, model=args.model,
                                             thinking=args.thinking, then_hunk=kind == "both")}


def _cmd_review_wait(ctx: _Context) -> dict:
    ref = ctx.args.ref
    code = agent_review.wait(ctx.store, ref, ctx.args.folder, writer=ctx.writer if LINEAR_REF.match(ref) else None,
                             config_file=config_path(ctx.args.config))
    return {"ref": ref, "exit_code": code}


def _merger(ctx: _Context) -> Merger:
    ref = ctx.args.ref
    return Merger(ctx.store, ctx.directory, writer=ctx.writer if LINEAR_REF.match(ref) else None,
                  ask_state=ctx.ask(), config_file=config_path(ctx.args.config))


def _cmd_merge(ctx: _Context) -> dict:
    if ctx.args.rewrite and ctx.args.mode == "local":
        raise UsageError("--rewrite works only for a pull request")
    return _merger(ctx).merge(ctx.args.ref, mode=ctx.args.mode, force=ctx.args.force, rewrite=ctx.args.rewrite)


def _cmd_pr_wait(ctx: _Context) -> dict:
    ref = ctx.args.ref
    code = pr_write.wait(ctx.store, ref, ctx.args.folder, writer=ctx.writer if LINEAR_REF.match(ref) else None)
    return {"ref": ref, "exit_code": code}


def _cmd_finish(ctx: _Context) -> dict:
    return _merger(ctx).finish(ctx.args.ref, force=ctx.args.force)


def _finish_line(result: dict) -> str:
    worktree = "worktree removed" if result["worktree_removed"] else "no worktree to remove"
    branch = "branch deleted" if result["branch_deleted"] else "branch kept"
    return f"{result['ref']}: finished; {worktree}; {branch}"


def _cmd_init(ctx: _Context) -> dict:
    if ctx.identity is None:
        raise HerdboardError("Not in a Git repository.")
    return {"board": ctx.store.init_board(ctx.identity)}


def _read_todo_file(path: Path) -> str:
    if not path.is_file():
        raise NotFoundError(f"No file: {path}")
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read file: {error}") from error


def _confirm(prompt: str) -> bool:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _cmd_migrate(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    path = args.file if args.file.is_absolute() else ctx.directory / args.file
    conversion = convert(_read_todo_file(path))
    items = conversion.items
    if not items:
        raise HerdboardError(f"Nothing to migrate: {path} has no list items or text")
    # The repository is the one that contains the file, not the working directory.
    identity = None if args.board else repo_identity(path.parent)
    target = store.import_target(identity, args.board)
    if target.done_column is None and any(item.checked for item in items):
        raise HerdboardError(target.done_error)
    preview = build_preview(conversion, target)
    if not args.json:
        print(preview_text(target, preview, len(items)))
    asked = not args.yes and not args.json and _interactive()
    write = args.yes or (
        asked and _confirm(f"Write {len(items)} cards to {target.path}? [y/N] ")
    )
    if write:
        keep_path = target.exists or target.identity is None
        store.import_cards(items, target.path if keep_path else None, target.identity)
    return {"board": target.path, "preview": preview, "cards": len(items),
            "written": bool(write), "file": path, "asked": asked}


def _describe(label: dict) -> str:
    team = label.get("team")
    return f"{label['name']} ({team['key'] if team else 'workspace'})"


def _resolve_project(client, name: str) -> dict:
    found = client.find_projects(name)
    if not found:
        raise HerdboardError(f"No Linear project named {name!r}")
    if len(found) > 1:
        options = "; ".join(
            f"{p['name']} [{', '.join(t['key'] for t in p['teams']) or 'no team'}] ({p['id']})"
            for p in found
        )
        raise HerdboardError(f"{len(found)} Linear projects are named {name!r}: {options}")
    return found[0]


def _resolve_label(client, spec: str) -> LabelLink:
    matches = []
    team, _, rest = spec.partition("/")
    if rest:
        matches = [
            label
            for label in client.find_labels(rest)
            if label.get("team") and label["team"]["key"].casefold() == team.casefold()
        ]
    if not matches:
        matches = client.find_labels(spec)
    if not matches:
        raise HerdboardError(f"No Linear label named {spec!r}")
    if len(matches) > 1:
        options = ", ".join(_describe(label) for label in matches)
        raise HerdboardError(
            f"{len(matches)} labels are named {spec!r}: {options}. Use TEAM/label to pick one"
        )
    return LabelLink(matches[0]["id"], matches[0]["name"])


def _linked_project(client, link: ProjectLink) -> dict:
    project = client.project_for_write(link.id)
    if project is not None:
        return project
    raise HerdboardError(
        f"Linear project {link.name!r}, linked to this repository, was not found "
        "or is not accessible"
    )


def _linear_target(
    client, project_name: str | None, label_specs: list[str], default: ProjectLink | None
):
    """The project and label IDs of a new issue: the options, else the `default` link."""
    project = _resolve_project(client, project_name) if project_name else None
    label_ids = [_resolve_label(client, spec).id for spec in label_specs]
    link = None if project_name else default
    if link is not None:
        project = _linked_project(client, link)
        link_ids = [label.id for label in link.labels]
        label_ids = link_ids + [i for i in label_ids if i not in link_ids]
    return project, label_ids


def _cmd_add_linear(ctx: _Context) -> dict:
    args = ctx.args
    if args.board:
        raise UsageError("--linear and --board cannot be used together")
    writer = ctx.writer
    with writer.online() as client:
        project, label_ids = _linear_target(client, args.project, args.label, ctx.default_link())
    issue = writer.create(
        args.text, team=args.team, project=project, label_ids=label_ids,
        column=args.column, ask=ctx.ask(), state_name=args.state,
    )
    return {"card": ctx.linear_card(issue), "hint": None}


def _cmd_promote(ctx: _Context) -> dict:
    args = ctx.args
    promoter = Promoter(ctx.config, ctx.store, ctx.writer)
    promotion = promoter.promote(
        args.ref,
        team=args.team,
        target=lambda client, view: _linear_target(
            client, args.project, [], ctx.todo_link(view)
        ),
        ask=ctx.ask(),
        state_name=args.state,
        project=args.project,
    )
    for warning in promotion.warnings:
        print(f"herdboard: warning: {warning}", file=sys.stderr)
    return {
        "card": ctx.linear_card(promotion.issue),
        "sub_issues": [sub["identifier"] for sub in promotion.sub_issues],
        "todo": promotion.todo,
        "warnings": promotion.warnings,
    }


def _cmd_link(ctx: _Context) -> dict:
    args = ctx.args
    if ctx.identity is None:
        raise HerdboardError("Not in a Git repository.")
    board = ctx.repo_board()
    if board is None:
        raise HerdboardError(INIT_MESSAGE)
    if args.remove:
        wanted = args.project.casefold()

        def remove(links: list[ProjectLink]) -> list[ProjectLink]:
            kept = [l for l in links if l.name.casefold() != wanted and l.id != args.project]
            if len(kept) == len(links):
                raise NotFoundError(f"{args.project!r} is not linked to this repository")
            return kept

        return {"board": board, "links": ctx.store.update_links(board, remove)}
    link = _project_link(ctx, args.project, args.label)
    return {"board": board, "links": ctx.store.update_links(board, lambda links: with_link(links, link))}


def _project_link(ctx: _Context, name: str, labels: list[str]) -> ProjectLink:
    """The link to the Linear project `name`, with the labels that `labels` name."""
    client = ctx.views.source.client
    project = _resolve_project(client, name)
    return ProjectLink(project["id"], project["name"], [_resolve_label(client, l) for l in labels])


def _init_for_board(ctx: _Context) -> Callable[[], Path] | None:
    """What the board's `i` key runs: create this repository's board, when it has none."""
    if ctx.identity is None or ctx.repo_board() is not None:
        return None
    identity = ctx.identity
    return lambda: ctx.store.init_board(identity)


def _cmd_board(ctx: _Context) -> dict:
    view = ctx.chosen_view()
    views = ctx.views
    views.paths(view)  # fails early when the view has no board
    try:
        from .board import HerdboardApp
    except ImportError as error:
        raise HerdboardError(f"The board needs Textual: {error}") from error
    HerdboardApp(
        ctx.store,
        view,
        paths_for=views.paths,
        repo_board=ctx.repo_board(),
        linear_for=views.linear,
        linear_active=views.source.active,
        available=views.available,
        linear_writer=ctx.writer,
        directory=ctx.directory,
        init_board=_init_for_board(ctx),
        config_file=config_path(ctx.args.config),
        # The project lookup reads Linear, so an offline board gets none.
        resolve_link=(lambda name, labels: _project_link(ctx, name, labels))
        if views.source.active and not views.source.offline else None,
    ).run()
    return {}


COMMANDS = {
    "list": _cmd_list,
    "add": _cmd_add,
    "show": _cmd_show,
    "move": _cmd_move,
    "done": _cmd_done,
    "delete": _cmd_delete,
    "archive": _cmd_archive,
    "unarchive": _cmd_unarchive,
    "edit": _cmd_edit,
    "log": _cmd_log,
    "attach": _cmd_attach,
    "promote": _cmd_promote,
    "start": _cmd_start,
    "plan": _cmd_plan,
    "plan-wait": _cmd_plan_wait,
    "review": _cmd_review,
    "review-wait": _cmd_review_wait,
    "pr-wait": _cmd_pr_wait,
    "merge": _cmd_merge,
    "finish": _cmd_finish,
    "init": _cmd_init,
    "migrate": _cmd_migrate,
    "link": _cmd_link,
    "board": _cmd_board,
}


def _board_dict(board) -> dict:
    return {
        "name": board.name,
        "path": str(board.path),
        "columns": [
            {"title": column.title, "limit": column.limit, "done": column.completed}
            for column in board.columns
        ],
    }


def _linear_json(result: LinearResult) -> dict:
    data = {}
    if result.info is not None:
        data["linear"] = result.info
    if result.errors:
        data["errors"] = result.errors
    return data


def _linear_warnings(result: LinearResult) -> None:
    for error in result.errors:
        print(f"herdboard: warning: {error}", file=sys.stderr)
    if result.stale and result.fetched_at is not None:
        stamp = result.fetched_at.astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"herdboard: warning: Linear data is stale (fetched {stamp})", file=sys.stderr)


def _render(command: str, result: dict, as_json: bool) -> None:
    if command == "board":
        return
    if command == "start":
        if as_json:
            _emit_json(result)
        else:
            print(f"{result['ref']}: {result['branch']} in {result['worktree_path']}")
            if result.get("base"):
                print(f"Base: {result['base']}")
            print(f"Workspace: {result['workspace_id']}; pane: {result['pane_id']}; "
                  f"agent: {result['agent_name'] or 'none'}")
            print("Steps done: " + ", ".join(result["steps_done"]))
        return
    if command in ("review-wait", "pr-wait", "plan-wait"):
        return
    if command == "plan":
        if as_json:
            _emit_json(result)
        else:
            model = f" ({result['model']})" if result["model"] else ""
            print(f"{result['ref']}: {result['agent']}{model} plans in pane {result['pane_id']}, in {result['checkout']}.")
            print(f"The plan: {result['plan']}")
        return
    if command == "review" and "reviewer" in result:
        if as_json:
            _emit_json(result)
        else:
            model = f" ({result['model']})" if result["model"] else ""
            print(f"{result['ref']}: agent review by {result['reviewer']}{model} "
                  f"in pane {result['pane_id']}. Files: {result['folder']}")
            print("The card's agent gets the findings when they are ready."
                  + (" Hunk opens when the findings are ready." if result["then_hunk"] else ""))
        return
    if command == "review":
        if as_json:
            _emit_json(result)
        else:
            print(f"{result['ref']}: Hunk review in pane {result['review_pane']} (workspace {result['workspace_id']})")
            if result["agent_restarted"]:
                print("The agent was gone; a new agent started with the card and its log.")
        return
    if command in ("merge", "finish"):
        if as_json:
            _emit_json(result)
            return
        if command == "merge" and result["mode"] == "pr":
            if result["pr_action"] == "pushed":
                print(f"{result['ref']}: pushed to the open pull request {result['pr_url']}")
                print("Use --rewrite to rewrite its description.")
            elif result["pr_url"]:
                print(f"{result['ref']}: {result['writer']} rewrites the description of {result['pr_url']}. "
                      f"Files: {result['folder']}")
                print("The pull request changes when the description is ready.")
            else:
                print(f"{result['ref']}: {result['writer']} writes the pull request description. "
                      f"Files: {result['folder']}")
                print("The draft pull request opens when the description is ready.")
            return
        if command == "merge":
            print(f"{result['ref']}: merged into {result['base']} at {result['commit'][:7]}")
        print(_finish_line(result))
        return
    if command == "attach":
        if as_json:
            _emit_json(result)
        elif "removed" in result:
            print(f"{result['ref']}: removed {result['removed']['name']}")
        else:
            print(f"{result['ref']}: attached {result['attachment']['name']}")
        return
    if command == "archive" and "archived" in result:
        if as_json:
            _emit_json({"board": result["board"], "archived": [_card_dict(card) for card in result["archived"]],
                        "skipped": [{"ref": card.ref, "reason": reason} for card, reason in result["skipped"]]})
        else:
            count = len(result["archived"])
            print(f"Archived {count} card{'' if count == 1 else 's'} from {result['board']}.")
            for card, reason in result["skipped"]:
                print(f"Kept {card.ref}: {reason}")
        return
    if command == "unarchive" and result["card"] is None:
        if as_json:
            _emit_json({"card": None})
        else:
            print("Nothing unarchived.")
        return
    if command == "log":
        if as_json:
            _emit_json(result)
        else:
            print(f"{result['ref']}: logged")
        return
    if command == "migrate":
        if as_json:
            _emit_json({"board": str(result["board"]), "preview": result["preview"],
                        "cards": result["cards"], "written": result["written"]})
        elif result["written"]:
            print(f"Wrote {result['cards']} cards to {result['board']}")
            print(f"Original file left unchanged: {result['file']}")
            print("Delete it yourself when you no longer need it.")
        else:
            print("Nothing written." + ("" if result["asked"] else " Run again with --yes to write."))
        return
    linear = result.get("linear")
    if as_json:
        if command == "delete":
            deleted = result["deleted"]
            _emit_json({"deleted": _card_dict(deleted) if deleted else None})
        elif command == "list":
            _emit_json({"cards": [_card_dict(card) for card in result["cards"]],
                        "boards": [_board_dict(board) for board in result["boards"]],
                        **_linear_json(linear)})
        elif command == "promote":
            _emit_json({"card": _card_dict(result["card"]), "sub_issues": result["sub_issues"],
                        "todo": _card_dict(result["todo"]), "warnings": result["warnings"]})
        elif command == "init":
            _emit_json({"board": str(result["board"])})
        elif command == "link":
            _emit_json({"board": str(result["board"]),
                        "links": [link.to_yaml() for link in result["links"]]})
        else:
            data = {"card": _card_dict(result["card"])}
            if result.get("notes") is not None:
                data["notes"] = result["notes"]
            if result.get("log") is not None:
                data["log"] = result["log"]
            if result.get("attachments") is not None:
                data["attachments"] = result["attachments"]
            if result.get("hint"):
                data["hint"] = result["hint"]
            if linear is not None:
                data.update(_linear_json(linear))
            _emit_json(data)
        return
    if command == "delete":
        deleted = result["deleted"]
        print(f"Deleted {deleted.ref}: {deleted.title} ({deleted.board} / {deleted.column})"
              if deleted else "Nothing deleted.")
    elif command == "list":
        print(_list_text(result["cards"]))
    elif command == "show":
        print(_show_text(result["card"], result.get("notes"), result.get("log"), result.get("attachments")))
    elif command == "promote":
        print(_card_line(result["card"]))
        if result["sub_issues"]:
            print("sub-issues: " + ", ".join(result["sub_issues"]))
        print(f"todo {_card_line(result['todo'])}")
    elif command == "init":
        print(result["board"])
    elif command == "link":
        names = ", ".join(link.name for link in result["links"]) or "none"
        print(f"{result['board']}: linked projects: {names}")
    elif command == "edit":
        card = result["card"]
        print(f"{card.ref} edited ({card.board} / {card.column})")
    else:
        print(_card_line(result["card"]))
        if result.get("hint"):
            print(result["hint"])
    if linear is not None:
        _linear_warnings(linear)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    as_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        as_json = args.json
        if args.command == "doctor":
            from .doctor import diagnose, render
            result = diagnose(args.config, repair_ids=args.fix_ids, offline=args.offline)
            if as_json:
                _emit_json(result)
            else:
                print(render(result))
            return 1 if result["summary"]["error"] else 0
        result = COMMANDS[args.command](_Context(args))
        _update_sidebar(args.command, result)
        _render(args.command, result, as_json)
        if args.command in ("review-wait", "pr-wait", "plan-wait"):
            return result["exit_code"]
    except HerdboardError as error:
        if as_json:
            details = {"code": error.code, "message": str(error)}
            if getattr(error, "issue_id", None):
                details["issue_id"] = error.issue_id
            if getattr(error, "candidates", None):
                details["candidates"] = error.candidates
            if hasattr(error, "steps_done"):
                details["steps_done"] = error.steps_done
            _emit_json({"error": details})
        else:
            print(f"herdboard: {error}", file=sys.stderr)
            if hasattr(error, "steps_done"):
                print("Steps done: " + (", ".join(error.steps_done) or "none"), file=sys.stderr)
        return error.exit_code
    return 0
