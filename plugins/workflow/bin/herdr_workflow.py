# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Launch managed Herdr Plus workflows using the current repository or zoxide."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

PLUGIN_ID = "cloudmanic.herdr-plus"
DIRECTORY_VARIABLE = "HERDR_WORKFLOW_DIRECTORY"
DIRECTORY_PROMPT = "Enter another directory..."
WORKFLOW_PROFILES = ("implementation", "top-level", "local-review", "pr-review")


class WorkflowError(RuntimeError):
    """A failure that the launcher can explain without a traceback."""


def command(argv: list[str], env: dict[str, str] | None = None):
    try:
        return subprocess.run(argv, env=env, check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise WorkflowError(f"Required program not found: {argv[0]}") from error
    except subprocess.CalledProcessError as error:
        raise WorkflowError(error.stderr.strip() or f"{argv[0]} failed") from error
    except subprocess.TimeoutExpired as error:
        raise WorkflowError(f"{argv[0]} did not finish within 120 seconds") from error


def current_directory() -> Path:
    if "HERDR_PLUS_WORKDIR" in os.environ:
        value = os.environ["HERDR_PLUS_WORKDIR"]
        if not value:
            raise WorkflowError("Herdr did not supply the invoking directory")
        return Path(value).expanduser()
    return Path.cwd()


def repository_root(directory: Path) -> Path:
    directory = directory.expanduser().resolve()
    if any(ord(character) < 32 or ord(character) == 127 for character in str(directory)):
        raise WorkflowError("Directory names must not contain control characters")
    if not directory.is_dir():
        raise WorkflowError(f"Directory does not exist: {directory}")
    result = command(["git", "-C", str(directory), "rev-parse", "--show-toplevel"])
    return Path(result.stdout.rstrip("\n")).resolve()


def display_path(directory: Path) -> str:
    try:
        relative = directory.relative_to(Path.home())
        return "~" if str(relative) == "." else f"~/{relative}"
    except ValueError:
        return str(directory)


def workspace_label(profile: str, directory: Path) -> str:
    # Include the path because different repositories can share a basename.
    return f"{profile} · {display_path(directory)}"


def list_repositories() -> list[Path]:
    if not shutil.which("git"):
        raise WorkflowError("Git is required to identify repositories")
    history = command(["zoxide", "query", "--list"]).stdout.splitlines()
    candidates = [current_directory(), *(Path(value) for value in history)]
    repositories = []
    seen = set()
    for candidate in candidates:
        try:
            root = repository_root(candidate)
        except WorkflowError:
            continue
        value = str(root)
        # Dynamic Quick Actions uses one line per value, with tabs as separators.
        if any(character in value for character in "\t\r\n") or value != value.strip():
            continue
        if root not in seen:
            seen.add(root)
            repositories.append(root)
    return repositories


def directory_completions(text: str) -> list[str]:
    if text == "~":
        return ["~/"]
    prefix, partial = os.path.split(text)
    try:
        parent = Path(prefix or ".").expanduser()
        if not parent.is_absolute():
            parent = current_directory() / parent
        return sorted(
            os.path.join(prefix, child.name) + os.sep
            for child in parent.iterdir()
            if child.is_dir()
            and child.name.startswith(partial)
            and (partial.startswith(".") or not child.name.startswith("."))
        )
    except (OSError, RuntimeError):
        return []


@contextmanager
def terminal_stdio():
    if sys.stdin.isatty() and sys.stdout.isatty():
        yield
        return
    # Herdr Plus actions forward output, but not stdin. Readline needs a terminal
    # on descriptors 0 and 1, rather than a separate file object's readline().
    try:
        terminal = os.open("/dev/tty", os.O_RDWR)
    except OSError as error:
        raise WorkflowError("Directory entry needs an interactive Herdr pane") from error
    saved_input, saved_output = os.dup(0), os.dup(1)
    try:
        sys.stdout.flush()
        os.dup2(terminal, 0)
        os.dup2(terminal, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved_input, 0)
        os.dup2(saved_output, 1)
        for descriptor in (terminal, saved_input, saved_output):
            os.close(descriptor)


def prompt_directory(message: str = "Repository directory (Tab completes; empty cancels): ") -> Path | None:
    # Libedit detects terminal capabilities when readline first initializes.
    with terminal_stdio():
        try:
            import readline
        except ImportError as error:
            raise WorkflowError("Python readline is required for directory completion") from error
        original = readline.get_completer()
        delimiters = readline.get_completer_delims()
        candidates = []

        def complete(text, state):
            if state == 0:
                candidates[:] = directory_completions(text)
            return candidates[state] if state < len(candidates) else None

        readline.set_completer(complete)
        readline.set_completer_delims("\t\n")
        binding = "bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete"
        readline.parse_and_bind(binding)
        try:
            try:
                value = input(message).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not value:
                return None
            directory = Path(value).expanduser()
            return directory if directory.is_absolute() else current_directory() / directory
        finally:
            readline.set_completer(original)
            readline.set_completer_delims(delimiters)


def template_data(text: str) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise WorkflowError(f"Invalid workflow template: {error}") from error
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise WorkflowError("Workflow template needs a name")
    if not isinstance(data.get("working_dir"), str) or not data.get("tabs"):
        raise WorkflowError("Workflow template needs working_dir and tabs")
    return data


def render_template(text: str, label: str) -> str:
    original = template_data(text)
    table = re.search(r"(?m)^\s*\[\[tabs\]\]\s*(?:#.*)?$", text)
    if table is None:
        raise WorkflowError("Workflow template needs a [[tabs]] table")
    header, body = text[: table.start()], text[table.start() :]
    values = {"name": label, "working_dir": f"${DIRECTORY_VARIABLE}"}
    for key, value in values.items():
        # Only replace single-line root strings; preserve comments and pane data.
        pattern = rf"(?m)^({key}\s*=\s*)(\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*')([ \t]*(?:#.*)?)$"
        header, count = re.subn(
            pattern,
            lambda match, value=value: match[1] + json.dumps(value, ensure_ascii=False) + match[3],
            header,
        )
        if count != 1:
            raise WorkflowError(f"Template {key} must be one single-line string")
    rendered = header + body
    if template_data(rendered) != dict(original, **values):
        raise WorkflowError("Workflow rendering changed unrelated template data")
    return rendered


def herdr_command(arguments: list[str]):
    executable = os.environ.get("HERDR_BIN_PATH") or "herdr"
    return command([executable, *arguments])


def herdr_result(arguments: list[str]) -> dict:
    output = herdr_command(arguments).stdout
    try:
        result = json.loads(output)["result"]
        if not isinstance(result, dict):
            raise ValueError("Expected an object")
        return result
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise WorkflowError("Herdr returned an invalid response") from error


def herdr_plus_config_dir() -> Path:
    # The templates and prompts are herdr-plus config. Ask Herdr rather than read
    # HERDR_PLUGIN_CONFIG_DIR: when this plugin runs the launcher, that names its own directory.
    value = herdr_command(["plugin", "config-dir", PLUGIN_ID]).stdout.strip()
    if not value or not Path(value).is_absolute():
        raise WorkflowError("Herdr did not return the Herdr Plus config directory")
    return Path(value)


def herdr_workspaces() -> list[dict]:
    workspaces = herdr_result(["workspace", "list"]).get("workspaces")
    if not isinstance(workspaces, list):
        raise WorkflowError("Herdr did not return a workspace list")
    return workspaces


def report_sidebar(workspace_id: str, profile: str, directory: Path) -> None:
    if profile not in WORKFLOW_PROFILES:
        return
    herdr_command(
        [
            "workspace",
            "report-metadata",
            workspace_id,
            "--source",
            "herdr-workflow",
            "--token",
            f"workflow_project={directory.name or display_path(directory)}",
            "--token",
            f"workflow_profile={profile}",
        ]
    )


def refresh_sidebar() -> int:
    if os.environ.get("HERDR_ENV") != "1":
        raise WorkflowError("Run workflow actions inside Herdr")
    count = 0
    for workspace in herdr_workspaces():
        label = workspace.get("label")
        workspace_id = workspace.get("workspace_id")
        if not isinstance(label, str) or not workspace_id:
            continue
        profile, separator, value = label.partition(" · ")
        if not separator or profile not in WORKFLOW_PROFILES:
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            continue
        try:
            directory = Path(value).expanduser()
            if not directory.is_absolute():
                continue
            directory = directory.resolve()
        except (OSError, RuntimeError):
            continue
        if workspace_label(profile, directory) != label:
            continue
        report_sidebar(workspace_id, profile, directory)
        count += 1
    return count


def workflow_template(profile: str) -> str:
    if os.environ.get("HERDR_ENV") != "1":
        raise WorkflowError("Run workflow actions inside Herdr")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", profile):
        raise WorkflowError("Invalid workflow profile name")
    path = herdr_plus_config_dir() / "projects" / f"{profile}.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkflowError(f"Cannot read workflow template: {path}") from error
    template_data(text)
    if not shutil.which("zoxide"):
        raise WorkflowError("Zoxide is required before opening a workflow")
    return text


def open_workflow(profile: str, directory: Path | None = None) -> str:
    text = workflow_template(profile)
    repository = repository_root(directory if directory is not None else current_directory())
    label = workspace_label(profile, repository)
    rendered = render_template(text, label)
    workspaces = herdr_workspaces()
    for workspace in workspaces:
        if workspace.get("label") == label and workspace.get("workspace_id"):
            report_sidebar(workspace["workspace_id"], profile, repository)
            herdr_result(["workspace", "focus", workspace["workspace_id"]])
            command(["zoxide", "add", "--", str(repository)])
            return label

    plugins = herdr_result(["plugin", "list", "--plugin", PLUGIN_ID, "--json"]).get("plugins", [])
    if len(plugins) != 1 or not plugins[0].get("enabled"):
        raise WorkflowError("Herdr Plus must be installed and enabled")
    root = plugins[0].get("plugin_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise WorkflowError("Herdr Plus has no valid installation directory")
    executable = Path(root) / "bin" / "herdr-plus"
    if not executable.is_file():
        raise WorkflowError("Herdr Plus executable is missing")

    with tempfile.TemporaryDirectory(prefix="herdr-workflow-") as temporary:
        config = Path(temporary)
        projects = config / "projects"
        projects.mkdir(mode=0o700)
        template = projects / f"{profile}.toml"
        template.write_text(rendered, encoding="utf-8")
        template.chmod(0o600)
        # Environment expansion happens once, preserving literal dollar signs in paths.
        env = dict(os.environ, HERDR_PLUGIN_CONFIG_DIR=str(config))
        env[DIRECTORY_VARIABLE] = str(repository)
        command([str(executable), "open", label], env)
    if profile in WORKFLOW_PROFILES:
        previous_ids = {workspace.get("workspace_id") for workspace in workspaces}
        created = [
            workspace
            for workspace in herdr_workspaces()
            if workspace.get("label") == label
            and workspace.get("workspace_id")
            and workspace["workspace_id"] not in previous_ids
        ]
        if len(created) != 1:
            raise WorkflowError("Workflow opened, but Herdr did not identify its new workspace")
        report_sidebar(created[0]["workspace_id"], profile, repository)
    command(["zoxide", "add", "--", str(repository)])
    return label


def create_project(directory: Path) -> str:
    if not shutil.which("git"):
        raise WorkflowError("Git is required to create a project")
    workflow_template("implementation")
    # These variables can redirect Git metadata outside the new project.
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        if variable in os.environ:
            raise WorkflowError(f"Unset {variable} before creating a project")
    directory = directory.expanduser()
    if not directory.is_absolute():
        directory = current_directory() / directory
    if any(ord(character) < 32 or ord(character) == 127 for character in str(directory)):
        raise WorkflowError("Directory names must not contain control characters")
    if directory.exists() or directory.is_symlink():
        raise WorkflowError(f"Project path already exists: {directory}")
    try:
        parent = directory.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkflowError(f"Parent directory must exist: {directory.parent}") from error
    if not parent.is_dir():
        raise WorkflowError(f"Parent path is not a directory: {parent}")
    directory = parent / directory.name
    # mkdir refuses a path another process creates after the existence check.
    directory.mkdir()
    try:
        command(["git", "init", "--quiet", str(directory)])
    except WorkflowError as error:
        raise WorkflowError(f"Cannot initialize Git at {directory}. Directory remains: {error}") from error
    try:
        return open_workflow("implementation", directory)
    except (WorkflowError, OSError, UnicodeError) as error:
        raise WorkflowError(f"Repository remains at {directory}. Cannot open Implementation: {error}") from error


def parse_pr_reference(value: str) -> tuple[str, str]:
    value = value.strip()
    repository = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"
    number = r"[1-9][0-9]{0,9}"
    match = re.fullmatch(rf"({repository})#({number})", value)
    if match is None:
        match = re.fullmatch(
            rf"https://github\.com/({repository})/pull/({number})/?(?:[?#][^\r\n]*)?",
            value,
        )
    if match is None or match[1].split("/", 1)[1] in (".", ".."):
        raise WorkflowError("Enter a GitHub pull request URL or owner/repo#number")
    return match[1], match[2]


def launch_review_agent(profile: str, agent: str) -> int:
    if profile not in ("local-review", "pr-review") or agent not in ("pi", "claude"):
        raise WorkflowError("Unknown review profile or agent")
    prompts = herdr_plus_config_dir() / "prompts"
    text = "\n\n".join((prompts / filename).read_text(encoding="utf-8") for filename in ("review.md", f"{profile}.md"))
    os.execvp(agent, [agent, "--append-system-prompt", text])
    return 0


def stack_claude_pane() -> None:
    if os.environ.get("HERDR_ENV") != "1":
        raise WorkflowError("Run layout startup inside Herdr")
    current = herdr_result(["pane", "current", "--current"]).get("pane")
    if not isinstance(current, dict) or not current.get("pane_id"):
        raise WorkflowError("Herdr did not identify the Claude startup pane")
    layout = herdr_result(["pane", "layout", "--pane", current["pane_id"]]).get("layout")
    if not isinstance(layout, dict):
        raise WorkflowError("Herdr did not return the startup pane layout")
    area, panes = layout.get("area"), layout.get("panes")
    if (
        not isinstance(area, dict)
        or not isinstance(panes, list)
        or len(panes) != 3
        or not layout.get("tab_id")
        or layout["tab_id"] != current.get("tab_id")
        or layout.get("workspace_id") != current.get("workspace_id")
    ):
        raise WorkflowError("Claude startup needs the three-pane workflow tab")
    if any(not isinstance(pane, dict) or not isinstance(pane.get("rect"), dict) for pane in panes):
        raise WorkflowError("Herdr returned invalid pane geometry")
    targets = [
        pane
        for pane in panes
        if pane.get("pane_id") != current["pane_id"]
        and pane["rect"].get("x") == area.get("x")
        and pane["rect"].get("y") == area.get("y")
    ]
    sources = [pane for pane in panes if pane.get("pane_id") == current["pane_id"]]
    if len(targets) != 1 or not targets[0].get("pane_id") or len(sources) != 1:
        raise WorkflowError("Herdr did not identify Pi and Claude's startup panes")
    target, source = targets[0], sources[0]
    if source["rect"].get("x") == target["rect"].get("x"):
        return
    # Herdr Plus splits from the previous pane, and Herdr's same-tab move is a
    # no-op. Detach Claude into a temporary tab, then stack it below Pi. Moving
    # it back closes the empty temporary tab and preserves the pane process.
    detached = (
        herdr_result(
            [
                "pane",
                "move",
                current["pane_id"],
                "--new-tab",
                "--workspace",
                current["workspace_id"],
                "--label",
                "Workflow layout startup",
                "--no-focus",
            ]
        )
        .get("move_result", {})
        .get("pane", {})
    )
    if not isinstance(detached, dict) or not detached.get("pane_id"):
        raise WorkflowError("Herdr did not identify the detached Claude pane")
    herdr_result(
        [
            "pane",
            "move",
            detached["pane_id"],
            "--tab",
            layout["tab_id"],
            "--split",
            "down",
            "--target-pane",
            target["pane_id"],
            "--ratio",
            "0.5",
            "--no-focus",
        ]
    )


def launch_layout_claude(profile: str) -> int:
    if profile not in WORKFLOW_PROFILES:
        raise WorkflowError("Unknown workflow profile")
    stack_claude_pane()
    if profile == "implementation":
        os.execvp("claude", ["claude"])
        return 0
    return launch_review_agent(profile, "claude")


def launch_review_hunk(profile: str, reference: str | None = None) -> int:
    if profile not in ("local-review", "pr-review"):
        raise WorkflowError("Unknown review profile")
    env = dict(os.environ)
    if profile == "local-review":
        # A local working-tree diff must not fall back to a branch's remote PR.
        env.update(GH_PR_NUMBER="", GH_PR_REPO="")
        argv = ["hunk", "diff", "--watch"]
    else:
        if reference is None:
            try:
                reference = input("Pull request URL or owner/repo#number (empty cancels): ")
            except (EOFError, KeyboardInterrupt):
                return 0
        if not reference.strip():
            return 0
        repository, number = parse_pr_reference(reference)
        if env.get("GH_HOST", "github.com") not in ("", "github.com"):
            raise WorkflowError("This review launcher supports github.com, not GitHub Enterprise")
        env.update(GH_PR_NUMBER=number, GH_PR_REPO=repository)
        if not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN"):
            token = command(["gh", "auth", "token", "--hostname", "github.com"]).stdout.strip()
            if not token:
                raise WorkflowError("Sign in with gh auth login before pull request review")
            env["GH_TOKEN"] = token
        argv = ["hunk", "gh", "pr", f"{repository}#{number}"]
    os.execvpe("hunk", argv, env)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list-repositories")
    commands.add_parser("create-project")
    commands.add_parser("refresh-sidebar")
    open_parser = commands.add_parser("open")
    open_parser.add_argument("profile")
    open_parser.add_argument("directory", nargs="?", type=Path)
    agent_parser = commands.add_parser("review-agent")
    agent_parser.add_argument("profile", choices=("local-review", "pr-review"))
    agent_parser.add_argument("agent", choices=("pi", "claude"))
    claude_parser = commands.add_parser("layout-claude")
    claude_parser.add_argument("profile", choices=WORKFLOW_PROFILES)
    hunk_parser = commands.add_parser("review-hunk")
    hunk_parser.add_argument("profile", choices=("local-review", "pr-review"))
    hunk_parser.add_argument("reference", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.action == "list-repositories":
            print(f"{DIRECTORY_PROMPT}\tEnter a path; Tab completes directories")
            for index, repository in enumerate(list_repositories()):
                description = "Current or visited repository" if index == 0 else "Visited repository"
                print(f"{display_path(repository)}\t{description}")
        elif args.action == "refresh-sidebar":
            print(f"Updated sidebar entries: {refresh_sidebar()}")
        elif args.action == "create-project":
            directory = prompt_directory("New project directory (Tab completes parents; empty cancels): ")
            if directory is None:
                return 0
            label = create_project(directory)
            print(f"Workflow: {label}")
        elif args.action == "review-agent":
            return launch_review_agent(args.profile, args.agent)
        elif args.action == "review-hunk":
            return launch_review_hunk(args.profile, args.reference)
        elif args.action == "layout-claude":
            return launch_layout_claude(args.profile)
        else:
            directory = args.directory
            if directory is not None and str(directory) == DIRECTORY_PROMPT:
                directory = prompt_directory()
                if directory is None:
                    return 0
            label = open_workflow(args.profile, directory)
            print(f"Workflow: {label}")
        return 0
    except (WorkflowError, OSError, UnicodeError) as error:
        print(f"herdr-workflow: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
