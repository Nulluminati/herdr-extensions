# Peer review

Ask the other agent to review uncommitted changes. Claude's changes go to pi, and pi's changes go to Claude. The reviewer opens in a split pane next to the requester, writes its findings to a file, and the requester gets a prompt to read them.

The requester lists each finding with its opinion and then waits for you. It does not change code. One request is one review round.

## Parts

- `herdr-plugin.toml` and `bin/peer_review.py`: the Herdr plugin, ID `nulluminati.peer-review`. The `request` action reviews the focused agent's changes.
- `skills/peer-review/SKILL.md`: the skill that lets Claude and pi request a review. Both agents read `~/.agents/skills/`.

## Install

From a local clone, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/peer-review
skills add Nulluminati/herdr-extensions@peer-review -g -y
```

Or from GitHub:

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/peer-review
skills add Nulluminati/herdr-extensions@peer-review -g -y
```

Bind the action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+p"
type = "plugin_action"
command = "nulluminati.peer-review.request"
description = "peer review: ask the other agent to review uncommitted changes"
```

## Use

- Press the bound key in a Claude or pi pane, or
- Ask the agent: "get a peer review from pi" (or "from Claude").

The review files go to `~/.local/state/peer-review/<timestamp>/`:

| File | Content |
|---|---|
| `request.md` | Instructions for the reviewer |
| `diff.patch` | Tracked changes against `HEAD` |
| `findings.md` | The reviewer's findings |
| `worker.log` | Errors from the background worker |

## Reviewer permissions

- pi runs with only the `read`, `grep`, `find`, `ls` and `write` tools. It cannot run commands or edit files, but `write` is not limited to the review directory.
- Claude runs in `dontAsk` mode. It can only read, search and edit files inside the review directory.

## Known limits

- In a folder that Claude has not seen, Claude shows its folder trust prompt first. Answer it in the reviewer pane. The review then starts.
- The requester must be a Claude or pi pane inside a Git repository with uncommitted changes.

## Tests

```bash
uv run python -m unittest tests/test_peer_review.py
```
