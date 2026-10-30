"""Open the herdboard board as a Herdr tab in the focused pane's directory.

The optional argument names the plugin pane to open: `board` (the default), or `prs`
for the board opened on its pull request list.
"""

import json
import os
import subprocess
import sys


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    entrypoint = args[0] if args else "board"
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    directory = context.get("focused_pane_cwd") or context.get("workspace_cwd")
    argv = [
        os.environ.get("HERDR_BIN_PATH", "herdr"),
        "plugin",
        "pane",
        "open",
        "--plugin",
        "nulluminati.herdboard",
        "--entrypoint",
        entrypoint,
        "--placement",
        "tab",
    ]
    if directory:
        argv += ["--cwd", directory]
    argv.append("--focus")
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip() or "herdr plugin pane open failed", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
