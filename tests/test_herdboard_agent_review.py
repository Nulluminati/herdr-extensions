"""Agent review: a second agent reviews a started card's work, then the card's agent answers."""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import agent_review, review, start  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_merge import GitHerdr, MergeTestCase  # noqa: E402


class ReviewerHerdr(GitHerdr):
    """GitHerdr with pane splits, a reviewer agent whose status follows a script, and notifications."""

    def __init__(self, root):
        super().__init__(root)
        self.reviewer_states = ["working", "idle"]
        self.findings = "## Confirmed problems\n- [high] a.py:1: bug. Fix it.\n- [low] a.py:2: style.\n"
        self.review_folder = None
        self.reviewer_pane = None

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "pane split":
            self.calls.append(args)
            workspace = args[2].split(":")[0]
            pane = f"{workspace}:p{self.next_pane}"
            self.next_pane += 1
            self.panes.setdefault(workspace, [workspace + ":p1"]).append(pane)
            self.reviewer_pane = pane
            return self.reply(argv, {"pane": {"pane_id": pane}})
        if operation == "pane close":
            self.calls.append(args)
            for panes in self.panes.values():
                if args[2] in panes:
                    panes.remove(args[2])
            return self.reply(argv, {"type": "ok"})
        if operation in ("notification show", "agent wait"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and args[2].endswith("-review"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent get" and (args[2] == self.reviewer_pane or args[2].endswith("-review")):
            self.calls.append(args)
            state = self.reviewer_states.pop(0) if self.reviewer_states else "idle"
            if state is None:
                return self.reply(argv, error="agent_not_found")
            if state == "idle" and self.findings is not None and self.review_folder:
                (self.review_folder / "findings.md").write_text(self.findings)
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        return super().__call__(argv, timeout)


class AgentReviewTestCase(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.fake.__class__ = ReviewerHerdr
        ReviewerHerdr.__init__(self.fake, self.root)
        self.workers = []
        self.enterContext(patch.object(agent_review, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))
        self.enterContext(patch.object(agent_review, "POLL_SECONDS", 0))
        self.reviewer = agent_review.AgentReviewer(self.store, self.repo)

    def request(self, **options):
        result = self.reviewer.request(self.card.ref, **options)
        self.fake.review_folder = self.folder = Path(result["folder"])
        return result

    def reviewer_start(self):
        return next(c for c in self.calls("agent", "start") if c[2].endswith("-review"))

    def wait(self):
        self.errors = io.StringIO()
        with contextlib.redirect_stderr(self.errors):
            return agent_review.wait(self.store, self.card.ref, self.folder)


class RequestTests(AgentReviewTestCase):
    def test_a_card_that_was_never_started_cannot_be_reviewed(self):
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "not_started")

    def test_a_branch_without_changes_has_nothing_to_review(self):
        self.started()
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "nothing_to_review")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_the_request_holds_the_branch_and_uncommitted_changes(self):
        self.started()
        self.commit(self.worktree, "a.txt", "committed\n", "feat: a")
        (self.worktree / "b.txt").write_text("untracked\n")
        (self.worktree / "a.txt").write_text("changed\n")
        folder = Path(self.request()["folder"])
        self.assertIn("+committed", (folder / "branch.patch").read_text())
        self.assertIn("+changed", (folder / "uncommitted.patch").read_text())
        text = (folder / "request.md").read_text()
        for part in ("Fix login timeout", self.branch, self.main_branch(), str(self.worktree), "b.txt",
                     str(folder / "findings.md")):
            self.assertIn(part, text)

    def test_the_other_kind_reviews_in_a_split_next_to_the_card_agent(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.request()
        self.assertEqual(self.calls("pane", "split"),
                         [["pane", "split", "w9:p1", "--direction", "right", "--cwd", str(self.worktree),
                           "--no-focus"]])
        start_call = self.reviewer_start()
        self.assertEqual(start_call[2], f"hb-{self.card.ref}-review")
        self.assertEqual(start_call[start_call.index("--kind") + 1], "claude")
        self.assertEqual(start_call[start_call.index("--pane") + 1], result["pane_id"])
        folder = Path(result["folder"])
        args = start_call[start_call.index("--") + 1:]
        self.assertIn(str(folder / "request.md"), args[0])
        self.assertEqual(args[1:], ["--permission-mode", "dontAsk", "--allowedTools", "Read", "Grep", "Glob",
                                    f"Edit(/{folder}/**)"])
        self.assertEqual(result["reviewer"], "claude")

    def test_a_chosen_pi_reviewer_gets_its_model_and_thinking(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="pi", model="zai/glm-5.3", thinking="high")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[:-2], ["--tools", "read,grep,find,ls,write", "--model", "zai/glm-5.3",
                                     "--thinking", "high"])
        self.assertEqual(args[-2], "--")

    def test_a_claude_model_goes_before_the_allowed_tools(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="claude", model="sonnet")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[1:5], ["--permission-mode", "dontAsk", "--model", "sonnet"])

    def test_thinking_works_only_with_pi(self):
        self.started()
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref, reviewer="claude", thinking="high")
        self.assertEqual(raised.exception.code, "usage")

    def test_the_record_logs_and_worker_follow(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.request(reviewer="pi", model="zai/glm-5.3", then_hunk=True)
        stored = self.records()[self.card.ref]["agent_review"]
        self.assertEqual(stored, {"folder": result["folder"], "pane_id": result["pane_id"], "kind": "pi",
                                  "model": "zai/glm-5.3", "then_hunk": True})
        self.assertEqual(self.log(), ["Agent review started: pi (zai/glm-5.3)."])
        self.assertEqual(len(self.workers), 1)
        arguments, log = self.workers[0]
        self.assertEqual(arguments[-4:], ["review-wait", self.card.ref, "--folder", result["folder"]])
        self.assertEqual(log, Path(result["folder"]) / "worker.log")

    def test_a_running_agent_review_blocks_a_second_one(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request()
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "review_running")

    def test_a_review_whose_reviewer_is_gone_does_not_block(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        first = self.request()
        self.fake.reviewer_states = [None]
        second = self.request()
        self.assertNotEqual(second["pane_id"], first["pane_id"])

    def test_an_old_record_without_a_base_uses_the_upstream(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        records = self.records()
        del records[self.card.ref]["base"]
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        folder = Path(self.request()["folder"])
        self.assertIn("+a", (folder / "branch.patch").read_text())

    def test_a_worker_that_cannot_start_closes_the_reviewer(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")

        def broken(arguments, log):
            raise OSError("no python")

        with patch.object(agent_review, "start_worker", broken):
            with self.assertRaises(review.ReviewError) as raised:
                self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "worker_failed")
        self.assertEqual(len(self.calls("pane", "close")), 1)
        self.assertNotIn("agent_review", self.records()[self.card.ref])


class WaitTests(AgentReviewTestCase):
    def setUp(self):
        super().setUp()
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")

    def test_findings_go_to_the_log_and_the_card_agent_then_the_pane_closes(self):
        result = self.request()
        self.assertEqual(self.wait(), 0)
        findings = Path(result["folder"]) / "findings.md"
        self.assertEqual(self.calls("pane", "close"), [["pane", "close", result["pane_id"]]])
        self.assertEqual(self.log()[-1], f"Agent review by claude done: 2 findings. {findings}")
        prompt = self.calls("agent", "prompt")[-1]
        self.assertEqual(prompt[2], "w9:p1")
        for part in (str(findings), "agree or disagree", "herdboard log", "Do not change any code",
                     "Wait for the user"):
            self.assertIn(part, prompt[3])
        notification = self.calls("notification", "show")[-1]
        self.assertEqual(notification[2], f"Agent review of {self.card.ref} done")
        self.assertNotIn("agent_review", self.records()[self.card.ref])
        self.assertEqual(self.calls("plugin", "action", "invoke"), [])

    def test_the_prompt_waits_for_a_busy_card_agent(self):
        self.request()
        self.wait()
        waits = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["agent", "wait"]]
        prompts = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["agent", "prompt"]]
        self.assertEqual(self.fake.calls[waits[0]][2:5], ["w9:p1", "--until", "idle"])
        self.assertLess(waits[0], prompts[0])

    def test_both_opens_hunk_after_the_findings(self):
        self.request(then_hunk=True)
        self.wait()
        self.assertEqual(len(self.calls("plugin", "action", "invoke")), 1)
        self.assertEqual(self.records()[self.card.ref]["review_pane"], "w9:p3:hunk")

    def test_a_reviewer_that_stops_without_findings_fails_without_a_prompt(self):
        self.fake.findings = None
        self.fake.reviewer_states = ["working", None]
        self.request(then_hunk=True)
        self.assertEqual(self.wait(), 1)
        self.assertIn("stopped before it wrote", self.errors.getvalue())
        self.assertTrue(any(entry.startswith("Agent review failed: The claude reviewer stopped before it wrote")
                            for entry in self.log()))
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(self.calls("notification", "show")[-1][2], f"Agent review of {self.card.ref} failed")
        self.assertEqual(len(self.calls("plugin", "action", "invoke")), 1)
        self.assertNotIn("agent_review", self.records()[self.card.ref])

    def test_a_reviewer_not_detected_yet_is_still_awaited(self):
        self.fake.reviewer_states = [None, "working", "idle"]
        self.request()
        self.assertEqual(self.wait(), 0)

    def test_a_review_that_takes_too_long_fails(self):
        self.fake.reviewer_states = ["working"] * 3
        self.fake.findings = None
        self.request()
        with patch.object(agent_review, "REVIEW_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 1)
        self.assertIn("did not finish", self.log()[-1])
        self.assertIn("did not finish", self.errors.getvalue())

    def test_a_finished_card_only_loses_the_reviewer_pane(self):
        result = self.request()
        records = self.records()
        records.pop(self.card.ref)
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls("pane", "close"), [["pane", "close", result["pane_id"]]])
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(self.errors.getvalue(), "")


class AgentReviewCliTests(CliTestCase):
    def test_human_and_agent_are_exclusive(self):
        data = self.run_json("review", "t123", "--human", "--agent", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_review_options_need_an_agent_review(self):
        data = self.run_json("review", "t123", "--model", "opus", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_agent_and_both_pass_the_reviewer_choice(self):
        calls = []

        def fake_request(reviewer_self, ref, reviewer=None, model=None, thinking=None, then_hunk=False):
            calls.append((ref, reviewer, model, thinking, then_hunk, reviewer_self.config_file))
            return {"ref": ref, "reviewer": reviewer or "claude", "model": model, "pane_id": "w9:p2", "folder": "/f",
                    "then_hunk": then_hunk}

        with patch.object(agent_review.AgentReviewer, "request", fake_request):
            data = self.run_json("review", "t123", "--agent", "--reviewer", "pi", "--model", "zai/glm-5.3",
                                 "--thinking", "high", directory=self.plain)
            self.assertEqual(data["pane_id"], "w9:p2")
            out, _ = self.run_cli("review", "t123", "--both", "--model", "opus", directory=self.plain)
        self.assertEqual([c[:5] for c in calls], [("t123", "pi", "zai/glm-5.3", "high", False),
                                                  ("t123", None, "opus", None, True)])
        self.assertIsNotNone(calls[0][5])
        self.assertIn("t123: agent review by claude (opus) in pane w9:p2", out)
        self.assertIn("Hunk opens when the findings are ready", out)
