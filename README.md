# herdr-extensions

Plugins for [Herdr](https://herdr.dev), the terminal multiplexer for coding agents. They work with Claude Code, pi and Codex.

| Plugin | What it does |
|---|---|
| [agent-icons](plugins/agent-icons/) | Shows each agent's vendor logo, such as Claude or pi, in the sidebar Agents list. |
| [auto-title](plugins/auto-title/) | Gives each tab a title from its agent's session name, its herdboard card or its program. Labels you set by hand stay. |
| [codex-usage](plugins/codex-usage/) | Shows each Codex agent's context use and estimated cost in the sidebar Agents list, in the same form as Claude and pi. |
| [default-layout](plugins/default-layout/) | Opens the projects, tabs and panes listed in a TOML file when the Herdr server starts, and relaunches their programs after a restore. |
| [peer-review](plugins/peer-review/) | Has a second agent review your agent's work: Claude to pi, pi to Codex, Codex to Claude. It reviews uncommitted changes, else the branch commits since the upstream. |
| [resource-usage](plugins/resource-usage/) | Shows each workspace's processor and memory use in the sidebar Spaces list. A main checkout's workspace also shows the total of its open worktrees. |

herdboard, a kanban board for Markdown todos and Linear issues that starts card work with an agent, lives in its own private repository: [Nulluminati/herdboard](https://github.com/Nulluminati/herdboard).

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

Each plugin's README gives the other steps: its agent skill and its key binding.

## Development

Run the tests, the linter and the formatter from the repository root:

```bash
uv run pytest -n auto
uv run ruff check .
uv run ruff format .
```

Install the pre-commit hook once per clone. It runs ruff before each commit:

```bash
uv run pre-commit install
```

Make `git blame` skip the commit that formatted all code:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

Dependabot keeps the GitHub Actions pins and the uv lock file current. It checks weekly and groups the minor and patch updates per ecosystem. See [.github/dependabot.yml](.github/dependabot.yml). `tests/repo/test_dependabot.py` holds the settings that matter.

[AGENTS.md](AGENTS.md) describes the layout and the rules for plugin code.
