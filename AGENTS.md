# AGENTS.md

Herdr plugins for Claude Code, pi and Codex. One repository, one directory per plugin.

## Layout

```
plugins/<name>/
  herdr-plugin.toml        # manifest; id is "nulluminati.<name>"
  bin/                     # code that the manifest commands run
  skills/<skill>/SKILL.md  # optional agent skill, installed with the skills CLI
  README.md                # install, key binding, use, limits
tests/test_<name>.py       # unittest, one file per plugin (or test_<name>_<module>.py)
tests/fixtures/<name>/     # test data
```

## How plugins run

- Herdr runs action commands from the plugin root. Use paths relative to it, such as `bin/peer_review.py`.
- Commands receive context in `HERDR_PLUGIN_CONTEXT_JSON`, `HERDR_PLUGIN_STATE_DIR`, `HERDR_PLUGIN_CONFIG_DIR`, `HERDR_PANE_ID` and `HERDR_BIN_PATH`.
- Drive Herdr through its CLI (`herdr agent ...`, `herdr pane ...`). The installed binary is the authority for syntax: run `herdr <group>` to list commands.
- Plugins run on the user's machine with Python 3.11 standard library only. Do not add dependencies.
- Exception: a plugin with its own `pyproject.toml` (such as `herdboard`) is installed with `uv tool install` and may declare dependencies there. Keep its core modules importable without them.

## Testing

```bash
uv run python -m unittest discover -s tests
```

- Tests load the plugin script with `importlib` and replace `command` with a fake, so no test calls the real `herdr`.
- Unit tests do not catch Herdr or agent behavior. After a change to how a plugin drives Herdr, test it live in a Herdr pane with a throwaway Git repository.

## Gotchas

- Do not type prompts into a new agent pane. Text can land in a startup prompt, such as Claude's or Codex's folder trust check. Pass the first message on the agent's command line.
- `herdr agent start` can report `agent_not_ready` or `timeout` for an agent that works: one blocked at a startup prompt, or one busy with its first message.
- Claude's `--allowedTools` takes every value after it. Put a positional prompt before it.
- The user's machine is linked to this checkout with `herdr plugin link`, so edits are live.
