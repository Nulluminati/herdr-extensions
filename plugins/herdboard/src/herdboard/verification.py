"""The verification contract of a factory run: what the plan promises, and what proves it.

The plan holds one fenced JSON block labeled `herdboard-verification`: the workflow, the acceptance
criteria and the checks that prove them. A new factory run needs a valid one. The run freezes the contract
and a digest of the plan. Before its ready signal, the builder writes `verification.json` and the output of
each check into the run's `evidence/` folder. The approver writes `approval.json` beside its verdict.
Software then decides whether the approval stands.

This checks that the evidence is complete, current and consistent. It does not prove that an agent ran a
command: an agent with the user's permissions can write any file. Command text is data here: herdboard
never runs it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import attachments
from .model import HerdboardError

VERSION = 1
LABEL = "herdboard-verification"
WORKFLOWS = ("feature", "defect", "refactor", "performance")
EVIDENCE_KINDS = ("command_output", "transcript", "screenshot", "file", "measurement")
CHECK_FIELDS = ("id", "criteria", "action", "expected", "surface", "evidence_kind", "isolation", "cleanup")
MAX_PLAN_BYTES = 1024 * 1024
MAX_CONTRACT_BYTES = 64 * 1024
# A plain identifier, such as AC1 or unit-tests.
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<label>[^`\s]*)[ \t]*$")
FIX = "Update the plan (press P on the board, or run `herdboard plan <ref>`), then start the factory again."


class VerificationError(HerdboardError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _invalid(problem: str) -> VerificationError:
    return VerificationError(f"The plan's {LABEL} block is invalid: {problem}. {FIX}", "verification_plan_invalid")


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_json(text: str):
    """JSON that rejects a key given twice, so two values cannot hide one another."""

    def pairs(items):
        found = {}
        for key, value in items:
            if key in found:
                raise ValueError(f"duplicate key {key!r}")
            found[key] = value
        return found

    return json.loads(text, object_pairs_hook=pairs)


@dataclass(frozen=True)
class Contract:
    version: int
    workflow: str
    criteria: tuple[dict, ...]
    checks: tuple[dict, ...]

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(check["id"] for check in self.checks)

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(criterion["id"] for criterion in self.criteria)

    def check(self, check_id: str) -> dict:
        return next(check for check in self.checks if check["id"] == check_id)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "workflow": self.workflow,
            "criteria": [dict(entry) for entry in self.criteria],
            "checks": [dict(entry) for entry in self.checks],
        }

    @property
    def digest(self) -> str:
        return digest(_canonical(self.to_dict()))

    @classmethod
    def from_dict(cls, value: dict) -> Contract:
        """A contract from the plan's JSON. Raises `verification_plan_invalid` with the field that is wrong."""
        if not isinstance(value, dict):
            raise _invalid("the block must be one JSON object")
        unknown = sorted(set(value) - {"version", "workflow", "criteria", "checks"})
        if unknown:
            raise _invalid(f"unknown field {unknown[0]}")
        version = value.get("version")
        if isinstance(version, bool) or version != VERSION:
            raise _invalid(f"version must be {VERSION}, not {version!r}")
        workflow = value.get("workflow")
        if workflow not in WORKFLOWS:
            raise _invalid(f"workflow must be one of {', '.join(WORKFLOWS)}, not {workflow!r}")
        criteria = value.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise _invalid("criteria must list at least one criterion")
        seen = set()
        for index, criterion in enumerate(criteria):
            where = f"criteria[{index}]"
            _fields(criterion, where, ("id", "outcome"))
            _identifier(criterion["id"], f"{where}.id")
            _text(criterion["outcome"], f"{where}.outcome")
            if criterion["id"] in seen:
                raise _invalid(f"duplicate criterion {criterion['id']}")
            seen.add(criterion["id"])
        checks = value.get("checks")
        if not isinstance(checks, list) or not checks:
            raise _invalid("checks must list at least one check")
        covered, check_ids = set(), set()
        for index, check in enumerate(checks):
            where = f"checks[{index}]"
            _fields(check, where, CHECK_FIELDS)
            _identifier(check["id"], f"{where}.id")
            if check["id"] in check_ids:
                raise _invalid(f"duplicate check {check['id']}")
            check_ids.add(check["id"])
            names = check["criteria"]
            if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
                raise _invalid(f"{where}.criteria must list the criteria that the check proves")
            for name in names:
                if name not in seen:
                    raise _invalid(f"{where}.criteria names unknown criterion {name}")
            covered.update(names)
            for name in ("action", "expected", "surface", "isolation", "cleanup"):
                _text(check[name], f"{where}.{name}")
            if check["evidence_kind"] not in EVIDENCE_KINDS:
                raise _invalid(f"{where}.evidence_kind must be one of {', '.join(EVIDENCE_KINDS)}")
        for criterion in criteria:
            if criterion["id"] not in covered:
                raise _invalid(f"criterion {criterion['id']} has no check")
        return cls(VERSION, workflow, tuple(dict(entry) for entry in criteria), tuple(dict(entry) for entry in checks))


def _fields(value, where: str, names) -> None:
    if not isinstance(value, dict):
        raise _invalid(f"{where} must be a JSON object")
    for name in names:
        if name not in value:
            raise _invalid(f"{where}.{name} is missing")
    unknown = sorted(set(value) - set(names))
    if unknown:
        raise _invalid(f"{where} has unknown field {unknown[0]}")


def _identifier(value, where: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise _invalid(f"{where} must be a short identifier, such as AC1 or unit")


def _text(value, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{where} must be text")


def _blocks(text: str) -> list[str]:
    """The bodies of the fenced blocks labeled LABEL. A fence closes with the same character, at least as long."""
    found, lines, index = [], text.splitlines(), 0
    while index < len(lines):
        opening = FENCE.match(lines[index])
        index += 1
        if not opening:
            continue
        fence = opening["fence"]
        body = []
        while index < len(lines):
            closing = FENCE.match(lines[index])
            if (
                closing
                and not closing["label"]
                and closing["fence"][0] == fence[0]
                and len(closing["fence"]) >= len(fence)
            ):
                break
            body.append(lines[index])
            index += 1
        else:
            if opening["label"] == LABEL:
                raise _invalid("its fence is not closed")
        index += 1
        if opening["label"] == LABEL:
            found.append("\n".join(body))
    return found


def parse_contract(plan_text: str | None) -> Contract:
    """The contract in the plan. Raises `verification_plan_required` when the plan has none,
    else `verification_plan_invalid` for a problem with it."""
    if plan_text is None or not plan_text.strip():
        raise VerificationError(
            f"The card has no plan, so a factory run has no {LABEL} block. {FIX}", "verification_plan_required"
        )
    if len(plan_text.encode("utf-8")) > MAX_PLAN_BYTES:
        raise _invalid(f"the plan is larger than {MAX_PLAN_BYTES // 1024} KB")
    blocks = _blocks(plan_text)
    if not blocks:
        raise VerificationError(
            f"The plan has no {LABEL} block with acceptance criteria and checks. {FIX}", "verification_plan_required"
        )
    if len(blocks) > 1:
        raise _invalid(f"the plan has {len(blocks)} {LABEL} blocks; keep one")
    if len(blocks[0].encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise _invalid(f"the block is larger than {MAX_CONTRACT_BYTES // 1024} KB")
    try:
        value = load_json(blocks[0])
    except ValueError as error:
        raise _invalid(f"it is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise _invalid("the block must be one JSON object")
    return Contract.from_dict(value)


def check_plan(config, ref: str) -> tuple[Contract, str]:
    """The contract and the text of the card's plan. Every factory start calls this before it launches anything."""
    text = attachments.plan_text(config, ref)
    if text is None:
        raise VerificationError(
            f"{ref} has no plan, so a factory run has no {LABEL} block. {FIX}".replace("<ref>", ref),
            "verification_plan_required",
        )
    try:
        return parse_contract(text), text
    except VerificationError as error:
        raise VerificationError(f"{ref}: {error}".replace("<ref>", ref), error.code) from None


def freeze(folder: Path, contract: Contract, plan_text: str) -> dict:
    """Keep a copy of the agreed plan in the run folder. Returns the keys that go beside `run` in the state."""
    try:
        (folder / "plan.md").write_text(plan_text, encoding="utf-8")
    except OSError as error:
        raise VerificationError(f"{folder}: cannot keep a copy of the plan: {error}", "io") from error
    return {"verification": VERSION, "contract": contract.to_dict(), "plan_digest": digest(plan_text)}
