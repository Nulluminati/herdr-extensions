# Auto title

Give each Herdr tab a useful title, so you do not have to rename tabs by hand.

Without this plugin, many tabs show only their number: every tab from `herdr worktree create` (so every `herdboard start` workspace) and every tab you make by hand. pi and Claude name their **pane** with the session name, but nothing names the **tab**.

## What names a tab

The plugin picks one pane of the tab, then takes the first of these that is not empty:

1. The agent's session name: the `claude_session_name` or `pi_session_name` pane token.
2. The pane label of an agent pane.
3. The herdboard card title from the `herdboard_card` token.
4. The name of the program in the pane, such as `fish`, `nvim` or `claude`.

The pane is an agent pane when the tab has one. A pane with a herdboard card comes first, then a pane with a session name, then the focused pane. So a review or pull request split does not take the title from the card's agent. A tab with no agent uses its focused pane, else its first pane.

A herdboard planner tab gets the prefix `Planning: `, such as `Planning: Codex support plan`. A planner tab is a tab that had a planning label such as `plan tveq`, or a tab in a workspace with that label. herdboard gives a card planner the tab label and an automation planner the workspace label.

Titles have at most 40 columns, with the prefix. A longer title ends with `…`.

## Your labels stay

The plugin renames a tab only when its label is one of these:

- digits only, which is Herdr's default label;
- a herdboard planning label, such as `plan tveq`;
- the label the plugin wrote last.

Any other label is yours, and the plugin does not change it. To make a tab automatic again, run the reset action, or rename the tab to digits only.

## Install

Requirements: Herdr 0.9 or later and Python 3.11 or later.

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/auto-title
```

To work on the plugin from a clone, link it instead, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/auto-title
```

The poller starts with the Herdr server. To start it now, run:

```bash
herdr plugin action invoke restart --plugin nulluminati.auto-title
```

If Herdr asks you for a name for each new tab, turn that off in `~/.config/herdr/config.toml`, so the plugin can name the tab:

```toml
[ui]
prompt_new_tab_name = false
```

## Actions

| Action | What it does |
|---|---|
| `nulluminati.auto-title.reset` | Make the focused tab automatic again. |
| `nulluminati.auto-title.doctor` | Show the label, the owner, the source pane, the source and the title for the focused tab. |
| `nulluminati.auto-title.pause` | Stop renaming tabs. |
| `nulluminati.auto-title.resume` | Rename tabs again. |
| `nulluminati.auto-title.restart` | Stop the poller, then start it. Use it after a config change. |

Bind the actions you use in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+t"
type = "plugin_action"
command = "nulluminati.auto-title.reset"
description = "auto title: make this tab automatic again"

[[keys.command]]
key = "prefix+alt+t"
type = "plugin_action"
command = "nulluminati.auto-title.doctor"
description = "auto title: explain this tab's title"
```

## Config

Put the config in `config.toml` in the plugin config folder. `herdr plugin config-dir nulluminati.auto-title` prints the folder.

```toml
# Most terminal columns in a title.
max_length = 40
# Seconds between two passes.
poll_seconds = 2
```

Run the restart action after a change. A bad value is written to the log, and the plugin uses the default.

## State

The plugin keeps its state in the plugin state folder, `~/.local/state/herdr/plugins/nulluminati.auto-title/`:

| File | Content |
|---|---|
| `tabs.json` | The label the plugin wrote to each tab, the planner tabs, and the tabs marked for reset |
| `paused` | Present while the plugin is paused |
| `poller.lock`, `poller.pid` | The running poller |
| `state.lock` | Held while the poller or an action changes `tabs.json` |
| `auto-title.log` | Errors from the poller |

## Known limits

- Herdr sends no event when a pane or token changes, so a title changes up to `poll_seconds` after its source.
- The poller exits when the plugin is disabled or removed, and about 30 seconds after the Herdr server stops. `herdr plugin enable` does not start it again; run the restart action.
- The plugin names tabs only. It does not name panes or workspaces.
- One Herdr server per plugin state folder.

## Tests

```bash
uv run python -m unittest tests/test_auto_title.py
```

Run it from the repository root.
