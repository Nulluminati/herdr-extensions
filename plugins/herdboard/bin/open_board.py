"""Open the herdboard board as a Herdr tab in the focused pane's directory."""

import json
import os
import subprocess
import sys


def main() -> int:
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    directory = context.get("focused_pane_cwd") or context.get("workspace_cwd")
    argv = [
        os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
        "--plugin", "nulluminati.herdboard", "--entrypoint", "board", "--placement", "tab",
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
