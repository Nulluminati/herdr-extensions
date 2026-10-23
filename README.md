# herdr-extensions

Plugins for [Herdr](https://herdr.dev), the terminal multiplexer for coding agents. They work with Claude Code and pi.

| Plugin | What it does |
|---|---|
| [agent-icons](plugins/agent-icons/) | Shows each agent's vendor logo, such as Claude or pi, in the sidebar Agents list. |
| [auto-title](plugins/auto-title/) | Gives each tab a title from its agent's session name, its herdboard card or its program. Labels you set by hand stay. |
| [herdboard](plugins/herdboard/) | A kanban board for Markdown todos and Linear issues. A card can start work in its own worktree with an agent, then go through review and merge, locally or as a draft GitHub pull request. |
| [peer-review](plugins/peer-review/) | Has the other agent review your agent's work: Claude's work goes to pi, and pi's work goes to Claude. It reviews uncommitted changes, else the branch commits since the upstream. |

## Install

Install a plugin from GitHub:

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/<plugin>
```

Or clone the repository and link a plugin, so local edits take effect at once:

```bash
git clone git@github.com:Nulluminati/herdr-extensions.git ~/Projects/herdr-extensions
herdr plugin link ~/Projects/herdr-extensions/plugins/<plugin>
```

Each plugin's README gives the other steps: its agent skill, its key binding and, for herdboard, the `herdboard` command.

## Development

Run the tests from the repository root:

```bash
uv run python -m unittest discover -s tests
```

[AGENTS.md](AGENTS.md) describes the layout and the rules for plugin code.
