"""Factory runs: lens reviewers check each move to Review, the builder fixes, an approver gives a verdict."""

import contextlib
import dataclasses
import io
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from herdboard import activity, agent_review, attachments, factory, start, verification
from herdboard.config import Config
from herdboard.linear_states import refuse
from herdboard.model import HerdboardError
from support.herdboard_cli import CliTestCase
from support.herdboard_merge import GitHerdr, MergeTestCase
from support.herdboard_verification import CONTRACT, approval, contract, result, write_report
from support.herdboard_verification import plan as verified_plan

REQUEST = re.compile(r"Read (\S+)/request\.md")


class FactoryHerdr(GitHerdr):
    """GitHerdr with lens reviewers and an approver that write their files when they turn idle,
    a builder whose status the test sets, and a hook that plays the builder after a prompt."""

    def __init__(self, root):
        super().__init__(root)
        self.reviewers = {}
        self.findings = {}
        self.verdict = "Verdict: approve\n\n## Plan steps not done\nNone.\n"
        # The approver's approval.json; None writes none.
        self.approval = approval()
        # Called when the approver writes its files, to play changes during the approval.
        self.on_approve = None
        self.builder_status = "idle"
        self.reviewer_states = {}
        self.on_prompt = None
        self.prompts = []
        self.failed_starts = set()

    def next_builder_status(self):
        """`builder_status` is one status, or a list that the builder goes through, one per `agent get`."""
        if isinstance(self.builder_status, list):
            return self.builder_status.pop(0) if len(self.builder_status) > 1 else self.builder_status[0]
        return self.builder_status

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "pane split":
            self.calls.append(args)
            workspace = args[2].split(":")[0]
            pane = f"{workspace}:p{self.next_pane}"
            self.next_pane += 1
            self.panes.setdefault(workspace, [workspace + ":p1"]).append(pane)
            return self.reply(argv, {"pane": {"pane_id": pane}})
        if operation == "pane close":
            self.calls.append(args)
            for panes in self.panes.values():
                if args[2] in panes:
                    panes.remove(args[2])
            self.reviewers.pop(args[2], None)
            return self.reply(argv, {"type": "ok"})
        if operation == "pane get":
            self.calls.append(args)
            if not any(args[2] in panes for panes in self.panes.values()):
                return self.reply(argv, error="pane_not_found")
            return self.reply(argv, {"pane": {"pane_id": args[2]}})
        if operation in ("notification show", "agent wait"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and re.search(r"-(security|ux|ui|code|approver)$", args[2]):
            self.calls.append(args)
            pane = args[args.index("--pane") + 1]
            folder = next(Path(m.group(1)) for arg in args if (m := REQUEST.search(arg)))
            lens = args[2].rsplit("-", 1)[1]
            if lens in self.failed_starts:
                return self.reply(argv, error="agent_kind_unavailable")
            self.reviewers[pane] = {
                "lens": lens,
                "folder": folder,
                "states": list(self.reviewer_states.get(lens, ["working", "idle"])),
            }
            return self.reply(argv, {"type": "ok"})
        if operation == "agent get" and args[2] in self.reviewers:
            self.calls.append(args)
            reviewer = self.reviewers[args[2]]
            state = reviewer["states"].pop(0) if reviewer["states"] else "idle"
            if state is None:
                return self.reply(argv, error="agent_not_found")
            if state == "idle":
                if reviewer["lens"] == "approver":
                    (reviewer["folder"] / "verdict.md").write_text(self.verdict)
                    if self.approval is not None:
                        (reviewer["folder"] / "approval.json").write_text(json.dumps(self.approval))
                    if self.on_approve:
                        self.on_approve()
                else:
                    (reviewer["folder"] / "findings.md").write_text(
                        self.findings.get(reviewer["lens"], "No findings.\n")
                    )
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        if operation == "agent get" and self.agent and args[2].endswith(":p1"):
            self.calls.append(args)
            status = self.next_builder_status()
            if status is None:
                return self.reply(argv, error="agent_not_found")
            return self.reply(argv, {"agent": {"agent_status": status, "pane_id": args[2]}})
        if operation == "agent prompt":
            self.calls.append(args)
            self.prompts.append(args[3])
            if self.on_prompt:
                self.on_prompt(args[3])
            return self.reply(argv, {"type": "ok"})
        return super().__call__(argv, timeout)


class FactoryTestCase(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.fake.__class__ = FactoryHerdr
        FactoryHerdr.__init__(self.fake, self.root)
        self.workers = []
        self.enterContext(
            patch.object(
                agent_review,
                "start_worker",
                lambda arguments, log: self.workers.append((arguments, log)) or os.getpid(),
            )
        )
        self.enterContext(patch.object(factory, "POLL_SECONDS", 0))
        self.enterContext(patch.object(agent_review, "POLL_SECONDS", 0))

    def start_factory(self, **options):
        """Start a factory run. A card without a plan gets one with a valid verification contract."""
        if not attachments.plan_path(self.config, self.card.ref).exists():
            self.write_plan(verified_plan())
        result = self.starter.start(self.card.ref, agent=options.pop("agent", "claude"), factory=True, **options)
        self.worktree = Path(result["worktree_path"])
        self.folder = Path(self.record()["factory"]["folder"])
        return result

    def state(self):
        return self.record()["factory"]

    def ready(self):
        """The builder's `move <id> Review` while the factory runs: a ready signal."""
        self.assertIsNotNone(factory.signal_ready(self.card.ref))

    def write_plan(self, text):
        path = attachments.plan_path(self.config, self.card.ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def write_evidence(self, results=None, **changes):
        """The builder's evidence for the current commit, by default a pass for each check of CONTRACT."""
        head = self.git(self.worktree, "rev-parse", "HEAD")
        digest = verification.Contract.from_dict(CONTRACT).digest
        return write_report(self.folder / "evidence", self.folder.name, digest, head, results, **changes)

    def builder_fixes(self, commit=True):
        """The builder answers a round: it commits a fix, checks it again and moves the card to Review."""
        self.columns = []

        def answer(message):
            self.columns.append(self.store.show(self.card.ref).column)
            if commit:
                self.commit(self.worktree, f"fix{len(self.fake.prompts)}.txt", "x\n", "fix: a finding")
                self.write_evidence()
            self.ready()

        self.fake.on_prompt = answer

    def wait(self):
        self.errors = io.StringIO()
        with contextlib.redirect_stderr(self.errors):
            return factory.wait(self.store, self.card.ref, self.folder)

    def drop_policy(self, run=False):
        """Make the record look like one written before the evidence policy (and, with `run`, before snapshots)."""
        records = json.loads((start.state_dir() / "starts.json").read_text())
        state = records[self.card.ref]["factory"]
        for key in ("verification", "contract", "plan_digest", *(["run"] if run else [])):
            state.pop(key, None)
        start._write(start.state_dir() / "starts.json", records)

    def lens_starts(self):
        return [c for c in self.calls("agent", "start") if re.search(r"-(security|ux|ui|code)$", c[2])]


class LensTests(FactoryTestCase):
    def test_parse_lenses(self):
        every = ("security", "ux", "ui", "code")
        for text, expected in (
            (None, every),
            ("# Plan\n\n## Decisions\n- Something.\n", every),
            ("## Decisions\nReview lenses: security, code\n", ("security", "code")),
            ("## Decisions\n- Review lenses: code, security\n", ("security", "code")),
            ("## Decisions\n* review LENSES: UX, Code.\n", ("ux", "code")),
            ("## Decisions\n- Review lenses: perf, docs\n", every),
            ("## Decisions\n- Review lenses: `security`, speed\n", ("security",)),
        ):
            with self.subTest(text=text):
                self.assertEqual(factory.parse_lenses(text), expected)

    def test_lens_kind_comes_from_the_config_as_is(self):
        config = Config()
        self.assertEqual(factory.lens_kind(config, "security"), ("codex", None, None))
        self.assertEqual(factory.lens_kind(config, "ux"), ("claude", None, None))
        config.factory_lenses["ux"] = {"agent": "codex", "model": "gpt-6.1", "thinking": "high"}
        self.assertEqual(factory.lens_kind(config, "ux"), ("codex", "gpt-6.1", "high"))

    def test_approver_kind(self):
        config = Config()
        self.assertEqual(factory.approver_kind(config, "claude"), "pi")
        self.assertEqual(factory.approver_kind(config, "codex"), "claude")
        config.factory_approver = "codex"
        self.assertEqual(factory.approver_kind(config, "claude"), "codex")

    def test_count_confirmed_counts_only_confirmed_problems(self):
        text = (
            "## Confirmed problems\n- [high] a.py:1: bug.\n- [low] a.py:2: style.\n\n"
            "## Risks and questions\n- a.py:3: why?\n"
        )
        self.assertEqual(factory.count_confirmed(text), 2)
        self.assertEqual(factory.count_questions(text), 1)
        self.assertEqual(factory.count_confirmed("## Risks and questions\n- a.py:3: why?\n"), 0)
        self.assertEqual(factory.count_confirmed("No findings.\n"), 0)
        # A reviewer may write "- None." in an empty section.
        self.assertEqual(factory.count_questions("## Confirmed problems\n- None.\n## Risks and questions\n- None\n"), 0)
        self.assertEqual(factory.count_confirmed("## Confirmed problems\n- none.\n"), 0)
        merged = "# security (codex)\n\n" + text + "\n# code (pi)\n\n## Confirmed problems\n- [low] b.py:1: x.\n"
        self.assertEqual(factory.count_confirmed(merged), 3)
        # A "#" line that is not a section heading, such as a note or a shell comment, does not end the section.
        noted = "## Confirmed problems\n- [high] a.py:1: bug.\n# Note: see below\n- [low] a.py:2: style.\n"
        self.assertEqual(factory.count_confirmed(noted), 2)
        self.assertEqual(factory.count_confirmed("## Confirmed problems\n- a\n### Detail\n- b\n"), 2)
        # A verification limit is not a finding: the builder gets no round for it.
        limits = text + "\n## Verification limits\n- The tests could not run in the sandbox.\n"
        self.assertEqual((factory.count_confirmed(limits), factory.count_questions(limits)), (2, 1))

    def test_count_confirmed_accepts_other_heading_styles(self):
        for confirmed, risks in [
            ("## Confirmed problems:", "## Risks and questions:"),
            ("# Confirmed problems", "# Risks and questions"),
            ("**Confirmed problems**", "**Risks and questions**"),
            ("**Confirmed problems:**", "**Risks and questions:**"),
            ("##Confirmed problems", "##Risks and questions"),
            ("### Confirmed Problems", "### Risks and Questions"),
            ("## __Confirmed problems__", "## __Risks and questions__"),
        ]:
            with self.subTest(heading=confirmed):
                text = f"{confirmed}\n- [high] src/app.py:12: injection\n\n{risks}\n- src/app.py:20: unclear\n"
                self.assertEqual(factory.count_confirmed(text), 1)
                self.assertEqual(factory.count_questions(text), 1)

    def test_a_lens_heading_ends_a_section_in_another_style(self):
        merged = (
            "# security (codex)\n\n**Confirmed problems:**\n- [high] a.py:1: bug.\n\n"
            "# code (pi)\n\n- [low] b.py:1: x.\n"
        )
        self.assertEqual(factory.count_confirmed(merged), 1)


class StartFactoryTests(FactoryTestCase):
    def test_default_run_and_unknown_preset(self):
        run = factory.default_run(self.config, "pi", "test/model", "high")
        self.assertEqual(run.builder.agent, "pi")
        self.assertEqual(run.builder.model, "test/model")
        self.assertEqual(run.approver.agent, factory.approver_kind(self.config, "pi"))
        self.assertEqual(run.rounds, self.config.factory_max_rounds)
        self.assertIsNone(run.lenses_on)
        with self.assertRaises(factory.FactoryError) as caught:
            factory.resolve_run(self.config, "missing")
        self.assertEqual(caught.exception.code, "unknown_preset")

    def test_stored_run_controls_builder_and_lenses(self):
        from herdboard.config import Role

        run = dataclasses.replace(
            factory.default_run(self.config),
            name="cheap",
            builder=Role("pi", "test/model", "high"),
            lenses_on=("security",),
            rounds=2,
        )
        self.start_factory(factory_run=run)
        self.assertEqual(self.record()["agent"], "pi")
        self.assertEqual(self.state()["run"], run.to_dict())
        self.assertEqual(factory.summary(self.state())["preset"], "cheap")
        self.commit(self.worktree, "change.txt", "change\n", "feat: change")
        self.write_plan(verified_plan(lenses="code, ux"))
        review = factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
        self.assertEqual([entry["lens"] for entry in review["reviewers"]], ["security"])

    def test_stored_lens_and_approver_launch_settings(self):
        from herdboard.config import Role

        run = dataclasses.replace(
            factory.default_run(self.config),
            approver=Role("claude", "opus", "max"),
            lenses={lens: Role("codex", "test-model", "high") for lens in ("security", "ux", "ui", "code")},
            lenses_on=("code",),
        )
        self.start_factory(factory_run=run)
        self.commit(self.worktree, "change.txt", "change\n", "feat: change")
        self.assertEqual(factory.lens_kind(self.config, "code", run), ("codex", "test-model", "high"))
        with patch.object(factory, "launch_reviewer", wraps=factory.launch_reviewer) as launch:
            factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
            self.assertEqual(launch.call_args.args[1:4], ("codex", "test-model", "high"))
            factory.run_approver(self.store, self.card.ref, self.record(), self.card)
            self.assertEqual(launch.call_args.args[1:4], ("claude", "opus", "max"))

    def test_legacy_record_resolves_default_run(self):
        self.start_factory(rounds=2)
        record = self.record()
        record["factory"].pop("run")
        run = factory.record_run(self.config, record)
        self.assertEqual(run.name, "default")
        self.assertEqual(run.rounds, 2)
        self.assertEqual(run.builder.agent, record["agent"])

    def test_start_writes_the_factory_state_and_starts_the_worker(self):
        result = self.start_factory(rounds=2)
        state = self.state()
        self.assertEqual((state["rounds"], state["round"], state["status"]), (2, 0, "building"))
        self.assertTrue(self.folder.is_dir())
        self.assertEqual(result["factory"]["status"], "building")
        arguments, log = self.workers[-1]
        self.assertEqual(arguments, ["factory-wait", self.card.ref, "--folder", str(self.folder)])
        self.assertEqual(log, self.folder / "worker.log")
        self.assertIn("Factory run: preset default (builder claude default; approver pi default; 2 rounds)", self.log())
        # The start records the worker's process ID, so a worker that dies at once does not look alive.
        self.assertEqual(state["pid"], os.getpid())

    def test_one_round_is_named_in_the_singular(self):
        self.start_factory(rounds=1)
        self.assertIn("1 round)", self.log()[0])

    def test_the_rounds_default_to_the_config(self):
        self.config.factory_max_rounds = 4
        self.start_factory()
        self.assertEqual(self.state()["rounds"], 4)

    def test_the_builder_gets_the_factory_rule_and_the_factory_folder(self):
        self.start_factory()
        call = next(c for c in self.calls("agent", "start") if c[2] == f"hb-{self.card.ref.lower()}")
        self.assertEqual(call[-2:], ["--add-dir", str(self.folder)])
        brief = Path(re.search(r"(/\S+\.md)", " ".join(call)).group(1)).read_text()
        self.assertIn("This card runs as a factory.", brief)

    def test_a_builder_restarted_during_a_review_round_gets_the_factory_folder(self):
        # The worker prompts the builder after the round, so a restart in any active status needs the folder.
        self.start_factory()
        record = self.record()
        for status in ("reviewing", "approving"):
            with self.subTest(status=status):
                factory.update(self.card.ref, self.folder, status=status)
                record["factory"]["status"] = status
                self.starter.launch_agent(self.card, record, "claude", None, None, None, lambda: None)
                call = [c for c in self.calls("agent", "start") if c[2] == f"hb-{self.card.ref.lower()}"][-1]
                self.assertEqual(call[-2:], ["--add-dir", str(self.folder)])

    def test_a_codex_builder_may_write_the_factory_folder(self):
        self.start_factory(agent="codex")
        call = next(c for c in self.calls("agent", "start") if c[2] == f"hb-{self.card.ref.lower()}")
        self.assertIn(str(self.folder), call[call.index("--add-dir") + 1])

    def test_refusals(self):
        for options, code in (
            ({"no_agent": True}, "usage"),
            ({"no_repo": True}, "usage"),
            ({"new_repo": True}, "usage"),
            ({"rounds": 0}, "usage"),
            ({"rounds": 11}, "usage"),
        ):
            with self.subTest(options=options):
                with self.assertRaises(start.StartError) as raised:
                    self.starter.start(self.card.ref, factory=True, **options)
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, rounds=2)
        self.assertEqual(raised.exception.code, "usage")
        self.assertEqual(self.workers, [])

    def test_a_failed_start_leaves_no_running_factory(self):
        self.write_plan(verified_plan())
        self.fake.fail = "agent start"
        with self.assertRaises(start.StartError):
            self.starter.start(self.card.ref, agent="claude", factory=True)
        self.assertEqual(self.record()["factory"]["status"], "stopped")
        self.assertFalse(factory.factory_running(self.card.ref))
        self.assertEqual(self.workers, [])

    def test_a_second_run_is_refused_while_the_first_runs(self):
        self.start_factory()
        self.fake.agent = False
        with self.assertRaises(factory.FactoryError) as raised:
            self.starter.start(self.card.ref, agent="claude", factory=True)
        self.assertEqual(raised.exception.code, "factory_running")

    def test_a_factory_does_not_start_over_a_running_agent(self):
        self.write_plan(verified_plan())
        self.starter.start(self.card.ref, agent="claude")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, agent="claude", factory=True)
        self.assertEqual(raised.exception.code, "agent_running")

    def test_every_kind_of_run_refuses_a_plan_without_a_valid_contract(self):
        from herdboard.config import Role

        preset = dataclasses.replace(factory.default_run(self.config), name="cheap", rounds=1)
        custom = dataclasses.replace(
            factory.default_run(self.config), name="custom", builder=Role("pi", "test/model", "high")
        )
        runs = {"default": None, "preset": preset, "custom": custom}
        for plan_text, code in (
            (None, "verification_plan_required"),
            ("# Plan\n\n## Testing\nRun the tests.\n", "verification_plan_required"),
            (verified_plan(contract(workflow="research")), "verification_plan_invalid"),
        ):
            for name, run in runs.items():
                with self.subTest(plan=plan_text, run=name):
                    if plan_text is None:
                        attachments.plan_path(self.config, self.card.ref).unlink(missing_ok=True)
                    else:
                        self.write_plan(plan_text)
                    before = len(self.fake.calls)
                    with self.assertRaises(verification.VerificationError) as raised:
                        self.starter.start(self.card.ref, agent="claude", factory=True, factory_run=run)
                    self.assertEqual(raised.exception.code, code)
                    self.assertIn("Update the plan", str(raised.exception))
                    # Nothing started: no worktree, no agent, no worker and no factory state.
                    self.assertEqual(self.fake.calls[before:], [])
                    self.assertEqual(self.workers, [])
                    self.assertIsNone(factory.factory_state(self.card.ref))
                    self.assertEqual(self.store.show(self.card.ref).column, self.card.column)

    def test_an_ordinary_start_needs_no_contract(self):
        result = self.starter.start(self.card.ref, agent="claude")
        self.assertIsNone(self.record().get("factory"))
        self.assertTrue(result["worktree_path"])

    def test_a_new_run_freezes_the_contract_beside_its_settings(self):
        run = dataclasses.replace(factory.default_run(self.config), name="cheap", lenses_on=("security",))
        before = run.to_dict()
        self.write_plan(verified_plan(lenses="code"))
        self.start_factory(factory_run=run)
        state = self.state()
        self.assertEqual(state["verification"], 1)
        self.assertEqual(state["contract"], CONTRACT)
        self.assertEqual(state["plan_digest"], verification.digest(verified_plan(lenses="code")))
        self.assertEqual((self.folder / "plan.md").read_text(), verified_plan(lenses="code"))
        self.assertTrue((self.folder / "evidence").is_dir())
        # The run settings keep their own format: the contract is not part of them.
        self.assertEqual(state["run"], before)
        self.assertNotIn("verification", state["run"])
        # A run without the code lens still starts.
        self.assertEqual(state["run"]["lenses_on"], ["security"])


class RoundTests(FactoryTestCase):
    def test_one_reviewer_per_plan_lens_with_its_kind_name_and_split(self):
        self.write_plan(verified_plan(lenses="security, code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        record = self.record()
        review = factory.run_round(self.store, self.card.ref, record, self.card, 1)
        starts = self.lens_starts()
        ref = self.card.ref.lower()
        self.assertEqual(
            [(c[2], c[c.index("--kind") + 1]) for c in starts],
            [(f"hb-{ref}-security", "codex"), (f"hb-{ref}-code", "pi")],
        )
        splits = self.calls("pane", "split")[-2:]
        self.assertEqual(splits[0][2:5], [record["pane_id"], "--direction", "right"])
        self.assertEqual(splits[1][2:5], [review["reviewers"][0]["pane_id"], "--direction", "down"])
        self.assertTrue(all("--no-focus" in split for split in splits))
        request = (self.folder / "round-1" / "security" / "request.md").read_text()
        self.assertIn(factory.LENS_FOCUS["security"], request)
        self.assertNotIn(agent_review.DEFAULT_FOCUS, request)
        self.assertNotIn("Earlier rounds", request)
        self.assertTrue((self.folder / "round-1" / "code" / "branch.patch").read_text())
        state = self.state()
        self.assertEqual((state["round"], state["lenses"], state["status"]), (1, ["security", "code"], "reviewing"))

    def test_the_next_round_gets_the_earlier_answers(self):
        self.write_plan(verified_plan(lenses="code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        factory.run_round(self.store, self.card.ref, self.record(), self.card, 2)
        request = (self.folder / "round-2" / "code" / "request.md").read_text()
        self.assertIn("## Earlier rounds", request)
        self.assertIn(str(self.folder / "round-1" / "responses.md"), request)
        self.assertIn("unless you have new evidence", request)
        self.assertIn("Check the evidence that an answer cites", request)

    def test_wait_round_merges_findings_and_a_failed_lens_does_not_stop_it(self):
        self.write_plan(verified_plan(lenses="security, code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n\n## Risks and questions\n- q\n"
        self.fake.reviewer_states["security"] = ["working", None]
        review = factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
        result = factory.wait_round(self.card.ref, self.folder, review)
        self.assertEqual((result["confirmed"], result["questions"]), (1, 1))
        self.assertEqual(list(result["failures"]), ["security"])
        merged = (self.folder / "round-1" / "findings.md").read_text()
        self.assertIn("# security", merged)
        self.assertIn("This lens failed", merged)
        self.assertIn("# code (pi)", merged)
        self.assertIn("- [high] a.txt:1: bug.", merged)
        closed = {c[2] for c in self.calls("pane", "close")}
        self.assertTrue({entry["pane_id"] for entry in review["reviewers"]} <= closed)
        self.assertEqual(self.state()["panes"], [])

    def test_a_lens_that_wrote_its_findings_keeps_them_when_the_round_times_out(self):
        self.write_plan(verified_plan(lenses="security, code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        # The code reviewer wrote its findings and then stayed busy; the security reviewer wrote nothing.
        self.fake.reviewer_states["code"] = ["working"] * 5
        self.fake.reviewer_states["security"] = ["working"] * 5
        review = factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
        (self.folder / "round-1" / "code" / "findings.md").write_text("## Confirmed problems\n- [high] a.txt:1: bug.\n")
        with patch.object(factory, "REVIEW_TIMEOUT_SECONDS", 0):
            result = factory.wait_round(self.card.ref, self.folder, review)
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(list(result["failures"]), ["security"])
        self.assertIn("did not finish", result["failures"]["security"])
        merged = (self.folder / "round-1" / "findings.md").read_text()
        self.assertIn("# code (pi)", merged)
        self.assertIn("- [high] a.txt:1: bug.", merged)
        self.assertNotIn("This lens failed: The pi reviewer", merged)
        self.assertIn("This lens failed: The codex reviewer", merged)

    def test_a_lens_that_does_not_launch_fails_alone(self):
        self.write_plan(verified_plan(lenses="security, code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.fake.failed_starts = {"security"}
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            review = factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
        self.assertIn("the security reviewer did not start: agent_kind_unavailable", errors.getvalue())
        # The pane that the failed launch split is closed; the code reviewer's pane stays.
        closed = [c[2] for c in self.calls("pane", "close")]
        self.assertEqual(len(closed), 1)
        self.assertNotEqual(closed[0], review["reviewers"][1]["pane_id"])
        self.assertEqual(self.state()["panes"], [review["reviewers"][1]["pane_id"]])
        self.assertEqual([entry["lens"] for entry in review["reviewers"]], ["security", "code"])
        failed = review["reviewers"][0]
        self.assertIsNone(failed["pane_id"])
        result = factory.wait_round(self.card.ref, self.folder, review)
        self.assertEqual(list(result["failures"]), ["security"])
        self.assertIn("did not start", result["failures"]["security"])
        self.assertIn("code", result["counts"])
        merged = (self.folder / "round-1" / "findings.md").read_text()
        self.assertIn("# security", merged)
        self.assertIn("This lens failed", merged)

    def test_a_round_where_no_lens_launches_fails(self):
        self.write_plan(verified_plan(lenses="security, code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.fake.failed_starts = {"security", "code"}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(HerdboardError) as raised:
            factory.run_round(self.store, self.card.ref, self.record(), self.card, 1)
        self.assertEqual(raised.exception.code, "launch_failed")


class WorkerTests(FactoryTestCase):
    def setUp(self):
        super().setUp()
        self.write_plan(verified_plan(lenses="code"))
        self.start_factory(rounds=3)
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.write_evidence()
        self.ready()

    def test_no_confirmed_problems_after_a_fix_goes_to_the_approver(self):
        self.fake.findings["code"] = "## Risks and questions\n- a.txt:1: why?\n"
        self.builder_fixes()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        log = self.log()
        self.assertIn("Factory round 1 of 3: 0 confirmed problems, 1 questions.", log)
        self.assertIn("Factory: round 1 had no confirmed problems, approver next.", log)
        self.assertIn("Factory done: approve.", log)
        self.assertEqual(len(self.fake.prompts), 1)
        prompt = self.fake.prompts[0]
        self.assertIn(str(self.folder / "round-1" / "findings.md"), prompt)
        self.assertIn(str(self.folder / "round-1" / "responses.md"), prompt)
        self.assertIn("| Lens | # | Finding | Agree/Disagree | What you did |", prompt)
        self.assertNotIn("\n", prompt)
        self.assertIn(factory.VERDICT_NAME, [e["name"] for e in attachments.listing(self.config, self.card.ref)])
        # The card stays in In progress during the rounds: Review means ready for the user.
        self.assertEqual(self.columns, ["In progress"])
        self.assertEqual(self.store.show(self.card.ref).column, "Review")
        self.assertEqual(self.state()["status"], "done")
        self.assertTrue(any(c[2] == f"Factory of {self.card.ref} done" for c in self.calls("notification", "show")))

    def test_no_findings_at_all_skips_the_builder(self):
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory: round 1 had no findings, approver next.", self.log())
        self.assertEqual(self.fake.prompts, [])

    def test_the_same_head_goes_to_the_approver(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes(commit=False)
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory: no new commits since round 1, approver next.", self.log())
        self.assertEqual(len(self.fake.prompts), 1)

    def test_the_same_head_with_uncommitted_changes_says_so(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.columns = []

        def fix_without_commit(message):
            (self.worktree / "a.txt").write_text("fixed\n")
            self.ready()

        self.fake.on_prompt = fix_without_commit
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn(
            "Factory: no new commits since round 1, approver next. The approver also reviews the uncommitted changes.",
            self.log(),
        )

    def test_a_failed_lens_is_not_no_confirmed_problems(self):
        # The failed lens never reviewed the change: the next round runs, without the builder.
        self.fake.reviewer_states["code"] = ["working", None]
        # The builder gets no prompt, so a wait for its signal would end the run at once.
        self.enterContext(patch.object(factory, "BUILDER_TIMEOUT_SECONDS", 0))
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        log = self.log()
        self.assertIn("Factory: round 1 had no findings, but lenses failed (code): round 2 next.", log)
        self.assertNotIn("had no confirmed problems", log)
        self.assertIn("Factory round 2 of 3 started: code (pi).", log)
        self.assertEqual(self.fake.prompts, [])
        self.assertIn("## Lenses that failed", (self.folder / "approver" / "request.md").read_text())

    def test_failed_lenses_at_the_round_limit_go_to_the_approver_and_the_log_names_them(self):
        self.fake.reviewer_states["code"] = ["working", None]
        factory.update(self.card.ref, self.folder, rounds=1)
        # The builder gets no prompt, so a wait for its signal would end the run at once.
        self.enterContext(patch.object(factory, "BUILDER_TIMEOUT_SECONDS", 0))
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory: round 1 had no findings, but lenses failed (code), approver next.", self.log())

    def test_the_last_round_goes_to_the_approver(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        log = self.log()
        self.assertIn("Factory round 3 of 3: 1 confirmed problems (code 1), 0 questions.", log)
        self.assertIn("Factory: round 3 of 3 was the last, approver next.", log)
        self.assertEqual(len(self.fake.prompts), 3)
        self.assertEqual(len(self.lens_starts()), 3)

    def test_a_branch_without_change_still_gets_an_evidence_verdict(self):
        self.git(self.worktree, "reset", "-q", "--hard", "HEAD~1")
        self.write_evidence()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        log = self.log()
        self.assertIn("Factory: no change to review, approver next.", log)
        self.assertIn("Factory done: approve.", log)
        self.assertEqual(self.lens_starts(), [])
        self.assertTrue(any(c[2].endswith("-approver") for c in self.calls("agent", "start")))
        self.assertEqual((self.folder / "approver" / "branch.patch").read_text(), "")
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(self.store.show(self.card.ref).column, "Review")

    def test_a_branch_without_change_and_without_evidence_needs_changes(self):
        self.git(self.worktree, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn(
            "Factory done: changes needed. The evidence gate found 1 problem: see factory-verdict.md.", self.log()
        )
        self.assertEqual(self.store.show(self.card.ref).column, "Review")

    def test_an_old_run_without_change_ends_without_reviews(self):
        # A run started before the evidence policy keeps the old shortcut.
        self.drop_policy()
        self.git(self.worktree, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory: no change to review.", self.log())
        self.assertFalse(any(c[2].endswith("-approver") for c in self.calls("agent", "start")))
        self.assertEqual(self.state()["status"], "done")
        self.assertEqual(self.store.show(self.card.ref).column, "Review")

    def test_a_gone_builder_stops_the_run(self):
        self.fake.builder_status = ["working", None]
        self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card's agent is gone.", self.log())
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")

    def test_a_builder_that_herdr_does_not_see_yet_does_not_stop_the_run(self):
        # Herdr can take a moment to detect a new agent, or the agent waits at a folder trust prompt.
        self.fake.builder_status = [None, None, "idle"]
        pane = self.record()["pane_id"]
        self.fake.panes.setdefault(pane.split(":")[0], [pane])
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory done: approve.", self.log())

    def test_a_builder_never_seen_whose_pane_is_gone_stops_the_run(self):
        self.fake.builder_status = None
        pane = self.record()["pane_id"]
        self.assertFalse(any(pane in panes for panes in self.fake.panes.values()))
        self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card's agent is gone.", self.log())

    def test_stop_during_a_reviewer_launch_holds(self):
        self.write_plan(verified_plan(lenses="security, code"))

        def stop_at_launch(argv, timeout=120):
            if argv[1:3] == ["agent", "start"] and argv[3].endswith("-security"):
                factory.stop(self.store, self.card.ref)
            return FactoryHerdr.__call__(self.fake, argv, timeout)

        with patch.object(start, "command", stop_at_launch):
            self.assertEqual(self.wait(), 1)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual([c[2].rsplit("-", 1)[1] for c in self.lens_starts()], ["security"])
        self.assertEqual(self.fake.reviewers, {})
        self.assertEqual(self.fake.prompts, [])

    def test_stop_during_the_approver_launch_holds(self):
        def stop_at_launch(argv, timeout=120):
            if argv[1:3] == ["agent", "start"] and argv[3].endswith("-approver"):
                factory.stop(self.store, self.card.ref)
            return FactoryHerdr.__call__(self.fake, argv, timeout)

        with patch.object(start, "command", stop_at_launch):
            self.assertEqual(self.wait(), 1)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.fake.reviewers, {})
        self.assertNotIn(factory.VERDICT_NAME, [e["name"] for e in attachments.listing(self.config, self.card.ref)])
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")

    def test_a_card_finished_during_approval_stays_finished(self):
        def finish_during_approval(argv, timeout=120):
            if argv[1:3] == ["agent", "get"] and self.fake.reviewers.get(argv[3], {}).get("lens") == "approver":
                if not self.store.show(self.card.ref).checked:
                    self.store.done(self.card.ref)
            return FactoryHerdr.__call__(self.fake, argv, timeout)

        with patch.object(start, "command", finish_during_approval):
            self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card was finished.", self.log())
        self.assertTrue(self.store.show(self.card.ref).checked)
        self.assertNotEqual(self.store.show(self.card.ref).column, "Review")
        self.assertEqual(self.fake.reviewers, {})

    def test_a_card_finished_during_a_round_stops_before_delivery(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"

        def finish_during_review(argv, timeout=120):
            if argv[1:3] == ["agent", "get"] and self.fake.reviewers.get(argv[3], {}).get("lens") == "code":
                if not self.store.show(self.card.ref).checked:
                    self.store.done(self.card.ref)
            return FactoryHerdr.__call__(self.fake, argv, timeout)

        with patch.object(start, "command", finish_during_review):
            self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card was finished.", self.log())
        self.assertEqual(self.fake.prompts, [])
        self.assertEqual(self.fake.reviewers, {})
        self.assertEqual(self.state()["status"], "stopped")

    def test_a_finished_card_stops_the_run(self):
        self.store.done(self.card.ref)
        self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card was finished.", self.log())

    def test_stop_ends_the_run_and_closes_the_reviewers(self):
        self.fake.reviewer_states["code"] = ["working"] * 1000

        def stop_during_review(argv, timeout=120):
            if argv[1:3] == ["agent", "get"] and argv[3] in self.fake.reviewers:
                factory.stop(self.store, self.card.ref)
            return FactoryHerdr.__call__(self.fake, argv, timeout)

        with patch.object(start, "command", stop_during_review):
            self.assertEqual(self.wait(), 1)
        self.assertEqual(self.state()["status"], "stopped")
        self.assertIn("Factory stopped by the user.", self.log())
        self.assertEqual(self.fake.reviewers, {})
        self.assertEqual(self.fake.prompts, [])

    def test_the_worker_waits_for_the_ready_signal_not_the_column(self):
        factory.update(self.card.ref, self.folder, ready=False)
        self.store.move(self.card.ref, "Review")
        with patch.object(factory, "BUILDER_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 1)
        self.assertEqual(self.lens_starts(), [])
        self.assertIn("did not signal that the work is ready", self.log()[-1])

    def test_the_last_round_goes_to_the_approver_and_review_only_then(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertEqual(self.columns, ["In progress"] * 3)
        self.assertEqual(self.store.show(self.card.ref).column, "Review")

    def test_an_unexpected_error_ends_the_run_and_closes_the_reviewers(self):
        with patch.object(factory, "wait_round", side_effect=KeyError("lens")):
            self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: an unexpected error: KeyError: 'lens'", self.log())
        self.assertIn("Traceback", self.errors.getvalue())
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.fake.reviewers, {})

    def test_the_builder_has_the_builder_wait_to_turn_idle(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes(commit=False)
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        wait_call = self.calls("agent", "wait")[0]
        self.assertEqual(wait_call[wait_call.index("--timeout") + 1], str(factory.BUILDER_TIMEOUT_SECONDS * 1000))

    def test_a_failure_during_a_round_closes_the_reviewers(self):
        with patch.object(factory, "wait_round", side_effect=start.StartError("broken", "herdr_error")):
            self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: broken", self.log())
        self.assertEqual(self.state()["status"], "stopped")
        self.assertEqual(self.fake.reviewers, {})

    def test_the_approver_verdict_is_attached_and_logged(self):
        self.fake.verdict = "Verdict: changes needed\n\n## Plan steps not done\n- Step 2.\n"
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory done: changes needed.", self.log())
        verdict = attachments.folder(self.config, self.card.ref) / factory.VERDICT_NAME
        self.assertIn("Step 2.", verdict.read_text())
        call = next(c for c in self.calls("agent", "start") if c[2].endswith("-approver"))
        self.assertEqual(call[call.index("--kind") + 1], "pi")
        # The first prompt names the verdict file: a reviewer writes the file that its prompt names.
        prompt = next(arg for arg in call if "request.md" in arg)
        self.assertIn(str(self.folder / "approver" / "verdict.md"), prompt)
        self.assertNotIn("findings.md", prompt)
        request = (self.folder / "approver" / "request.md").read_text()
        # The approver reads the plan as it was when the run started.
        self.assertIn(str(self.folder / "plan.md"), request)
        self.assertIn(str(self.folder / "round-1" / "findings.md"), request)
        self.assertIn("Verdict: approve", request)
        self.assertIn(str(self.folder / "approver" / "approval.json"), prompt)


class GateTests(FactoryTestCase):
    """A run under the evidence policy approves only with current, complete evidence."""

    def setUp(self):
        super().setUp()
        self.write_plan(verified_plan(lenses="code"))
        self.start_factory(rounds=3)
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.main = self.git(self.repo, "rev-parse", "HEAD")
        self.ready()

    def published(self):
        return (attachments.folder(self.config, self.card.ref) / factory.VERDICT_NAME).read_text()

    def finish(self):
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        # Every assessment ends in Review for the user, and nothing merges.
        self.assertEqual(self.store.show(self.card.ref).column, "Review")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.main)
        self.assertEqual(self.state()["status"], "done")
        return self.log()[-1], self.published()

    def test_valid_current_evidence_keeps_the_approval(self):
        self.write_evidence()
        message, text = self.finish()
        self.assertEqual(message, "Factory done: approve.")
        self.assertTrue(text.startswith("Verdict: approve\n"))
        self.assertIn("Each required check has current evidence", text)
        self.assertIn("The approver wrote `Verdict: approve`.", text)
        approver = self.folder / "approver"
        self.assertEqual((approver / "evidence" / "unit.txt").read_text(), "output of unit\n")
        self.assertIn("The report is complete and current.", (approver / "validation.md").read_text())
        self.assertEqual(json.loads((approver / "contract.json").read_text()), CONTRACT)

    def assert_changes_needed(self, *words):
        message, text = self.finish()
        self.assertTrue(message.startswith("Factory done: changes needed. The evidence gate found"), message)
        self.assertTrue(text.startswith("Verdict: changes needed\n"), text)
        self.assertIn("herdboard changed the approver's verdict from approve to changes needed:", text)
        for word in words:
            self.assertIn(word, text)
        return text

    def test_missing_evidence(self):
        self.assert_changes_needed("The builder wrote no verification.json")

    def test_malformed_evidence(self):
        (self.folder / "evidence" / "verification.json").write_text("{ nope")
        self.assert_changes_needed("verification.json is not valid JSON")

    def test_stale_evidence(self):
        self.write_evidence()
        self.commit(self.worktree, "b.txt", "b\n", "feat: b")
        self.assert_changes_needed("not the current commit")

    def test_a_dirty_tree(self):
        self.write_evidence()
        (self.worktree / "a.txt").write_text("dirty\n")
        (self.worktree / "new.txt").write_text("new\n")
        self.assert_changes_needed("uncommitted or untracked changes: a.txt, new.txt")

    def test_incomplete_evidence(self):
        self.write_evidence([result("unit")])
        self.assert_changes_needed("The report has no result for check live.")

    def test_a_failed_check_with_a_false_approval(self):
        self.write_evidence([result("unit", "failed"), result("live", evidence=["live.txt"])])
        self.assert_changes_needed("Required check unit failed.")

    def test_a_changed_plan(self):
        self.write_evidence()
        self.write_plan(verified_plan(lenses="code, ux"))
        self.assert_changes_needed(
            "The plan changed after the factory started", f"Restart the factory of {self.card.ref}"
        )

    def test_evidence_changed_during_the_approval(self):
        self.write_evidence()
        self.fake.on_approve = lambda: (self.folder / "evidence" / "unit.txt").write_text("edited\n")
        self.assert_changes_needed("The evidence changed after herdboard captured it: unit.txt.")

    def test_the_approvers_copy_changed_during_the_approval(self):
        self.write_evidence()
        copy = self.folder / "approver" / "evidence"
        self.fake.on_approve = lambda: (copy / "unit.txt").write_text("edited\n")
        self.assert_changes_needed("The approver's copy of the evidence changed after herdboard captured it: unit.txt.")

    def test_a_commit_during_the_approval(self):
        self.write_evidence()
        self.fake.on_approve = lambda: self.commit(self.worktree, "late.txt", "late\n", "feat: late")
        self.assert_changes_needed("A commit landed during the approval")

    def test_a_missing_structured_approval(self):
        self.write_evidence()
        self.fake.approval = None
        self.assert_changes_needed("The approver wrote no approval.json.")

    def test_an_unsatisfied_criterion(self):
        self.write_evidence()
        criteria = approval()["criteria"]
        criteria[0]["outcome"] = "unsatisfied"
        self.fake.approval = approval(criteria=criteria)
        self.assert_changes_needed("The approver found criterion AC1 unsatisfied.")

    def test_a_blocked_check_with_a_named_exception(self):
        self.write_evidence([result("unit"), result("live", "blocked", evidence=[], observed="No display here.")])
        exception = {"check": "live", "reason": "No display.", "risk": "The board is not checked live."}
        self.fake.approval = approval(exceptions=[exception])
        message, text = self.finish()
        self.assertEqual(message, "Factory done: approve.")
        self.assertIn("- live: No display. Remaining risk: The board is not checked live.", text)

    def test_a_blocked_check_without_an_exception(self):
        self.write_evidence([result("unit"), result("live", "blocked", evidence=[])])
        self.assert_changes_needed("Required check live is blocked and has no exception.")

    def test_the_approvers_changes_needed_stays(self):
        self.write_evidence()
        self.fake.verdict = "Verdict: changes needed\n\n## Requirements not met\n- AC2.\n"
        self.fake.approval = approval("changes needed")
        message, text = self.finish()
        self.assertEqual(message, "Factory done: changes needed.")
        self.assertTrue(text.startswith("Verdict: changes needed\n"))
        self.assertIn("- AC2.", text)

    def test_the_builder_prompt_asks_for_fresh_evidence(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes()
        self.wait()
        prompt = self.fake.prompts[0]
        self.assertIn("run every check of the plan's verification contract again on your final commit", prompt)
        self.assertIn(str(self.folder / "evidence" / "verification.json"), prompt)
        self.assertIn("cite the code path, test or evidence file", prompt)
        self.assertNotIn("\n", prompt)

    def test_the_builder_brief_explains_the_evidence(self):
        call = next(c for c in self.calls("agent", "start") if c[2] == f"hb-{self.card.ref.lower()}")
        brief = Path(re.search(r"(/\S+\.md)", " ".join(call)).group(1)).read_text()
        self.assertIn("## Verification evidence", brief)
        self.assertIn(f"`{self.folder / 'evidence'}/verification.json`", brief)
        self.assertIn(f'"run": "{self.folder.name}"', brief)
        self.assertIn(verification.Contract.from_dict(CONTRACT).digest, brief)
        self.assertIn("It does not prove that you ran a command", brief)


class PresetGateTests(FactoryTestCase):
    def test_a_run_without_the_code_lens_still_needs_evidence(self):
        run = dataclasses.replace(factory.default_run(self.config), name="secure", lenses_on=("security",))
        self.start_factory(factory_run=run)
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.ready()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory round 1 of 3 started: security (codex).", self.log())
        self.assertTrue(self.log()[-1].startswith("Factory done: changes needed. The evidence gate found"))


class OldRunTests(FactoryTestCase):
    """Runs started before the evidence policy keep the old approval."""

    def setUp(self):
        super().setUp()
        self.start_factory(rounds=1)
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")

    def test_a_record_with_settings_but_no_policy(self):
        self.drop_policy()
        self.ready()
        self.fake.approval = None
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory done: approve.", self.log())
        request = (self.folder / "approver" / "request.md").read_text()
        self.assertIn(str(attachments.plan_path(self.config, self.card.ref)), request)
        self.assertNotIn("approval.json", request)
        text = (attachments.folder(self.config, self.card.ref) / factory.VERDICT_NAME).read_text()
        self.assertNotIn("Evidence gate", text)

    def test_a_record_without_settings(self):
        self.drop_policy(run=True)
        self.ready()
        self.fake.approval = None
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory done: approve.", self.log())

    def test_the_old_builder_prompt_asks_for_no_evidence(self):
        self.assertNotIn("verification", factory.builder_message("t1", Path("/r"), "herdboard"))


class RefusalTests(FactoryTestCase):
    def test_agent_review_and_merge_refuse_while_a_factory_runs(self):
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        with self.assertRaises(factory.FactoryError) as raised:
            agent_review.AgentReviewer(self.store, self.repo).request(self.card.ref)
        self.assertEqual(raised.exception.code, "factory_running")
        with self.assertRaises(factory.FactoryError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "factory_running")

    def test_a_dead_worker_does_not_block(self):
        self.start_factory()
        factory.update(self.card.ref, self.folder, pid=2**22 + 12345)
        self.assertFalse(factory.factory_running(self.card.ref))
        factory.update(self.card.ref, self.folder, panes=["w9:p5"])
        # The builder's move to Review is then a plain move, and the stale run is stopped.
        self.assertIsNone(factory.signal_ready(self.card.ref, self.store))
        self.assertEqual(self.state()["status"], "stopped")
        self.assertFalse(self.state()["ready"])
        self.assertEqual(self.state()["panes"], [])
        self.assertIn(["pane", "close", "w9:p5"], self.calls("pane", "close"))
        self.assertIn("Factory stopped: its worker is gone.", self.log())

    def test_stop_without_a_run(self):
        with self.assertRaises(factory.FactoryError) as raised:
            factory.stop(self.store, self.card.ref)
        self.assertEqual(raised.exception.code, "not_running")


class FactoryCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.init_repo_board()
        self.ref = self.run_json("add", "Fix login", directory=self.repo)["card"]["ref"]

    def write_state(self, status="building"):
        folder = start.state_dir() / "factory" / "x"
        folder.mkdir(parents=True)
        state = {
            "rounds": 3,
            "round": 1,
            "folder": str(folder),
            "lenses": ["code"],
            "status": status,
            "heads": [],
            "panes": [],
            "pid": os.getpid(),
        }
        start._write(start.state_dir() / "starts.json", {self.ref: {"ref": self.ref, "factory": state}})
        return folder

    def test_preset_usage_errors(self):
        for flags in (("--agent", "pi"), ("--model", "opus"), ("--thinking", "high"), ("--rounds", "2")):
            with self.subTest(flags=flags):
                result = self.run_json(
                    "start", self.ref, "--factory", "--preset", "cheap", *flags, directory=self.repo, expect=1
                )
                self.assertEqual(result["error"]["code"], "usage")
        result = self.run_json("start", self.ref, "--preset", "cheap", directory=self.repo, expect=1)
        self.assertEqual(result["error"]["code"], "usage")

    def test_preset_passes_complete_run_to_starter(self):
        import tomlkit

        value = factory.default_run(Config()).to_dict()
        value.pop("name")
        value["builder"] = {"agent": "pi", "model": "test/model", "thinking": "high"}
        with self.config.open("a") as stream:
            stream.write(tomlkit.dumps({"factory": {"presets": {"cheap": value}}}))
        with patch.object(start.Starter, "start", return_value={"ref": self.ref}) as started:
            self.run_json("start", self.ref, "--factory", "--preset", "cheap", directory=self.repo)
        run = started.call_args.kwargs["factory_run"]
        self.assertEqual(
            (run.name, run.builder.agent, run.builder.model, run.builder.thinking),
            ("cheap", "pi", "test/model", "high"),
        )

    def test_start_passes_factory_and_rounds(self):
        with patch.object(start.Starter, "start", return_value={"ref": self.ref}) as started:
            self.run_json("start", self.ref, "--factory", "--rounds", "2", directory=self.repo)
        self.assertEqual((started.call_args.kwargs["factory"], started.call_args.kwargs["rounds"]), (True, 2))

    def test_show_json_has_the_factory_state(self):
        self.assertNotIn("factory", self.run_json("show", self.ref, directory=self.repo))
        folder = self.write_state()
        shown = self.run_json("show", self.ref, directory=self.repo)["factory"]
        self.assertEqual(
            shown,
            {
                "round": 1,
                "rounds": 3,
                "status": "building",
                "lenses": ["code"],
                "folder": str(folder),
                "preset": "default",
            },
        )

    def test_move_to_review_is_a_ready_signal_while_a_factory_runs(self):
        self.run_json("move", self.ref, "In progress", directory=self.repo)
        self.write_state()
        result = self.run_json("move", self.ref, "Review", directory=self.repo)
        self.assertEqual(result["factory"]["status"], "building")
        self.assertIn("stays in In progress", result["hint"])
        self.assertEqual(self.run_json("show", self.ref, directory=self.repo)["card"]["column"], "In progress")
        self.assertTrue(json.loads((start.state_dir() / "starts.json").read_text())[self.ref]["factory"]["ready"])
        out, _ = self.run_cli("move", self.ref, "Review", directory=self.repo)
        self.assertIn("stays in In progress", out)
        self.run_json("factory", self.ref, "--stop", directory=self.repo)
        self.assertEqual(self.run_json("move", self.ref, "Review", directory=self.repo)["card"]["column"], "Review")

    def test_factory_shows_and_stops_the_run(self):
        self.run_json("factory", self.ref, directory=self.repo, expect=1)
        self.write_state()
        out, _ = self.run_cli("factory", self.ref, directory=self.repo)
        self.assertEqual(out, f"{self.ref}: factory building, round 1 of 3, preset default\n")
        self.assertEqual(self.run_json("factory", self.ref, "--stop", directory=self.repo)["status"], "stopped")
        self.assertEqual(
            json.loads((start.state_dir() / "starts.json").read_text())[self.ref]["factory"]["status"], "stopped"
        )
        self.run_json("factory", self.ref, "--stop", directory=self.repo, expect=1)


class ReadySignalTests(FactoryTestCase):
    def test_a_signal_needs_a_building_or_fixing_run(self):
        self.assertIsNone(factory.signal_ready(self.card.ref))
        self.start_factory()
        self.assertEqual(factory.signal_ready(self.card.ref)["status"], "building")
        self.assertTrue(self.state()["ready"])
        factory.update(self.card.ref, self.folder, status="done", ready=False)
        self.assertIsNone(factory.signal_ready(self.card.ref))
        self.assertFalse(self.state()["ready"])


class LinearFactoryTests(FactoryTestCase):
    """A Linear issue runs as a todo does, and the worker writes to Linear only at the end."""

    def setUp(self):
        super().setUp()
        self.write_plan(verified_plan(lenses="code"))
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.write_evidence()
        records = json.loads((start.state_dir() / "starts.json").read_text())
        record = records.pop(self.card.ref)
        records["issue-uuid"] = {**record, "ref": "ENG-7"}
        start._write(start.state_dir() / "starts.json", records)
        source = attachments.plan_path(self.config, self.card.ref)
        target = attachments.plan_path(self.config, "ENG-7")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
        self.writer = MagicMock()
        self.enterContext(
            patch.object(
                start, "fetch_linear_card", return_value=dataclasses.replace(self.card, ref="ENG-7", source="linear")
            )
        )
        self.enterContext(patch.object(self.store, "show", side_effect=AssertionError("no board read")))

    def test_the_run_moves_the_issue_to_review_once_at_the_end(self):
        self.assertIsNotNone(factory.signal_ready("ENG-7"))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = factory.wait(self.store, "ENG-7", self.folder, writer=self.writer)
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(self.writer.move.call_args_list, [call("ENG-7", "Review", ask=refuse)])
        self.assertIn("Factory done: approve.", [e["message"] for e in activity.read(self.config, "ENG-7")])

    def test_a_failed_move_is_logged_and_the_verdict_stays(self):
        self.writer.move.side_effect = HerdboardError("Team ENG has no workflow state for column 'Review'")
        factory.signal_ready("ENG-7")
        with contextlib.redirect_stderr(io.StringIO()):
            factory.wait(self.store, "ENG-7", self.folder, writer=self.writer)
        log = [e["message"] for e in activity.read(self.config, "ENG-7")]
        self.assertTrue(any(m.startswith("Factory: the card did not move to Review: Team ENG") for m in log), log)
        self.assertIn(factory.VERDICT_NAME, [e["name"] for e in attachments.listing(self.config, "ENG-7")])
