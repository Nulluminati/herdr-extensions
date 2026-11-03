# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Ask a second agent (Claude, pi or Codex) to review changes in a Herdr pane.

The changes are the uncommitted ones. When there are none, they are the commits
on the branch since its upstream, so committed agent work can be reviewed too.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REVIEWERS = {"claude": "pi", "pi": "codex", "codex": "claude"}
# A review of a large diff can take a long time; the worker runs in the background.
REVIEW_TIMEOUT_MS = 30 * 60 * 1000
POLL_SECONDS = 5
# Claude's and pi's sandbox reject "*", so the shell reaches GitHub only. Codex matches them.
GITHUB_DOMAINS = ("github.com", "*.github.com", "*.githubusercontent.com")
CODEX_GITHUB_DOMAINS = ("**.github.com", "**.githubusercontent.com")
# Credential files and folders in the home folder that the reviewer may not read. gh keeps its token.
SECRETS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".netrc",
    ".docker/config.json",
    ".local/share/keyrings",
    ".password-store",
    ".claude/.credentials.json",
    ".codex/auth.json",
    ".pi/agent/auth.json",
)
# Tools outside pi's sandbox that only read remote documentation and search results.
PI_READ_TOOLS = (
    "read",
    "grep",
    "find",
    "ls",
    "bash",
    "web_fetch",
    "web_search",
    "context7_resolve_library_id",
    "context7_get_library_docs",
    "context7_get_cached_doc_raw",
    "code_search",
)


class ReviewError(RuntimeError):
    """A failure that the script can explain without a traceback."""


def command(argv: list[str], timeout: int = 120):
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise ReviewError(f"Required program not found: {argv[0]}") from error
    except subprocess.CalledProcessError as error:
        raise ReviewError(error.stderr.strip() or f"{argv[0]} failed") from error
    except subprocess.TimeoutExpired as error:
        raise ReviewError(f"{argv[0]} did not finish within {timeout} seconds") from error


def herdr(*arguments: str, timeout: int = 120):
    try:
        return command([os.environ.get("HERDR_BIN_PATH", "herdr"), *arguments], timeout=timeout)
    except ReviewError as error:
        if arguments[:2] != ("agent", "start") or '"agent_name_not_found"' not in str(error):
            raise
        kind = arguments[arguments.index("--kind") + 1] if "--kind" in arguments else None
        pane = arguments[arguments.index("--pane") + 1] if "--pane" in arguments else None
        if kind != "codex" or not pane:
            raise
        # Herdr can clear Codex's launch name while the same agent still runs.
        try:
            found = herdr("agent", "get", pane)
            data = json.loads(found.stdout)
        except (ReviewError, ValueError, TypeError):
            raise error from None
        result = data.get("result") if isinstance(data, dict) else None
        agent = result.get("agent") if isinstance(result, dict) else None
        if isinstance(agent, dict) and agent.get("agent") == kind:
            return found
        raise


def reviewer_for(agent: str) -> str:
    if agent not in REVIEWERS:
        raise ReviewError(f"Peer review supports Claude, pi and Codex, not {agent}")
    return REVIEWERS[agent]


def requester_pane(explicit: str | None) -> str:
    if explicit:
        return explicit
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    pane = context.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise ReviewError("Run peer review inside a Herdr pane")
    return pane


def review_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(state) / "peer-review"


def secret_paths() -> list[str]:
    return [str(Path.home() / secret) for secret in SECRETS]


def secret_env_names() -> list[str]:
    """Environment variables whose names look like credentials. Claude's sandbox takes exact names only."""
    return sorted(
        name
        for name in os.environ
        if re.fullmatch(r"[A-Za-z_]\w*", name, re.ASCII)
        and re.search(r"KEY|SECRET|TOKEN|PASSWORD", name, re.IGNORECASE)
    )


def shared_git_folder(path: Path) -> list[Path]:
    """The Git folder that a linked worktree shares with its main checkout, when it lies outside `path`."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode or not result.stdout.strip():
        return []
    common = Path(result.stdout.strip()).resolve()
    return [] if common.is_relative_to(Path(path).resolve()) else [common]


def sandbox_policy(review: Path, repository: Path) -> dict:
    """The sandbox-runtime policy for Claude's `sandbox` settings and pi's `--sandbox`.

    The same shape as herdboard's reviewer sandbox (herdboard/sandbox.py), without the browser.
    The sandbox writes the working directory unless denyWrite names it."""
    return {
        "network": {"allowedDomains": list(GITHUB_DOMAINS)},
        "filesystem": {
            "allowWrite": [str(review)],
            "denyWrite": [str(repository), *map(str, shared_git_folder(repository))],
            "denyRead": secret_paths(),
        },
    }


def reviewer_arguments(reviewer: str, review: Path, repository: Path) -> list[str]:
    # The review instruction is the first message, not typed into the pane later:
    # typed text can land in a startup prompt (such as Claude's folder trust check).
    prompt = (
        f"Read {review / 'request.md'} and do the review it asks for. "
        f"Write your findings to {review / 'findings.md'} and nowhere else."
    )
    # The reviewer may run any shell command in an OS sandbox: it reads the disk except SECRETS, writes only
    # the review folder and reaches only GitHub. Its shell gets no credential variables.
    # Nobody answers prompts, so no approval judge runs.
    if reviewer == "pi":
        tools = ",".join((*PI_READ_TOOLS, "write"))
        return [
            "--approval-mode",
            "off",
            "--sandbox",
            json.dumps(sandbox_policy(review, repository)),
            "--tools",
            tools,
            "--",
            prompt,
        ]
    if reviewer == "codex":
        path = json.dumps(str(review), ensure_ascii=False)
        secrets = "".join(f',{json.dumps(secret, ensure_ascii=False)}="deny"' for secret in secret_paths())
        domains = ",".join(f'{json.dumps(domain)}="allow"' for domain in CODEX_GITHUB_DOMAINS)
        # Codex's default excludes drop names with KEY, SECRET or TOKEN from the shell; PASSWORD is added.
        return [
            "-a",
            "never",
            "-c",
            "features.network_proxy=true",
            "-c",
            'web_search="live"',
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
            "-c",
            'shell_environment_policy.exclude=["*PASSWORD*"]',
            "-c",
            'default_permissions="hb-review"',
            "-c",
            f'permissions.hb-review.filesystem={{":root"="read",{path}="write"{secrets}}}',
            "-c",
            f"permissions.hb-review.network={{enabled=true,domains={{{domains}}}}}",
            prompt,
        ]
    # Read, Grep and Glob run outside the sandbox, so a permission rule keeps them from SECRETS too.
    deny = [rule for secret in secret_paths() for rule in (f"Read(/{secret})", f"Read(/{secret}/**)")]
    settings = {
        "permissions": {"deny": deny},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "credentials": {"envVars": [{"name": name, "mode": "deny"} for name in secret_env_names()]},
            **sandbox_policy(review, repository),
        },
    }
    # The prompt goes first because --allowedTools takes every value after it.
    return [
        prompt,
        "--permission-mode",
        "dontAsk",
        "--settings",
        json.dumps(settings),
        "--allowedTools",
        "Read",
        "Grep",
        "Glob",
        "Bash",
        "WebFetch",
        "WebSearch",
        "Skill",
        "mcp__context7",
        "mcp__grep_app",
        f"Edit(/{review}/**)",
        f"Write(/{review}/**)",
    ]


def git_output(repository: Path, *arguments: str) -> str | None:
    """The output of a Git command, or None when Git fails."""
    try:
        return command(["git", "-C", str(repository), *arguments]).stdout
    except ReviewError:
        return None


def changes(repository: Path, requester: str) -> tuple[str, list[str], str]:
    """The diff to review, the untracked files, and a sentence that says what the diff holds."""
    diff = command(["git", "-C", str(repository), "diff", "HEAD"]).stdout
    untracked = command(
        ["git", "-C", str(repository), "ls-files", "--others", "--exclude-standard"]
    ).stdout.splitlines()
    if diff or untracked:
        return diff, untracked, f"{requester} changed the repository at `{repository}`."
    upstream = (git_output(repository, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()
    if not upstream:
        raise ReviewError(f"No uncommitted changes in {repository}, and its branch has no upstream to compare with")
    branch = command(["git", "-C", str(repository), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    diff = command(["git", "-C", str(repository), "diff", f"{upstream}...HEAD"]).stdout
    if not diff:
        raise ReviewError(f"No uncommitted changes, and no commits on {branch} since {upstream}, in {repository}")
    return diff, [], (f"{requester} made commits on `{branch}` since `{upstream}` in the repository at `{repository}`.")


def create_review() -> Path:
    """Make a review folder that no other request in the same second holds."""
    root = review_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReviewError(f"{root}: cannot make the review root: {error}") from error
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Two requests can fall in the same second, so the folder name takes a counter.
    for attempt in range(1, 100):
        review = root / (stamp if attempt == 1 else f"{stamp}-{attempt}")
        try:
            review.mkdir()
        except FileExistsError:
            continue
        except OSError as error:
            raise ReviewError(f"{review}: cannot make the review folder: {error}") from error
        return review
    raise ReviewError(f"{root}: cannot make a review folder for {stamp}")


def write_request(repository: Path, requester: str) -> Path:
    """Write the request files into a new review folder and return that folder."""
    diff, untracked, summary = changes(repository, requester)

    review = create_review()
    (review / "diff.patch").write_text(diff)
    untracked_list = "\n".join(f"- {path}" for path in untracked) or "- (none)"
    (review / "request.md").write_text(f"""# Peer review request

{summary}

- Diff of tracked files: `{review / "diff.patch"}`
- New untracked files (read them from the repository):
{untracked_list}

Review these changes for bugs, security problems and missing tests.
Read the surrounding code in the repository when you need context.
Do not change any file in the repository.
You may run any read-only command, such as `git log`, `git show`, `grep` or `gh`.
Do not run the project's code, tests or builds from the shell. Do not start a server.
Do not change Git state: no checkout, commit, reset or push.
Do not use `terminal-browser`.

Write your findings to `{review / "findings.md"}` in this format:

    ## Confirmed problems
    - [high|medium|low] path:line: problem. Suggested fix.

    ## Risks and questions
    - path:line: concern.

If you find nothing, write "No findings."
""")
    return review


def request(pane_option: str | None) -> int:
    pane = requester_pane(pane_option)
    agent = json.loads(herdr("agent", "get", pane).stdout)["result"]["agent"]
    requester = agent["agent"]
    reviewer = reviewer_for(requester)
    repository = Path(command(["git", "-C", agent["cwd"], "rev-parse", "--show-toplevel"]).stdout.rstrip("\n"))

    review = write_request(repository, requester)

    split = herdr("pane", "split", pane, "--direction", "right", "--cwd", str(repository), "--no-focus")
    reviewer_pane = json.loads(split.stdout)["result"]["pane"]["pane_id"]
    try:
        herdr(
            "agent",
            "start",
            f"review-{review.name}",
            "--kind",
            reviewer,
            "--pane",
            reviewer_pane,
            "--",
            *reviewer_arguments(reviewer, review, repository),
        )
    except ReviewError as error:
        # A startup prompt (such as Claude's folder trust check) is for the user to answer,
        # and a reviewer busy with its first message never looks ready to Herdr.
        # The worker watches the reviewer in both cases.
        if '"agent_not_ready"' not in str(error) and '"timeout"' not in str(error):
            raise
        print(
            f"{reviewer} has not reported ready in pane {reviewer_pane}. "
            "If it shows a prompt there, answer it and the review continues."
        )
    start_worker(
        [
            "deliver",
            "--review",
            str(review),
            "--requester",
            pane,
            "--reviewer-pane",
            reviewer_pane,
            "--reviewer",
            reviewer,
        ],
        review / "worker.log",
    )
    print(f"{reviewer} is reviewing in pane {reviewer_pane}. Review files: {review}")
    print("The findings arrive as a new prompt when the review is done.")
    return 0


def start_worker(arguments: list[str], log: Path) -> None:
    # Detach so the caller (a plugin action or an agent's shell tool) returns at once.
    with log.open("a") as output:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def reviewer_status(pane: str) -> str | None:
    try:
        return json.loads(herdr("agent", "get", pane).stdout)["result"]["agent"]["agent_status"]
    except ReviewError as error:
        if "agent_not_found" in str(error):
            return None
        raise


def wait_for_findings(review: Path, reviewer_pane: str, reviewer: str) -> Path:
    findings = review / "findings.md"
    deadline = time.monotonic() + REVIEW_TIMEOUT_MS / 1000
    seen = False
    while True:
        status = reviewer_status(reviewer_pane)
        # Herdr can take a moment to detect a new agent, so only a reviewer that was seen can stop.
        seen = seen or status is not None
        if seen and status is None and not findings.exists():
            raise ReviewError(f"The {reviewer} reviewer stopped before it wrote {findings}")
        if findings.exists() and status in (None, "idle", "done"):
            return findings
        if time.monotonic() >= deadline:
            raise ReviewError(f"The {reviewer} review did not finish in {REVIEW_TIMEOUT_MS // 60000} minutes")
        time.sleep(POLL_SECONDS)


def deliver(review: Path, requester: str, reviewer_pane: str, reviewer: str) -> int:
    try:
        findings = wait_for_findings(review, reviewer_pane, reviewer)
        message = (
            f"Peer review by {reviewer} is ready: {findings}. Read it. "
            "For each finding, say if you agree or disagree, and why. "
            "Do not change any code. Wait for the user to decide what to fix."
        )
        status = 0
    except ReviewError as error:
        print(f"peer-review: {error}", file=sys.stderr)
        message = (
            f"Peer review failed: {error}. Tell the user. "
            f"The reviewer pane is {reviewer_pane} and the log is {review / 'worker.log'}."
        )
        status = 1
    herdr("agent", "wait", requester, "--until", "idle", timeout=REVIEW_TIMEOUT_MS // 1000)
    herdr("agent", "prompt", requester, message)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    request_parser = commands.add_parser(
        "request", help="start a review of uncommitted changes, else of the commits since the upstream"
    )
    request_parser.add_argument("--pane", help="pane of the requesting agent (default: this pane)")
    deliver_parser = commands.add_parser("deliver", help=argparse.SUPPRESS)
    deliver_parser.add_argument("--review", type=Path, required=True)
    deliver_parser.add_argument("--requester", required=True)
    deliver_parser.add_argument("--reviewer-pane", required=True)
    deliver_parser.add_argument("--reviewer", required=True)
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "request":
            return request(arguments.pane)
        return deliver(arguments.review, arguments.requester, arguments.reviewer_pane, arguments.reviewer)
    except ReviewError as error:
        print(f"peer-review: {error}", file=sys.stderr)
        if arguments.command == "request":
            notify_failure(str(error))
        return 1


def notify_failure(message: str) -> None:
    """Show a failed request in Herdr: a plugin action's error output goes only to the plugin log."""
    try:
        herdr("notification", "show", "Peer review failed", "--body", message)
    except ReviewError as error:
        print(f"peer-review: cannot show a notification: {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
