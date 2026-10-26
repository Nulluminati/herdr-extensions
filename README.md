# herdr-extensions

Plugins for [Herdr](https://herdr.dev), the terminal multiplexer for coding agents. They work with Claude Code, pi and Codex.

| Plugin | What it does |
|---|---|
| [agent-icons](plugins/agent-icons/) | Shows each agent's vendor logo, such as Claude or pi, in the sidebar Agents list. |
| [auto-title](plugins/auto-title/) | Gives each tab a title from its agent's session name, its herdboard card or its program. Labels you set by hand stay. |
| [default-layout](plugins/default-layout/) | Opens the projects, tabs and panes listed in a TOML file when the Herdr server starts, and relaunches their programs after a restore. |
| [herdboard](plugins/herdboard/) | A kanban board for Markdown todos and Linear issues. A card can start work in its own worktree with an agent, then go through review and merge, locally or as a draft GitHub pull request. |
| [peer-review](plugins/peer-review/) | Has a second agent review your agent's work: Claude to pi, pi to Codex, Codex to Claude. It reviews uncommitted changes, else the branch commits since the upstream. |

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

Dependabot keeps the GitHub Actions pins and the two uv lock files current. It checks weekly and groups the minor and patch updates per ecosystem. See [.github/dependabot.yml](.github/dependabot.yml). `tests/test_dependabot.py` holds the settings that matter.

[AGENTS.md](AGENTS.md) describes the layout and the rules for plugin code.
