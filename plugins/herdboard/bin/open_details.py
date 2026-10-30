"""Open the card details pane above the card's agent, for the focused pane's workspace.

`herdboard details --open` finds the card: the planner of the pane's tab, else the started
card of the pane's workspace. It opens no second pane when one is open. A failure shows
a Herdr notification, because Herdr does not show the output of an action.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = os.environ.get("HERDR_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    pane = os.environ.get("HERDR_PANE_ID") or context.get("focused_pane_id") or context.get("pane_id")
    argv = ["uv", "run", "--quiet", "--project", root, "herdboard", "details", "--open"]
    if pane:
        argv += ["--pane", pane]
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
        code, message = result.returncode, result.stderr.strip()
    except OSError as error:
        code, message = 1, str(error)
    if code:
        message = message or "herdboard details --open failed"
        print(message, file=sys.stderr)
        try:
            subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), "notification", "show",
                            "herdboard: no details pane", "--body", message], capture_output=True, text=True)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
