"""Factory runs: lens reviewers check each move to Review, the builder fixes, an approver gives a verdict."""

import contextlib
import io
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from herdboard import agent_review, attachments, factory, start
from herdboard.config import Config
from support.herdboard_cli import CliTestCase
from support.herdboard_merge import GitHerdr, MergeTestCase

REQUEST = re.compile(r"Read (\S+)/request\.md")


class FactoryHerdr(GitHerdr):
    """GitHerdr with lens reviewers and an approver that write their files when they turn idle,
    a builder whose status the test sets, and a hook that plays the builder after a prompt."""

    def __init__(self, root):
        super().__init__(root)
        self.reviewers = {}
        self.findings = {}
        self.verdict = "Verdict: approve\n\n## Plan steps not done\nNone.\n"
        self.builder_status = "idle"
        self.reviewer_states = {}
        self.on_prompt = None
        self.prompts = []

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
        if operation in ("notification show", "agent wait"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and re.search(r"-(security|ux|ui|code|approver)$", args[2]):
            self.calls.append(args)
            pane = args[args.index("--pane") + 1]
            folder = next(Path(m.group(1)) for arg in args if (m := REQUEST.search(arg)))
            lens = args[2].rsplit("-", 1)[1]
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
                else:
                    (reviewer["folder"] / "findings.md").write_text(
                        self.findings.get(reviewer["lens"], "No findings.\n")
                    )
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        if operation == "agent get" and self.agent and args[2].endswith(":p1"):
            self.calls.append(args)
            if self.builder_status is None:
                return self.reply(argv, error="agent_not_found")
            return self.reply(argv, {"agent": {"agent_status": self.builder_status, "pane_id": args[2]}})
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
            patch.object(agent_review, "start_worker", lambda arguments, log: self.workers.append((arguments, log)))
        )
        self.enterContext(patch.object(factory, "POLL_SECONDS", 0))
        self.enterContext(patch.object(agent_review, "POLL_SECONDS", 0))

    def start_factory(self, **options):
        result = self.starter.start(self.card.ref, agent=options.pop("agent", "claude"), factory=True, **options)
        self.worktree = Path(result["worktree_path"])
        self.folder = Path(self.record()["factory"]["folder"])
        return result

    def state(self):
        return self.record()["factory"]

    def to_review(self):
        self.store.move(self.card.ref, "Review")

    def write_plan(self, text):
        path = attachments.plan_path(self.config, self.card.ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def builder_fixes(self, commit=True):
        """The builder answers a round: it commits a fix and moves the card to Review."""

        def answer(message):
            if commit:
                self.commit(self.worktree, f"fix{len(self.fake.prompts)}.txt", "x\n", "fix: a finding")
            self.to_review()

        self.fake.on_prompt = answer

    def wait(self):
        self.errors = io.StringIO()
        with contextlib.redirect_stderr(self.errors):
            return factory.wait(self.store, self.card.ref, self.folder)

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
        merged = "# security (codex)\n\n" + text + "\n# code (pi)\n\n## Confirmed problems\n- [low] b.py:1: x.\n"
        self.assertEqual(factory.count_confirmed(merged), 3)


class StartFactoryTests(FactoryTestCase):
    def test_start_writes_the_factory_state_and_starts_the_worker(self):
        result = self.start_factory(rounds=2)
        state = self.state()
        self.assertEqual((state["rounds"], state["round"], state["status"]), (2, 0, "building"))
        self.assertTrue(self.folder.is_dir())
        self.assertEqual(result["factory"]["status"], "building")
        arguments, log = self.workers[-1]
        self.assertEqual(arguments, ["factory-wait", self.card.ref, "--folder", str(self.folder)])
        self.assertEqual(log, self.folder / "worker.log")
        self.assertIn("Factory started: up to 2 review rounds.", self.log())

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
        self.starter.start(self.card.ref, agent="claude")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, agent="claude", factory=True)
        self.assertEqual(raised.exception.code, "agent_running")


class RoundTests(FactoryTestCase):
    def test_one_reviewer_per_plan_lens_with_its_kind_name_and_split(self):
        self.write_plan("## Decisions\n- Review lenses: security, code\n")
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
        self.write_plan("Review lenses: code\n")
        self.start_factory()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        factory.run_round(self.store, self.card.ref, self.record(), self.card, 2)
        request = (self.folder / "round-2" / "code" / "request.md").read_text()
        self.assertIn("## Earlier rounds", request)
        self.assertIn(str(self.folder / "round-1" / "responses.md"), request)
        self.assertIn("unless you have new evidence", request)

    def test_wait_round_merges_findings_and_a_failed_lens_does_not_stop_it(self):
        self.write_plan("Review lenses: security, code\n")
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


class WorkerTests(FactoryTestCase):
    def setUp(self):
        super().setUp()
        self.write_plan("Review lenses: code\n")
        self.start_factory(rounds=3)
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.to_review()

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

    def test_the_last_round_goes_to_the_approver(self):
        self.fake.findings["code"] = "## Confirmed problems\n- [high] a.txt:1: bug.\n"
        self.builder_fixes()
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        log = self.log()
        self.assertIn("Factory round 3 of 3: 1 confirmed problems (code 1), 0 questions.", log)
        self.assertIn("Factory: round 3 of 3 was the last, approver next.", log)
        self.assertEqual(len(self.fake.prompts), 3)
        self.assertEqual(len(self.lens_starts()), 3)

    def test_a_branch_without_change_ends_without_reviews(self):
        self.git(self.worktree, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.wait(), 0, self.errors.getvalue())
        self.assertIn("Factory: no change to review.", self.log())
        self.assertEqual(self.lens_starts(), [])
        self.assertFalse(any(c[2].endswith("-approver") for c in self.calls("agent", "start")))
        self.assertEqual(self.state()["status"], "done")

    def test_a_gone_builder_stops_the_run(self):
        self.fake.builder_status = None
        self.assertEqual(self.wait(), 1)
        self.assertIn("Factory stopped: the card's agent is gone.", self.log())
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
        request = (self.folder / "approver" / "request.md").read_text()
        self.assertIn(str(attachments.plan_path(self.config, self.card.ref)), request)
        self.assertIn(str(self.folder / "round-1" / "findings.md"), request)
        self.assertIn("Verdict: approve", request)


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

    def test_start_passes_factory_and_rounds(self):
        with patch.object(start.Starter, "start", return_value={"ref": self.ref}) as started:
            self.run_json("start", self.ref, "--factory", "--rounds", "2", directory=self.repo)
        self.assertEqual((started.call_args.kwargs["factory"], started.call_args.kwargs["rounds"]), (True, 2))

    def test_show_json_has_the_factory_state(self):
        self.assertNotIn("factory", self.run_json("show", self.ref, directory=self.repo))
        folder = self.write_state()
        shown = self.run_json("show", self.ref, directory=self.repo)["factory"]
        self.assertEqual(
            shown, {"round": 1, "rounds": 3, "status": "building", "lenses": ["code"], "folder": str(folder)}
        )

    def test_factory_shows_and_stops_the_run(self):
        self.run_json("factory", self.ref, directory=self.repo, expect=1)
        self.write_state()
        out, _ = self.run_cli("factory", self.ref, directory=self.repo)
        self.assertEqual(out, f"{self.ref}: factory building, round 1 of 3\n")
        self.assertEqual(self.run_json("factory", self.ref, "--stop", directory=self.repo)["status"], "stopped")
        self.assertEqual(
            json.loads((start.state_dir() / "starts.json").read_text())[self.ref]["factory"]["status"], "stopped"
        )
        self.run_json("factory", self.ref, "--stop", directory=self.repo, expect=1)
