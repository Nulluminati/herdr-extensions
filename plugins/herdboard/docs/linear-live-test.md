# Linear live test

The unit tests use a fake Linear server that validates every query against Linear's schema. This checklist confirms the behaviors that only the real API can show. Run it on a computer with a Linear API key, in a terminal inside Herdr.

## Setup

1. Create a personal API key in Linear: Settings, then Security & access, then Personal API keys.
2. Run these commands. They use a temporary todo folder, so your real `~/todos` stays unchanged.

```bash
export LINEAR_API_KEY=lin_api_...          # your key
export HB_REPO=~/personal/herdr-extensions  # where this repository is cloned
export HB_TMP=$(mktemp -d)
printf 'todos_dir = "%s/todos"\n' "$HB_TMP" > "$HB_TMP/config.toml"
export HERDBOARD_CONFIG="$HB_TMP/config.toml"
hb() { uv run --quiet --project "$HB_REPO/plugins/herdboard" herdboard "$@"; echo "[exit $?]"; }
```

3. Pick one work repository that has a Linear project. Change into it: `cd ~/work/<repo>`.

## Checks

Write the result next to each check: pass, or the output.

| # | Command | Expected | Confirms |
|---|---|---|---|
| 1 | `hb list --view mine` | Your open issues, plus issues completed in the last 14 days. Exit 0. | Raw API key in the `Authorization` header. The `done_days` filter (`-P14D`) is accepted. |
| 2 | `hb --json list --view mine \| jq '.linear'` | `stale` is `false`, and `fetched_at` is set | Cache write |
| 3 | `hb init`, then `hb link "<Project Name>"` with the project name in different letter case | One project is linked. The board file has `linear-projects` with the ID and name. | Case-insensitive project match (`eqIgnoreCase`) |
| 4 | `hb list` | Local todos plus every issue of the project, for all assignees | Project issue query, no assignee filter |
| 5 | `hb link "<Project Name>" --label <label>`, then `hb list` | Only issues with that label | Label filter. A label shared by several teams asks for `TEAM/label`. |
| 6 | `hb show <ENG-123>` with an issue from check 4 | Title, state, team, URL, labels, description | `issue(id:)` accepts an identifier |
| 7 | `hb show ENG-99999` (an identifier that does not exist) | A clear "not found" error, exit 1 or 3 | The not-found response of `issue(id:)` |
| 8 | Pick an issue with more than 100 sub-issues or labels, if one exists. Run `hb --json show <id> \| jq '.card'` | All children and labels | `first: 100` is accepted, and follow-up paging works |
| 9 | `LINEAR_API_KEY=invalid hb list --view mine` | "authentication failed" message, exit 3, no cached cards | The authentication error shape |
| 10 | Turn off the network. `hb list` | Cached cards with a "stale" notice | Offline cache |
| 11 | `hb list --offline` | Same cards, marked stale, without network | Cache-only mode |
| 12 | Turn on the network. `hb board`, then `r` | Linear cards in the columns, marked `◆`. The title bar shows `Linear: HH:MM`. | Board fetch worker |
| 13 | A project that you cannot access: edit the board file, change one `linear-projects` ID to a wrong value, then `hb list` | A warning for that link. The other links still show. | Inaccessible project returns no nodes |
| 14 | Rate-limit headers: `curl -s -D - -o /dev/null -H "Authorization: $LINEAR_API_KEY" -H 'Content-Type: application/json' --data '{"query":"{ viewer { id } }"}' https://api.linear.app/graphql \| grep -i ratelimit` | Headers named `X-RateLimit-Requests-Reset` and `X-RateLimit-Complexity-Reset`, and `...-Remaining` headers | The header names that the client reads |

## Write checks

These checks change real issues. Run them only on a throwaway issue and a throwaway project that you can delete. Create the issue in Linear first, in a team where you can move issues. Write its identifier as `<ENG-T>` below. Link the project to the repository board as in check 3.

| # | Command | Expected | Confirms |
|---|---|---|---|
| W1 | `hb move <ENG-T> "In progress"`, then open the issue in Linear | Exit 0, one line `<ENG-T> → In progress (<project>)`. The issue shows a started state in Linear. | `issueUpdate` with `stateId`, and the `success` flag |
| W2 | `hb move <ENG-T> "In progress"` again | Exit 0 and no change. In Linear, the activity log has no new entry. | A move to the current state sends nothing |
| W3 | `hb done <ENG-T>` | Exit 0. The issue shows the completed state, not the canceled one. | `done` picks the completed state |
| W4 | `hb --json move <ENG-T> Todo`, then `hb --json list --view mine \| jq '.cards[] \| select(.ref=="<ENG-T>") \| .column'` | `"Todo"`. The list shows the change at once, without `--offline`. | The write drops the cache |
| W5 | Put 3 throwaway issues in the same state. Run `hb move <ENG-T> Todo --position top`, then `--position bottom`. Look at the column in the Linear board view. | After `top` the issue is the first card of its column in Linear's board. After `bottom` it is the last. If the order is reversed, lower `sortOrder` does not show higher, and the README rule "below the lowest" must flip. | Direction of `sortOrder`. The README assumes that a lower value shows higher. |
| W6 | `hb --json list --view mine \| jq '[.cards[] \| select(.column=="Todo") \| .ref]'` after W5 | The refs follow the order of Linear's board column. | Card order by `sortOrder` |
| W7 | Add a second started state to the team in Linear (Settings, Teams, Workflow), for example "Coding". Run `hb move <ENG-T> "In progress"` in a terminal. | A numbered list with both started states. After an answer the move works. A second run asks nothing. | The state question, and the stored choice in `~/.local/state/herdboard/<workspace-id>/<user-id>/state-choices.json` |
| W8 | `hb --json move <ENG-T> "In progress"` after you delete the stored choice file | Exit 1, `error.code` is `ambiguous_state`, and `error.candidates` lists both state names. `--state Coding` answers it. | The non-terminal error |
| W9 | Delete the "Coding" state in Linear (move its issues first). Run `hb move <ENG-T> "In progress"` | The stored state ID is gone, so the tool uses the one remaining state, or asks again if several remain. | A stale stored choice |
| W10 | `hb add --linear --team <ENG> "Throwaway issue"` from a directory outside any repository | Exit 0. The issue exists in Linear, assigned to you, in the first state that maps to Backlog. | `issueCreate` with a client-generated `id`, and the assignee |
| W11 | From the repository with exactly one linked project and labels: `hb add --linear "Throwaway issue 2"` | The issue is in the linked project with the link labels, and `hb list` shows it in the repository view. | Defaults from the repository board |
| W12 | Run the snippet below the table | The lookup by the generated UUID returns the created issue. | `IssueCreateInput.id` is accepted and kept, and `issue(id:)` finds an issue by UUID right after creation |
| W13 | `hb board`, select a Linear card, press `L`, then `H`, then `d` | Each key moves the card in Linear. The board fetches Linear again after each write. With several started states, a dialog opens. | The board write path |
| W14 | Turn off the network and press `L` on a Linear card in the board | "Linear cards are read only offline". No change. | Offline refusal |
| W15 | `hb move <ENG-T> Todo --offline` | Exit 3, `linear_offline`. | Offline refusal in the CLI |

Snippet for W12. It creates one issue in the first team that you belong to.

```bash
uv run --project "$HB_REPO/plugins/herdboard" python - <<'PY'
import os, uuid
from herdboard.linear import LinearClient
client = LinearClient(os.environ["LINEAR_API_KEY"])
team = client.my_teams()[0]
state = client.team_states(team["id"])[0]
issue_id = str(uuid.uuid4())
made = client.create_issue(issue_id=issue_id, team_id=team["id"], title="Throwaway UUID check",
                           state_id=state["id"])
found = client.issue_for_write(issue_id)
print(made["id"] == issue_id, found and found["identifier"] == made["identifier"])
PY
```

Expected output: `True True`. Delete the issue afterwards.

Not covered live: an unknown write result (timeout after the write). The unit tests cover delayed commits with the fake server. To test this manually, set `timeout` very low in `LinearClient` for one `hb move` run. Check that `hb` reports success or `linear_unknown` after up to three verification reads. An old state or missing issue does not prove rejection. For create, an unknown result includes the generated ID in the message and the `--json` error (`issue_id`). Check that ID before another create.

## Pin checks

These checks change only the board file of the repository from check 3. Pick an issue of your team that the linked project does not show, and write its identifier as `<ENG-P>`.

| # | Command | Expected | Confirms |
|---|---|---|---|
| P1 | `hb search <word>` | Only open issues of teams that you are a member of. Exit 0. | `searchIssues` with the team and state filter |
| P2 | `hb search <ENG-P>`, and an identifier of a team that you are not a member of | Each gives that one issue | The identifier skips the search |
| P3 | `hb board`, press `I`, search, press enter on `<ENG-P>`. Then press `r`. | The card shows on the board. The board file has `linear-issues` with its ID and identifier. | The search dialog and the pinned fetch (`issues` with `id: {in: ...}`) |
| P4 | Select the card from P3 and press `u` | The card goes away. The board file has no `linear-issues`. | Unpin |
| P5 | Remove the pin from P4. Start `<ENG-P>` from the `mine` view with `s`, and choose the repository board | The board asks for the board. The board file gets the pin. | The repository question and auto-pin in `start` |
| P6 | `hb doctor` after you change the ID of a pin in the board file to a wrong value | A `linear_pin_missing` warning | The doctor check |

## Cleanup

```bash
rm -rf "$HB_TMP"; unset HERDBOARD_CONFIG HB_TMP
```

Remove the test board link from the work repository only if you created it in check 3. It lives in the temporary folder, so the cleanup removes it. Delete the throwaway issues and the throwaway project in Linear.
