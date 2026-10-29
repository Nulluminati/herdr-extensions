"""Report the Herdr sidebar tokens of the started cards again, in the background.

Herdr keeps sidebar tokens in memory only, so a restart clears them. Herdr runs this
at startup and when it finds an agent in a pane. It starts `herdboard sidebar-refresh`
in its own session and returns at once, so Herdr does not wait for uv or the board.
"""

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = os.environ.get("HERDR_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    argv = ["uv", "run", "--quiet", "--project", root, "herdboard", "sidebar-refresh"]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"herdboard: cannot start the sidebar refresh: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
