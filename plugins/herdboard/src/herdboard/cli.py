"""Command-line interface for herdboard."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .config import load_config
from .identity import repo_identity
from .model import HerdboardError
from .store import CardView, Store

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

    sub = command("list", "list cards")
    sub.add_argument("--view", choices=["repo", "global", "all"])
    sub.add_argument("--column")
    sub.add_argument("--board", help="board name: inbox or a project name")

    sub = command("add", "add a card")
    sub.add_argument("text")
    sub.add_argument("--column")
    sub.add_argument("--position", choices=["top", "bottom"], default="bottom")
    sub.add_argument("--board", help="board name: inbox or a project name")

    sub = command("show", "show one card")
    sub.add_argument("ref")

    sub = command("move", "move a card to a column")
    sub.add_argument("ref")
    sub.add_argument("column")
    sub.add_argument("--position", choices=["top", "bottom"], default="bottom")

    sub = command("done", "move a card to the done column")
    sub.add_argument("ref")

    command("init", "create the board for this repository")
    command("board", "show the board view")
    return parser


def _card_dict(card: CardView) -> dict:
    data = asdict(card)
    data["path"] = str(card.path)
    return data


def _emit_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _card_line(card: CardView) -> str:
    return f"{card.ref} → {card.column} ({card.board})"


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
                    lines.append(f"  {card.ref}  {card.title}")
    return "\n".join(lines)


def _show_text(card: CardView, notes: list[dict] | None = None) -> str:
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
        self.store = Store(load_config(args.config))
        self.directory = args.directory or Path.cwd()
        self._identity = False

    @property
    def identity(self):
        if self._identity is False:
            self._identity = repo_identity(self.directory)
        return self._identity

    def repo_board(self) -> Path | None:
        return self.store.board_for_repo(self.identity) if self.identity else None


def _cmd_list(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    if args.board:
        paths = [store.resolve_board(args.board)]
    else:
        view = args.view
        if view is None:
            view = "repo" if ctx.repo_board() else "global"
        if view == "repo":
            board = ctx.repo_board()
            if board is None:
                raise HerdboardError(
                    "No board for this repository. Run `herdboard init` to create one."
                )
            paths = [board]
        elif view == "global":
            paths = [store.inbox_path] if store.inbox_path.is_file() else []
        else:
            paths = store.board_paths()
    cards = store.list_cards(paths)
    if args.column:
        wanted = args.column.casefold()
        cards = [card for card in cards if card.column.casefold() == wanted]
    return {"cards": cards}


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


def _cmd_show(ctx: _Context) -> dict:
    card = ctx.store.show(ctx.args.ref)
    return {"card": card, "notes": ctx.store.notes_for(card)}


def _cmd_move(ctx: _Context) -> dict:
    args = ctx.args
    return {"card": ctx.store.move(args.ref, args.column, args.position)}


def _cmd_done(ctx: _Context) -> dict:
    return {"card": ctx.store.done(ctx.args.ref)}


def _cmd_init(ctx: _Context) -> dict:
    if ctx.identity is None:
        raise HerdboardError("Not in a Git repository.")
    return {"board": ctx.store.init_board(ctx.identity)}


def _cmd_board(ctx: _Context) -> dict:
    raise _NotImplemented("the board view arrives in a later step")


class _NotImplemented(HerdboardError):
    code = "not_implemented"


COMMANDS = {
    "list": _cmd_list,
    "add": _cmd_add,
    "show": _cmd_show,
    "move": _cmd_move,
    "done": _cmd_done,
    "init": _cmd_init,
    "board": _cmd_board,
}


def _render(command: str, result: dict, as_json: bool) -> None:
    if as_json:
        if command == "list":
            _emit_json({"cards": [_card_dict(card) for card in result["cards"]]})
        elif command == "init":
            _emit_json({"board": str(result["board"])})
        else:
            data = {"card": _card_dict(result["card"])}
            if result.get("notes") is not None:
                data["notes"] = result["notes"]
            if result.get("hint"):
                data["hint"] = result["hint"]
            _emit_json(data)
        return
    if command == "list":
        print(_list_text(result["cards"]))
    elif command == "show":
        print(_show_text(result["card"], result.get("notes")))
    elif command == "init":
        print(result["board"])
    else:
        print(_card_line(result["card"]))
        if result.get("hint"):
            print(result["hint"])


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
