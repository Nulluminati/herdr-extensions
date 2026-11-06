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
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
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
# A fence may be indented, such as in a list item or in the plan format's example.
FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<label>[^`\s]*)[ \t]*$")
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


# The builder's evidence.

REPORT_NAME = "verification.json"
APPROVAL_NAME = "approval.json"
EVIDENCE_DIR = "evidence"
STATUSES = ("passed", "failed", "blocked", "not_applicable")
OUTCOMES = ("satisfied", "unsatisfied")
MAX_REPORT_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_FILES = 200
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass
class Report:
    """A checked `verification.json`. `problems` is empty when it can support an approval."""

    problems: list[str]
    statuses: dict[str, str]
    # The digest of the report and of each evidence file, by its path in the evidence folder.
    files: dict[str, str]
    data: dict | None = None


def _read_bounded(path: Path, limit: int) -> bytes:
    """At most `limit` bytes; raises ValueError for a larger file."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} is larger than {limit // 1024} KB")
    return data


def _read_json(path: Path, limit: int, problems: list[str], digests: dict | None = None):
    """The JSON object in `path`, or None with the problem added. With `digests`, records the digest of the
    bytes that it parsed, so a later change to the file cannot slip between the check and the digest."""
    try:
        data = _read_bounded(path, limit)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        problems.append(f"{path.name}: {error}")
        return None
    try:
        value = load_json(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        problems.append(f"{path.name} is not valid JSON: {error}")
        return None
    if not isinstance(value, dict):
        problems.append(f"{path.name} must be one JSON object")
        return None
    if digests is not None:
        digests[path.name] = digest(data)
    return value


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def tree_state(worktree: Path) -> tuple[str, list[str]]:
    """The full HEAD commit and `git status` lines for every tracked change and untracked file.

    The same HEAD does not mean the same tree, so the evidence check needs both."""

    def git(*args):
        result = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True)
        if result.returncode:
            raise VerificationError(f"git {' '.join(args)} failed in {worktree}: {result.stderr.strip()}", "git_failed")
        return result.stdout

    head = git("rev-parse", "HEAD").strip()
    dirty = git("status", "--porcelain", "--untracked-files=all").splitlines()
    return head, dirty


def _evidence_file(evidence_dir: Path, name, problems: list[str]) -> Path | None:
    if not isinstance(name, str) or not name.strip():
        problems.append("An evidence path must be text.")
        return None
    if Path(name).is_absolute():
        problems.append(f"Evidence path {name} is absolute; name a file in the evidence folder.")
        return None
    root = evidence_dir.resolve()
    path = (evidence_dir / name).resolve()
    if not path.is_relative_to(root):
        problems.append(f"Evidence path {name} is outside the evidence folder.")
        return None
    if path == root / REPORT_NAME:
        problems.append(f"Evidence path {name} is the report itself; name the output of the check.")
        return None
    if not path.exists():
        problems.append(f"Evidence file {name} does not exist.")
        return None
    if not path.is_file():
        problems.append(f"Evidence path {name} is not a file.")
        return None
    return path


def validate_report(evidence_dir: Path, *, run_id: str, contract: Contract, head: str, dirty: list[str]) -> Report:
    """Check the builder's report against the frozen contract and the current tree. Reads only bounded files
    in `evidence_dir`. Collects every problem, so the approver and the user see them all."""
    problems: list[str] = []
    statuses: dict[str, str] = {}
    files: dict[str, str] = {}
    if dirty:
        names = ", ".join(line[3:] for line in dirty)
        problems.append(f"The worktree has uncommitted or untracked changes: {names}. Commit them, then check again.")
    path = evidence_dir / REPORT_NAME
    report = _read_json(path, MAX_REPORT_BYTES, problems, files)
    if report is None:
        if not problems or not any(REPORT_NAME in problem for problem in problems):
            problems.append(f"The builder wrote no {REPORT_NAME} in {evidence_dir}.")
        return Report(problems, statuses, files)
    if isinstance(report.get("version"), bool) or report.get("version") != VERSION:
        problems.append(f"{REPORT_NAME} version must be {VERSION}.")
    if report.get("run") != run_id:
        problems.append(f"{REPORT_NAME} is for another factory run, not {run_id}.")
    if report.get("contract_digest") != contract.digest:
        problems.append(f"{REPORT_NAME} is for another contract, not {contract.digest}.")
    commit = report.get("commit")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        problems.append(f"{REPORT_NAME} commit must be the full commit identifier of the checked tree.")
    elif commit != head:
        problems.append(
            f"The report is for commit {commit}, not the current commit {head}. Check the final commit again."
        )
    results = report.get("results")
    if not isinstance(results, list):
        problems.append(f"{REPORT_NAME} results must be a list.")
        results = []
    total = 0
    for index, entry in enumerate(results):
        where = f"results[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a JSON object.")
            continue
        check_id = entry.get("check")
        if check_id not in contract.check_ids:
            problems.append(f"{where} names unknown check {check_id}; report only the contract's checks.")
            continue
        if check_id in statuses:
            problems.append(f"The report has two results for check {check_id}.")
            continue
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(f"{where}.status must be one of {', '.join(STATUSES)}.")
            continue
        statuses[check_id] = status
        for name in ("action", "observed"):
            if not isinstance(entry.get(name), str) or not entry[name].strip():
                problems.append(f"{where}.{name} must describe what was done and seen.")
        started, finished = _time(entry.get("started")), _time(entry.get("finished"))
        if started is None:
            problems.append(f"{where}.started must be an ISO 8601 time with a time zone.")
        if finished is None:
            problems.append(f"{where}.finished must be an ISO 8601 time with a time zone.")
        if started and finished and finished < started:
            problems.append(f"Check {check_id} finishes before it starts.")
        if contract.check(check_id)["evidence_kind"] == "command_output" and status in ("passed", "failed"):
            code = entry.get("exit_code")
            if isinstance(code, bool) or not isinstance(code, int):
                problems.append(f"{where}.exit_code must be the command's exit code.")
            elif status == "passed" and code != 0:
                problems.append(f"Check {check_id} passed with exit code {code}.")
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list):
            problems.append(f"{where}.evidence must list files in the evidence folder.")
            continue
        if status == "passed" and not evidence:
            problems.append(f"Check {check_id} passed without evidence.")
        for name in evidence:
            if len(files) > MAX_EVIDENCE_FILES:
                problems.append(f"The report names more than {MAX_EVIDENCE_FILES} evidence files.")
                break
            found = _evidence_file(evidence_dir, name, problems)
            if found is None or name in files:
                continue
            size = found.stat().st_size
            if size > MAX_EVIDENCE_BYTES:
                problems.append(f"Evidence file {name} is larger than {MAX_EVIDENCE_BYTES // 1024} KB.")
                continue
            if size == 0:
                problems.append(f"Evidence file {name} is empty.")
                continue
            total += size
            if total > MAX_TOTAL_EVIDENCE_BYTES:
                problems.append(f"The evidence is larger than {MAX_TOTAL_EVIDENCE_BYTES // (1024 * 1024)} MB in all.")
                break
            files[name] = digest(_read_bounded(found, MAX_EVIDENCE_BYTES))
    for check_id in contract.check_ids:
        if check_id not in statuses and not any(f"check {check_id}" in problem for problem in problems):
            problems.append(f"The report has no result for check {check_id}.")
    return Report(problems, statuses, files, report)


def snapshot(report: Report, evidence_dir: Path, target: Path) -> None:
    """Copy the report and its evidence files for the approver, who reads the copy."""
    for name in report.files:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile((evidence_dir / name).resolve(), destination)


def changed_since(report: Report, evidence_dir: Path) -> list[str]:
    """A problem when any captured file differs now, so evidence changed during the approval does not count."""
    changed = []
    for name, expected in report.files.items():
        try:
            data = _read_bounded((evidence_dir / name).resolve(), max(MAX_REPORT_BYTES, MAX_EVIDENCE_BYTES))
        except (OSError, ValueError):
            data = None
        if data is None or digest(data) != expected:
            changed.append(name)
    if not changed:
        return []
    return [f"The evidence changed after herdboard captured it: {', '.join(changed)}."]


# The approver's structured approval.


def _approval_problems(contract: Contract, report: Report, approval, verdict: str):
    """The problems of `approval.json`, the exceptions that it grants, and the criteria found unsatisfied."""
    problems, exceptions, unsatisfied = [], {}, []
    if isinstance(approval.get("version"), bool) or approval.get("version") != VERSION:
        problems.append(f"{APPROVAL_NAME} version must be {VERSION}.")
    stated = approval.get("verdict")
    if stated not in ("approve", "changes needed"):
        problems.append(f'{APPROVAL_NAME} verdict must be "approve" or "changes needed".')
    elif verdict == "approve" and stated != "approve":
        problems.append(f"{APPROVAL_NAME} says {stated}, but verdict.md says approve.")
    criteria = approval.get("criteria")
    if not isinstance(criteria, list):
        problems.append(f"{APPROVAL_NAME} criteria must list one outcome per criterion.")
        criteria = []
    outcomes = {}
    for index, entry in enumerate(criteria):
        where = f"{APPROVAL_NAME} criteria[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a JSON object.")
            continue
        name = entry.get("id")
        if name not in contract.criterion_ids:
            problems.append(f"{where} names unknown criterion {name}.")
        elif name in outcomes:
            problems.append(f"{APPROVAL_NAME} has two outcomes for criterion {name}.")
        elif entry.get("outcome") not in OUTCOMES:
            problems.append(f"{where}.outcome must be satisfied or unsatisfied.")
        else:
            outcomes[name] = entry["outcome"]
    for name in contract.criterion_ids:
        if name not in outcomes and not any(f"criterion {name}" in problem for problem in problems):
            problems.append(f"{APPROVAL_NAME} has no outcome for criterion {name}.")
        elif outcomes.get(name) == "unsatisfied":
            unsatisfied.append(name)
    granted = approval.get("exceptions", [])
    if not isinstance(granted, list):
        problems.append(f"{APPROVAL_NAME} exceptions must be a list, one entry per exempted check.")
        granted = []
    for index, entry in enumerate(granted):
        where = f"{APPROVAL_NAME} exceptions[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a JSON object.")
            continue
        check_id = entry.get("check")
        if check_id not in contract.check_ids:
            problems.append(f"{where} names unknown check {check_id}; name one blocked or inapplicable check.")
            continue
        if check_id in exceptions:
            problems.append(f"{APPROVAL_NAME} has two exceptions for check {check_id}.")
            continue
        missing = [
            name for name in ("reason", "risk") if not isinstance(entry.get(name), str) or not entry[name].strip()
        ]
        if missing:
            problems += [f"{where}.{name} must say why the check is exempt and what risk remains." for name in missing]
            continue
        status = report.statuses.get(check_id)
        if status == "failed":
            problems.append(f"Check {check_id} failed, and a failed check cannot have an exception.")
            continue
        if status == "passed":
            problems.append(f"Check {check_id} passed, so it needs no exception.")
            continue
        exceptions[check_id] = {"check": check_id, "reason": entry["reason"], "risk": entry["risk"]}
    return problems, exceptions, unsatisfied


def gate(contract: Contract, report: Report, approval_path: Path, verdict: str) -> tuple[str, list[str], list[dict]]:
    """The effective verdict, the reasons that it is not the approver's, and the exceptions that stand.

    Software can only lower an approval: a verdict other than approve stays changes needed."""
    reasons = list(report.problems)
    exceptions = {}
    approval = _read_json(approval_path, MAX_REPORT_BYTES, reasons)
    if approval is None:
        if not any(APPROVAL_NAME in reason for reason in reasons):
            reasons.append(f"The approver wrote no {APPROVAL_NAME}.")
        unsatisfied = []
    else:
        problems, exceptions, unsatisfied = _approval_problems(contract, report, approval, verdict)
        reasons += problems
    for check_id in contract.check_ids:
        status = report.statuses.get(check_id)
        if status == "failed":
            reasons.append(f"Required check {check_id} failed.")
        elif status in ("blocked", "not_applicable") and check_id not in exceptions:
            label = "blocked" if status == "blocked" else "not applicable"
            reasons.append(f"Required check {check_id} is {label} and has no exception.")
    reasons += [f"The approver found criterion {name} unsatisfied." for name in unsatisfied]
    if verdict != "approve":
        if verdict != "changes needed":
            reasons.insert(0, "The verdict's first line is not `Verdict: approve` or `Verdict: changes needed`.")
        # The approver's own changes needed stands; the reasons only explain the evidence.
        return "changes needed", reasons, list(exceptions.values())
    return ("changes needed" if reasons else "approve"), reasons, list(exceptions.values())
