"""Plans with a verification contract, builder evidence and approver files for the factory tests."""

import copy
import json
from pathlib import Path

CONTRACT = {
    "version": 1,
    "workflow": "feature",
    "criteria": [
        {"id": "AC1", "outcome": "The change does what the card asks."},
        {"id": "AC2", "outcome": "The change keeps the old behavior."},
    ],
    "checks": [
        {
            "id": "unit",
            "criteria": ["AC1", "AC2"],
            "action": "uv run pytest",
            "expected": "Exit 0 and every test passes.",
            "surface": "unit tests",
            "evidence_kind": "command_output",
            "isolation": "Test temporary directories.",
            "cleanup": "Fixture teardown.",
        },
        {
            "id": "live",
            "criteria": ["AC1"],
            "action": "Open the board and start a card.",
            "expected": "The card starts.",
            "surface": "the real board",
            "evidence_kind": "transcript",
            "isolation": "A scratch todos folder.",
            "cleanup": "Close the panes that the check opened.",
        },
    ],
}


def contract(**changes) -> dict:
    value = copy.deepcopy(CONTRACT)
    value.update(changes)
    return value


def block(value, label="herdboard-verification") -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    return f"```{label}\n{text}\n```\n"


def plan(value=None, lenses: str | None = None) -> str:
    """A plan with the standard headings and one contract block under Testing."""
    lines = ["# Plan: test", "", "## Goal", "A goal.", "", "## Decisions", "- A decision."]
    if lenses:
        lines.append(f"Review lenses: {lenses}")
    lines += ["", "## Steps", "1. A step.", "", "## Testing", "", "- AC1: the change works.", ""]
    text = "\n".join(lines) + "\n" + block(CONTRACT if value is None else value)
    return text + "\n## Out of scope\nNothing.\n\n## Open questions\nNone.\n"


def result(check: str, status: str = "passed", evidence=("unit.txt",), **changes) -> dict:
    value = {
        "check": check,
        "status": status,
        "action": "uv run pytest",
        "observed": "12 passed",
        "started": "2026-11-06T10:00:00Z",
        "finished": "2026-11-06T10:01:00Z",
        "evidence": list(evidence),
    }
    if check == "unit":
        value["exit_code"] = 0 if status == "passed" else 1
    value.update(changes)
    return value


def write_report(evidence_dir: Path, run_id: str, digest: str, commit: str, results=None, **changes) -> Path:
    """A builder's `verification.json` with one passing result per check of CONTRACT, and its files."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if results is None:
        results = [result("unit"), result("live", evidence=["live.txt"])]
    for entry in results:
        for name in entry.get("evidence") or ():
            path = evidence_dir / name
            if not path.exists() and "/" not in name and not name.startswith("."):
                path.write_text(f"output of {entry['check']}\n")
    report = {"version": 1, "run": run_id, "contract_digest": digest, "commit": commit, "results": results}
    report.update(changes)
    path = evidence_dir / "verification.json"
    path.write_text(json.dumps(report, indent=2))
    return path


def approval(verdict="approve", criteria=None, exceptions=(), **changes) -> dict:
    value = {
        "version": 1,
        "verdict": verdict,
        "criteria": (
            criteria
            if criteria is not None
            else [
                {"id": "AC1", "outcome": "satisfied", "checks": ["unit", "live"], "evidence": "unit.txt"},
                {"id": "AC2", "outcome": "satisfied", "checks": ["unit"], "evidence": "unit.txt"},
            ]
        ),
        "exceptions": list(exceptions),
    }
    value.update(changes)
    return value
