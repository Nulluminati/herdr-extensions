# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Write each agent's vendor logo to the `$agent_icon` pane token for the Herdr sidebar.

With no event it covers every agent pane (startup and the refresh action).
Driven by a Herdr event it covers only the pane that the event names.
"""

import json
import os
import subprocess
import sys
import time


TOKEN = "agent_icon"
SOURCE = "nulluminati.agent-icons"
FONT_FAMILY = "Herdr Agent Icons Max"

# Private Use Area codepoints of the Herdr Agent Icons Max font from herdr-radar.
LOGOS = {
    "claude": "",
    "codex": "",
    "opencode": "",
    "omp": "",
    "cline": "",
    "mastracode": "",
    "kimi": "",
    "kilo": "",
    "maki": "",
    "pi": "",
    "hermes": "",
    "cursor": "",
    "copilot": "",
    "gemini": "",
    "qwen": "",
    "grok": "",
    "agy": "",
    "kiro": "",
    "amp": "",
    "devin": "",
    "qodercli": "",
    "muse": "",
}


def command(argv: list[str]):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30)


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_json(*arguments: str) -> dict:
    return json.loads(command([herdr_bin(), *arguments]).stdout or "{}")


def font_installed() -> bool:
    # fc-match always returns some font, so compare the family names it reports.
    try:
        output = command(["fc-match", "--format", "%{family}\n", FONT_FAMILY]).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    families = output.replace(",", "\n").splitlines()
    return any(family.strip() == FONT_FAMILY for family in families)


def find_pane_id(value) -> str | None:
    # The pane id sits at a depth that varies by event, so search for it.
    if isinstance(value, dict):
        if isinstance(value.get("pane_id"), str):
            return value["pane_id"]
        value = list(value.values())
    if isinstance(value, list):
        for entry in value:
            found = find_pane_id(entry)
            if found:
                return found
    return None


def targets() -> list[tuple[str, str | None]]:
    event = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    if event:
        try:
            pane_id = find_pane_id(json.loads(event))
        except json.JSONDecodeError:
            pane_id = None
        if not pane_id:
            return []
        pane = herdr_json("pane", "get", pane_id).get("result", {}).get("pane") or {}
        return [(pane_id, pane.get("agent"))]
    agents = herdr_json("agent", "list").get("result", {}).get("agents") or []
    return [(agent["pane_id"], agent.get("agent")) for agent in agents if isinstance(agent.get("pane_id"), str)]


def report(pane_id: str, glyph: str | None) -> None:
    change = ["--token", f"{TOKEN}={glyph}"] if glyph else ["--clear-token", TOKEN]
    command([herdr_bin(), "pane", "report-metadata", pane_id,
             "--source", SOURCE, "--seq", str(time.time_ns()), *change])


def failure(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or "").strip() or str(error)
    return str(error)


def main() -> None:
    # A startup or event hook has no one to show an error to, so log it and go on.
    show_logos = font_installed()
    try:
        panes = targets()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"agent-icons: cannot list panes: {failure(error)}", file=sys.stderr)
        return
    for pane_id, agent in panes:
        glyph = LOGOS.get(agent) if show_logos else None
        try:
            report(pane_id, glyph)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"agent-icons: {pane_id}: {failure(error)}", file=sys.stderr)


if __name__ == "__main__":
    main()
