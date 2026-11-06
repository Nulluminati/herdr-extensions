"""The verification contract in a plan, the builder's evidence and the approver's structured approval."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import attachments, verification
from herdboard.config import Config
from herdboard.store import Store
from support.git_env import isolated
from support.herdboard_store import write_board
from support.herdboard_verification import CONTRACT, approval, block, contract, plan, result, write_report

HEAD = "a" * 40


class ContractTests(unittest.TestCase):
    def parse(self, text):
        return verification.parse_contract(text)

    def refused(self, text, code="verification_plan_invalid"):
        with self.assertRaises(verification.VerificationError) as caught:
            self.parse(text)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def test_a_valid_contract(self):
        parsed = self.parse(plan())
        self.assertEqual(parsed.workflow, "feature")
        self.assertEqual([entry["id"] for entry in parsed.criteria], ["AC1", "AC2"])
        self.assertEqual(parsed.check_ids, ("unit", "live"))
        self.assertEqual(parsed.to_dict(), CONTRACT)
        self.assertRegex(parsed.digest, r"^sha256:[0-9a-f]{64}$")
        # The digest depends on the contract, not on its layout in the plan.
        self.assertEqual(self.parse(block(json.dumps(CONTRACT))).digest, parsed.digest)
        self.assertEqual(verification.Contract.from_dict(parsed.to_dict()), parsed)

    def test_every_workflow(self):
        for workflow in ("feature", "defect", "refactor", "performance"):
            with self.subTest(workflow=workflow):
                self.assertEqual(self.parse(plan(contract(workflow=workflow))).workflow, workflow)

    def test_a_plan_without_a_block_or_without_a_plan(self):
        message = self.refused("# Plan\n\n## Testing\nRun the tests.\n", "verification_plan_required")
        self.assertIn("herdboard-verification", message)
        self.refused(None, "verification_plan_required")
        # A block with another label, such as plain JSON, is not the contract.
        self.refused(block(CONTRACT, label="json"), "verification_plan_required")

    def test_two_blocks(self):
        self.assertIn("2 herdboard-verification blocks", self.refused(plan() + block(CONTRACT)))

    def test_malformed_json(self):
        self.assertIn("not valid JSON", self.refused(block("{ not json")))
        self.assertIn("duplicate key", self.refused(block('{"version": 1, "version": 1}')))
        self.assertIn("JSON object", self.refused(block("[]")))
        self.assertIn("not closed", self.refused("```herdboard-verification\n{}\n"))

    def test_an_oversized_block(self):
        self.assertIn("larger than", self.refused(block(" " * (verification.MAX_CONTRACT_BYTES + 1))))

    def test_field_errors_name_the_field(self):
        broken_check = {**CONTRACT["checks"][0], "evidence_kind": "vibes"}
        missing_field = {key: value for key, value in CONTRACT["checks"][0].items() if key != "cleanup"}
        for value, words in (
            (contract(version=2), "version"),
            (contract(version=True), "version"),
            (contract(workflow="research"), "workflow"),
            (contract(criteria=[]), "at least one criterion"),
            (contract(checks=[]), "at least one check"),
            (contract(criteria=[CONTRACT["criteria"][0], CONTRACT["criteria"][0]]), "duplicate criterion AC1"),
            (contract(checks=[CONTRACT["checks"][0], CONTRACT["checks"][0]]), "duplicate check unit"),
            (contract(checks=[{**CONTRACT["checks"][0], "criteria": ["AC9"]}]), "unknown criterion AC9"),
            (contract(checks=[{**CONTRACT["checks"][0], "criteria": ["AC1"]}]), "AC2 has no check"),
            (contract(checks=[{**CONTRACT["checks"][0], "criteria": []}]), "checks[0].criteria"),
            (contract(checks=[broken_check]), "checks[0].evidence_kind"),
            (contract(checks=[missing_field]), "checks[0].cleanup"),
            (contract(criteria=[{"id": "AC1"}]), "criteria[0].outcome"),
            (contract(criteria=[{"id": "", "outcome": "x"}]), "criteria[0].id"),
            (contract(criteria=[{"id": "has space", "outcome": "x"}]), "criteria[0].id"),
            (contract(extra=1), "unknown field extra"),
        ):
            with self.subTest(words=words):
                self.assertIn(words, self.refused(plan(value)))

    def test_the_error_says_how_to_fix_the_plan(self):
        message = self.refused("# Plan\n", "verification_plan_required")
        self.assertIn("Update the plan", message)


class CheckPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        todos = Path(temp.name) / "todos"
        write_board(todos, "work", [("Todo", [{"id": "t001", "title": "one"}])])
        self.config = Config(todos_dir=todos)
        self.store = Store(self.config)

    def write_plan(self, text):
        path = attachments.plan_path(self.config, "t001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_check_plan_returns_the_contract_and_the_plan(self):
        self.write_plan(plan())
        found, text = verification.check_plan(self.config, "t001")
        self.assertEqual(found.check_ids, ("unit", "live"))
        self.assertEqual(text, plan())

    def test_check_plan_refuses_a_card_without_a_plan(self):
        with self.assertRaises(verification.VerificationError) as caught:
            verification.check_plan(self.config, "t001")
        self.assertEqual(caught.exception.code, "verification_plan_required")
        self.assertIn("t001 has no plan", str(caught.exception))

    def test_freeze_writes_the_plan_copy_and_returns_the_state_keys(self):
        self.write_plan(plan())
        found, text = verification.check_plan(self.config, "t001")
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        keys = verification.freeze(folder, found, text)
        self.assertEqual(keys["verification"], 1)
        self.assertEqual(keys["contract"], CONTRACT)
        self.assertEqual(keys["plan_digest"], verification.digest(text))
        self.assertEqual((folder / "plan.md").read_text(), text)


class EvidenceTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.evidence = self.root / "run" / "evidence"
        self.contract = verification.Contract.from_dict(CONTRACT)
        self.run_id = "t001-20261106-100000"

    def report(self, commit=HEAD, **changes):
        return write_report(self.evidence, self.run_id, self.contract.digest, commit, **changes)

    def validate(self, head=HEAD, dirty=()):
        return verification.validate_report(
            self.evidence, run_id=self.run_id, contract=self.contract, head=head, dirty=list(dirty)
        )

    def problems(self, **options):
        return " | ".join(self.validate(**options).problems)


class ReportTests(EvidenceTestCase):
    def test_a_valid_report(self):
        self.report()
        report = self.validate()
        self.assertEqual(report.problems, [])
        self.assertEqual(report.statuses, {"unit": "passed", "live": "passed"})
        self.assertEqual(set(report.files), {"verification.json", "unit.txt", "live.txt"})
        self.assertEqual(report.files["unit.txt"], verification.digest(b"output of unit\n"))

    def test_a_missing_or_malformed_report(self):
        self.assertIn("no verification.json", self.problems())
        self.evidence.mkdir(parents=True)
        (self.evidence / "verification.json").write_text("{ nope")
        self.assertIn("not valid JSON", self.problems())
        (self.evidence / "verification.json").write_text("[]")
        self.assertIn("JSON object", self.problems())
        (self.evidence / "verification.json").write_text(" " * (verification.MAX_REPORT_BYTES + 1))
        self.assertIn("larger than", self.problems())

    def test_a_report_of_another_run_contract_or_commit(self):
        for changes, words in (
            ({"version": 2}, "version"),
            ({"run": "other"}, "another factory run"),
            ({"contract_digest": "sha256:" + "0" * 64}, "another contract"),
            ({"commit": "b" * 40}, f"commit {'b' * 40}, not the current commit {HEAD}"),
            ({"commit": "HEAD"}, "full commit"),
        ):
            with self.subTest(words=words):
                self.report(**changes)
                self.assertIn(words, self.problems())

    def test_a_dirty_tree(self):
        self.report()
        problems = self.problems(dirty=[" M a.py", "?? new.txt"])
        self.assertIn("uncommitted or untracked changes: a.py, new.txt", problems)

    def test_result_problems(self):
        unit, live = result("unit"), result("live", evidence=["live.txt"])
        for results, words in (
            ([unit], "no result for check live"),
            ([unit, live, result("unit")], "two results for check unit"),
            ([unit, live, result("extra", evidence=["unit.txt"])], "unknown check extra"),
            ([{**unit, "status": "ok"}, live], "results[0].status"),
            ([{**unit, "observed": ""}, live], "results[0].observed"),
            ([{**unit, "action": None}, live], "results[0].action"),
            ([{**unit, "started": "yesterday"}, live], "results[0].started"),
            ([{**unit, "finished": "2026-11-06T09:00:00Z"}, live], "finishes before it starts"),
            ([{**unit, "exit_code": 1}, live], "passed with exit code 1"),
            ([{key: value for key, value in unit.items() if key != "exit_code"}, live], "results[0].exit_code"),
            ([{**unit, "exit_code": True}, live], "results[0].exit_code"),
            ([unit, {**live, "evidence": []}], "Check live passed without evidence"),
            ([unit, {**live, "evidence": "live.txt"}], "results[1].evidence"),
        ):
            with self.subTest(words=words):
                shutil.rmtree(self.evidence, ignore_errors=True)
                self.report(results=results)
                self.assertIn(words, self.problems())

    def test_a_failed_or_blocked_result_needs_no_exit_code_of_zero(self):
        self.report(results=[result("unit", "failed"), result("live", "blocked", evidence=[])])
        report = self.validate()
        self.assertEqual(report.problems, [])
        self.assertEqual(report.statuses, {"unit": "failed", "live": "blocked"})

    def test_evidence_paths_stay_in_the_evidence_folder(self):
        outside = self.root / "secret.txt"
        outside.write_text("secret\n")
        self.evidence.mkdir(parents=True)
        os.symlink(outside, self.evidence / "link.txt")
        (self.evidence / "empty.txt").write_text("")
        (self.evidence / "folder").mkdir()
        for name, words in (
            (str(outside), "absolute"),
            ("../secret.txt", "outside the evidence folder"),
            ("folder/../../secret.txt", "outside the evidence folder"),
            ("link.txt", "outside the evidence folder"),
            ("missing.txt", "missing.txt does not exist"),
            ("folder", "not a file"),
            ("empty.txt", "empty.txt is empty"),
            ("verification.json", "the report itself"),
        ):
            with self.subTest(name=name):
                self.report(results=[result("unit", evidence=[name]), result("live", evidence=["live.txt"])])
                self.assertIn(words, self.problems())

    def test_a_link_inside_the_evidence_folder_is_allowed(self):
        self.evidence.mkdir(parents=True)
        (self.evidence / "real.txt").write_text("real\n")
        os.symlink(self.evidence / "real.txt", self.evidence / "alias.txt")
        self.report(results=[result("unit", evidence=["alias.txt"]), result("live", evidence=["live.txt"])])
        self.assertEqual(self.validate().problems, [])

    def test_oversized_evidence(self):
        self.evidence.mkdir(parents=True)
        (self.evidence / "big.txt").write_bytes(b"x" * 11)
        self.report(results=[result("unit", evidence=["big.txt"]), result("live", evidence=["live.txt"])])
        with patch.object(verification, "MAX_EVIDENCE_BYTES", 10):
            self.assertIn("big.txt is larger than", self.problems())

    def test_snapshot_and_later_changes(self):
        self.report()
        report = self.validate()
        target = self.root / "run" / "approver" / "evidence"
        verification.snapshot(report, self.evidence, target)
        self.assertEqual((target / "unit.txt").read_text(), "output of unit\n")
        self.assertTrue((target / "verification.json").exists())
        self.assertEqual(verification.changed_since(report, self.evidence), [])
        (self.evidence / "unit.txt").write_text("other\n")
        (self.evidence / "verification.json").write_text("{}")
        self.assertEqual(
            verification.changed_since(report, self.evidence),
            ["The evidence changed after herdboard captured it: verification.json, unit.txt."],
        )


class TreeStateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, isolated()))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "first")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def test_tree_state_reports_the_commit_and_every_change(self):
        head, dirty = verification.tree_state(self.repo)
        self.assertEqual(head, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(dirty, [])
        (self.repo / "a.txt").write_text("changed\n")
        (self.repo / "folder").mkdir()
        (self.repo / "folder" / "new.txt").write_text("new\n")
        # The same HEAD does not mean the same tree.
        self.assertEqual(verification.tree_state(self.repo), (head, [" M a.txt", "?? folder/new.txt"]))


class GateTests(EvidenceTestCase):
    def setUp(self):
        super().setUp()
        self.approver = self.root / "run" / "approver"
        self.approver.mkdir(parents=True)

    def gate(self, verdict="approve", value=None, results=None, raw=None):
        self.report(results=results)
        report = self.validate()
        if raw is not None:
            (self.approver / "approval.json").write_text(raw)
        elif value is not False:
            (self.approver / "approval.json").write_text(json.dumps(approval() if value is None else value))
        return verification.gate(self.contract, report, self.approver / "approval.json", verdict)

    def test_valid_evidence_and_approval_stand(self):
        effective, reasons, accepted = self.gate()
        self.assertEqual((effective, reasons), ("approve", []))
        self.assertEqual(accepted, [])

    def test_changes_needed_is_never_upgraded(self):
        effective, reasons, _ = self.gate("changes needed", approval("changes needed"))
        self.assertEqual((effective, reasons), ("changes needed", []))
        self.assertEqual(self.gate("unknown")[0], "changes needed")

    def test_an_approver_that_writes_approve_falsely(self):
        bad = [result("unit", "failed"), result("live", evidence=["live.txt"])]
        effective, reasons, _ = self.gate(results=bad)
        self.assertEqual(effective, "changes needed")
        self.assertIn("Required check unit failed.", reasons)

    def test_missing_or_malformed_approval(self):
        for options, words in (
            ({"value": False}, "no approval.json"),
            ({"raw": "{ nope"}, "not valid JSON"),
            ({"value": approval("changes needed")}, "approval.json says changes needed"),
            ({"value": approval(version=2)}, "version"),
            ({"value": approval(criteria=[approval()["criteria"][0]])}, "no outcome for criterion AC2"),
            (
                {"value": approval(criteria=[*approval()["criteria"], {**approval()["criteria"][0]}])},
                "two outcomes for criterion AC1",
            ),
            (
                {"value": approval(criteria=[*approval()["criteria"], {"id": "AC9", "outcome": "satisfied"}])},
                "unknown criterion AC9",
            ),
            ({"value": approval(criteria=[{"id": "AC1", "outcome": "maybe"}])}, "criteria[0].outcome"),
        ):
            with self.subTest(words=words):
                effective, reasons, _ = self.gate(**options)
                self.assertEqual(effective, "changes needed")
                self.assertIn(words, " | ".join(reasons))

    def test_an_unsatisfied_criterion(self):
        criteria = approval()["criteria"]
        criteria[1] = {**criteria[1], "outcome": "unsatisfied"}
        effective, reasons, _ = self.gate(value=approval(criteria=criteria))
        self.assertEqual(effective, "changes needed")
        self.assertIn("The approver found criterion AC2 unsatisfied.", reasons)

    def test_report_problems_force_changes_needed(self):
        effective, reasons, _ = self.gate(results=[result("unit")])
        self.assertEqual(effective, "changes needed")
        self.assertIn("no result for check live", " | ".join(reasons))

    def test_exceptions(self):
        blocked = [result("unit"), result("live", "blocked", evidence=[], observed="No display in the sandbox.")]
        exception = {"check": "live", "reason": "No display.", "risk": "The board is not checked live."}
        effective, reasons, accepted = self.gate(value=approval(exceptions=[exception]), results=blocked)
        self.assertEqual((effective, reasons), ("approve", []))
        self.assertEqual(accepted, [exception])
        # Without an exception, a blocked check does not pass.
        effective, reasons, _ = self.gate(results=blocked)
        self.assertEqual(effective, "changes needed")
        self.assertIn("Required check live is blocked and has no exception.", reasons)
        not_applicable = [result("unit"), result("live", "not_applicable", evidence=[])]
        self.assertEqual(self.gate(value=approval(exceptions=[exception]), results=not_applicable)[0], "approve")

    def test_invalid_exceptions(self):
        blocked = [result("unit"), result("live", "blocked", evidence=[])]
        failed = [result("unit", "failed"), result("live", "blocked", evidence=[])]
        good = {"check": "live", "reason": "No display.", "risk": "Not checked live."}
        for exceptions, results, words in (
            ([good, good], blocked, "two exceptions for check live"),
            ([{**good, "check": "nope"}], blocked, "unknown check nope"),
            ([{**good, "check": "*"}], blocked, "unknown check *"),
            ([{**good, "reason": ""}], blocked, "exceptions[0].reason"),
            ([{**good, "risk": " "}], blocked, "exceptions[0].risk"),
            ([{**good, "check": "unit"}], blocked, "Check unit passed, so it needs no exception"),
            (
                [good, {**good, "check": "unit"}],
                failed,
                "Check unit failed, and a failed check cannot have an exception",
            ),
            ("all", blocked, "exceptions must be a list"),
        ):
            with self.subTest(words=words):
                effective, reasons, _ = self.gate(value=approval(exceptions=exceptions), results=results)
                self.assertEqual(effective, "changes needed")
                self.assertIn(words, " | ".join(reasons))

    def test_an_exception_does_not_excuse_an_unsatisfied_criterion(self):
        blocked = [result("unit"), result("live", "blocked", evidence=[])]
        criteria = approval()["criteria"]
        criteria[0] = {**criteria[0], "outcome": "unsatisfied"}
        good = {"check": "live", "reason": "No display.", "risk": "Not checked live."}
        effective, reasons, _ = self.gate(value=approval(criteria=criteria, exceptions=[good]), results=blocked)
        self.assertEqual(effective, "changes needed")
        self.assertIn("The approver found criterion AC1 unsatisfied.", reasons)
