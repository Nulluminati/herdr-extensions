"""Command-line options that put an unattended reviewer agent in an OS sandbox.

A reviewer may run any shell command. The sandbox lets it read the whole disk, write only its own
folder (or nothing), and reach only GitHub over the shell network. No approval judge runs: nobody
answers a prompt in a reviewer pane. Web tools (Claude WebFetch, pi web_fetch, Codex live web
search) and documentation MCP servers run outside the sandbox and only read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

# Claude's and pi's sandbox reject "*", so the shell reaches GitHub only. Codex matches them.
GITHUB_DOMAINS = ("github.com", "*.github.com", "*.githubusercontent.com")
CODEX_GITHUB_DOMAINS = ("**.github.com", "**.githubusercontent.com")
HUNK_DEFAULT_PORT = 47657
# Tools outside pi's sandbox that only read remote documentation and search results.
PI_READ_TOOLS = ("read", "grep", "find", "ls", "bash", "web_fetch", "web_search", "context7_resolve_library_id",
                 "context7_get_library_docs", "context7_get_cached_doc_raw", "code_search")


def hunk_port() -> int:
    """The port of the Hunk daemon: HUNK_MCP_PORT, else Hunk's default."""
    try:
        return int(os.environ.get("HUNK_MCP_PORT") or HUNK_DEFAULT_PORT)
    except ValueError:
        return HUNK_DEFAULT_PORT


def shared_git_folder(path: Path) -> list[Path]:
    """The Git folder that a linked worktree shares with its main checkout, when it lies outside `path`.

    Commits and branch changes write there, so the sandbox denies it with the worktree."""
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode or not result.stdout.strip():
        return []
    common = Path(result.stdout.strip()).resolve()
    return [] if common.is_relative_to(Path(path).resolve()) else [common]


def _policy(writable: Path | None, read_only: Path, domains: list[str], excluded: list[str]) -> dict:
    """The sandbox-runtime policy that Claude's `sandbox` settings and pi's `--sandbox` share."""
    filesystem = {"allowWrite": [str(writable)]} if writable else {}
    # The sandbox writes the working directory unless denyWrite names it.
    filesystem["denyWrite"] = [str(read_only), *map(str, shared_git_folder(read_only))]
    policy = {"network": {"allowedDomains": domains}, "filesystem": filesystem}
    if excluded:
        # terminal-browser splits panes through the Herdr socket, which the sandbox blocks.
        policy["excludedCommands"] = excluded
    return policy


def claude_reviewer_options(writable: Path | None, read_only: Path, browser: bool, hunk: bool) -> list[str]:
    """`--permission-mode dontAsk` and a `--settings` sandbox."""
    # Claude's sandbox sends 127.0.0.1 past its proxy, so Hunk runs outside the sandbox instead.
    excluded = ["terminal-browser *"] * browser + ["hunk session *"] * hunk
    policy = _policy(writable, read_only, list(GITHUB_DOMAINS), excluded)
    settings = {"sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                            **policy}}
    return ["--permission-mode", "dontAsk", "--settings", json.dumps(settings)]


def claude_reviewer_tools(writable: Path | None) -> list[str]:
    """The `--allowedTools` list. It takes every value after it, so the caller puts it last."""
    edits = [f"Edit(/{writable}/**)", f"Write(/{writable}/**)"] if writable else []
    return ["--allowedTools", "Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "Skill",
            "mcp__context7", "mcp__grep_app", *edits]


def pi_reviewer_options(writable: Path | None, read_only: Path, browser: bool, hunk: bool) -> list[str]:
    """`--approval-mode off`, a `--sandbox` policy and the tool list."""
    domains = list(GITHUB_DOMAINS) + [f"127.0.0.1:{hunk_port()}"] * hunk
    policy = _policy(writable, read_only, domains, ["terminal-browser *"] * browser)
    tools = ",".join(PI_READ_TOOLS + ("write",) * bool(writable))
    return ["--approval-mode", "off", "--sandbox", json.dumps(policy), "--tools", tools]


def codex_reviewer_options(name: str, writable: Path | None, hunk: bool) -> list[str]:
    """A permissions profile `name` and `-a never`. terminal-browser runs outside it by a Codex rule."""
    filesystem = '":root"="read"'
    if writable:
        filesystem += f",{json.dumps(str(writable), ensure_ascii=False)}=\"write\""
    domains = list(CODEX_GITHUB_DOMAINS) + [f"127.0.0.1:{hunk_port()}"] * hunk
    allowed = ",".join(f'{json.dumps(domain)}="allow"' for domain in domains)
    return ["-a", "never", "-c", "features.network_proxy=true", "-c", 'web_search="live"',
            "-c", f'default_permissions="{name}"',
            "-c", f"permissions.{name}.filesystem={{{filesystem}}}",
            "-c", f"permissions.{name}.network={{enabled=true,domains={{{allowed}}}}}"]
