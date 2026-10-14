# Review and merge

Status: design draft. All 9 build steps are built.

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
| `pr_write` | While an agent writes the PR description: the folder, the writer pane (or no key when the card's agent writes), whether it rewrites an open PR |

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
- Linear sub-issues already have a parent in Linear. The board reads that parent, so local and Linear cards show the same way. A sub-issue whose parent is not in the view shows the parent's identifier.
- A Linear sub-issue does not start from its parent's branch, because its parent can belong to another repository. `start --base` sets the base.
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
| `herdboard review <id> [--human \| --agent \| --both]` | Open the card's work in Hunk next to its agent, have another agent review it, or both |
| `herdboard merge <id> [--local \| --pr] [--rewrite] [--force]` | Merge the card branch into its base |
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

### Agent review

`herdboard review <id> --agent [--reviewer claude|pi] [--model NAME] [--thinking LEVEL]` asks a second agent to review the card's work. `--both` does the agent review first, then opens Hunk. `--human` is the default and is the review above.

- `--reviewer` defaults to the other kind: a Claude card gets a pi reviewer, and a pi card gets a Claude reviewer.
- `--model` is one model name. `--thinking` works only with pi.
- The board offers the Claude models `opus`, `sonnet` and `haiku`, and pi's model list from the start dialog.

Steps:

1. Read the start record. Without one, stop with `not_started`. If an agent review of the card still runs, stop with `review_running`. A stored review whose reviewer agent is gone does not count.
2. Find the card's agent, as in `review` step 3. Start a new one if it is gone.
3. Collect the change: `git diff <base>...HEAD`, the uncommitted changes (`git diff HEAD`) and the untracked files, all in the worktree. If all are empty, stop with `nothing_to_review`.
4. Write the review folder `<state>/reviews/<key>-<time>-<random>/`: `request.md`, `branch.patch`, `uncommitted.patch`. `request.md` has the card title and description, the branch, the base, the worktree, what to check and the findings format.
5. Split the card agent's pane to the right, without focus, in the worktree. Start the reviewer there as `hb-<id>-review`. It can only read the repository and write `findings.md` in the review folder. These are the same limits as the peer-review plugin.
6. Store `agent_review` in the start record: the folder, the pane, the kind, the model and whether Hunk follows. Log: `Agent review started: pi (model).`
7. Start a background worker and return.

The worker:

1. Waits for `findings.md` and an idle reviewer, for 30 minutes at most.
2. Closes the reviewer pane. Agents use memory too.
3. Logs: `Agent review by pi done: <n> findings. <findings path>`.
4. Waits until the card's agent is idle. Then it sends this prompt: read the findings, say for each one if you agree or disagree and why, record the answer with `herdboard log`, change no code, and wait for the user.
5. With `--both`, it opens Hunk as `review` does.
6. Shows a Herdr notification: `Agent review of <id> done`.
7. Removes `agent_review` from the record.

On a failure (the reviewer stopped, a timeout, a Herdr error), the worker logs `Agent review failed: <reason>`, shows a Herdr notification, and does not prompt the card's agent. With `--both`, it still opens Hunk. If the card was finished before the review ended, the worker only closes the pane.

The board shows `agent review` on a card while its agent review runs.

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

An agent writes the PR title and description. herdboard pushes the branch and opens the PR.

1. If `pr_write` is in the start record and its writer still runs, stop with `pr_writing`.
2. If a PR for the branch is already open, store its URL:
   - Without `--rewrite`: push the branch without `-u` (`git push origin <branch>`), log `PR updated: <url>`, and stop.
   - With `--rewrite`: continue at step 3. The board asks first: "A pull request is open. Rewrite its description? This replaces any edits made on GitHub."
3. Find the PR templates in the worktree, in the places that GitHub reads: `pull_request_template.md` in the root, `.github/` and `docs/`, and every `.md` file in a `PULL_REQUEST_TEMPLATE/` folder in those places. Names are not case-sensitive.
4. Write the folder `<state>/prs/<key>-<time>-<random>/` with `request.md` and `branch.patch` (`git diff <base>...HEAD`). `request.md` has the card title and description, the branch, the base, the template paths and the rules below.
5. Choose the writer:
   - If the card's agent runs, it writes. herdboard prompts it when it is idle.
   - Else, start a writer agent of the card's kind in a split of the workspace root pane, as `hb-<id>-pr`. It has the same read-only limits as the agent reviewer, and it can write only `pr.md` in the folder. Its first message also has the full log.
6. Store `pr_write`. Log: `PR description requested from <writer>.`
7. Start a background worker and return.

The writer's rules, in `request.md`:

- Write `pr.md`. The first line is the title. The rest is the body.
- The title always follows one conventional commit guide, whatever the style of the repository: `<type>(<scope>): <summary>`. The type is one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci` or `chore`. The scope is optional. The summary is in the imperative mood, starts in lower case and has no period. The title is at most 72 characters. A breaking change adds `!` before the colon.
- Describe only the testing that the notes or the change show. Claim no check that nobody ran.
- With one template, follow it: the same headings in the same order. With several, pick the one that fits.
- With no template, use this format. Leave out "Trade-offs" when there are none:

  ```markdown
  <title>

  ## Why
  One to three sentences: what was wrong or missing, and who it affected.

  ## What changed
  The approach and the decisions, not a list of changed files.

  ## Trade-offs
  What was given up, or the risks.

  ## Testing
  How the change was checked.
  ```

- Keep it short. Write plain prose. Use the `humanizer` skill if you have it.
- Do not name the tools, the agent or the card. Do not add a Linear link.
- Do not run `gh`, push, or change code.

The worker:

1. Waits for `pr.md` and an idle writer, for 30 minutes at most.
2. Closes the writer pane, if herdboard started one.
3. Reads the title from the first line, without a leading `#`. An empty title is a failure.
4. Adds `Linear: <url>` at the end of the body for a Linear card.
5. Pushes the branch without `-u`.
6. Runs `gh pr create --draft --base <base> --head <branch> --title <title> --body-file <body>`. With `--rewrite`, it runs `gh pr edit <url> --title <title> --body-file <body>`.
7. Stores `pr_url` and `pr_state = "open"`, and removes `pr_write`.
8. Logs `PR opened: <url>` or `PR description rewritten: <url>`.
9. Shows a Herdr notification with the URL.

On a failure (no `pr.md`, a timeout, an empty title, a push or `gh` error), the worker logs `PR not opened: <reason>`, shows a notification, and removes `pr_write`. It opens no PR. If the card was finished first, the worker only closes the writer pane.

The card stays in Review. The board shows `PR: writing` while `pr_write` exists. The PR has no line that names herdboard.

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

- The board reads the state of each open pull request in the start records, at most once a minute. It uses `gh pr view --json state`.
- A merged PR shows the marker `PR merged`. A closed PR shows `PR closed`.
- The board does not run `finish`. You run it.
- A card in Done that still has a start record and a worktree shows `worktree still exists`. `finish` works on it.
- Without `gh`, or offline, the board shows no PR marker and no error. `doctor` reports a missing `gh` as info.

## Board

Keys. The current `r` is reload, so review uses `R`. All are built. `m` and `f` ask for confirmation.

| Key | Action |
|---|---|
| `R` | review: choose human, agent or both. For an agent review, choose the agent and the model |
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
- A sub-card cannot be promoted alone. It stops with `parent_not_promoted`.
- A sub-card's checklist stays text in its issue description, because sub-issues go one level deep.

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
| `review_running` | An agent review of the card still runs |
| `nothing_to_review` | The card branch has no commits since its base and no uncommitted changes |
| `worker_failed` | The agent review or PR worker did not start. Its pane is closed |
| `pr_writing` | An agent still writes the card's PR description |

## Claude and pi

| Step | Claude | pi |
|---|---|---|
| Brief and skill | Same text | Same text |
| Log author | From the stored agent kind | From the stored agent kind |
| Hunk feedback | hunkdiff `send-review` to the pane | Same |
| Conflict message | `herdr agent prompt` | Same |
| Agent reviewer | `--permission-mode dontAsk`, read tools, edits only in the review folder | `--tools read,grep,find,ls,write` |
| PR writer | The card's agent, else a writer with the reviewer's limits | Same |
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
8. Agent review from `review` and the board.
9. Agent-written draft PRs: the writer, the worker, `--rewrite` and the board confirmation.

## Testing

- Unit tests use a fake `herdr`, a fake `gh` and temporary Git repositories. Rebase, conflict, fast-forward and branch removal run against real Git in a temporary folder.
- Live test in a Herdr pane with a throwaway repository, for each agent kind: start, commit, review, feedback through Hunk, local merge with and without a conflict, finish.
- Live test of the PR path on a throwaway GitHub repository: a new draft PR with and without a template, a PR written by a writer agent, and `--rewrite` of an open PR.
