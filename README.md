# herdr-extensions

Plugins for [Herdr](https://herdr.dev), the terminal multiplexer for coding agents. They are built for Claude Code and pi.

| Plugin | What it does |
|---|---|
| [peer-review](plugins/peer-review/) | Asks the other agent to review uncommitted changes. Claude's work goes to pi, and pi's work goes to Claude. |

## Install

Each plugin lives in its own directory under `plugins/`. Install one from GitHub:

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/<plugin>
```

Or clone the repository and link a plugin, so local edits take effect at once:

```bash
git clone git@github.com:Nulluminati/herdr-extensions.git ~/Projects/herdr-extensions
herdr plugin link ~/Projects/herdr-extensions/plugins/<plugin>
```

A plugin that ships agent skills lists the `skills add` command in its README.

## Development

```bash
uv run python -m unittest discover -s tests
```
