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
| 12 | Turn on the network. `hb board`, then `r` | Linear cards in the columns, marked `◆`. The title bar shows `Linear: HH:MM`. `H`, `L` and `d` on a Linear card show "Linear writes arrive in a later step". | Board fetch worker |
| 13 | A project that you cannot access: edit the board file, change one `linear-projects` ID to a wrong value, then `hb list` | A warning for that link. The other links still show. | Inaccessible project returns no nodes |
| 14 | Rate-limit headers: `curl -s -D - -o /dev/null -H "Authorization: $LINEAR_API_KEY" -H 'Content-Type: application/json' --data '{"query":"{ viewer { id } }"}' https://api.linear.app/graphql \| grep -i ratelimit` | Headers named `X-RateLimit-Requests-Reset` and `X-RateLimit-Complexity-Reset`, and `...-Remaining` headers | The header names that the client reads |

## Cleanup

```bash
rm -rf "$HB_TMP"; unset HERDBOARD_CONFIG HB_TMP
```

Remove the test board link from the work repository only if you created it in check 3. It lives in the temporary folder, so the cleanup removes it.
