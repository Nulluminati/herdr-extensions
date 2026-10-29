# Codex usage

Show each Codex agent's context use and estimated cost in the Herdr sidebar Agents list, in the same form as Claude and pi: `ctx 7%` and `$0.533`.

The plugin writes two pane tokens:

| Token | Example | Value |
|---|---|---|
| `$codex_context` | `ctx 7%` | Tokens of the last model call, as a share of the model's context window |
| `$codex_cost` | `$0.533` | Estimated cost of the session in US dollars |

Your Herdr config decides where the tokens show.

## Where the numbers come from

Codex writes a token count to its session log (`~/.codex/sessions/<year>/<month>/<day>/rollout-*.jsonl`) after each model call. Herdr knows the session ID of each Codex pane, so the plugin finds the log of that pane.

Codex does not report a cost. On a ChatGPT plan it does not bill per token. The plugin prices each model call with pi's price list (`~/.pi/agent/models-store.json`, provider `openai-codex`, else `openai`), the same way pi prices its own calls. If you change the model during a session, each call gets the price of its own model. Calls of a model that pi does not list add no cost. If no call has a price, the cost token does not show. The cost is an estimate of the API price, not a charge.

## Install

Requirements: Herdr 0.9 or later, Python 3.11 or later, Herdr's Codex integration (it reports the session ID), and pi for the price list.

```bash
herdr plugin install Nulluminati/herdr-extensions/plugins/codex-usage
```

To work on the plugin from a clone, link it instead, so edits take effect at once:

```bash
herdr plugin link ~/Projects/herdr-extensions/plugins/codex-usage
```

## Use

Add the tokens to a sidebar row in `~/.config/herdr/config.toml`. Rows whose tokens have no value disappear, so one row can hold the tokens of every agent:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "agent"],
  ["tab"],
  ["$claude_context", "$claude_cost", "$pi_context", "$pi_cost", "$codex_context", "$codex_cost"],
]
```

Then run `herdr server reload-config`.

## When the tokens change

- At Herdr startup, for every Codex pane.
- When a Codex agent changes state, such as from working to idle, for that pane.
- When you run the action "Codex usage: refresh", for every Codex pane.
- When another agent starts in a pane where Codex ran. The plugin clears the old Codex values of that pane.

The values do not change while a turn runs. They catch up when the agent changes state.

## Limits

- `CODEX_HOME` and `PI_CODING_AGENT_DIR` change where the plugin looks, as they do for Codex and pi.
- The Codex log format is not a stable interface. A Codex update can break the numbers. Then the tokens disappear.
