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
  inbox.md                        # todos that belong to no repository
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
- A move to a column resolves the column to the states of the issue's team that map to it. A canceled state is a candidate only when no other state matches. A triage state is a candidate only when no backlog state matches. So `done` picks the completed state, not the canceled one.
- When several states of one team match a column (for example, two started states), the tool asks which one to use, then stores the choice by state ID for that team.
- When a stored state ID no longer exists, the tool asks again.
- The CLI asks with a numbered list in a terminal. Without a terminal, or with `--json`, it fails with the code `ambiguous_state` and lists the candidates. `--state <name>` on `move`, `done` and `add --linear` answers the question, and the tool stores that answer too. The board asks in a dialog.
- Choices live in `~/.local/state/herdboard/<workspace-id>/<user-id>/state-choices.json`, keyed by team ID and column.
- When no state of the team matches a column, the move fails and names the team and the column.

### Repositories and Linear projects

- The link is many-to-many. Each repository board lists its Linear projects in `linear-projects` frontmatter. A project that serves several repositories appears in each of those lists. The tool computes the reverse direction.
- `herdboard link <project> [--label <label>]` writes the link with stable IDs and display names. Names update when they change in Linear. It replaces only the `linear-projects` entry in the frontmatter; other keys, their order and comments stay unchanged.
- A repository view shows every issue of each linked project, for every assignee. A link with labels shows only issues that have at least one of those labels.
- When a linked project or label no longer exists or is not accessible, the board shows an error for that link. It does not show an empty board.
- When Linear's GitHub integration attaches a pull request from this repository to an issue, the card shows a marker. The tool matches the pull request URL against the board's `repo:` identity. The marker does not filter.

### API rules

- Every list query uses pagination until the end.
- Every response is checked for GraphQL errors, and every mutation for `success`.
- A rate-limited read waits and retries, with a limit. A rate-limited write fails and says so.
- An issue that two linked projects return shows once (by issue ID).
- Archived projects and issues are hidden. `--archived` shows them.
- Completed issues show only when they were completed in the last `done_days` days (default 14). Linear filters them on the server with a duration such as `-P14D`. Open issues always show.

### Cache and offline use

- The tool caches Linear data in `~/.local/state/herdboard/<workspace-id>/<user-id>/`, so a different API key never sees another account's cache.
- The board shows the time of the last fetch. When Linear is not reachable, the board shows the cache and marks it stale.
- Writes to Linear need a connection. Offline, Linear cards are read only.
- Before a write, the tool fetches the issue again. If a write times out, the tool fetches the issue to learn the result before it reports success or failure. It never repeats a write blindly.
- A new issue gets a client-generated ID (`IssueCreateInput.id`), so after a timeout the tool can look up that ID to learn whether Linear created it.
- After a write timeout, the tool checks the issue up to three times. It waits 0.5 seconds and 1 second between checks. If no check confirms the write, it reports `linear_unknown`. An old state or missing issue does not prove rejection.
- For an unknown create result, the message and `--json` error contain the generated ID (`issue_id`). Check that ID in Linear before another create.
- Successful and unknown writes invalidate the account cache. Cache filesystem failures do not change the write result. If the tool cannot record invalidation, it reports a warning on stderr.
- A move to the state that the issue already has changes nothing and succeeds.
- After a write, the tool drops the cached queries that the write affects, so the next list shows the change.

### Card order

- Linear cards in a column follow Linear's manual order (`sortOrder`, lowest first), after the local cards of the column.
- `--position top|bottom` on a Linear issue sets `sortOrder` below the lowest or above the highest value among the issues of the same team and target state. For completed and canceled states, only issues inside the `done_days` window count.

## Views

| View | Shows |
|---|---|
| Repository (default inside a repository) | That repository's todos and every issue of its linked Linear projects |
| Linear project | That project's issues and the todos of every repository linked to it |
| My issues | Linear issues assigned to you, from every team |
| Inbox (default outside a repository) | `inbox.md`: todos that belong to no repository |
| All | Every board and your Linear issues |

Outside Git, or before a repository board exists, `add` goes to `inbox.md` unless `--board` names a board. `herdboard init` creates the board for the current repository. A board with invalid frontmatter is shown read only, with the error.

## CLI

| Command | Action |
|---|---|
| `herdboard board [--view]` | Open the kanban (Textual) |
| `herdboard list [--view] [--source] [--column] [--json]` | List cards |
| `herdboard add "text" [--board] [--column]` | Add a todo. Default board: the current repository's board, else `inbox.md`. Default column: the first column |
| `herdboard add --linear [--team ENG] [--project NAME] [--label NAME] [--column] [--state] "title"` | Create a Linear issue, assigned to you. `--team` is optional when you belong to one team, or when the project has one team. Inside a repository board with exactly one linked project, that project and its link labels are the default, so the issue shows in the repository view. Default column: the first column |
| `herdboard show <id> [--log]` | Show a card, its detail and the last 10 entries of its activity log. `--log` shows every entry |
| `herdboard move <id> <column> [--position top\|bottom] [--state]` | Move a card. Default position for a todo: bottom. A Linear issue without `--position` keeps its place in Linear's order |
| `herdboard move <id> --up\|--down` | Swap a todo with the card above or below it in its column. Linear issues keep Linear's order |
| `herdboard move <id> [column] --board NAME` | Move a todo, with its details and its ID, to another board (`inbox` or a project name). Without a column, it keeps its column name when the board has it, else it goes to the first column. Linear issues stay in Linear |
| `herdboard done <id>` | Move a card to its board's done column |
| `herdboard edit <id> [--title TEXT] [--body TEXT \| --body-file PATH]` | Change a todo's first line, its indented lines, or both. The checkbox and the ID stay. `--body ""` removes the indented lines. Linear issues are not edited |
| `herdboard log <id> "text"` | Append an entry to the card's activity log (see below). Works for todos and Linear issues, with no Linear connection |
| `herdboard delete <id> [--yes]` | Delete a todo and its indented lines. Asks first in a terminal; without a terminal or with `--json`, `--yes` is required. Linear issues are not deleted |
| `herdboard start <id> [--agent claude\|pi] [--model NAME] [--thinking LEVEL] [--no-agent] [--repo] [--checkout PATH] [--base BRANCH]` | Start work on a card (see below). `--model` sets the agent's model (pi: `provider/model`; Claude: `opus`, `sonnet` and so on). `--thinking` sets pi's thinking level |
| `herdboard review <id>` | Open the card's branch diff in Hunk, next to its agent (see below) |
| `herdboard merge <id> [--local \| --pr]` | Merge the card branch into its base, then finish the card (see below). `--pr` is not built yet |
| `herdboard finish <id> [--force]` | Remove a merged card's worktree and branch, and move the card to Done (see below) |
| `herdboard promote <id> [--team ENG] [--project] [--state]` | Turn a todo into a Linear issue (see below) |
| `herdboard link <project> [--label]` | Link the current repository board to a Linear project |
| `herdboard init` | Create the board for the current repository |
| `herdboard migrate <TODO.md> [--board] [--yes]` | Convert an existing `TODO.md` (see below) |
| `herdboard doctor [--fix-ids] [--offline]` | Check the boards, the config, Linear links and the state files (see below) |

- `<id>` is a todo ID (`t7k2`), a temporary reference (`homelab:12`) or a Linear identifier (`ENG-123`).
- `list --json` also returns `boards`: each listed board's name, path and every column (title, card limit, done flag), including empty columns.
- With `--json`, every command writes JSON to standard output, and errors have the form `{"error": {"code": "…", "message": "…"}}`.
- Exit codes: 0 success, 1 user error (unknown ID, missing column), 2 conflict (the card changed, the lock is held), 3 Linear error.

### log

Each card has an activity log: `<todos_dir>/.herdboard/log/<id>.md`, one file per todo ID or Linear identifier. Obsidian ignores the hidden folder. Each entry is one line, `- 2026-10-12 14:03 · pi · Ready for review.`, and extra lines of a message are indented below it. The author comes from the Herdr pane: the kind of agent in `HERDR_PANE_ID` (`claude`, `pi`), else `user`. The tool only appends, under the store lock.

Agents record progress in the log, not in the card. `edit` fails with `agent_edit_denied` when `HERDR_PANE_ID` is the agent pane of a started card. This guards against mistakes. It is not a security boundary.

### start

1. Find the repository. For a repository board, it is that repository. For a Linear issue, it is the repository of the current view, or `--repo`. For an inbox todo, `--repo` is required. `--repo` takes a board name or a path inside a checkout. The tool never picks the first of several linked repositories.
2. Find the local checkout from a registry of checkouts in `~/.local/state/herdboard/checkouts.json`. The registry maps a repository identity to the paths of its main checkouts. The tool adds the main checkout each time it runs inside a repository. If none is known, `start` asks for a path in a terminal. Without a terminal, or with `--json`, it fails with the code `checkout_unknown`. `--checkout PATH` answers, and the tool stores the answer. When several checkouts are known, the tool uses the most recently seen one that still exists.
3. Choose the branch. For a Linear issue, the issue's `branchName`. For a todo, the card ID plus a short name from the card text, for example `t7k2-fix-login-timeout`: lowercase, words joined by `-`, without tags, dates and links, at most 50 characters. The tool validates the name with `git check-ref-format --branch`. A todo without an ID gets one first, with the usual write rules.
4. Choose the base: the local branch that the card branch starts from, and later merges into. `--base` names it. Else a restart keeps the recorded base. Else it is the branch of the main checkout. A base that is not a local branch, or a checkout with a detached HEAD and no `--base`, fails with `base_unknown` before the worktree exists.
5. Open the worktree:
   - If the state file has a worktree for the card and the path still exists, reuse it. If Herdr has it open as a workspace, focus that workspace. Otherwise open it with `herdr worktree open --path`.
   - If Git already has a worktree for the branch, open that worktree.
   - Otherwise create it with `herdr worktree create --cwd <checkout> --branch <branch> --base <base> --focus`. Herdr creates the branch from the base when the branch does not exist, and checks it out when it exists. Herdr chooses the worktree path (`~/.herdr/worktrees/<repo>/<branch>`).
   - Herdr also opens a workspace for the main checkout when none is open. The tool leaves it open.
6. Set the base as the upstream of the card branch: `git branch --set-upstream-to=<base> <branch>`. Hunk's branch review and `git status` then compare the branch with its base. A failure stops with `upstream_failed`.
7. Move the card to the column that `start_column` names (default: the column that `linear.columns.started` names, "In progress"). For Linear, the issue moves to the mapped started state. A card that is already in that column does not move.
8. Start the agent in the root pane of the worktree workspace: `herdr agent start hb-<id> --kind <agent> --pane <pane> -- <message>`. The message has the card ID, title, body, URL for Linear, and this instruction: use the `herdboard` CLI to update the card. The agent comes from `--agent`, else `start_agent` in config (default `claude`). `--no-agent` skips this step. A startup timeout or `agent_not_ready` from Herdr does not mean the agent failed (see peer-review). The tool does not start a second agent for a card whose stored agent pane still runs an agent.
9. Each step records its progress in `~/.local/state/herdboard/starts.json`, keyed by the card ID or the Linear issue ID. A failed `start` reports which steps succeeded. Running `start` again continues from the first step that did not finish.
10. `start` needs Herdr: a `herdr` binary (`HERDR_BIN_PATH`, else `herdr` on the path) and a reachable Herdr server. Without them it fails with the code `herdr_unavailable`.

On the board, `s` starts the selected card with the default agent. `S` asks for the agent first.

When the board refreshes, it checks each stored worktree. If the path no longer exists, the card shows `worktree removed`. The card does not move by itself, and the state entry stays until the next `start` of that card replaces it.

Review, merge and finish after `start` are designed in [docs/review-and-merge.md](docs/review-and-merge.md).

### review

`herdboard review <id>` opens the work of a started card in the hunkdiff Herdr plugin (`jhochenbaum.hunkdiff`). The card's branch has its base as upstream, so Hunk shows `<base>...HEAD`.

1. Read the start record. Without one, or when the worktree is gone, stop with `not_started`.
2. Focus the card's worktree workspace, or open it again, as `start` does.
3. If the recorded agent no longer runs, start a new agent of the same kind. Its brief has the card, the full activity log, and a note that review feedback follows. Log: `Agent restarted for review.`
4. Focus the agent pane, then invoke the hunkdiff action `review:branch`. hunkdiff reads its target from the focused pane, and it links the review to the agent only when that pane runs an agent. Then the hunkdiff `send-review` action sends your comments to that agent. Without the plugin, stop with `hunk_unavailable`.
5. Store the ID of the new Hunk pane. A second review reuses the open pane. Log: `Review opened.`

Hunk uses much memory, so the pane stays open only while the card is in Review. When a herdboard command or a board key moves the card to another column or deletes it, the tool closes the pane.

On the board, `R` reviews the selected card.

### merge and finish

The design is in [docs/review-and-merge.md](docs/review-and-merge.md). The local mode is built:

1. Stop with `worktree_dirty` when the card's worktree has uncommitted or untracked files, and with `nothing_to_merge` when the branch has no commits ahead of its base.
2. If a checkout has the base checked out and has uncommitted changes to tracked files, stop with `base_dirty`.
3. Rebase the branch onto the base in the worktree. On a conflict, abort the rebase, move the card to In progress, and send the conflicting files to the card's agent (a new agent if it is gone). Exit with `rebase_conflict`, code 2.
4. Fast-forward the base: `git merge --ff-only` in the checkout that has it, else `git update-ref` with the old value. A failure stops with `merge_failed`.
5. Log `Merged into <base> at <commit>.` and run `finish`.

The tool does not push the base.

`finish` checks that the base contains the branch (else `not_merged`), and that the worktree has no uncommitted or untracked files (else `worktree_dirty`, also with `--force`). Then it closes the Hunk pane, removes the worktree with `herdr worktree remove`, deletes the branch, moves the card to its done column (a card already there stays), logs `Finished.`, and removes the start record. `--force` finishes an unmerged card, but keeps its branch.

The mode comes from `--local` or `--pr`, else from `merge:` in the board's frontmatter, else from `merge` in the config (default `local`).

On the board, `m` merges and `f` finishes the selected card, after you confirm with `y`.

### promote

`herdboard promote <id> [--team ENG] [--project NAME] [--state]` turns a todo into a Linear issue. Team and project follow the rules of `add --linear`, but the default project and labels come from the board links of the todo itself, not the current directory (an inbox todo has none); `--project` wins.

1. Read the todo, including its indented lines, tags and due date. The todo needs an ID; a todo without one gets one first.
2. Create the Linear issue, assigned to you:
   - The first line, without tags, the due date and the block ID, becomes the title.
   - Indented checklist items (`- [ ] …` one level below the card) become sub-issues, in order. A checked item becomes a sub-issue in the completed state.
   - A tag becomes a label when a label with that name exists in the issue's team or in the workspace. A team label wins over a workspace label.
   - The `📅` date becomes the due date.
   - A linked note becomes the description when `vault_root` is set and the note exists.
   - Anything that cannot be converted goes into the description as text: other indented lines, deeper sub-items, tags without a label, and links without a note.
   - The issue starts in the state of the todo's current column. A todo in Done becomes an issue in the completed state.
   - A sub-issue shares the project and the assignee of the issue, and has no labels. An unchecked sub-issue starts in the state of the issue. If that state is completed, it starts in the state of the first column (the default of `add --linear`).
3. Record the operation in `~/.local/state/herdboard/<workspace-id>/<user-id>/promotions.json` before the first call: the todo ID, and a client-generated ID and the create arguments for the issue and for each sub-issue. Update the record after each confirmed issue. A rerun follows the record, not the todo, so it creates exactly the planned issues. The cache drop keeps the file. One `promote` runs at a time per account.
4. Only after Linear confirms the issue and every sub-issue, move the todo to its board's done column and add `→ ENG-123` to its first line, before the block ID. The todo is not deleted. Then remove the record. If the todo changed since `promote` read it, the todo stays as it is, the record stays, and the command exits with code 2. Running `promote` again then finishes the todo update and creates nothing.
5. If a call times out, the tool looks up the recorded ID before it creates anything again (see "Cache and offline use"). Running `promote` again on a todo with a record looks up every recorded ID and creates only what Linear does not have.
6. A todo whose first line already has `→ <identifier>` is promoted. `promote` refuses it with the code `already_promoted`.

### migrate

`herdboard migrate <TODO.md> [--board NAME] [--yes]` converts a free-form todo file. The existing `TODO.md` files use several styles, and the tool reads all of them:

- Section headings: Markdown headings (`# Testing`), underlined headings (`Dots Todo:` with `=====` or `-----` below), and a plain line that ends with `:` and has list items below it (`Follow Ups:`). The trailing `:` is not part of the name.
- List items: `-`, `*`, `+` and `1.` bullets, with or without a checkbox (`- [ ]`, `- [x]`).
- Indentation in tabs or spaces. The tool finds the indent unit of the file (a tab, or the smallest step in spaces) and writes one tab for each level. A single one-space indent is ignored for the unit when the other indents are multiples of a larger step, or when the file is indented with tabs. Mixed tab and space prefixes are read in order, a tab advancing to the next multiple of the unit. A line whose level is not a whole number of units is listed in the preview as ambiguous, with the parent it got.

Conversion:

1. A top-level item of a section becomes a card. Deeper items stay indented under their card, with their nesting.
2. The section name becomes a tag on each of its cards: `Follow Ups` becomes `#follow-ups` (lowercase, other characters become `-`). Items before the first heading get no tag.
3. Open items go into the first column (Backlog). Checked items go into the done column, checked.
4. A text line that is not a list item becomes an indented line of the card above it. Without a card above it in its section, it becomes a card. The preview lists these lines, so you can check them.
5. Blank lines and heading underlines are dropped.

Target and writes:

1. The target is the board of the repository that contains the file, or `--board`. When that board does not exist, the tool creates it like `init`. When it exists, the tool adds the cards at the bottom of their columns and changes nothing else.
2. The tool shows the result first: the cards for each column, with their tags and indented lines. In a terminal it asks for confirmation. Without a terminal, or with `--json`, it writes nothing unless `--yes` is given. `--json` returns the preview and `"written": true|false`.
3. The write uses the normal write rules: one lock, new IDs, atomic replace.
4. The original file is not changed. The tool prints its path, so you can delete it yourself.

### doctor

`herdboard doctor [--fix-ids] [--offline]` checks the store and reports each finding with a level (error, warning, info), a code, the file and line when there is one, and a fix when one exists. Exit code 0 when there is no error, 1 when there is an error.

| Check | Level |
|---|---|
| A board does not parse, its frontmatter is not closed, or it has no `kanban-plugin: board` | error |
| Two cards share an ID, in one board or across boards, archive included | error |
| Two boards claim the same `repo:` identity | error |
| `linear-projects` frontmatter is not valid | error |
| The config file does not parse or has an invalid value | error |
| A board has no done column | warning |
| A card has no ID (it gets one on the next write) | info |
| Linear is enabled, but `LINEAR_API_KEY` is not set or is refused | error |
| A linked Linear project or label does not exist or is not accessible | warning |
| An unfinished promotion record exists. The fix names the `promote` command that finishes it | warning |
| A `start` record points to a worktree that no longer exists | info |
| The checkout registry has a path that no longer exists or is no longer that repository | info |
| No `herdr` binary is found (only `start` needs it) | info |

- `--offline` skips the Linear checks.
- `--fix-ids` gives a new ID to every duplicate except the first, in this order: board path, then file order, with archive cards last. It uses the normal write rules and reports each change. No other check changes a file.
- `doctor` never changes Linear.

## Configuration

`~/.config/herdboard/config.toml`. Every setting has a default, so the file is optional.

```toml
todos_dir = "~/todos"
columns = ["Backlog", "Todo", "In progress", "Review", "Done"]
complete_markers = []      # added to the English "Complete"
archive_titles = []        # added to the English "Archive"
# vault_root = "~/Notes"     # set to resolve [[note]] links
start_agent = "claude"     # or "pi"
# start_model = "openai-codex/gpt-6.1-sol"   # default: the agent's own default
# start_thinking = "medium"                  # pi only: off, minimal, low, medium, high, xhigh, max
# pi_models = ["zai/glm-5.3", "openai-codex/gpt-6.1-sol"]   # the board's pi model list; default: pi's enabledModels
theme = "ansi-dark"        # the terminal's own colors; "ansi-light" or any Textual theme name
tag_style = "round"        # tag blocks with Nerd Font round ends; "block" for other fonts
# start_column = "In progress"   # default: linear.columns.started
merge = "local"            # or "pr"; a board's `merge:` frontmatter wins

[linear]
enabled = true            # default: true when LINEAR_API_KEY is set
done_days = 14            # completed issues older than this are hidden

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
