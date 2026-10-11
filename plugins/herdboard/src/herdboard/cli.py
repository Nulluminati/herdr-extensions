"""Command-line interface for herdboard."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .config import load_config
from .identity import repo_identity
from .linear_cards import LINEAR_REF, issue_card
from .linear_links import LabelLink, ProjectLink
from .linear_source import LinearSource
from .linear_states import refuse
from .linear_writes import LinearWriter
from .migrate import build_preview, preview_text
from .migrate_convert import convert
from .model import HerdboardError, IoError, NotFoundError
from .promote import Promoter
from .store import CardView, Store
from .start import Starter, register_checkout
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

    def write_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--state", help="Linear workflow state, when several match the column")
        sub.add_argument("--offline", action="store_true",
                         help="Linear writes need a connection: this makes them fail")

    sub = command("add", "add a card, or with --linear a Linear issue assigned to you")
    sub.add_argument("text")
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
    sub.add_argument("--offline", action="store_true", help="read Linear from the cache only")

    sub = command("move", "move a card to a column")
    sub.add_argument("ref")
    sub.add_argument("column")
    sub.add_argument(
        "--position", choices=["top", "bottom"],
        help="place the card at the top or bottom of the column (todos default to the bottom; "
        "a Linear issue keeps Linear's order unless you ask)",
    )
    write_options(sub)

    sub = command("done", "move a card to the done column")
    sub.add_argument("ref")
    write_options(sub)

    sub = command("promote", "turn a todo into a Linear issue, its checklist into sub-issues")
    sub.add_argument("ref")
    sub.add_argument("--team", help="Linear team key")
    sub.add_argument("--project", help="Linear project name")
    write_options(sub)

    sub = command("start", "start work on a card in Herdr")
    sub.add_argument("ref")
    sub.add_argument("--agent", choices=["claude", "pi"])
    sub.add_argument("--no-agent", action="store_true")
    sub.add_argument("--repo", help="repository board name or checkout path")
    sub.add_argument("--checkout", type=Path)

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
    del data["worktree_removed"]  # Board-only status; preserve the CLI card schema.
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
        return "repo" if self.repo_board() else "global"


def _cmd_list(ctx: _Context) -> dict:
    args, store = ctx.args, ctx.store
    result = LinearResult()
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


def _require_todo_options(args: argparse.Namespace) -> None:
    if args.state:
        raise UsageError("--state works only with Linear issues")


def _cmd_move(ctx: _Context) -> dict:
    args = ctx.args
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
    )
    return starter.start(args.ref, agent=args.agent, no_agent=args.no_agent,
                         repo=args.repo, checkout=args.checkout)


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
        linear_writer=ctx.writer,
        directory=ctx.directory,
    ).run()
    return {}


COMMANDS = {
    "list": _cmd_list,
    "add": _cmd_add,
    "show": _cmd_show,
    "move": _cmd_move,
    "done": _cmd_done,
    "promote": _cmd_promote,
    "start": _cmd_start,
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
            print(f"Workspace: {result['workspace_id']}; pane: {result['pane_id']}; "
                  f"agent: {result['agent_name'] or 'none'}")
            print("Steps done: " + ", ".join(result["steps_done"]))
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
        if command == "list":
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
        _render(args.command, result, as_json)
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
