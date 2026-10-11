# herdboard

Status: design draft. Nothing is built yet.

A kanban board for your own todos and your Linear issues, inside Herdr. Todos are local Markdown files that Obsidian can open. Linear issues show on the same board, in the same columns, with the same commands.

## Goals

- One board and one set of commands for local todos and Linear issues.
- Todos are plain Markdown in the Obsidian Kanban plugin format. Moving them into an Obsidian vault later is a folder copy.
- Global todos and per-repository todos. Repository todos live outside the repository.
- Claude Code and pi use the same command-line interface (CLI) through one skill.
- A card can start work: open a worktree and hand the card to an agent.

Non-goals: sync between machines, phone access, goals or OKRs, a personal/work split for todos.

## Data

### Layout

```
~/todos/                          # todos_dir
  inbox.md                        # global board
  projects/
    nulluminati-homelab.md        # one board per repository
    acme-api.md
```

### Repository identity

- The tool reads the `origin` remote and normalizes it to `host/owner/repo`. SSH and HTTPS forms give the same result (`git@github.com:acme/api.git` and `https://github.com/acme/api` are both `github.com/acme/api`). GitHub names are not case-sensitive, so for `github.com` the path is lowercased. Other hosts keep their case. Nested namespaces keep every segment (`gitlab.com/group/sub/repo`).
- The file name is the owner and repository, joined with `-` (`acme-api.md`). If that name is taken by another repository, the tool adds the host (`github.com-acme-api.md`).
- Each board stores the full identity in `repo:` frontmatter. Before the tool uses a board, it checks that `repo:` matches. A mismatch is an error, not a silent reuse.
- Without a remote, the identity is the absolute path of the main checkout, from `git rev-parse --git-common-dir`.
- If the remote changes, the tool reports that no board matches and offers to update `repo:` in the old board.

### File format

Obsidian Kanban plugin format, as written by the plugin's serializer (`src/parsers/formats/list.ts` in `mgmeyers/obsidian-kanban`).

````markdown
---
kanban-plugin: board
repo: github.com/acme/api
linear-projects:
  - id: 9c1e…
    name: API Platform
    labels:
      - id: 41aa…
        name: repo:api
  - id: 77b0…
    name: Billing v2
---

## Backlog

- [ ] Compare tile suppliers #research ^t3m9
	- [ ] Ask for samples

## Todo

- [ ] Order tiles 📅 2026-10-20 ^t7k2

## In progress

## Review

## Done

**Complete**
- [x] Measure the room ^t2a6


***

## Archive

- [x] Old card ^t1x0

%% kanban:settings
```
{"kanban-plugin":"board"}
```
%%
````

Supported format contract:

- `## Title` is a column. A title can end in a card limit, such as `Todo (5)`. The tool keeps the limit and shows it.
- A card is a `- [c]` line plus every indented line below it. Indented lines (sub-items, continuation text) belong to the card's text.
- `^t7k2` at the end of a card's first line is the card ID (Obsidian block ID syntax).
- `#tag` is a category. Headings are statuses, so categories are tags.
- `📅 YYYY-MM-DD` is a due date. This is the only Obsidian Tasks syntax that the tool reads. The tool does not claim Obsidian Tasks compatibility: recurrence and completion dates are kept as text but not processed.
- `[[note]]` in a card is a link to a note. The tool keeps it as literal text. With `vault_root` set in config, `show` displays the note's content.
- `**Complete**` directly under a heading marks a completed column. The plugin translates this word into the Obsidian display language. The tool accepts the English word and any word set in `complete_markers` in config.
- `***` directly followed by `## Archive` starts the archive, as in the plugin. The plugin translates "Archive"; `archive_titles` in config adds other names. A `***` before any other heading is an ordinary line. Archive cards are not a column, and commands do not change them. The board can show them on request.
- `%% kanban:settings` followed by a fenced JSON block and `%%` holds plugin settings. The tool keeps it unchanged.
- Nested and continuation lines use a tab, or 4 spaces when the vault sets `useTab` to false. The tool keeps the indentation that the file uses.
- Any line that the tool does not understand stays unchanged in its place.

### Card IDs

- The first write to a board gives every card in its columns a new ID if it has none, not only the card that changed. Archive cards keep what they have, because commands do not address them.
- IDs are 4 characters (`t` plus 3 base-36 characters) and unique across all boards in `todos_dir`. A new ID is checked against all boards.
- Duplicate IDs (for example, after a board was copied), including IDs of archive cards, stop every command that touches either card. The error names both locations. `herdboard doctor --fix-ids` gives the second card a new ID.
- Before a board has IDs, `list` shows a temporary reference for each card (`homelab:12`, board and card number). Commands accept it, and the write that follows assigns real IDs.

### Columns

- New boards get the columns in `columns` from config. Default: Backlog, Todo, In progress, Review, Done. The tool writes `**Complete**` under Done.
- Any `##` heading is a column, so a board can add its own columns. The board shows a file's columns in file order.
- The done column of a board is the column with a complete marker. Without a marker, the column named "Done". Without both, commands that need a done column (`done`, `promote`) stop with an error that names the board.
- A move into a completed column checks the card's box (`[x]`). A move out of it clears the box (`[ ]`). The Obsidian Kanban plugin does the same.
- In a view that combines several boards and Linear, columns with the same name merge. Their order follows `columns` from config, then the order of first appearance. Every card keeps its source board, so a move always writes to the correct file. A drop into a merged column uses that column in the card's own board. If the card's board has no column with that name, the move fails and says so.

### Writes

- Agents and scripts change todos only through the CLI, never by editing files.
- Every write takes one lock for the whole store: the hidden file `~/todos/.herdboard.lock`. Obsidian ignores hidden files. One lock also serializes ID allocation across boards and board creation. Under the lock, the tool reads the file again, applies the change to the current content, writes to a temporary file in the same folder, and renames it over the board.
- The tool compares the card at read time with the card under the lock: its lines and its column. If either changed, the command stops and asks to retry. Changes to other cards are kept.
- The tool writes only to boards whose frontmatter is closed and contains `kanban-plugin: board`. It reads other boards but does not change them.
- A file with CRLF line endings keeps CRLF on every write.
- The tool moves a card as one unit (its line and all its indented lines) and does not reformat other lines.
- Obsidian does not use the lock. The tool cannot prevent a conflict with an unsaved Obsidian edit. The content check catches most cases, and the README says this plainly.

## Worktrees

All worktrees of a repository share one board, because the tool finds the board through the repository identity, not through the working directory. Bare repositories with worktrees work the same way.

## Linear

Linear is optional. It turns on when `LINEAR_API_KEY` is set, or from config. The tool uses the Linear GraphQL API.

### Model mapping

| herdboard | Linear |
|---|---|
| Board | A view: "my issues", a team, or a project |
| Column | Workflow state of the issue's team |
| Card | Issue |
| Indented sub-item | Sub-issue |
| `#tag` | Label |
| `📅` date | Due date |
| `[[note]]` | Description |
| `t7k2` | `ENG-123` |

### Workflow states

- Workflow states belong to a team. The tool resolves a column to a state in the team of each issue.
- Default mapping by state type: backlog and triage go to Backlog, unstarted to Todo, started to In progress, completed to Done, canceled to Done (hidden by default). A state name can override its type: `"In Review" = "Review"`.
- When several states of one team match a column (for example, two started states), the tool asks which one to use, then stores the choice by state ID for that team.
- When a stored state ID no longer exists, the tool asks again.

### Repositories and Linear projects

- The link is many-to-many. Each repository board lists its Linear projects in `linear-projects` frontmatter. A project that serves several repositories appears in each of those lists. The tool computes the reverse direction.
- `herdboard link <project> [--label <label>]` writes the link with stable IDs and display names. Names update when they change in Linear.
- A repository view shows every issue of each linked project, for every assignee. A link with labels shows only issues that have at least one of those labels.
- When a linked project or label no longer exists or is not accessible, the board shows an error for that link. It does not show an empty board.
- When Linear's GitHub integration attaches a pull request from this repository to an issue, the card shows a marker. The tool matches the pull request URL against the board's `repo:` identity. The marker does not filter.

### API rules

- Every list query uses pagination until the end.
- Every response is checked for GraphQL errors, and every mutation for `success`.
- A rate-limited read waits and retries, with a limit. A rate-limited write fails and says so.
- An issue that two linked projects return shows once (by issue ID).
- Archived projects and issues are hidden. `--archived` shows them.

### Cache and offline use

- The tool caches Linear data in `~/.local/state/herdboard/<workspace-id>/<user-id>/`, so a different API key never sees another account's cache.
- The board shows the time of the last fetch. When Linear is not reachable, the board shows the cache and marks it stale.
- Writes to Linear need a connection. Offline, Linear cards are read only.
- Before a write, the tool fetches the issue again. If a write times out, the tool fetches the issue to learn the result before it reports success or failure. It never repeats a write blindly.

## Views

| View | Shows |
|---|---|
| Repository (default inside a repository) | That repository's todos and every issue of its linked Linear projects |
| Linear project | That project's issues and the todos of every repository linked to it |
| My issues | Linear issues assigned to you, from every team |
| Global (default outside a repository) | `inbox.md` |
| All | Every board and your Linear issues |

Outside Git, or before a repository board exists, `add` goes to `inbox.md` unless `--board` names a board. `herdboard init` creates the board for the current repository. A board with invalid frontmatter is shown read only, with the error.

## CLI

| Command | Action |
|---|---|
| `herdboard board [--view]` | Open the kanban (Textual) |
| `herdboard list [--view] [--source] [--column] [--json]` | List cards |
| `herdboard add "text" [--board] [--column]` | Add a todo. Default board: the current repository's board, else `inbox.md`. Default column: the first column |
| `herdboard add --linear --team ENG [--project] "title"` | Create a Linear issue |
| `herdboard show <id>` | Show a card and its detail |
| `herdboard move <id> <column> [--position top\|bottom]` | Move a card. Default position: bottom |
| `herdboard done <id>` | Move a card to its board's done column |
| `herdboard start <id> [--agent claude\|pi] [--repo]` | Start work on a card (see below) |
| `herdboard promote <id> --team ENG [--project]` | Turn a todo into a Linear issue (see below) |
| `herdboard link <project> [--label]` | Link the current repository board to a Linear project |
| `herdboard init` | Create the board for the current repository |
| `herdboard migrate <TODO.md>` | Convert an existing `TODO.md` (see below) |
| `herdboard doctor [--fix-ids]` | Check every board for format errors and duplicate IDs |

- `<id>` is a todo ID (`t7k2`), a temporary reference (`homelab:12`) or a Linear identifier (`ENG-123`).
- With `--json`, every command writes JSON to standard output, and errors have the form `{"error": {"code": "…", "message": "…"}}`.
- Exit codes: 0 success, 1 user error (unknown ID, missing column), 2 conflict (the card changed, the lock is held), 3 Linear error.

### start

1. Find the repository. For a repository board, it is that repository. For a Linear issue, it is the repository of the current view, or `--repo`. For a global todo, `--repo` is required. The tool never picks the first of several linked repositories.
2. Find the local checkout from a registry of checkouts in `~/.local/state/herdboard/checkouts.json`. The tool adds a checkout each time it runs inside a repository. If none is known, `start` asks for a path.
3. Choose the branch. For a Linear issue, the issue's `gitBranchName`. For a todo, a name from the card text. The tool validates the name with `git check-ref-format`.
4. If the card already has a worktree in the state file, reuse it and its workspace. Otherwise create it with `herdr worktree create` and open it as a workspace.
5. Move the card to In progress. For Linear, the issue moves to the mapped started state.
6. Start the agent with the card as its first command-line message. A startup timeout from Herdr does not mean the agent failed (see peer-review).
7. Each step records its progress in the state file. A failed `start` reports which steps succeeded. Running `start` again continues from the first step that did not finish.

When the board refreshes, it checks each stored worktree. If Herdr removed one, the board shows this on the card. The card does not move by itself.

### promote

1. Read the todo, including its indented lines, tags and due date.
2. Create the Linear issue: the first line becomes the title, the indented sub-items become sub-issues, tags become labels when a label with that name exists, the due date becomes the due date, and a linked note becomes the description when `vault_root` is set. Anything that cannot be converted goes into the description as text.
3. Record the operation and the new issue ID in the state file before and after the call.
4. Only after Linear confirms the issue, move the todo to Done and add `→ ENG-123` to its first line. The todo is not deleted.
5. If the call times out, the tool searches Linear for the recorded operation before it creates anything again.

### migrate

1. Read the `TODO.md` file.
2. Convert it: headings become tags on their items (`## Follow Ups` becomes `#follow-ups`), all items go into Backlog, and nested items stay indented under their parent card.
3. Show the converted board.
4. Write it to `~/todos/projects/<name>.md` only after you confirm. The original file is not changed.

## Configuration

`~/.config/herdboard/config.toml`. Every setting has a default, so the file is optional.

```toml
todos_dir = "~/todos"
columns = ["Backlog", "Todo", "In progress", "Review", "Done"]
complete_markers = []      # added to the English "Complete"
archive_titles = []        # added to the English "Archive"
# vault_root = "~/Notes"     # set to resolve [[note]] links

[linear]
enabled = true            # default: true when LINEAR_API_KEY is set

[linear.columns]
backlog = "Backlog"
triage = "Backlog"
unstarted = "Todo"
started = "In progress"
completed = "Done"
canceled = "Done"

[linear.state_names]
"In Review" = "Review"
```

## Herdr integration

- Plugin ID `nulluminati.herdboard`. Minimum Herdr version: confirmed against the installed binary before release.
- Action `board`: opens `herdboard board` as a Herdr tab in the focused pane's directory, so the board shows that repository. A tab gives the columns the full width.
- Key binding: the user binds the action in `~/.config/herdr/config.toml`, for example:

  ```toml
  [[keys.command]]
  key = "prefix+shift+b"
  type = "plugin_action"
  command = "nulluminati.herdboard.board"
  description = "herdboard: open the kanban board"
  ```

## Agent skill

`skills/herdboard/SKILL.md` teaches Claude and pi to use the CLI with `--json`: list the board, add todos, move cards, and start work. The skill never edits todo files directly.

## Packaging

- herdboard has its own `pyproject.toml` with a `herdboard` console entry point and Textual as its only dependency. This is an exception to the repository rule for standard-library plugins.
- The todo store and CLI do not import Textual, so they work without the board.

## Install (public)

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/herdboard
uv tool install "git+https://github.com/Nulluminati/herdr-extensions#subdirectory=plugins/herdboard"
npx skills add Nulluminati/herdr-extensions@herdboard
```

Requirements: Herdr 0.9 or later, `uv`, Python 3.11 or later.

## Testing

- Round-trip fixtures: real board files saved by Obsidian with the Kanban plugin, including multi-line cards, nested items, card limits, the complete marker, the archive and the settings block. Reading and writing a fixture without changes gives the same bytes.
- Concurrency tests: two writers on one board.
- Linear tests use a fake GraphQL server.
- `start` is tested live in a Herdr pane with a throwaway repository.

## Build order

1. Todo store, format contract, card IDs, locking, and the CLI, with tests
2. Textual board and Herdr action
3. Linear: read, views, cache
4. Linear: writes and workflow states
5. `start` and `promote`
6. `migrate` and `doctor`
7. Skill
