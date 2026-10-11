"""Command-line interface for herdboard."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .config import load_config
from .identity import repo_identity
from .linear_cards import LINEAR_REF, issue_card
from .linear_links import LabelLink, ProjectLink
from .linear_source import LinearSource, LinearWriteError
from .model import HerdboardError, NotFoundError
from .store import CardView, Store
from .views import INIT_MESSAGE, VIEW_NAMES, LinearResult, Views

LINEAR_WRITE_MESSAGE = "Linear writes arrive in a later step"

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

    sub = command("add", "add a card")
    sub.add_argument("text")
    sub.add_argument("--column")
    sub.add_argument("--position", choices=["top", "bottom"], default="bottom")
    sub.add_argument("--board", help="board name: inbox or a project name")

    sub = command("show", "show one card")
    sub.add_argument("ref")
    sub.add_argument("--offline", action="store_true", help="read Linear from the cache only")

    sub = command("move", "move a card to a column")
    sub.add_argument("ref")
    sub.add_argument("column")
    sub.add_argument("--position", choices=["top", "bottom"], default="bottom")

    sub = command("done", "move a card to the done column")
    sub.add_argument("ref")

    command("init", "create the board for this repository")
    sub = command("link", "link this repository's board to a Linear project")
    sub.add_argument("project")
    sub.add_argument("--label", action="append", default=[], help="label name or TEAM/label")
    sub.add_argument("--remove", action="store_true", help="remove the link")
    sub = command("board", "show the board view")
    view_options(sub)
    return parser


def _card_dict(card: CardView) -> dict:
    data = asdict(card)
    data["path"] = str(card.path) if card.path is not None else None
    return data


def _emit_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _card_line(card: CardView) -> str:
    return f"{card.ref} → {card.column} ({card.board})"


def _card_text(card: CardView) -> str:
    line = f"  {card.ref}  {card.title}"
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


def _show_text(card: CardView, notes: list[dict] | None = None) -> str:
    if card.source == "linear":
        return _show_linear_text(card)
    lines = [card.title, f"{card.board} / {card.column}", f"ref: {card.ref}"]
    if card.due:
        lines.append(f"due: {card.due}")
    if card.tags:
        lines.append("tags: " + ", ".join(card.tags))
    lines.extend(card.body)
    for note in notes or []:
        lines.append(f"--- {note['name']} ---")
        lines.append("(note not found)" if note["content"] is None else note["content"].rstrip("\n"))
    return "\n".join(lines)


class _Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config = load_config(args.config)
        self.store = Store(self.config)
        self.directory = args.directory or Path.cwd()
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
        return "repo" if self.repo_board() else "global"


def _cmd_list(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    result = LinearResult()
    if args.board:
        cards = store.list_cards([store.resolve_board(args.board)])
    else:
        view = ctx.chosen_view()
        if view == "project":
            ctx.views.resolve_project()
        cards = store.list_cards(ctx.views.paths(view))
        if args.source != "todo":
            result = ctx.views.linear(view)
            cards += result.cards
    if args.source:
        cards = [card for card in cards if card.source == args.source]
    if args.column:
        wanted = args.column.casefold()
        cards = [card for card in cards if card.column.casefold() == wanted]
    return {"cards": cards, "linear": result}


def _cmd_add(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    hint = None
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


def _cmd_show(ctx: _Context) -> dict:
    ref = ctx.args.ref
    if LINEAR_REF.match(ref):
        ctx.views.source.require_active()
        card, result = _find_linear_card(ctx, ref)
        return {"card": card, "linear": result}
    card = ctx.store.show(ref)
    return {"card": card, "notes": ctx.store.notes_for(card)}


def _refuse_linear_write(ref: str) -> None:
    if LINEAR_REF.match(ref):
        raise LinearWriteError(LINEAR_WRITE_MESSAGE)


def _cmd_move(ctx: _Context) -> dict:
    args = ctx.args
    _refuse_linear_write(args.ref)
    return {"card": ctx.store.move(args.ref, args.column, args.position)}


def _cmd_done(ctx: _Context) -> dict:
    _refuse_linear_write(ctx.args.ref)
    return {"card": ctx.store.done(ctx.args.ref)}


def _cmd_init(ctx: _Context) -> dict:
    if ctx.identity is None:
        raise HerdboardError("Not in a Git repository.")
    return {"board": ctx.store.init_board(ctx.identity)}


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
    client = ctx.views.source.client
    project = _resolve_project(client, args.project)
    link = ProjectLink(
        project["id"], project["name"], [_resolve_label(client, l) for l in args.label]
    )

    def add(links: list[ProjectLink]) -> list[ProjectLink]:
        kept = [l for l in links if l.id != link.id]
        position = next((i for i, l in enumerate(links) if l.id == link.id), len(kept))
        return kept[:position] + [link] + kept[position:]

    return {"board": board, "links": ctx.store.update_links(board, add)}


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
    ).run()
    return {}


COMMANDS = {
    "list": _cmd_list,
    "add": _cmd_add,
    "show": _cmd_show,
    "move": _cmd_move,
    "done": _cmd_done,
    "init": _cmd_init,
    "link": _cmd_link,
    "board": _cmd_board,
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
    linear = result.get("linear")
    if as_json:
        if command == "list":
            _emit_json({"cards": [_card_dict(card) for card in result["cards"]],
                        **_linear_json(linear)})
        elif command == "init":
            _emit_json({"board": str(result["board"])})
        elif command == "link":
            _emit_json({"board": str(result["board"]),
                        "links": [link.to_yaml() for link in result["links"]]})
        else:
            data = {"card": _card_dict(result["card"])}
            if result.get("notes") is not None:
                data["notes"] = result["notes"]
            if result.get("hint"):
                data["hint"] = result["hint"]
            if linear is not None:
                data.update(_linear_json(linear))
            _emit_json(data)
        return
    if command == "list":
        print(_list_text(result["cards"]))
    elif command == "show":
        print(_show_text(result["card"], result.get("notes")))
    elif command == "init":
        print(result["board"])
    elif command == "link":
        names = ", ".join(link.name for link in result["links"]) or "none"
        print(f"{result['board']}: linked projects: {names}")
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
        result = COMMANDS[args.command](_Context(args))
        _render(args.command, result, as_json)
    except HerdboardError as error:
        if as_json:
            _emit_json({"error": {"code": error.code, "message": str(error)}})
        else:
            print(f"herdboard: {error}", file=sys.stderr)
        return error.exit_code
    return 0
