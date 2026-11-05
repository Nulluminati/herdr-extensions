# Default layout

Start Herdr with the same projects every time. You list your projects in one TOML file, each with its directory and its tabs. When the Herdr server starts with a fresh session, the plugin opens every project that is not open yet. When the server restores a saved session, it relaunches the programs that the restore brought back as bare shells, and it opens no workspace.

## Install

Requirements: Herdr 0.9 or later and Python 3.11 or later.

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/default-layout
```

To work on the plugin from a clone, link it instead, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/default-layout
```

Run `herdr plugin list` and look for `nulluminati.default-layout`.

## The layout file

The plugin reads `layout.toml` in its config directory. Find the directory with:

```bash
herdr plugin config-dir nulluminati.default-layout
```

Start from the example. Copy it there, then change the labels and paths:

```bash
cp examples/layout.toml "$(herdr plugin config-dir nulluminati.default-layout)/layout.toml"
```

The plugin never writes this file. With no file, the plugin does nothing.

### Format

Each `[[projects]]` table is one workspace:

| Key | Meaning |
|---|---|
| `label` | The workspace name. Required. |
| `path` | The project directory. Required. `~` is expanded. |

Each `[[projects.tabs]]` table is one tab, in file order. The first tab is the active tab.

| Key | Meaning |
|---|---|
| `name` | The tab label. Required. |
| `command` | A program for a one-pane tab, such as `"nvim"`. |
| `agent` | An agent for a one-pane tab, such as `"claude"` or `"pi"`. |
| `working_dir` | The pane directory. A relative path starts at the project `path`. |

A tab with none of these keys is one shell in the project directory.

For more than one pane, give the tab up to four `[[projects.tabs.panes]]` tables instead of `command` or `agent`:

| Key | Meaning |
|---|---|
| `label` | The pane name. |
| `command`, `agent`, `working_dir` | As above. Set `command` or `agent`, not both. No program gives a shell. |
| `split` | `"down"` (the default) or `"right"`. Each pane after the first splits all the panes above it. |
| `ratio` | The share of the space that the panes above keep, between 0 and 1. The default is 0.5. |

An agent is a plain name. The plugin runs it as the pane's command, and Herdr detects it as an agent. The plugin adds no model, thinking or prompt flags.

Each program runs as `sh -c '<program>; exec $SHELL'`. When you quit the program, your shell stays in the pane.

### Example

```toml
[[projects]]
label = "herdr-extensions"
path = "~/Projects/herdr-extensions"

[[projects.tabs]]
name = "agents"

[[projects.tabs.panes]]
agent = "pi"

[[projects.tabs.panes]]
split = "right"
ratio = 0.6

[[projects.tabs]]
name = "editor"
command = "nvim"

[[projects.tabs]]
name = "git"
working_dir = "plugins"
```

This gives one workspace, `herdr-extensions`, with three tabs. The `agents` tab has pi on the left (60% of the width) and a shell on the right.

The plugin checks the whole file before it builds anything. It reports every problem at once in the plugin log, and builds nothing.

## When it runs

- **Server startup.** Herdr runs the plugin once, after it restores the session.
- **The `apply` action.** You run it by hand in a workspace.

A client attach, a config reload and a `herdr plugin link` do not run it. New workspaces, such as worktree or herdboard workspaces, get no tabs from it.

### A fresh session

A fresh session has no saved session to restore. Herdr saves a session in `session.json` in its session directory: the Herdr config directory for the default session, and `sessions/<name>` below it for a named session. The socket can sit elsewhere, so the plugin asks `herdr session list` for the directory.

The plugin treats a session as fresh when the server has no workspace, or when that file is missing. It reads the file at the start of the hook, because a fresh server writes it a few seconds after startup. A later read could mistake the first save for a restore. The plugin then opens each project in the file:

- A project is open when a workspace has its label, or has a pane in its directory or below it.
- A workspace at the project directory wins over one that holds a pane lower down.
- A project that is not open gets a new workspace with its label and its tabs.
- When the fresh workspace is in a project's directory, the plugin builds that project into it.
- An open project gets only the tabs whose names it does not have. The plugin removes no tab.

### A restored session

Herdr restores workspaces, tabs, splits and directories, but each pane comes back as a bare shell. A restore leaves `session.json` in place, so the plugin treats a session that has that file as restored. Herdr keeps a file it could not read, and the plugin still takes the restore path. The plugin reports that file in the plugin log. In each open project, the plugin rebuilds a tab from the file when:

- the file gives the tab a program or an agent, and
- every pane of the tab is an idle shell.

A pane is an idle shell when Herdr reports its shell as the only foreground process. Any shell counts, whatever its name. A shell that is busy inside its own process, in a builtin loop or when it waits in `read`, still shows one foreground process. The plugin cannot tell that shell from an idle one. A pane whose shell pid Herdr does not report never counts as idle, so its tab stays as restored.

The plugin leaves a tab alone when it holds an agent, including an agent that Herdr will resume, or a running program. A tab of shells needs nothing, so the plugin skips it. The rebuilt tab keeps its place in the tab row.

A restored session opens no workspace. A project that you closed stays closed until you start a fresh session or run `apply` in a workspace for it.

### The apply action

`apply` rebuilds the focused workspace from the project that matches it. A project matches a workspace by its label first. Without a label match, the project that holds the workspace's own directory, or a pane directory inside it, matches. When two projects both hold it, the project with the longer path wins.

It replaces the focused tab with the first tab in the file, and appends the other tabs after it. When no project matches the workspace, it does nothing and exits with status 1.

Bind it in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+l"
type = "plugin_action"
command = "nulluminati.default-layout.apply"
description = "default layout: apply to this workspace"
```

## Known limits

- A tab holds at most four panes.
- The plugin tells a fresh session from a restored one by `session.json` in the session directory, because Herdr gives a startup hook no restore flag. Herdr restores from the same file, so keep the two in step.
- The plugin does not hook `workspace.created`.
- The plugin does not change focus.
- The plugin never removes, renames or reorders a tab that it did not build.
- A project whose directory does not exist is skipped, with one line in the plugin log.
- `herdr plugin log list --plugin nulluminati.default-layout` shows what each run did.

## Tests

```bash
uv run pytest tests/default-layout
```

Run it from the repository root.
