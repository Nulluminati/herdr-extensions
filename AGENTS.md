# AGENTS.md

Herdr plugins for Claude Code, pi and Codex. One repository, one directory per plugin.

## Layout

```
plugins/<name>/
  herdr-plugin.toml        # manifest; id is "nulluminati.<name>"
  bin/                     # code that the manifest commands run
  skills/<skill>/SKILL.md  # optional agent skill, installed with the skills CLI
  README.md                # install, key binding, use, limits
tests/<name>/              # unittest.TestCase tests for one plugin, run with pytest
  test_<module>.py
  fixtures/                # test data
tests/repo/                # tests of repository files, such as CI and Dependabot
tests/support/             # shared helpers and base test cases; test modules import only from here
```

## How plugins run

- Herdr runs action commands from the plugin root. Use paths relative to it, such as `bin/peer_review.py`.
- Commands receive context in `HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PANE_ID` and `HERDR_BIN_PATH`.
- Drive Herdr through its CLI (`herdr agent ...`, `herdr pane ...`). The installed binary is the authority for syntax: run `herdr <group>` to list commands.
- Plugins run on the user's machine with Python 3.11 standard library only. Do not add dependencies.

## Testing

```bash
uv run pytest -n auto                          # full suite
uv run ruff check .
uv run ruff format .
uv run pre-commit install                      # once per clone: ruff before each commit
```

- Tests load the plugin script with `load_script` from `tests/support/scripts.py` and replace `command` with a fake, so no test calls the real `herdr`.
- Unit tests do not catch Herdr or agent behavior. After a change to how a plugin drives Herdr, test it live in a Herdr pane with a throwaway Git repository.

## Gotchas

- Do not type prompts into a new agent pane. Text can land in a startup prompt, such as Claude's or Codex's folder trust check. Pass the first message on the agent's command line.
- `herdr agent start` can report `agent_not_ready` or `timeout` for an agent that works: one blocked at a startup prompt, or one busy with its first message.
- Claude's `--allowedTools` takes every value after it. Put a positional prompt before it.
- The user's machine is linked to this checkout with `herdr plugin link`, so edits are live.
- Claude's and pi's reviewer sandbox writes the working directory unless `denyWrite` names it. Name the repository there.
- `terminal-browser` splits panes through the Herdr socket, so it must run outside a reviewer sandbox. Claude and pi exclude it with `excludedCommands`. Codex runs it outside with a rule in `~/.codex/rules/`, not by allowing the socket: with the socket allowed, `herdr pane run` lets any command escape the sandbox.
- `plugins/peer-review/bin/peer_review.py` keeps a hand copy of herdboard's reviewer sandbox options from `src/herdboard/sandbox.py` in `Nulluminati/herdboard`. No test checks that the two match, so change both.
