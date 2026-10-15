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
- A card has `title`, `column`, `checked`, `due`, `tags`, `body`, `board`, `source` (`todo` or `linear`), `url`, `state` and `team`. `read_only: true` means you cannot change it. `archived: true` means the todo is in its board's archive. `stale: true` means the Linear data comes from the cache.
- Columns: `list --json` returns `boards`, with every column of each listed board (`title`, `limit`, `done`), also the empty ones. Move a todo only to a column of its own board. A Linear issue can move to any column that one of its team's workflow states maps to; the defaults are Backlog, Todo, In progress, Review and Done. When no state fits, the error is `no_state`.
- Tags and the due date are part of the card text: `#auth` adds a tag, `📅 2026-11-01` sets the due date.
- A todo can be a sub-card of another todo on the same board. Its `parent` field names the parent. Sub-cards go one level deep. A sub-card starts from its parent's branch and merges into it. A Linear sub-issue also has `parent` (its parent's identifier), but it starts from the current branch.

## Read

```bash
herdboard --json list                      # this repository: todos and linked Linear issues
herdboard --json list --view inbox         # todos that belong to no repository
herdboard --json list --view all           # every board and your Linear issues
herdboard --json list --view mine          # Linear issues assigned to the user
herdboard --json list --column "In progress"
herdboard --json show tep3                 # also the last 10 activity log entries; --log shows all
herdboard --json list --archived           # the archived todos of this repository's board
```

## Write

Do these when the user asks for them, or when a handed-over card tells you to update it:

```bash
herdboard --json add "Fix the login timeout #auth 📅 2026-11-01"   # this repository's board, else the inbox
herdboard --json add "Buy milk" --board inbox
herdboard --json move tep3 "In progress"
herdboard --json move ENG-123 Review
herdboard --json done tep3
herdboard --json move tep3 --board inbox             # to another board; the ID stays
herdboard --json move tep3 --up                      # one place higher in its column
herdboard --json edit tep3 --title "Fix the login timeout #auth"   # the checkbox and the ID stay
herdboard --json edit tep3 --body "first detail line\nsecond detail line"
herdboard --json log tep3 "Ready for review: 3 commits."   # append to the card's activity log
herdboard --json attach tep3 docs/spec.md             # copy a large document into the card's attachments
herdboard --json attach tep3 --remove spec.md         # remove an attachment
herdboard --json add "Add invoice table" --parent t5aa     # a sub-card, on the parent's board
herdboard --json edit tep3 --parent t5aa              # make a todo a sub-card
herdboard --json edit tep3 --no-parent                # make it a plain todo again
herdboard --json archive tep3                        # to the board's archive, hidden on the board
herdboard --json unarchive tep3                      # back to the board's done column
```

The archive keeps old todos out of the columns. Commands other than `show`, `log` and `unarchive` refuse an archived todo. Linear issues are not archived here.

The activity log is the card's history. It works for todos and Linear issues, and it never changes the card text. herdboard records who wrote each entry.

Attachments hold documents too large for the card text. `show` lists them in `attachments`. The attachment `plan.md` is the card's plan: the agent that starts work on the card gets it in its brief.

Ask the user first before these. They change shared Linear data, open workspaces, or rewrite many cards:

- `add --linear` (creates a Linear issue), `promote` (turns a todo into a Linear issue), and `move` or `done` on a Linear issue.
- `start` (creates a worktree and starts an agent).
- `plan` (opens a Herdr tab and starts a planner agent).
- `review` (focuses the card's workspace and opens Hunk; it can start a new agent). `review --agent` and `--both` also start a reviewer agent.
- `merge` and `finish` (change the base branch, remove the worktree and delete the branch). Never run them for the card you work on.
- `delete <id> --yes` (removes a todo for good; Linear issues cannot be deleted here).
- `archive --done` (archives every card in the board's done column).
- `link`, `init`, `migrate --yes`, `doctor --fix-ids`.

```bash
herdboard --json start tep3 --agent pi        # worktree, workspace, card to In progress, pi with the card
herdboard --json start tep3 --no-agent        # the same, but no agent starts
herdboard --json start tep3 --base feature-x  # start from, and later merge into, a local branch
herdboard --json plan tep3 --agent claude --model opus   # a planner asks the user questions, then writes plan.md
herdboard --json review tep3                  # open the card's branch diff in Hunk, next to its agent
herdboard --json review tep3 --agent --reviewer pi --model zai/glm-5.3   # another agent reviews; the card's agent gets the findings
herdboard --json review tep3 --both           # an agent review first, then Hunk
herdboard --json merge tep3                   # the repository's mode: local (rebase, fast-forward, finish) or PR
herdboard --json merge tep3 --pr              # an agent writes the description, then a draft PR opens; the card stays in Review
herdboard --json merge tep3 --pr --rewrite    # an open PR: an agent rewrites its description (without --rewrite: push only)
herdboard --json finish tep3                  # after a merge: remove the worktree and branch, card to Done
herdboard --json promote tep3                 # add --team ENG or --project NAME when the user names them
herdboard --json add --linear "Fix the login timeout" --team ENG
```

`start` without `--agent` starts the default agent (Claude, unless the user's config says otherwise). `--no-agent` still creates the worktree and moves the card. Without `--base`, the card starts from the branch of the main checkout.

`doctor` without `--fix-ids` only reads, and you can run it to explain a problem.

## When a card was handed to you

`herdboard start` opens you in a new worktree. Your first message names the card and a brief file. Then:

1. Read the brief. It has the card's details, linked notes, the worktree and branch, and the exact herdboard command to use.
2. Do the work that the card describes, in this worktree.
3. Record progress, decisions and blockers with `herdboard --json log <id> "..."`. Do not change the card title or details: `edit` fails with `agent_edit_denied` in your pane.
4. Commit every change on the card's branch, with conventional commit messages.
5. When the work is ready for the user to check, commit it, then run `herdboard --json move <id> Review`.
6. When review feedback arrives, move the card to `In progress`. Fix, commit, and move it back to Review.
7. When an agent review arrives, read the findings file. For each finding, say if you agree or disagree, and why. Record the answer with `herdboard --json log <id> "..."`. Do not change code. Wait for the user to decide what to fix.
8. Do not push, merge, rebase onto another branch, or remove the worktree. herdboard does that.
9. Do not run `done`. The user moves the card to Done.

The Herdr sidebar shows the card next to your agent row, and it follows the card's column.

## When you plan a card

`herdboard plan` opens you in the repository's main checkout. Your first message names the card and a brief file. Read the brief, then plan the work with the user: read the code, ask about the unknowns, and write the plan to the `plan.md` path that the brief gives. Do not change the code or the card text. herdboard logs each save of the plan.

## Errors

Errors are JSON on stdout: `{"error": {"code": "...", "message": "..."}}`. Exit code 1 is a user error, 2 a conflict, 3 a Linear error.

| Code | Do this |
|---|---|
| `no_state` | No workflow state of the issue's team maps to that column. Ask the user for another column. |
| `agent_edit_denied` | You are the card's agent, and the card text is the user's. Record the change with `log` instead. |
| `linear_edit` | herdboard does not edit Linear issues. Tell the user to edit it in Linear. |
| `confirmation_required` | `delete` needs `--yes`. Ask the user first, then run it with `--yes`. |
| `linear_delete` | herdboard does not delete Linear issues. Tell the user to delete it in Linear. |
| `usage` | The arguments are wrong. Use only the forms in this skill, or read `herdboard <command> --help`. Do not guess options. |
| `archived` | The todo is archived. Ask the user before you run `unarchive <id>`. |
| `card_active` | The card's worktree still exists or its agent still runs. Tell the user; `finish` the card first. |
| `has_sub_cards` | A parent with sub-cards on the board stays. Archive the sub-cards first, after you ask the user. |
| `linear_archive` | herdboard archives only todos. Tell the user. |
| `not_found` | Run `list` to get the correct reference, or ask the user for the column. |
| `conflict` | The card changed, or another write holds the lock. Run `show` again, then retry once. |
| `ambiguous_state` | Several Linear states match the column. The error lists `candidates`. Ask the user which one, then run the command again with `--state NAME`. |
| `linear_unknown` | The write may have happened. Do not run it again. Tell the user to check the issue in Linear. For a new issue, give them `issue_id`. |
| `linear_offline`, `linear`, `linear_rejected` | Linear is not reachable or refused the request, and nothing changed. Tell the user. |
| `linear_disabled` | Linear is off (no `LINEAR_API_KEY`, or `linear.enabled = false` in the config). Tell the user the message. |
| `team_required` | Ask the user which Linear team, then use `--team KEY`. |
| `already_promoted` | The todo already has a Linear issue (`→ ENG-123`). Tell the user. |
| `not_a_todo` | `promote` takes a todo, not a Linear issue. |
| `checkout_unknown` | `start` does not know where the repository is. Ask the user for the path, then use `--checkout PATH`. |
| `base_unknown` | The base is not a local branch, or the checkout has no branch, or an old start record has no base. Ask the user which local branch to use, then run `start` with `--base BRANCH`. |
| `upstream_failed` | Git could not set the base as the branch upstream. Tell the user the message. Running `start` again retries. |
| `not_started` | The card has no worktree from `start`, or it was removed. Tell the user to run `start` first. |
| `hunk_unavailable` | `review` needs the hunkdiff Herdr plugin (`jhochenbaum.hunkdiff`). Tell the user the message. |
| `agent_unavailable` | No agent runs in the card's pane, so review comments have no target. Tell the user the message. |
| `plan_running` | A planner for the card still runs in a Herdr tab. Tell the user to finish or close it. |
| `review_running` | An agent review of the card still runs. Tell the user; its findings arrive by themselves. |
| `nothing_to_review` | The card branch has no commits since its base and no uncommitted changes. Tell the user. |
| `worker_failed` | The agent review or pull request worker did not start, and its agent was closed. Tell the user the message. |
| `pr_writing` | An agent still writes the card's pull request description. Wait; the PR opens when it is done. |
| `worktree_dirty` | The card's worktree has uncommitted changes. Tell the user; the card's agent must commit them. |
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
| `repo_required` | Ask the user which repository, then use `--repo`. |
| `worktree_create_failed` | Herdr could not create the worktree. Tell the user the message and `steps_done`. `invalid reference: HEAD` means the repository has no commit yet. Do not retry, and do not make a commit, unless the user asks. |
| `herdr_unavailable`, `herdr_error` | `start` needs a running Herdr, or a Herdr command failed. Tell the user the message. Running `start` again continues from the step that failed. |
