# Resource usage

Show the processor and memory use of each open workspace in the Herdr sidebar:

```
herdr-extensions
CPU 38%  MEM 3.2 GiB  total
  feat-tdv5-resource-usage
  CPU 12%  MEM 1.1 GiB
  feat-tvmx-default-layout
  CPU 24%  MEM 1.8 GiB
dotfiles-ansible
CPU 1%  MEM 240 MiB
```

The plugin writes the text to the `$resource_usage` workspace token. Your Herdr config decides where the token shows.

## What counts

A workspace's processes are the shell of each of its panes and every descendant of those shells: agents, editors, test runs, servers you started in the pane. Plain shells and plugin panes count too.

- **CPU** is the processor time those processes used in the last two seconds, on a whole-machine scale. 100% means every logical processor was busy. One busy processor on an 8-processor machine shows 12%.
- **MEM** is the sum of the processes' resident set size (RSS). Below 1 GiB it shows whole MiB, above it GiB with one decimal place.
- `--` means the value is unknown. Just after the worker starts, CPU shows `--` for one interval: it needs two samples to measure a rate. A workspace whose pane shells the plugin cannot find shows `CPU --  MEM --`.

### Worktree totals

A linked worktree workspace, such as one from `herdr worktree create` or `herdboard start`, shows its own usage. The workspace of its main checkout shows its own processes plus those of every open linked worktree of the same repository, and the word `total`.

Do not add a main workspace's total to its worktrees' values: the total already includes them. A process that two workspaces share counts once in the total.

When the main checkout has no open workspace, each worktree workspace shows only its own usage, without a total. When several workspaces show the main checkout, the one that Herdr names as the source workspace gets the total.

## Install

Requirements: Herdr 0.9.5 or later and Python 3.11 or later on Linux or macOS. The plugin uses only the Python standard library. On Linux it reads `/proc`. On macOS it runs the system `ps`. The `python3` on your `PATH` must be 3.11 or later; the one that comes with the Xcode Command Line Tools is older.

1. Install the plugin:

   ```bash
   herdr plugin install Nulluminati/herdr-extensions/plugins/resource-usage
   ```

   To work on the plugin from a clone, link it instead, so edits take effect at once:

   ```bash
   herdr plugin link ~/Projects/herdr-extensions/plugins/resource-usage
   ```

2. Add the token to the workspace rows in `~/.config/herdr/config.toml`. Append `["$resource_usage"]` to the `rows` array you have under `[ui.sidebar.spaces]`, and keep your other rows:

   ```toml
   [ui.sidebar.spaces]
   rows = [
     # ... your rows ...
     ["$resource_usage"],
   ]
   ```

   If the file has no `[ui.sidebar.spaces]` table yet, add one. Do not add a second copy of a table that is there already: TOML refuses a table defined twice.

3. Apply the config:

   ```bash
   herdr server reload-config
   ```

4. Start the worker. It starts with the Herdr server, and also when you next change the focused workspace. To start it now, run:

   ```bash
   herdr plugin action invoke nulluminati.resource-usage.enable
   ```

The rows show after about two seconds, and CPU after about four.

## Actions

| Action | What it does |
|---|---|
| `nulluminati.resource-usage.enable` | Turn collection on and start the worker. |
| `nulluminati.resource-usage.disable` | Turn collection off. The worker clears its rows and exits within two seconds. |

The choice stays across Herdr restarts. A fresh install collects.

## How it works

A startup hook and a `workspace.focused` hook run `bin/resource_usage.py --ensure-running`. It starts one background worker for each Herdr session, unless one runs or you disabled collection. A lock file in the plugin state folder makes sure only one worker runs for each session.

Every two seconds the worker:

1. reads the workspaces and panes with `herdr api snapshot`;
2. reads the process table once: `/proc/<pid>/stat` on Linux, `ps -A -o pid=,ppid=,lstart=,time=,rss=` on macOS;
3. finds each pane's shell with `herdr pane process-info`, and keeps it until the shell changes;
4. writes each workspace's text with `herdr workspace report-metadata --ttl-ms 8000`.

Each row expires eight seconds after its last update, so the rows go away when the worker stops. The worker exits when you disable collection, when Herdr disables or removes the plugin (checked every 30 seconds), when the Herdr server stops, or after one minute of failed cycles.

## State

The plugin keeps its state in the plugin state folder, `~/.local/state/herdr/plugins/nulluminati.resource-usage/`:

| File | Content |
|---|---|
| `preference` | `enabled` or `disabled` |
| `worker-<session>.lock` | Held by the session's running worker |
| `worker-<session>.log` | Errors from the worker; emptied at worker start when it is larger than 256 KiB |

## Known limits

- Only open Herdr workspaces count. A worktree directory with no open workspace has no row.
- A process that leaves its pane's process tree, such as a daemon that detaches, does not count. Services that a pane only talks to, such as a database or a container, do not count.
- RSS counts shared memory pages, such as shared libraries, once for each process that maps them. MEM can be more than the memory the workspace adds to the machine.
- A process that starts and exits between two samples does not count.
- Each Herdr session measures only its own panes on its own machine.
- macOS support uses the documented `ps` fields but is not tested on a Mac yet.

## Troubleshooting

- No row: check that `$resource_usage` is in your `[ui.sidebar.spaces]` rows, then run `herdr server reload-config`.
- Still no row: run the enable action, then check that a worker runs: `pgrep -af resource_usage.py`.
- Errors: read the worker log in the state folder, and `herdr plugin log list --plugin nulluminati.resource-usage` for the hooks.

## Tests

```bash
uv run python -m unittest tests/test_resource_usage.py
```

Run it from the repository root.
