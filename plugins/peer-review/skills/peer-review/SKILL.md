---
name: peer-review
description: "Ask the other coding agent to review your changes through Herdr: uncommitted changes, else your branch commits since its upstream. Claude's changes go to pi, pi's changes go to Codex, and Codex's changes go to Claude. Use only when the user asks for a peer review, a second opinion from another agent, or a review by pi, Claude or Codex. Requires HERDR_ENV=1."
---

# Peer review

A peer review opens a second agent in a split pane next to you. That agent reviews the uncommitted changes in your repository (or, when there are none, your branch commits since its upstream) and writes its findings to a file. You do not wait for it.

## Request a review

1. Check that you run inside Herdr:

   ```bash
   test "${HERDR_ENV:-}" = 1
   ```

   If the check fails, tell the user that peer review needs Herdr and stop.

2. Start the review from the repository directory:

   ```bash
   python3 "$(herdr plugin list --plugin nulluminati.peer-review --json | jq -r '.result.plugins[0].plugin_root')/bin/peer_review.py" request
   ```

   The command returns at once. It prints the reviewer pane and the review directory.

3. Tell the user that the review started. End your turn. Do not poll the review.

## When the findings arrive

A new prompt arrives with the path of `findings.md`. Then:

1. Read the findings file.
2. For each finding, say if you agree or disagree, and why. Check the code before you agree.
3. Do not change any code.
4. Wait for the user to decide what to fix.

## Limits

- Uncommitted changes are reviewed first: tracked changes against `HEAD` and new untracked files.
- When there are none, the commits on your branch since its upstream are reviewed.
- The command fails when there are no uncommitted changes and no commits since the upstream. Tell the user.
- One review round. Do not request a second review unless the user asks.
