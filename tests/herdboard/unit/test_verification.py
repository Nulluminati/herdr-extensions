"""The verification contract in a plan, the builder's evidence and the approver's structured approval."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from herdboard import attachments, verification
from herdboard.config import Config
from herdboard.store import Store
from support.herdboard_store import write_board
from support.herdboard_verification import CONTRACT, block, contract, plan


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
