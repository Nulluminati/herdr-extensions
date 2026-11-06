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
| `label` | The workspace name. Required. Unique in the file. |
| `path` | The project directory. Required. Absolute, or starts with `~`, which is expanded. A relative path is a problem. |

Each `[[projects.tabs]]` table is one tab, in file order. The first tab is the active tab.

| Key | Meaning |
|---|---|
| `name` | The tab label. Required. Two tabs in one project may share a name. |
| `command` | A program for a one-pane tab, such as `"nvim"`. |
| `agent` | An agent for a one-pane tab, such as `"claude"` or `"pi"`. |
| `working_dir` | The pane directory. For a tab with panes, the default directory of every pane that sets none. A relative path starts at the project `path`. A relative path that climbs out of the project falls back to it. |

A tab with none of these keys is one shell in the project directory.

A tab with `panes` may not also set `command`, `agent`, `label`, `split` or `ratio`. The plugin reports that as a problem, because it would ignore the key. `label` names a pane, so name the tab with `name`. `split` and `ratio` belong to the pane that splits. An empty `panes = []` list is a tab without panes: the plugin builds one pane from the tab's own keys.

Two tabs with one name are a natural way to write a second shell or a second agent in another directory. The plugin matches tabs by name in file order, so each open tab answers for one entry and a restored tab gets the program of its own entry. A name of only digits, such as `"1"`, is a problem: Herdr gives that label to a tab without a name, so the plugin cannot tell the two apart.

For more than one pane, give the tab up to four `[[projects.tabs.panes]]` tables instead of `command`, `agent` or `label`:

| Key | Meaning |
|---|---|
| `label` | The pane name. |
| `command`, `agent` | As above. Set `command` or `agent`, not both. No program gives a shell. |
| `working_dir` | The pane directory, as above. A pane that sets none uses the tab's `working_dir`. |
| `split` | `"down"` (the default) or `"right"`. Each pane after the first splits all the panes above it. |
| `ratio` | The share of the space that the panes above keep, between 0 and 1. The default is 0.5. |

The first pane holds no split of its own, so it may not set `split` or `ratio`. The plugin reports both as a problem. A tab without `panes` is one pane, so it may not set them either. A pane with no `working_dir` starts in the tab's `working_dir`, or in the project directory when the tab sets none.

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

The plugin checks the whole file before it builds anything. It reports every problem at once in the plugin log, and builds nothing. That includes a file the plugin cannot read, such as one with the wrong permissions. A repeated `label` is a problem too, because two workspaces with one label cannot be told apart, and so is a `label` with a space at either end. A key that the plugin would ignore is a problem as well, such as `label`, `split` or `ratio` on a tab with panes, or `split` or `ratio` on the first pane. A repeated tab `name` is not a problem. A `name` of only digits is a problem, because Herdr gives that label to an unnamed tab. An absolute `working_dir` may point anywhere, inside the project or not.

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
- An open project gets a file tab when the workspace holds fewer tabs of that name than the file asks for. One open tab answers for one file tab, so a file with two `shell` tabs adds the second one. The plugin removes no tab.

### A restored session

Herdr restores workspaces, tabs, splits and directories, but each pane comes back as a bare shell. A restore leaves `session.json` in place, so the plugin treats a session that has that file as restored. Herdr keeps a file it could not read, and the plugin still takes the restore path. The plugin reports that file in the plugin log. In each open project, the plugin rebuilds a tab from the file when:

- the file gives the tab a program or an agent, and
- every pane of the tab is an idle shell.

A pane is an idle shell when Herdr reports its shell as the only foreground process. Any shell counts, whatever its name. A shell that is busy inside its own process, in a builtin loop or when it waits in `read`, still shows one foreground process. The plugin cannot tell that shell from an idle one. A pane whose shell pid Herdr does not report never counts as idle, so its tab stays as restored.

The plugin leaves a tab alone when it holds an agent, including an agent that Herdr will resume, or a running program. A tab of shells needs nothing, so the plugin skips it. Either way the tab answers for its file entry, so a later tab of the same name takes the next entry. The rebuilt tab keeps its place in the tab row.

A restored session opens no workspace. A project that you closed stays closed until you start a fresh session or run `apply` in a workspace for it.

### The apply action

`apply` rebuilds the focused workspace from the project that matches it. A project matches a workspace by its label first. Without a label match, the project that holds the workspace's own directory, or a pane directory inside it, matches. Every project that holds the workspace counts, whatever the depth of the directory it holds. When two projects both hold it, the project with the longer path wins, so a package workspace rebuilds from the package and not from the repository that holds it. Two projects of one path length keep the first in the file.

It rebuilds the tab named like the first tab in the file. Without such a tab, it replaces the focused tab with the first tab. Then it appends only the tabs of the file that the workspace does not hold yet. A second `apply` on an unchanged workspace thus keeps one tab of each name. When no project matches the workspace, it does nothing and exits with status 1.

`apply` leaves the tab that it would rebuild alone when a program or an agent runs in it, and exits with status 1. It prints two lines, such as:

```
default-layout: herdr-extensions/agents left alone: something is running in w1:p2
default-layout: run apply with --force to replace it and lose that work
```

The tab keeps its work, because `layout.apply` closes the tab that it rebuilds, and Herdr cannot keep a live process in a rebuilt tab.

A manual run must not rebuild the tab that holds the pane it starts from, because the close would stop the run itself. When the run lives in the focused tab, `apply` rebuilds another tab of the workspace instead. When that is the only tab, or the run lives in the tab named like the first tab, it does nothing and exits with status 1:

```
default-layout: herdr-extensions/agents runs this command; run apply from a pane in another tab
```

The `apply` action passes no arguments, so it never overrides the busy check. Add `--force` to replace the tab whatever runs in it:

```bash
python3 bin/default_layout.py apply --force
```

Run that from the plugin root in a pane in another tab of the workspace. The pane gives the plugin the workspace id. `--force` never rebuilds the tab that runs it, for the same reason the action does not.

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
- The plugin removes and reorders only a tab that it rebuilds: a tab that a restore rehydrates, or the tab that `apply` replaces. It never renames a tab.
- The file may not name a tab with only digits. Herdr labels a tab without a name with its position, so the plugin reports such a name as a problem and builds nothing.
- A pane whose `working_dir` is not a directory opens in the project directory, with one line in the plugin log. A tab-level `working_dir` does the same for all of its panes, and reports one line for the tab. A relative `working_dir` that climbs out of the project, even through a symbolic link, does the same, such as `"../etc"`. Herdr does not refuse a missing directory: it opens the pane in your home directory instead, so the plugin never sends one.
- A project whose directory does not exist is skipped, with one line in the plugin log.
- A request to Herdr gives up after 30 seconds, from the connect to the reply. A server that stalls fails that project with one line, and the run moves to the next project.
- Two runs do not build one project at once: a run holds a lock on the claim marker until it ends. A file system that cannot lock falls back to the marker's age, so on such a file system a run past 60 seconds can still lose its claim. `flock` on some CIFS and FUSE mounts reports success without excluding anyone, and this plugin cannot tell.
- A run with no `HERDR_PLUGIN_STATE_DIR`, such as a manual run, claims through `default-layout-<uid>` in the temporary directory. If that path holds a symbolic link, a file, or a directory that another user made, the run refuses it with one line in the plugin log and builds without claims, so two such runs can build one project at once until the path is clean.
- A run that hangs keeps its project locked until it exits. The marker is `building-<key>` in the plugin state directory; remove it by hand only when no run holds that project.
- `herdr plugin log list --plugin nulluminati.default-layout` shows what each run did.

## Tests

```bash
uv run pytest tests/default-layout
```

Run it from the repository root.
