# Peer review

Have a second coding agent review your agent's work inside Herdr. Claude's work goes to pi, pi's work goes to Codex, and Codex's work goes to Claude.

The reviewer opens in a split next to your agent. It writes its findings to a file. Your agent then gets a prompt to read them, says for each finding whether it agrees or disagrees, and waits for you. Neither agent changes code. One request is one review round.

## What gets reviewed

1. The uncommitted changes: tracked changes against `HEAD`, and new untracked files.
2. When there are none, the commits on the branch since its upstream (`git diff @{u}...HEAD`).

When there is nothing to review, or the branch has no upstream, the request fails and Herdr shows a notification.

## Install

Requirements: Herdr 0.9 or later and Python 3.11 or later. Install the agents for your pairing: Claude Code, pi or Codex. The agent skill also needs `jq`.

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/peer-review
npx skills add Nulluminati/herdr-extensions@peer-review -g -y
```

To work on the plugin from a clone, link it instead, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/peer-review
```

Bind the action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+p"
type = "plugin_action"
command = "nulluminati.peer-review.request"
description = "peer review: ask the other agent to review the focused agent's changes"
```

## Use

Do one of these:

- Press the bound key in a Claude, pi or Codex pane.
- Ask the agent for a review, for example "get a peer review from pi". The skill runs the request.

The request returns at once. A background worker waits for the findings and then prompts your agent.

Each review has a folder, `~/.local/state/peer-review/<timestamp>/`. A second request in the same second adds a counter, as in `<timestamp>-2`:

| File | Content |
|---|---|
| `request.md` | The instructions for the reviewer |
| `diff.patch` | The change under review |
| `findings.md` | The reviewer's findings |
| `worker.log` | Errors from the background worker |

## Reviewer permissions

Each reviewer may run any shell command inside an operating system sandbox. The sandbox reads the whole disk, writes only the review folder, blocks writes to the repository, and lets the shell reach only GitHub (`github.com`, its subdomains and `*.githubusercontent.com`). No approval prompts appear: nobody answers them in the reviewer pane. The request tells the reviewer not to run the project's code, not to change Git state and not to use `terminal-browser`. The plugin passes the sandbox for one run; it does not change your agent config.

- Claude runs in `dontAsk` mode with a `--settings` sandbox (`failIfUnavailable`, no unsandboxed commands). Its allowed tools are `Read`, `Grep`, `Glob`, `Bash`, `WebFetch`, `WebSearch`, `Skill`, the `context7` and `grep_app` MCP servers, and `Edit` and `Write` in the review folder.
- pi runs with `--approval-mode off` and a `--sandbox` policy. Its tools are `read`, `grep`, `find`, `ls`, `bash`, `write`, `web_fetch`, `web_search`, the context7 tools and `code_search`. These flags and tools come from pi-extensions (`worker-sandbox` and `approval-mode`), which must be installed.
- Codex runs with approvals set to `never` and a permission profile that writes only the review folder. Its network proxy allows only the GitHub domains, and `web_search="live"` reads live pages. The `context7` and `grep_app` MCP servers come from your Codex config, when it has them.

Web tools and MCP servers run outside the sandbox. They only read.

## Known limits

- Claude and Codex can show a folder trust prompt in an unfamiliar folder. Answer it in the reviewer pane to start the review.
- The focused pane must run Claude, pi or Codex inside a Git repository.
- `herdr plugin log list` shows the details of a failed request.

## Tests

```bash
uv run pytest tests/peer-review
```

Run it from the repository root.
