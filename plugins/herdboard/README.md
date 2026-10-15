# herdboard

A kanban board inside Herdr for your own todos and your Linear issues. Todos are Markdown files in the Obsidian Kanban format, so Obsidian can open them. Linear issues show on the same board, in the same columns, with the same commands.

A card can also start work: herdboard opens a Git worktree for it and hands the card to Claude Code or pi. When the agent is done, herdboard opens the work for review, merges it locally or as a draft GitHub pull request, and cleans up.

## Install

Requirements:

- Herdr 0.9 or later
- `uv` and Python 3.11 or later
- Claude Code or pi, to start work on a card
- The hunkdiff Herdr plugin (`jhochenbaum.hunkdiff`), for review in Hunk
- The GitHub CLI (`gh`), signed in, for pull requests

Install the Herdr plugin, the `herdboard` command and the agent skill:

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/herdboard
uv tool install "git+https://github.com/Nulluminati/herdr-extensions#subdirectory=plugins/herdboard"
npx skills add Nulluminati/herdr-extensions@herdboard -g -y
```

The board pane runs through `uv` from the plugin folder, so it works without the `herdboard` command. Agents need the command on the path.

To work on herdboard from a clone, link the plugin and install the command in editable mode:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/herdboard
uv tool install -e ~/Projects/herdr-extensions/plugins/herdboard
```

Bind the board action in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+b"
type = "plugin_action"
command = "nulluminati.herdboard.board"
description = "herdboard: open the kanban board"
```

The action opens the board as a Herdr tab in the focused pane's directory, so the board shows that repository.

## Quick start

1. Open a terminal in a repository, inside Herdr.
2. Run `herdboard init` to create the board for the repository.
3. Run `herdboard add "Fix login timeout"`. The command prints the card ID, for example `t3m9`.
4. Press the board key. Select the card and press `s`. herdboard creates the worktree and branch `t3m9-fix-login-timeout`, and starts the agent with the card as its brief.
5. When the agent moves the card to Review, press `R` to review the work.
6. Press `m` to merge, then confirm with `y`.

## The board

| Key | Action |
|---|---|
| `h` `j` `k` `l` or the arrow keys | Select a column or a card |
| `H` / `L` (or shift+left / shift+right) | Move the card to the previous or next column |
| `K` / `J` (or shift+up / shift+down) | Move a todo up or down in its column |
| `/` | Filter. Words match the title, tags and details. `#tag` matches a tag, and `#type/bug` a type. `^t5aa` shows `t5aa` and its sub-cards. Escape clears the filter |
| enter | Card detail, with the last 10 log entries |
| `a` | Add a card to the selected column |
| `e` | Edit the todo: title, type, tags, due date and details. ctrl+s saves |
| `d` | Mark the card done |
| `b` | Move the todo to another board, for example from the inbox to this repository |
| `x` | Delete the todo, after you confirm |
| `A` | Archive the todo. In the archive column, move it back to the done column |
| `D` | Archive every card in the done column, after you confirm |
| `z` | Show or hide the archive column |
| `p` | Set or remove the todo's parent |
| `s` | Start work with the default agent |
| `S` | Choose claude or pi, the model and pi's thinking level, then start work |
| `P` | Plan the card with an agent: choose claude or pi and the model |
| `R` | Review a started card: in Hunk, by another agent, or both |
| `m` | Merge the card, after you confirm |
| `f` | Finish a merged card, after you confirm |
| `v` | Change the view: repo, inbox, all, mine |
| `t` | Hide or show Linear cards |
| `i` | Create the board for this repository |
| `c` | Configure this repository's board: the merge mode and the linked Linear projects. ctrl+s saves |
| `r` | Reload, and fetch Linear again |
| `?` | Help |
| `q` | Quit |

### Views

| View | Shows |
|---|---|
| repo (default inside a repository) | The repository's todos and the issues of its linked Linear projects |
| inbox (default outside a repository) | `inbox.md`: todos that belong to no repository |
| all | Every board and your Linear issues |
| mine | Linear issues assigned to you, from every team |
| project (`--view project --project NAME`) | One Linear project's issues and the todos of every repository linked to it |

In a view with several boards, columns with the same name merge. Each card keeps its own board, so a move writes to the correct file.

### Card types

A card can have one type, such as bug or feature. The type is an Obsidian nested tag on the first line: `#type/bug`. Obsidian shows it as a tag.

- `herdboard add "Fix login timeout" --type bug` writes `Fix login timeout #type/bug`. `edit <id> --type feature` changes the type, and `--no-type` removes it.
- On the board, the add and edit forms have a Type field. The card shows the type before its tags. Bug is red, feature green, docs cyan and refactor magenta.
- The `types` setting lists the allowed types. The default is bug, feature, chore, docs and refactor. herdboard refuses another type with `unknown_type`. A `#type/<name>` tag that you write yourself always counts.
- JSON output has the type in `type`. `tags` leaves out the `type/` tag.
- `start` puts the conventional commit type in front of the branch name: `fix/t3m9-fix-login-timeout` for a bug, `feat/` for a feature. Other types use their own name, such as `chore/`. A Linear issue keeps its own branch name.
- The agent brief names the type and its commit type. The pull request title uses that commit type.
- A Linear issue's type is its first label that names a type, in any letter case. `Bug` is the type bug. `add --linear --type bug` and `promote` of a `#type/bug` todo add the label `bug` of the issue's team, else the workspace label `bug`. Without such a label, `add --linear` creates no issue.

### Archive

The archive keeps old todos off the board. It is the archive of the Obsidian Kanban plugin: a `***` line and a `## Archive` heading after the last column. Obsidian shows it in its own archive view.

- `A`, or `herdboard archive <id>`, moves a todo to the archive. `L` on the last column does the same when the archive column shows.
- `D`, or `herdboard archive --done`, archives every card in the done column. In a view with several boards, `D` archives the done column of each board, and skips a board without one. In a repository without a board, `archive --done` needs `--board inbox` to archive the inbox.
- `z` shows the archive column at the end of the board. The board hides it again on the next `z`. `a` does not add cards to the archive column.
- `A` on an archived card, or `herdboard unarchive <id>`, moves it back to the done column. A card that Obsidian archived without an ID takes its archive reference, such as `acme-api:archive-2` from `list --archived`, and gets an ID.
- A sub-card whose parent is archived too brings its parent back with it. The board asks first. The command asks in a terminal, and needs `--yes` without one.

herdboard does not archive a card whose worktree still exists, or whose agent or planner still runs. `finish` the card, or close the planner tab, first. A parent whose sub-cards stay on the board does not archive either. `archive --done` keeps these cards in the done column and reports why.

An archived card keeps its ID, its log and its attachments. `show` and `log` work on it, also with an archive reference. `log` gives a card without an ID its ID. Other commands, such as `start`, `plan`, `review`, `merge` and `attach`, refuse it with `archived` before they change anything. Linear issues are not archived here.

### Card markers

| Marker | Meaning |
|---|---|
| `↳ <parent>` | A sub-card. For a Linear sub-issue whose parent is not in the view, the marker shows the parent's identifier, such as `↳ ENG-12` |
| `2/5 done` | A parent card and the progress of its sub-cards |
| `agent review: pi` | An agent review runs |
| `PR: writing` | An agent writes the pull request description |
| `PR open`, `PR merged`, `PR closed` | The state of the card's pull request. The board reads it at most once a minute |
| `worktree removed` | The card's worktree folder is gone |
| `worktree still exists` | The card is in Done, but its worktree is still there. Run `finish` |

## Work on a card

### Plan before the work

1. Select the card and press `P`, or run `herdboard plan t3m9`.
2. Choose the planner (claude or pi) and its model. A new Herdr tab opens in the repository's main checkout, with the planner in it.
3. The planner reads the card and the code. It asks you about the unknowns, then writes the plan.
4. Answer its questions in the tab. Close the tab when the plan is complete.
5. Start the work with `s`. The agent gets the plan in its brief.

### Cards into main

1. `herdboard add "Fix login timeout"` adds the card `t3m9`.
2. `herdboard start t3m9` creates the worktree and the branch `t3m9-fix-login-timeout` from `main`, moves the card to In progress and starts the agent.
3. The agent commits on its branch and records progress with `herdboard log`. When it is done, it moves the card to Review.
4. `herdboard review t3m9` opens the branch diff in Hunk next to the agent. Send your comments to the agent with the hunkdiff `send-review` action. The agent moves the card back to In progress, fixes the code, commits, and moves the card to Review again.
5. `herdboard merge t3m9` merges the branch into `main`, or opens a draft pull request.
6. `herdboard finish t3m9` removes the worktree and the branch, and moves the card to Done. A local merge runs `finish` itself.

### Cards into a feature branch

1. `herdboard add "Billing v2"` adds the parent card `t5aa`.
2. `herdboard add "Add invoice table" --parent t5aa` adds a sub-card. Add one for each part of the work.
3. `herdboard start` on a sub-card first starts the parent with no agent (branch `t5aa-billing-v2`, from `main`). Then it starts the sub-card from the parent's branch.
4. Each sub-card merges into the parent branch.
5. `herdboard review t5aa` shows all the merged sub-cards together. `herdboard merge t5aa` merges the parent branch into `main`.

A parent with sub-cards that are not done does not merge. `--force` merges it anyway.

### start

`herdboard start <id>` does these steps:

1. Find the repository. For a repository board, it is that repository. For a Linear issue, it is the repository of the current view, or `--repo`. An inbox todo needs `--repo`, which takes a board name or a path in a checkout.
2. Find the local checkout. herdboard records every main checkout that it runs in. When it knows none, it asks for a path, or `--checkout PATH` gives one.
3. Choose the branch. A Linear issue uses its own branch name. A todo uses its ID and a short name from its title, such as `t3m9-fix-login-timeout`. A typed todo's branch starts with the commit type, such as `fix/t3m9-fix-login-timeout`.
4. Choose the base: `--base`, else the recorded base of an earlier start, else the parent's branch for a sub-card, else the branch of the main checkout. A Linear sub-issue does not use its parent's branch, because the parent can belong to another repository.
5. Open the worktree with `herdr worktree create`, or reuse the card's worktree when it exists. The base becomes the upstream of the card branch, so `git status` and Hunk compare the branch with its base.
6. Move the card to In progress (`start_column`). A Linear issue moves to its started state.
7. Start the agent in the worktree as `hb-<id>`. Its first message has the card and these rules: record progress with `herdboard log`, do not change the card text, commit with conventional commits, and do not push, merge or remove the worktree.

`--agent claude|pi`, `--model` and `--thinking` (pi only) choose the agent. `--no-agent` opens the worktree without one. herdboard does not start a second agent for a card whose agent still runs.

A failed `start` reports the steps that worked. Run `start` again to continue from the first step that failed.

### plan

`herdboard plan <id>` starts a planner agent in a new Herdr tab, in the main checkout of the card's repository. It finds the repository and the checkout as `start` does, and takes the same `--repo` and `--checkout`. It creates no worktree or branch, and the card stays in its column.

The planner gets a brief with the card, its linked notes, its attachments, its log and the current plan. Claude can read the repository and edit the card's attachments without a question. For other tools, it follows your default permission mode. pi gets only read, search and write tools, and no shell. The planner asks you questions until the main unknowns have answers. Then it writes the plan to the `plan.md` attachment, with these sections: Goal, Decisions, Steps, Testing, Out of scope and Open questions.

A background worker watches the plan. Each time the planner saves it and is idle, the worker logs `Plan saved: <path>`. When the tab closes, the worker logs `Planning ended.` It stops watching after 8 hours, but a planner that still runs still blocks a second one.

When planning ends with a changed plan and the card's work agent still runs, the worker tells that agent to read the plan. The agent tells you how its work differs, and waits for you to decide.

`plan` on a card that has a plan gives the planner the current plan to revise. A second `plan` for a card whose planner still runs fails with `plan_running`.

`--agent claude|pi`, `--model` and `--thinking` (pi only) choose the planner. On the board, `P` asks for them.

### Attachments

Attachments hold documents that are too large for the card text, such as a specification. They are files in `<todos_dir>/.herdboard/attachments/<id>/`, next to the log. They work for Linear cards too, and herdboard posts nothing to Linear.

- `herdboard attach <id> FILE [--name NAME]` copies a file into the card's attachments and logs it. An attachment with the same name is replaced.
- `herdboard attach <id> --remove NAME` removes an attachment.
- `herdboard show <id>` lists the attachments.

When `start` starts the work agent, its brief includes the `plan.md` attachment in full, and lists the other attachments by path. `review` and a rebase conflict give a brief only to a new agent, when the card's agent is gone. A running agent gets no new brief. An agent review gets the path of each attachment, and checks the work against the plan.

### Activity log

Each card has a log in `<todos_dir>/.herdboard/log/<id>.md`. Obsidian does not show hidden folders. One entry is one line:

```markdown
- 2026-10-12 14:03 · pi · Ready for review: 3 commits.
- 2026-10-12 14:20 · herdboard · Rebase conflict in src/auth.py. Sent to the agent.
```

herdboard sets the author. In a card's agent pane, the author is the agent kind (`claude` or `pi`). Entries that herdboard writes have the author `herdboard`. All other entries have the author `user`. The log works for Linear cards too, without a connection, and herdboard posts nothing to Linear.

Agents record progress in the log, not in the card. `edit` fails with `agent_edit_denied` in a card's agent pane. This guards against mistakes. It is not a security boundary.

### review

`herdboard review <id>` has three modes:

- `--human` (the default) opens the branch diff in Hunk, in a split next to the card's agent. Your Hunk comments go to that agent. A second `review` reuses the open Hunk pane. Hunk uses much memory, so herdboard closes the pane when the card leaves Review.
- `--agent` has a second agent review the work. By default, Claude reviews pi's work and pi reviews Claude's work. `--reviewer`, `--model` and `--thinking` change this.
- `--both` runs the agent review first, and opens Hunk when the findings are ready.

When the card's agent no longer runs, `review` starts a new one of the same kind. Its first message has the card and the full log.

The agent reviewer gets the commits since the base, the uncommitted changes and the untracked files. It starts in a split as `hb-<id>-review`. It can read the repository, and it can write only its findings file. A background worker waits up to 30 minutes for the findings. Then it closes the reviewer pane and sends the findings to the card's agent. The agent says for each finding whether it agrees or disagrees, logs its answer, changes no code, and waits for you. A Herdr notification tells you when the review is done or failed.

On the board, `R` asks for Human, Agent or Both. For an agent review, it then asks for the reviewer and the model: `opus`, `sonnet` or `haiku` for Claude, and the pi model list for pi.

### merge

The mode comes from `--local` or `--pr`, else from `merge:` in the board's frontmatter, else from `merge` in the config. The default is `local`. On the board, `c` sets the board's mode.

Both modes stop when the worktree has uncommitted or untracked files (`worktree_dirty`), or when the branch has no commits ahead of its base (`nothing_to_merge`).

Local merge:

1. Rebase the branch onto the base in the worktree. On a conflict, herdboard aborts the rebase, moves the card to In progress, and sends the conflicting files to the card's agent. The command exits with `rebase_conflict`.
2. Fast-forward the base to the branch. A checkout that has the base checked out must have no uncommitted changes to tracked files (`base_dirty`).
3. Log `Merged into <base> at <commit>.` and run `finish`.

herdboard does not push the base.

Pull request:

1. herdboard writes a request for the card's agent: the card, its log, the branch diff, and the repository's pull request templates. When the card's agent no longer runs, a read-only writer agent of the same kind starts in a split as `hb-<id>-pr`.
2. The agent writes the title and the description. The title is a conventional commit, such as `feat(auth): add magic links`. With a template, the description follows its headings in the same order. Without one, it uses Why, What changed, Trade-offs (only when there are some) and Testing. The agent describes only the testing that the log or the change shows. The text does not name herdboard, the agent or the card.
3. A background worker waits up to 30 minutes for the text. Then it pushes the branch to `origin` and runs `gh pr create --draft`. For a Linear card, it adds the issue link at the end of the description.
4. The worker logs `PR opened: <url>` and shows a Herdr notification. The card stays in Review.

When the branch already has an open pull request, `merge --pr` only pushes the new commits. `--rewrite` also has an agent write the title and description again, which replaces edits made on GitHub. On the board, `m` asks before it rewrites.

A failure logs `PR not opened: <reason>` and shows a notification.

### finish

`herdboard finish <id>` checks that the work is merged: the base contains the branch, or `gh` reports the pull request merged. Then it does these steps:

1. Close the Hunk pane.
2. Remove the worktree with `herdr worktree remove`.
3. Delete the branch. After a merged pull request, it also deletes the branch on `origin` if GitHub did not.
4. Move the card to its done column and log `Finished.`

A worktree with uncommitted or untracked files always stops `finish`. `--force` finishes a card that is not merged, but keeps its branch.

The board never runs `finish` itself.

## Linear

Linear is optional. It turns on when `LINEAR_API_KEY` holds a Linear personal API key.

| herdboard | Linear |
|---|---|
| Column | The workflow state of the issue's team |
| Card | Issue |
| Sub-card or indented checklist item | Sub-issue |
| `#tag` | Label |
| `#type/bug` | The label `bug` (any letter case) |
| `📅` date | Due date |
| Card ID (`t7k2`) | Identifier (`ENG-123`) |

### Link a repository to Linear projects

`herdboard link "API Platform" [--label repo:api]` links the current repository board to a Linear project. The repository view then shows every issue of that project. With labels, it shows only the issues that have one of the labels. `link --remove` removes the link. A project can serve several repositories, and a repository can have several projects.

On the board, `c` shows the linked projects. Press enter on a project to mark it for removal. Type a project name, and optional labels separated by commas, to link a project. Enter in the project field moves to the labels. ctrl+s, or enter in the labels field, writes the changes. A label name with a comma needs `herdboard link --label`. A link needs a Linear connection, so an offline board (`--offline`) cannot add one. A removal works offline.

When Linear's GitHub integration attaches a pull request from this repository to an issue, the card shows `PR`.

### Workflow states

- Each state type maps to a column: backlog and triage to Backlog, unstarted to Todo, started to In progress, completed to Done, canceled to Done. `[linear.columns]` changes this. `[linear.state_names]` maps one state by name, for example `"In Review" = "Review"`.
- A move picks the state of the issue's team that maps to the column. A canceled state is used only when no other state matches, so `done` picks the completed state.
- When several states of one team match, herdboard asks once and stores the answer for that team. Without a terminal or with `--json`, the command fails with `ambiguous_state` and lists the states. `--state <name>` gives the answer.

### Offline use

herdboard caches Linear data per workspace and per user. When Linear is not reachable, the board shows the cache and marks it stale. Linear cards are read-only while you are offline. `--offline` on a command uses only the cache.

Completed issues show for `done_days` days (default 14). Archived issues do not show.

Before a write, herdboard fetches the issue again. When a write times out, it checks the result before it reports. When it cannot learn the result, it fails with `linear_unknown`. Check the issue in Linear before you try again.

### Card order

Linear cards follow Linear's manual order, after the local cards of the column. `move --position top|bottom` puts an issue below the lowest or above the highest order value of its team and state.

### promote

`herdboard promote <id> [--team ENG] [--project NAME] [--state NAME]` turns a todo into a Linear issue assigned to you:

- The first line becomes the title. Tags become labels when a label with that name exists. `#type/bug` becomes the label `bug`. The due date stays. A linked note becomes the description when `vault_root` is set.
- Indented checklist items and sub-cards become sub-issues. A sub-card cannot be promoted alone (`parent_not_promoted`).
- The issue starts in the state of the todo's column.
- Anything that herdboard cannot convert goes into the description as text.

After Linear confirms every issue, the todo moves to Done with `→ ENG-123` on its first line. The todo is not deleted. herdboard records each step first, so a second `promote` after a failure creates only the missing issues.

## Commands

| Command | Action |
|---|---|
| `herdboard board [--view NAME] [--project NAME]` | Open the board |
| `herdboard list [--view] [--board] [--column] [--source todo\|linear] [--archived]` | List cards. `--archived` lists the archived todos |
| `herdboard add "text" [--type NAME] [--board] [--column] [--position top\|bottom] [--parent ID]` | Add a todo. Default board: the parent's board, else the repository board, else `inbox.md` |
| `herdboard add --linear "title" [--type NAME] [--team] [--project] [--label] [--column] [--state]` | Create a Linear issue assigned to you. In a repository with one linked project, that project and its labels are the default |
| `herdboard show <id> [--log]` | Show a card, its attachments and its last 10 log entries. `--log` shows them all |
| `herdboard move <id> <column> [--position top\|bottom] [--state]` | Move a card to a column |
| `herdboard move <id> --up\|--down` | Move a todo up or down in its column |
| `herdboard move <id> [column] --board NAME` | Move a todo, with its ID and details, to another board |
| `herdboard done <id> [--state]` | Move a card to its board's done column |
| `herdboard edit <id> [--title] [--body \| --body-file] [--parent ID \| --no-parent] [--type NAME \| --no-type]` | Change a todo's first line, its indented lines, its parent or its type. `--body ""` removes the indented lines |
| `herdboard log <id> "text"` | Add an entry to the card's activity log |
| `herdboard attach <id> FILE [--name NAME]` / `attach <id> --remove NAME` | Add a file to the card's attachments, or remove one |
| `herdboard plan <id> [--agent claude\|pi] [--model] [--thinking] [--repo] [--checkout PATH]` | Plan the card with an agent in a new Herdr tab |
| `herdboard delete <id> [--yes]` | Delete a todo and its indented lines |
| `herdboard archive <id>` / `archive --done [--board NAME]` | Move a todo to its board's archive, or every card in the done column |
| `herdboard unarchive <id> [--yes]` | Move an archived todo back to its board's done column. `--yes` also moves an archived parent back without a question |
| `herdboard start <id> [--agent claude\|pi] [--model] [--thinking] [--no-agent] [--repo] [--checkout PATH] [--base BRANCH]` | Start work on a card |
| `herdboard review <id> [--human \| --agent \| --both] [--reviewer claude\|pi] [--model] [--thinking]` | Review a started card's work |
| `herdboard merge <id> [--local \| --pr] [--rewrite] [--force]` | Merge the card branch, or open a draft pull request |
| `herdboard finish <id> [--force]` | Remove a merged card's worktree and branch, and move it to Done |
| `herdboard promote <id> [--team] [--project] [--state]` | Turn a todo into a Linear issue |
| `herdboard link <project> [--label NAME] [--remove]` | Link the repository board to a Linear project |
| `herdboard init` | Create the board for the current repository |
| `herdboard migrate <TODO.md> [--board] [--yes]` | Convert a free-form `TODO.md` into cards |
| `herdboard doctor [--fix-ids] [--offline]` | Check the boards, the config, Linear and the state files |

- `<id>` is a todo ID (`t7k2`), a temporary reference (`homelab:12`) or a Linear identifier (`ENG-123`).
- Every command takes `--json`, `--config FILE` and `-C DIR` (run as if in `DIR`).
- With `--json`, output goes to standard output as JSON. An error has the form `{"error": {"code": "…", "message": "…"}}`. The skill lists the error codes.
- Exit codes: 0 success, 1 user error, 2 conflict (the card changed, the lock is held, a rebase conflict), 3 Linear error.
- A command that asks a question in a terminal fails without a terminal or with `--json`. `delete --yes` and `migrate --yes` confirm without a question.

### migrate

`herdboard migrate TODO.md` converts a free-form todo file. It reads Markdown and underlined headings, a plain line that ends with `:`, all common bullets, checkboxes, and tab or space indents.

- Each top-level item of a section becomes a card, with its deeper items indented under it.
- The section name becomes a tag: `Follow Ups:` becomes `#follow-ups`.
- Open items go into the first column. Checked items go into the done column.

herdboard shows the result and asks before it writes. The original file does not change.

### doctor

`herdboard doctor` reports problems with a level, a code and a fix when there is one. It checks:

- Boards that do not parse, duplicate card IDs, and two boards with the same repository
- The config file, and Linear links, labels and the API key
- Sub-cards whose parent is missing
- Unfinished `promote` records, old start records and old checkout paths
- Whether `herdr` and `gh` are on the path

`--fix-ids` gives a new ID to each duplicate card except the first. `--offline` skips the Linear checks. `doctor` never changes Linear.

## Configuration

The config file is `~/.config/herdboard/config.toml`. `HERDBOARD_CONFIG` or `--config` names another file. Every setting has a default, so the file is optional.

```toml
todos_dir = "~/todos"
columns = ["Backlog", "Todo", "In progress", "Review", "Done"]   # for new boards
complete_markers = []      # added to the English "Complete"
archive_titles = []        # added to the English "Archive"
# vault_root = "~/Notes"   # resolves [[note]] links in `show` and `promote`
start_agent = "claude"     # or "pi"
# start_model = "openai-codex/gpt-6.1-sol"   # default: the agent's own default
# start_thinking = "medium"                  # pi only: off, minimal, low, medium, high, xhigh, max
# pi_models = ["zai/glm-5.3"]                # the board's pi model list; default: pi's enabledModels
# start_column = "In progress"               # default: linear.columns.started
merge = "local"            # or "pr"; a board's `merge:` frontmatter wins
theme = "ansi-dark"        # the terminal's own colors; "ansi-light" or any Textual theme name
tag_style = "round"        # tag ends in Nerd Font half circles; "block" for other fonts
types = ["bug", "feature", "chore", "docs", "refactor"]   # card types: the #type/<name> tag

[linear]
# enabled = true           # default: on when the API key is set
api_key_env = "LINEAR_API_KEY"
done_days = 14             # completed issues older than this are hidden

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

To make pull requests the default for one repository, add `merge: pr` to the frontmatter of its board, or press `c` on the board and choose `pr`.

## Board files

```
~/todos/                       # todos_dir
  inbox.md                     # todos that belong to no repository
  projects/
    nulluminati-homelab.md     # one board per repository
    acme-api.md
  .herdboard/log/              # activity logs
  .herdboard/attachments/      # card attachments, such as plan.md
```

A board file uses the format of the Obsidian Kanban plugin:

````markdown
---
kanban-plugin: board
repo: github.com/acme/api
merge: pr
---

## Backlog

- [ ] Compare tile suppliers #research ^t3m9
	- [ ] Ask for samples

## Todo

- [ ] Order tiles 📅 2026-10-20 ^t7k2
- [ ] Add invoice table [[#^t5aa]] ^t4b1

## In progress

- [ ] Billing v2 ^t5aa

## Review

## Done

**Complete**
- [x] Measure the room ^t2a6

%% kanban:settings
```
{"kanban-plugin":"board"}
```
%%
````

- A `##` heading is a column. A title can end in a card limit, such as `Todo (5)`.
- A card is a `- [ ]` line and all the indented lines under it.
- `^t7k2` is the card ID. IDs are unique across all boards. The first write to a board gives an ID to every card that has none. Until then, `list` shows temporary references such as `homelab:12`.
- `#tag` is a tag. `📅 YYYY-MM-DD` is a due date. Other Obsidian Tasks syntax stays as text.
- `[[#^t5aa]]` makes the card a sub-card of `t5aa` on the same board. Sub-cards go one level deep.
- `[[note]]` is a note link. With `vault_root` set, `show` displays the note.
- `**Complete**` under a heading marks the done column. A move into it checks the box, and a move out clears it.
- `***` and `## Archive` start the archive. Only `archive` and `unarchive` move cards in and out of it.
- herdboard keeps every line that it does not understand, the settings block, the line endings and the indent style.

### Repository identity

herdboard finds a repository's board from its `origin` remote, as `host/owner/repo`. SSH and HTTPS remotes give the same identity. The board stores it in `repo:` frontmatter. All worktrees of a repository share its board. A repository without a remote uses the path of its main checkout.

### Writes

- Agents and scripts change boards only through the CLI.
- Every write takes one lock for the store (`<todos_dir>/.herdboard.lock`), reads the file again, and replaces it in one step. When the card changed since the command read it, the command stops with exit code 2. Changes to other cards stay.
- Obsidian does not use the lock. herdboard cannot see an unsaved Obsidian edit. The check above catches most conflicts.

## Files outside the board

herdboard keeps its state in `~/.local/state/herdboard/`:

| Path | Content |
|---|---|
| `starts.json` | The worktree, branch, base, agent pane and pull request of each started card |
| `checkouts.json` | The known main checkouts of each repository |
| `plans.json`, `plans/<id>.log` | The running planner of each card, and the plan worker's output |
| `reviews/<id>-<time>-<random>/` | Agent review requests and findings |
| `prs/<id>-<time>-<random>/` | Pull request requests and the written description |
| `<workspace>/<user>/` | The Linear cache, state choices and `promote` records, per Linear account |

## Known limits

- Claude shows its folder trust prompt in a worktree where no Claude ran before. A new agent, reviewer, writer or planner waits until you answer it in its pane.
- In Claude's default permission mode, the card's agent asks once before it writes the pull request description in the state folder.
- herdboard does not sync between machines.
- It is not confirmed that Obsidian opens a `[[#^t5aa]]` parent link from a card.

## Agent skill

`skills/herdboard/SKILL.md` teaches Claude and pi to use the CLI with `--json`. It lists the error codes.

## Tests

```bash
uv run python -m unittest discover -s tests
```

Run it from the repository root. The tests use fake `herdr`, `gh` and Linear servers. Behavior that depends on Herdr, the agents or GitHub needs a live test with a throwaway repository. [docs/linear-live-test.md](docs/linear-live-test.md) is the checklist for the real Linear API.
