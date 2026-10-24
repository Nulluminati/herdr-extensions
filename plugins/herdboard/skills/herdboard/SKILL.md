---
name: herdboard
description: "Read and update the user's kanban boards with the herdboard CLI: local todos in ~/todos (one board per repository plus an inbox) and their Linear issues. Use when the user asks about their todos, backlog, board, cards, or Linear issues, asks to add, move, finish, start, archive or promote a card, or when a herdboard card was handed to you as your first message. Not for your own session task list."
---

# herdboard

herdboard keeps the user's todos as Markdown kanban boards (Obsidian Kanban format) and shows Linear issues in the same columns. You use it only through its CLI. Never edit the board files in `~/todos` yourself: the CLI takes a lock, checks for conflicts and assigns card IDs.

## Run the CLI

Use `herdboard` when it is on the `PATH`. Otherwise run it from the Herdr plugin:

```bash
uv run --quiet --project "$(herdr plugin list --plugin nulluminati.herdboard --json | jq -r '.result.plugins[0].plugin_root')" herdboard --json list
```

Always pass `--json` (before the command name or after it). Read stdout as JSON. Run commands from the repository directory, or pass `-C DIR`, because the current repository selects the board.

## Cards

- A card reference is a todo ID (`tep3`), a temporary reference (`acme-api:12`, before the board has IDs) or a Linear identifier (`ENG-123`). Use the `ref` field from `list` output.
- A card has `title`, `column`, `checked`, `due`, `tags`, `type`, `body`, `board`, `source` (`todo` or `linear`), `url`, `state` and `team`. `read_only: true` means you cannot change it. `archived: true` means the todo is in its board's archive. `stale: true` means the Linear data comes from the cache.
- Columns: `list --json` returns `boards`, with every column of each listed board (`title`, `limit`, `done`), also the empty ones. Move a todo only to a column of its own board. A Linear issue can move to any column that one of its team's workflow states maps to; the defaults are Backlog, Todo, In progress, Review and Done. When no state fits, the error is `no_state`.
- Tags and the due date are part of the card text: `#auth` adds a tag, `📅 2026-11-01` sets the due date.
- The type is a tag too: `#type/bug`. The `type` field holds `bug`, and `tags` leaves it out. The `types` setting lists the types (default: bug, feature, chore, docs, refactor). A Linear issue's type is its first label that names a type.
- A todo can be a sub-card of another todo on the same board. Its `parent` field names the parent. Sub-cards go one level deep. A sub-card starts from its parent's branch and merges into it. A Linear sub-issue also has `parent` (its parent's identifier), but it starts from the current branch.

## Read

```bash
herdboard --json list                      # this repository: todos, linked and pinned Linear issues
herdboard --json list --view inbox         # todos that belong to no repository
herdboard --json list --view all           # every board and your Linear issues
herdboard --json list --view mine          # Linear issues assigned to the user
herdboard --json list --column "In progress"
herdboard --json show tep3                 # also the last 10 activity log entries; --log shows all
herdboard --json list --archived           # the archived todos of this repository's board
herdboard --json search "login timeout"    # open Linear issues of the user's teams; ENG-123 finds one issue
```

## Write

Do these when the user asks for them, or when a handed-over card tells you to update it:

```bash
herdboard --json add "Fix the login timeout #auth 📅 2026-11-01"   # this repository's board, else the inbox
herdboard --json add "Buy milk" --board inbox
herdboard --json add "Fix the login timeout" --type bug     # adds #type/bug
herdboard --json add "Fix the login timeout" --body "Check the retry limit"
herdboard --json add "Fix the login timeout" --body-file docs/login.md
herdboard --json move tep3 "In progress"
herdboard --json move ENG-123 Review
herdboard --json done tep3
herdboard --json move tep3 --board inbox             # to another board; the ID stays
herdboard --json move tep3 --up                      # one place higher in its column
herdboard --json edit tep3 --title "Fix the login timeout #auth"   # the checkbox and the ID stay
herdboard --json edit tep3 --body "Replace the card details"
herdboard --json edit tep3 --body-file docs/login.md  # read multiple detail lines from a file
herdboard --json edit tep3 --type feature             # or --no-type
herdboard --json log tep3 "Ready for review: 3 commits."   # append to the card's activity log
herdboard --json attach tep3 docs/spec.md             # copy a large document into the card's attachments
herdboard --json attach tep3 --remove spec.md         # remove an attachment
herdboard --json add "Add invoice table" --parent t5aa     # a sub-card, on the parent's board
herdboard --json edit tep3 --parent t5aa              # make a todo a sub-card
herdboard --json edit tep3 --no-parent                # make it a plain todo again
herdboard --json archive tep3                        # to the board's archive, hidden on the board
herdboard --json unarchive tep3                      # back to the board's done column
herdboard --json unarchive acme-api:archive-2        # an archived card without an ID, by its archive reference
```

Use `add --body` or `add --body-file` to set details when you create a card, including from a started agent. Do not use both options. Relative file paths start from the command directory (`-C`, if set). With `add --linear`, these options set the issue description.

For multiple detail lines, use `--body-file` with `add` or `edit`. In shell double quotes, `\n` stays literal; it does not create a line break.

The archive keeps old todos out of the columns. Commands other than `show`, `log` and `unarchive` refuse an archived todo. Linear issues are not archived here.

The activity log is the card's history. It works for todos and Linear issues, and it never changes the card text. herdboard records who wrote each entry.

Attachments hold documents too large for the card text. `show` lists them in `attachments`. The attachment `plan.md` is the card's plan: the agent that starts work on the card gets it in its brief.

Ask the user first before these. They change shared Linear data, open workspaces, or rewrite many cards:

- `add --linear` (creates a Linear issue), `promote` (turns a todo into a Linear issue), and `move` or `done` on a Linear issue.
- `start` (creates a worktree and starts an agent).
- `plan` (creates the card's worktree when needed, and starts a planner agent in a tab in it).
- `review` (focuses the card's workspace and opens Hunk; it can start a new agent). `review --agent` and `--both` also start a reviewer agent.
- `merge` and `finish` (change the base branch, remove the worktree and delete the branch). Never run them for the card you work on.
- `pr-review` (opens a workspace and Hunk, and starts a reviewer agent), `pr-review-refresh` (moves `pr/<N>` and reopens Hunk) and `pr-review-finish` (removes the PR's worktree and branch).
- `delete <id> --yes` (removes a todo for good; Linear issues cannot be deleted here).
- `archive --done` (archives every card in the board's done column).
- `link`, `pin`, `init`, `migrate --yes`, `doctor --fix-ids`.

```bash
herdboard --json start tep3 --agent pi        # worktree, workspace, card to In progress, pi with the card
herdboard --json start tep3 --no-agent        # the same, but no agent starts
herdboard --json start tep3 --no-repo         # no repository: the card runs in its own folder
herdboard --json start tep3 --new-repo ~/Projects/corvid   # create a local repository first, then start
herdboard --json plan tep3 --no-repo          # plan in the card folder, without a repository
herdboard --json pin ENG-123                  # show the issue on this repository's board
herdboard --json pin ENG-123 --remove         # remove the pin
herdboard --json start tep3 --base feature-x  # start from, and later merge into, a local branch
herdboard --json plan tep3 --agent claude --model opus   # a planner asks the user questions, then writes plan.md
herdboard --json review tep3                  # open the card's branch diff in Hunk, next to its agent
herdboard --json review tep3 --agent --reviewer pi --model zai/glm-5.3   # another agent reviews; the card's agent gets the findings
herdboard --json review tep3 --both           # an agent review first, then Hunk
herdboard --json merge tep3                   # the repository's mode: local (rebase, fast-forward, finish) or PR
herdboard --json merge tep3 --pr              # an agent writes the description, then a draft PR opens; the card stays in Review
herdboard --json merge tep3 --pr --rewrite    # an open PR: an agent rewrites its description (without --rewrite: push only)
herdboard --json pr-sync tep3                 # push the branch, then send the card's pr.md to the open PR
herdboard --json pr-sync tep3 --fetch         # write the PR's GitHub title and body to a new pr.md; change nothing
herdboard --json finish tep3                  # after a merge: remove the worktree and branch, card to Done
herdboard --json prs                          # the open GitHub PRs of this repository, with review markers
herdboard --json prs --search "author:octo" --page 2   # GitHub search qualifiers; 50 PRs to a page
herdboard --json pr-review 42 --agent claude --model sonnet   # an agent reviews PR #42 with notes in Hunk; it never submits
herdboard --json pr-review-refresh 42         # after new commits: move pr/42, open Hunk again with the old notes
herdboard --json pr-review-go 42              # focus the review workspace of PR #42
herdboard --json pr-review-finish 42          # close the workspace, remove the worktree and the pr/42 branch
herdboard --json promote tep3                 # add --team ENG or --project NAME when the user names them
herdboard --json add --linear "Fix the login timeout" --team ENG
```

`start`, `plan` and `pr-review` accept `--agent claude|pi|codex`. Agent reviews accept `--reviewer claude|pi|codex`. `--thinking` applies to pi or Codex. Codex levels are `low`, `medium`, `high`, `xhigh`, `max` and `ultra`; support depends on the model. The default review rotation is Claude to pi, pi to Codex, Codex to Claude.

`start` without `--agent` starts the default agent (Claude, unless the user's config says otherwise). `--no-agent` still creates the worktree and moves the card. Without `--base`, the card starts from the branch of the main checkout.

`--thinking` chooses pi's thinking level for one run. Without it, `start`, `plan`, `review --agent` and `pr-review` use the level that the user last chose for that model (the `[thinking]` table of the config file), else `start_thinking`, else pi's own default. A command that names no model uses `start_model`. Do not set a level unless the user asks for it: the saved level is their choice.

A card on the inbox can run without a repository. `start` and `plan` then fail with `repo_required` unless the user picks one:

- A repository board: `--repo BOARD`. The card stays on its board; `--repo` only names the repository.
- A new repository: `--new-repo [PATH]`. herdboard runs `git init -b main` and a first commit at PATH, creates the board, moves the card, then starts as usual. Without PATH it uses `projects_dir/<title>`; the default is `~/Projects`. The path must not exist, or must be an empty folder; a folder that a stopped start left with only an unborn repository is reused. Only todos can do this, and not a card with a parent or sub-cards (`new_repo_family`).
- No repository: `--no-repo`. herdboard creates `<todos>/.herdboard/work/<id>/` and opens a workspace there. There is no Git, no branch and no worktree.

A repoless card writes its result to the card's attachments (for example `findings.md`). Review it with `review <id> --agent`; Hunk and `merge` refuse with `no_repository`. `finish` closes the workspace, deletes the card folder with everything in it, and moves the card to Done. `finish` also drops a card that was planned with `--new-repo` before its repository exists. The start record keeps the choice, so a later `start` or `plan` asks nothing. `--checkout` and `--base` need a repository that the card already has, so `--no-repo` and `--new-repo` refuse them with `usage`.

`doctor` without `--fix-ids` only reads, and you can run it to explain a problem.

A pull request reviewer only adds notes to the Hunk session. The user submits the review from Hunk. Never submit, approve or comment on GitHub for them.

## When a card was handed to you

`herdboard start` opens you in a new worktree. Your first message names the card and a brief file. Then:

1. Read the brief. It has the card's details, linked notes, the worktree and branch, and the exact herdboard command to use.
2. Do the work that the card describes, in this worktree.
3. Record progress, decisions and blockers with `herdboard --json log <id> "..."`. Do not change the card title or details: `edit` fails with `agent_edit_denied` in your pane.
4. Commit every change on the card's branch, with conventional commit messages.
5. When the work is ready for the user to check, commit it, then run `herdboard --json move <id> Review`.
6. When review feedback arrives, move the card to `In progress`. Fix, commit, and move it back to Review.
7. When an agent review arrives, read the findings file. Answer with one table: `| # | Description | Severity | Agree/Disagree | Reasoning |`, one row for each finding. In Description, describe the finding briefly in your own words: say what the problem is. Do not give only the finding's name or number. In Severity, give the reviewer's severity, or `question` for a risk or question. In Reasoning, say why you agree or disagree. Record the answer with `herdboard --json log <id> "..."`. Do not change code. Wait for the user to decide what to fix.
8. Do not push, merge, rebase onto another branch, or remove the worktree. herdboard does that.
9. Do not run `done`. The user moves the card to Done.

A repoless card has no repository and no branch. Work in its card folder, write the result to the card's attachments, and do not commit. herdboard deletes the folder at `finish`, so everything worth keeping goes in the attachments.

You may run `pr-sync <id>` for your own card. It pushes your branch and sends `pr.md` to the open pull request. When the user asks to change the PR title or description, edit the `pr.md` you wrote when you know its path. Otherwise, run `pr-sync <id> --fetch`, which names a new `pr.md`. Edit that file, then run `pr-sync <id>`.

The Herdr sidebar shows the card next to your agent row, and it follows the card's column.

## When you plan a card

`herdboard plan` opens you in a tab in the card's worktree; herdboard creates the worktree when the card has none. Your first message names the card and a brief file. Read the brief, then plan the work with the user: read the code, ask about the unknowns, and write the plan to the `plan.md` path that the brief gives. You may change code and run commands in the worktree to test ideas. Revert every change you made before you end, and do not commit. Do not change the card text. herdboard logs each save of the plan.

## Errors

Errors are JSON on stdout: `{"error": {"code": "...", "message": "..."}}`. Exit code 1 is a user error, 2 a conflict, 3 a Linear error.

| Code | Do this |
|---|---|
| `no_state` | No workflow state of the issue's team maps to that column. Ask the user for another column. |
| `agent_edit_denied` | You are the card's agent, and the card text is the user's. Record the change with `log` instead. |
| `linear_edit` | herdboard does not edit Linear issues. Tell the user to edit it in Linear. |
| `confirmation_required` | `delete` needs `--yes`. `unarchive` needs it when the card's parent is archived too, because the parent comes back with it. Ask the user first, then run it with `--yes`. |
| `linear_delete` | herdboard does not delete Linear issues. Tell the user to delete it in Linear. |
| `unknown_type` | The type is not in the `types` setting. The message lists the types. Use one of them, or ask the user. |
| `usage` | The arguments are wrong. Use only the forms in this skill, or read `herdboard <command> --help`. Do not guess options. |
| `io` | herdboard could not read or write a file or folder. Tell the user the message. |
| `archived` | The todo is archived. Ask the user before you run `unarchive <id>`. |
| `card_active` | The card's worktree still exists, or its agent or planner still runs. Tell the user the message. |
| `has_sub_cards` | A parent with sub-cards on the board stays. Archive the sub-cards first, after you ask the user. |
| `linear_archive` | herdboard archives only todos. Tell the user. |
| `not_found` | Run `list` to get the correct reference, or ask the user for the column. |
| `conflict` | The card changed, or another write holds the lock. Run `show` again, then retry once. |
| `ambiguous_state` | Several Linear states match the column. The error lists `candidates`. Ask the user which one, then run the command again with `--state NAME`. |
| `linear_unknown` | The write may have happened. Do not run it again. Tell the user to check the issue in Linear. For a new issue, give them `issue_id`. |
| `linear_offline`, `linear`, `linear_rejected` | Linear is not reachable or refused the request, and nothing changed. Tell the user. |
| `linear_disabled` | Linear is off (neither `linear.api_key_command` nor an environment key is set, or `linear.enabled = false`). The default environment variable is `LINEAR_API_KEY`. Tell the user the message. |
| `team_required` | Ask the user which Linear team, then use `--team KEY`. |
| `already_promoted` | The todo already has a Linear issue (`→ ENG-123`). Tell the user. |
| `not_a_todo` | `promote` takes a todo, not a Linear issue. |
| `checkout_unknown` | `start` does not know where the repository is. Ask the user for the path, then use `--checkout PATH`. |
| `base_unknown` | The base is not a local branch, or the checkout has no branch, or an old start record has no base. Ask the user which local branch to use, then run `start` with `--base BRANCH`. |
| `upstream_failed` | Git could not set the base as the branch upstream. Tell the user the message. Running `start` again retries. |
| `not_started` | The card has no worktree from `start`, or it was removed. Tell the user to run `start` first. |
| `hunk_unavailable` | `review` needs the hunkdiff Herdr plugin (`jhochenbaum.hunkdiff`), and `pr-review` needs `hunk gh pr` to start a session. Tell the user the message. |
| `remote_unknown` | No Git remote of the checkout points to the PR's base repository. Tell the user. |
| `branch_exists` | A local branch `pr/<N>` exists that no PR review owns. Tell the user to delete or rename it. |
| `agent_running` | A reviewer with another agent kind, model or thinking level still runs for the PR. Tell the user to close it, or to review with the same agent and model. For `pr-review-refresh`, the reviewer still works: refresh after it finishes. |
| `workspace_closed` | The PR's review workspace is not open in Herdr. Run `pr-review <N>` again to open it, after you ask the user. |
| `worktree_missing` | The PR's worktree is gone. Tell the user to run `pr-review-finish <N>` and review again. |
| `new_commits` | The PR has commits that its review worktree does not have. Ask the user, then run `pr-review-refresh <N>` before `pr-review <N>`. |
| `unsent_notes` | Hunk still has notes of the user that are not submitted. Tell the user. Use `--force` only when they say so. |
| `gh_failed`, `git_failed`, `hunk_failed` | A `gh`, `git` or `hunk` command failed. Tell the user the message. For `pr-review-finish`, `git_failed` means Git kept `pr/<N>`: the review stays, and a second finish deletes the branch. For `pr-review-refresh` or `pr-review`, `hunk_failed` can mean Hunk refused saved notes: they stay saved, and a second refresh applies them again. |
| `agent_unavailable` | No agent runs in the card's pane, so review comments have no target. Tell the user the message. |
| `plan_running` | A planner for the card still runs in a Herdr tab. Tell the user to finish or close it. |
| `review_running` | An agent review of the card still runs. Tell the user; its findings arrive by themselves. |
| `nothing_to_review` | The card branch has no commits since its base and no uncommitted changes. Tell the user. |
| `worker_failed` | The agent review or pull request worker did not start, and its agent was closed. Tell the user the message. |
| `pr_writing` | An agent still writes the card's pull request description. Wait; the PR opens when it is done. |
| `no_pr` | The card's branch has no open pull request. Press `m`, or ask the user to run `merge <id> --pr`. |
| `no_description` | No written `pr.md` for the card. Run `pr-sync <id> --fetch` first, edit the file that it names, then run `pr-sync <id>`. |
| `worktree_dirty` | The card's worktree has uncommitted changes. Tell the user; the card's agent must commit them. For `pr-sync`, the same: the agent must commit before it syncs. For `pr-review-refresh`, the PR worktree has changes: tell the user. For `pr-review-finish`, the PR worktree has changes: tell the user to commit or remove them, or to finish with force. |
| `nothing_to_merge` | The card branch has no commits ahead of its base. Tell the user. |
| `base_dirty` | The checkout of the base branch has uncommitted changes. Tell the user to commit or stash them. |
| `rebase_conflict` | The rebase conflicted, and herdboard sent the conflict to the card's agent. Tell the user. Do not merge again until the card is back in Review. |
| `merge_failed` | Git could not fast-forward the base. Tell the user the message. |
| `not_merged` | `finish` found the branch not merged into its base. Tell the user; `finish --force` keeps the branch. |
| `gh_unavailable` | The PR path needs the GitHub CLI `gh`. Tell the user. |
| `push_failed`, `pr_failed` | `git push` or `gh` failed. Tell the user the message. Nothing was merged. |
| `parent_missing` | The parent is not a todo on the same board. Run `list` and use a todo of that board. |
| `parent_nested` | Sub-cards go one level deep: the parent is a sub-card, or the card has sub-cards. Tell the user. |
| `parent_not_promoted` | `promote` takes the parent card; its sub-cards become sub-issues. Ask the user before you promote the parent. |
| `children_open` | `merge` of a parent card found sub-cards that are not done. Tell the user; `merge --force` merges anyway. |
| `repo_required` | The card has no repository. Ask the user: a repository board (`--repo`), a new local repository (`--new-repo [PATH]`), or no repository (`--no-repo`). |
| `no_repository` | The card runs without a repository, so there is no branch. Review with `review <id> --agent`, then run `finish <id>`. Tell the user. |
| `new_repo_family` | The card has a parent or sub-cards, and their cards must stay on one board. Tell the user; run it with `--no-repo` instead. |
| `path_exists` | The new repository path exists and is not an empty folder. Ask the user for another path. |
| `new_repo_failed` | herdboard could not create the repository or its first commit. Tell the user the message; a missing git `user.name` or `user.email` is the usual cause. |
| `worktree_create_failed` | Herdr could not create the worktree. Tell the user the message and `steps_done`. `invalid reference: HEAD` means the repository has no commit yet. Do not retry, and do not make a commit, unless the user asks. |
| `herdr_unavailable`, `herdr_error` | `start` needs a running Herdr, or a Herdr command failed. Tell the user the message. Running `start` again continues from the step that failed. |
