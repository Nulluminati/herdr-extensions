# Review and merge

Status: design draft. Build steps 1 (activity log, brief and skill, `edit` guard), 2 (base and upstream), 3 (review) and 4 (local merge and finish) are built. The other steps are not.

`start` gives a card a worktree and an agent. The agent moves the card to Review. This design covers what happens after that: review the work, send feedback to the agent, merge the branch, and clean up. It also adds an activity log for agents and sub-cards for local todos.

## Goals

- Open the work of a card for review in Hunk, next to the agent that wrote it.
- Merge a card branch into its base locally, or through a GitHub pull request (PR). Each repository has a default.
- Remove the worktree and the branch after the merge, and move the card to Done.
- Support two workflows: many cards into `main`, and many cards into a feature branch that later goes to `main`.
- Agents record progress in an activity log. They do not edit the card description.
- Every step works the same for Claude and pi.

Non-goals: a merge queue that runs by itself, CI checks before a local merge, resume of an old agent session.

## Workflows

### A: cards into main

1. `herdboard add "Fix login timeout"` adds card `t3m9`.
2. `start t3m9` records the base `main`, creates the worktree and the branch `t3m9-fix-login-timeout`, and starts the agent.
3. The agent works. It writes progress with `herdboard log`. It commits on its branch.
4. The agent moves the card to Review.
5. `review t3m9` opens Hunk next to the agent. Hunk shows `main...t3m9-fix-login-timeout`.
6. You comment in Hunk and send the comments with the hunkdiff `send-review` action. The agent moves the card to In progress, fixes, commits, and moves the card back to Review. Repeat as needed.
7. `merge t3m9` merges with the repository default: local or PR.
8. `finish t3m9` removes the worktree and the branch, and moves the card to Done. A local merge runs `finish` itself.

### B: cards into a feature branch

1. `herdboard add "Billing v2"` adds the parent card `t5aa`.
2. `herdboard add "Add invoice table" --parent t5aa` adds a sub-card. Repeat for each sub-card.
3. `start t3m9` on a sub-card finds that the parent has no branch. It first starts the parent with no agent: branch `t5aa-billing-v2`, base `main`. Then it starts the sub-card with the base `t5aa-billing-v2`.
4. Each sub-card follows steps 3 to 8 of workflow A. Its merge goes into the parent branch.
5. `review t5aa` shows `main...t5aa-billing-v2`: all merged sub-cards together.
6. `merge t5aa` merges the parent branch into `main`, locally or with a PR.

## Data

### Start record

`~/.local/state/herdboard/starts.json` gets these keys in each record:

| Key | Value |
|---|---|
| `base` | The branch that the card branch starts from, and the merge target |
| `agent` | The agent kind: `claude` or `pi` |
| `review_pane` | The pane ID of the open Hunk review, or no key |
| `pr_url` | The PR URL after `merge --pr` |
| `pr_state` | The last PR state that the board read: `open`, `merged` or `closed` |
| `pr_checked` | The time of that read |

A record that was written before this change has no `base`. Commands that need the base then use the upstream of the card branch (see "Base branch"). Without an upstream, they stop with `base_unknown`.

### Activity log

- One file per card: `<todos_dir>/.herdboard/log/<id>.md`, where `<id>` is the todo ID or the Linear identifier. Obsidian ignores hidden folders, so the log does not show as a note.
- One entry per line, oldest first:

  ```markdown
  - 2026-10-12 14:03 · pi · Ready for review: 3 commits.
  - 2026-10-12 14:20 · herdboard · Rebase conflict in src/auth.py. Sent to the agent.
  ```

- The author is `herdboard`, `user`, `claude` or `pi`. The tool sets it, the caller does not. When `HERDR_PANE_ID` is the stored agent pane of a start record, the author is that record's agent kind. When herdboard writes an entry itself, the author is `herdboard`. Otherwise it is `user`.
- A message with several lines keeps them as indented lines under its entry.
- Writes take the store lock, the same as board writes. A log write only appends.
- The log is local for Linear cards too. The tool does not post Linear comments.
- `promote` writes `Promoted to ENG-123.` to the todo log, and `Promoted from t5aa.` as the first entry of the issue log.

### Parent link

A sub-card has an Obsidian block link to its parent on its first line, before its own ID:

```markdown
## Todo

- [ ] Add invoice table [[#^t5aa]] ^t3m9

## In progress

- [ ] Billing v2 #feature ^t5aa
```

- `[[#^t5aa]]` is Obsidian syntax for the block `t5aa` in the same file. The parent stores nothing about its sub-cards.
- The parent and the sub-card are on the same board. A link to an ID that is not on the board is an error in `doctor` (`parent_missing`), and the card shows as a card without a parent.
- Sub-cards go one level deep. A card with a parent cannot be a parent. `add` and `edit` refuse it with `parent_nested`.
- Inbox todos can have sub-cards. They have no repository, so `start` and `merge` do not apply.
- Linear sub-issues already have a parent in Linear. The board reads that parent, so local and Linear cards show the same way.
- The parent link is not part of the title. `show --json` returns it as `parent`. `todo_branch` already removes `[[...]]` from the branch name.

## Base branch

- `start --base REF` sets the base. Without it:
  - A sub-card uses its parent's branch. If the parent has no start record, `start` first starts the parent with `--no-agent`.
  - Other cards use the current branch of the main checkout. This is the branch that `herdr worktree create` already starts from.
- `start` passes the base to `herdr worktree create --base`.
- `start` sets the base as the upstream of the card branch: `git branch --set-upstream-to=<base> <branch>`. hunkdiff uses the upstream as the base of a branch review, so Hunk shows `<base>...HEAD` with no hunkdiff config. `git status` in the worktree also shows "ahead" and "behind" against the base.
- A push for a PR does not use `-u`, so the upstream stays the base. If a push changes the upstream, the next herdboard command sets it back.

## Commands

| Command | Action |
|---|---|
| `herdboard log <id> "text"` | Append an entry to the card's activity log |
| `herdboard review <id>` | Open the card's work in Hunk next to its agent |
| `herdboard merge <id> [--local \| --pr] [--force]` | Merge the card branch into its base |
| `herdboard finish <id> [--force]` | Remove the worktree and the branch, and move the card to Done |
| `herdboard start <id> [--base REF]` | As now, plus the base |
| `herdboard add "text" [--parent ID]` | As now, plus the parent link |
| `herdboard edit <id> [--parent ID \| --no-parent]` | As now, plus the parent link |
| `herdboard show <id> [--log]` | As now, plus the last 10 log entries. `--log` shows all of them |

### review

1. Read the start record. Without one, stop with `not_started`.
2. Focus the card's worktree workspace. If Herdr does not have it open, open it with `herdr worktree open --path`.
3. Find the agent. If the stored agent pane no longer runs an agent, start a new agent of the same kind in the workspace root pane. Its first message is the card brief, the full log, and this line: "The card is in review. Review feedback follows. Wait for it." Log: `Agent restarted for review.`
4. Open Hunk. If `review_pane` still exists, focus it. Otherwise open the hunkdiff branch review as a split next to the agent pane, and store its pane ID in `review_pane`.
5. Log: `Review opened.`

Hunk is the only review tool. herdboard does not show diffs itself. Without the hunkdiff plugin, `review` stops after step 3 with `hunk_unavailable`.

Open the hunkdiff pane through its own action, not as a bare pane: hunkdiff links the review pane to the agent pane, and `send-review` needs that link. `herdr plugin action invoke` takes no pane: hunkdiff reads the focused pane. So `review` runs `herdr agent focus <agent pane>` first, then `herdr plugin action invoke review:branch --plugin jhochenbaum.hunkdiff`, and finds the new pane by comparing `herdr pane list` before and after. A live test confirmed the link (`agentPaneId` in the hunkdiff index) and the `main...HEAD` target.

### Close the review pane

Hunk uses much memory, so one Hunk pane stays open only while its card is in Review.

- When a card leaves Review by a herdboard command (`move`, `merge`, `finish`, a board key), the tool closes `review_pane` and removes the key.
- An agent that moves its card to In progress also closes the pane, because it uses `move`.
- `herdr worktree remove` also makes hunkdiff close its pane, through its `worktree.removed` event.

### merge

The mode comes from `--local` or `--pr`, else from `merge` in the board's frontmatter, else from `merge` in config (default `local`).

Before either mode:

1. Read the start record. Without one, stop with `not_started`.
2. If the card is a parent and a sub-card is not in its done column, stop with `children_open`. The message names the sub-cards. `--force` continues.
3. If the worktree has uncommitted changes, stop with `worktree_dirty`. The agent must commit first.
4. If the branch has no commits ahead of the base, stop with `nothing_to_merge`.

#### Local

1. Rebase the branch onto the base in the worktree: `git rebase <base>`.
2. On a conflict, run `git rebase --abort`. Send the list of conflicting files to the agent with `herdr agent prompt`: "Rebase onto `<base>` conflicts in these files. Rebase, fix the conflicts, commit, and move the card to Review." If the agent pane is gone, start a new agent of the same kind with the brief, the log and this message. Move the card to In progress. Log: `Rebase conflict in <files>. Sent to the agent.` Exit with `rebase_conflict`, code 2.
3. Fast-forward the base to the branch:
   - If a checkout has the base checked out, run `git merge --ff-only <branch>` in that checkout. If that checkout has uncommitted changes to tracked files, stop with `base_dirty` before step 1.
   - If no checkout has the base checked out, run `git update-ref refs/heads/<base> <branch> <old base commit>`. The old value makes Git refuse the update if the base moved after step 1.
4. Log: `Merged into <base> at <short commit>.`
5. Run `finish`.

The tool does not push the base. CI runs when you push it.

#### PR

1. Push the branch without `-u`: `git push origin <branch>`.
2. Run `gh pr create --base <base> --head <branch> --title <card title> --body <body>`. The body has the card description and a link to the Linear issue, if one exists. It does not have the log.
3. Store `pr_url` and `pr_state = "open"`.
4. Log: `PR opened: <url>.`
5. The card stays in Review.

If a PR for the branch already exists, `merge --pr` stores its URL and does not create another.

### finish

1. Read the start record. Without one, stop with `not_started`.
2. Check the merge:
   - Local: the base contains the branch (`git merge-base --is-ancestor <branch> <base>`).
   - PR: `gh pr view <url>` reports the state `MERGED`.
   - If the check fails, stop with `not_merged`. `--force` continues, but step 6 then keeps the branch, so no commit is lost.
3. If the worktree has uncommitted or untracked changes, stop with `worktree_dirty`. `--force` does not skip this check, so work is never lost.
4. Close `review_pane`.
5. Remove the worktree: `herdr worktree remove --workspace <id>`. Herdr refuses a dirty worktree with `dirty_worktree_requires_force`, removes the folder, closes the workspace, and keeps the branch.
6. Delete the branch: `git branch -d <branch>`. For a PR that GitHub merged with squash or rebase, Git does not see the branch as merged, so the tool uses `-D` after the check in step 2. Delete the remote branch only if GitHub did not already delete it.
7. Move the card to its done column, if it is not there already. A Linear issue can already be in Done, because Linear's GitHub integration moves it when the PR merges.
8. Log: `Finished.` Remove the start record.

### PR state on the board

- The board reads the state of each card in Review with a `pr_url`, at most once a minute. It uses `gh pr view --json state`.
- A merged PR shows the marker `PR merged`. A closed PR shows `PR closed`.
- The board does not run `finish`. You run it.
- A card in Done that still has a start record and a worktree shows `worktree still exists`. `finish` works on it.
- Without `gh`, or offline, the board shows no PR marker and no error. `doctor` reports a missing `gh` as info.

## Board

Keys. The current `r` is reload, so review uses `R`. `R`, `m` and `f` are built; `m` and `f` ask for confirmation. The others are proposed.

| Key | Action |
|---|---|
| `R` | review |
| `m` | merge with the repository default |
| `M` | merge, and ask for the mode first |
| `f` | finish |
| `p` | set or remove the parent |

- A sub-card shows `↳ <parent title>`.
- A parent shows its progress: `2/5 done`.
- The filter can show one parent and its sub-cards.
- The detail view shows the last log entries under the description.

## Agent brief

The brief that `start` sends changes:

- Record progress, decisions and blockers with `herdboard log <id> "..."`.
- Do not change the card title or description.
- Commit on the card branch. Use conventional commits.
- Before you move the card to Review, commit every change.
- When review feedback arrives, move the card to In progress. Fix, commit, and move it back to Review.
- Do not push, merge, rebase onto another branch, or remove the worktree. herdboard does that.

The herdboard skill gets the same rules, so both Claude and pi follow them.

`edit --title` and `edit --body` refuse with `agent_edit_denied` when `HERDR_PANE_ID` is the stored agent pane of a start record. This is a guard against mistakes, not a security boundary: an agent can still edit the file. `log` and `move` stay allowed.

## Promote

- A parent todo with sub-cards becomes a Linear issue, and each sub-card becomes a sub-issue. Checklist items still become sub-issues, as now.
- Each promoted sub-card gets its own `→ ENG-124` marker and moves to its done column, the same as the parent.
- A sub-card cannot be promoted alone while its parent is a local todo. It stops with `parent_not_promoted`.

## Configuration

```toml
merge = "local"             # or "pr"; a board's frontmatter `merge:` wins
```

Board frontmatter:

```yaml
merge: pr
```

## Error codes

| Code | Cause |
|---|---|
| `not_started` | The card has no start record |
| `base_unknown` | The record has no base and the branch has no upstream |
| `worktree_dirty` | The worktree has uncommitted changes |
| `base_dirty` | The checkout of the base has uncommitted changes |
| `nothing_to_merge` | The branch has no commits ahead of the base |
| `children_open` | A sub-card is not done |
| `rebase_conflict` | The rebase conflicted. The agent has the conflict |
| `not_merged` | `finish` found no merge |
| `gh_unavailable` | `merge --pr` or `finish` of a PR needs `gh`, and it is missing or not signed in |
| `hunk_unavailable` | The hunkdiff plugin is not installed |
| `agent_edit_denied` | An agent tried to edit the card text |
| `parent_nested` | The parent already has a parent |
| `parent_missing` | The parent ID is not on the board |
| `parent_not_promoted` | A sub-card was promoted before its parent |

## Claude and pi

| Step | Claude | pi |
|---|---|---|
| Brief and skill | Same text | Same text |
| Log author | From the stored agent kind | From the stored agent kind |
| Hunk feedback | hunkdiff `send-review` to the pane | Same |
| Conflict message | `herdr agent prompt` | Same |
| New agent | `herdr agent start --kind claude` with the brief and the log | `--kind pi`, same message |

Claude can stop at a permission prompt for `git commit`. That delays Review, but does not change the design.

## Unconfirmed

Check these against the installed Herdr and hunkdiff before you build the step that needs them:

1. Whether the Obsidian Kanban plugin opens `[[#^t5aa]]` from a card.

## Build order

Each step is usable without the steps after it.

1. Activity log, the brief and skill changes, and the `edit` guard.
2. Base and upstream in `start`.
3. `review`, the review pane life cycle, and the new agent for a card without one.
4. `merge --local` and `finish`.
5. `merge --pr`, the PR marker, and `finish` for PR cards.
6. Sub-cards and parent cards.
7. `promote` of sub-cards.

## Testing

- Unit tests use a fake `herdr`, a fake `gh` and temporary Git repositories. Rebase, conflict, fast-forward and branch removal run against real Git in a temporary folder.
- Live test in a Herdr pane with a throwaway repository, for each agent kind: start, commit, review, feedback through Hunk, local merge with and without a conflict, finish.
- Live test of the PR path on a throwaway GitHub repository.
