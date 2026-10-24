"""Agent review: a second agent reviews a started card's work, then the card's agent answers."""
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import agent_review, attachments, review, start  # noqa: E402
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
        # The file that the agent in the split writes when it turns idle.
        self.output = "findings.md"

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
                (self.review_folder / self.output).write_text(self.findings)
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


class WorkerTests(CliTestCase):
    def test_detached_worker_uses_the_callers_package_from_an_unrelated_directory(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\ntag_style = "outline"\n')
        folder = self.plain / "review"
        folder.mkdir()
        stale = self.plain / "stale"
        package = stale / "herdboard"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text('raise SystemExit("tag_style must be round or block")\n')
        captured = {}

        def launch(argv, **options):
            captured.update(argv=argv, options=options)

        with patch.dict(os.environ, {"PYTHONPATH": str(stale)}), \
                patch.object(agent_review.subprocess, "Popen", side_effect=launch):
            agent_review.start_worker(["--config", str(self.config), "review-wait", "t123",
                                       "--folder", str(folder)], folder / "worker.log")
        environment = captured["options"]["env"]
        self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[1:], [str(stale)])
        result = subprocess.run([sys.executable, "-c", "import herdboard; print(herdboard.__file__)"],
                                cwd=self.plain, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(),
                         Path(agent_review.__file__).with_name("__init__.py").resolve())
        result = subprocess.run(captured["argv"], cwd=self.plain, env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read the agent review", result.stderr)
        self.assertNotIn("tag_style must be", result.stderr)


class RequestTests(AgentReviewTestCase):
    def test_codex_review_profile_quotes_the_folder(self):
        import tomllib
        folder = Path('/reviews/a"b\\c')
        args = agent_review.reviewer_arguments("codex", "task", folder, "gpt-6.1", "ultra")
        self.assertEqual(args[:4], ["-a", "never", "-c", 'default_permissions="hb-review"'])
        profile = tomllib.loads(args[5])["permissions"]["hb-review"]["filesystem"]
        self.assertEqual(profile, {":root": "read", str(folder): "write"})
        self.assertEqual(args[6:], ["--model", "gpt-6.1", "-c", 'model_reasoning_effort="ultra"', "task"])
        self.assertEqual(agent_review.REVIEWERS, {"claude": "pi", "pi": "codex", "codex": "claude"})

    def test_codex_review_keeps_its_no_network_profile_without_browser_instructions(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="codex")
        request = (self.folder / "request.md").read_text()
        self.assertNotIn("terminal-browser", request)
        args = self.reviewer_start()
        self.assertIn('default_permissions="hb-review"', args)
        self.assertFalse(any("network={enabled=true}" in arg for arg in args))

    def test_codex_reviewer_rejects_pi_only_thinking(self):
        self.started()
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref, reviewer="codex", thinking="off")
        self.assertEqual(raised.exception.code, "usage")

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

    def test_the_request_names_the_plan_and_the_other_attachments(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        for name in ("plan.md", "spec.md"):
            source = self.root / name
            source.write_text("# Doc\n")
            attachments.add(self.store, self.card.ref, source)
        text = (Path(self.request()["folder"]) / "request.md").read_text()
        folder = attachments.folder(self.store.config, self.card.ref)
        self.assertIn(f"Check that the change follows the plan that the user agreed on: `{folder / 'plan.md'}`", text)
        # A blank line ends the list, so the instructions do not join its last item.
        self.assertIn(f"- `{folder / 'spec.md'}`\n\nReview this change", text)

    def test_an_unreadable_attachment_folder_leaves_no_review_folder(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        with patch.object(attachments, "listing", side_effect=attachments.IoError("cannot read")), \
                self.assertRaises(attachments.IoError):
            self.reviewer.request(self.card.ref)
        reviews = start.state_dir() / "reviews"
        self.assertEqual(list(reviews.iterdir()) if reviews.exists() else [], [])
        self.assertEqual(self.calls("pane", "split"), [])

    def test_a_request_without_attachments_names_none(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        text = (Path(self.request()["folder"]) / "request.md").read_text()
        self.assertNotIn("plan that the user agreed on", text)
        self.assertNotIn("attachments", text)

    def test_codex_reviews_pi_in_a_split_next_to_the_card_agent(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.request()
        self.assertEqual(self.calls("pane", "split"),
                         [["pane", "split", "w9:p1", "--direction", "right", "--cwd", str(self.worktree),
                           "--no-focus"]])
        start_call = self.reviewer_start()
        self.assertEqual(start_call[2], f"hb-{self.card.ref}-review")
        self.assertEqual(start_call[start_call.index("--kind") + 1], "codex")
        self.assertEqual(start_call[start_call.index("--pane") + 1], result["pane_id"])
        folder = Path(result["folder"])
        args = start_call[start_call.index("--") + 1:]
        self.assertIn(str(folder / "request.md"), args[-1])
        self.assertEqual(args[:-1], agent_review.codex_review_options(folder))
        self.assertEqual(result["reviewer"], "codex")
        request = (folder / "request.md").read_text()
        self.assertIn("Herdboard automatically delivers", request)
        self.assertIn("You do not need to run a command or send a notification", request)
        self.assertIn("Do not report that you cannot run tests", request)
        self.assertNotIn("state those limits", request)
        self.assertNotIn("terminal-browser", request)

    def test_claude_reviewer_keeps_browser_support(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="claude")
        self.assertIn("Bash(terminal-browser:*)", self.reviewer_start())
        request = (self.folder / "request.md").read_text()
        self.assertIn("When the change has a page", request)
        self.assertIn("terminal-browser open --no-merge --split right", request)
        self.assertIn("terminal-browser action --browser <key> -- close", request)
        self.assertIn("Do not run `terminal-browser shutdown`", request)
        self.assertIn("stays listed", request)
        self.assertIn("do not start a server", request)

    def test_the_review_starts_in_the_background(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.fake.calls.clear()
        self.request()
        self.assertEqual(self.calls("workspace", "focus"), [])

    def test_a_closed_card_workspace_opens_in_the_background(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        for entry in self.fake.worktrees:
            entry["open_workspace_id"] = None
        self.fake.calls.clear()
        self.request()
        opened = self.calls("worktree", "open")
        self.assertEqual(len(opened), 1)
        self.assertIn("--no-focus", opened[0])
        self.assertNotIn("--focus", opened[0])

    def test_a_chosen_pi_reviewer_gets_its_model_and_thinking(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="pi", model="zai/glm-5.3", thinking="high")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[:-2], ["--tools", "read,grep,find,ls,write,bash", "--model", "zai/glm-5.3",
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

    def test_a_pi_reviewer_without_a_level_takes_the_saved_one_of_its_model(self):
        self.store.config.thinking = {"zai/glm-5.3": "xhigh", "start/one": "minimal"}
        self.store.config.start_model = "start/one"
        self.store.config.start_thinking = "low"
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="pi", model="zai/glm-5.3")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[args.index("--thinking") + 1], "xhigh")

    def test_a_pi_reviewer_without_a_model_takes_the_saved_level_of_start_model(self):
        self.store.config.start_agent = "pi"
        self.store.config.thinking = {"start/one": "xhigh"}
        self.store.config.start_model = "start/one"
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="pi")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[args.index("--thinking") + 1], "xhigh")

    def test_a_pi_reviewer_takes_the_default_level_without_a_saved_one(self):
        self.store.config.start_agent = "pi"
        self.store.config.thinking = {"other/model": "xhigh"}
        self.store.config.start_model = "start/one"
        self.store.config.start_thinking = "low"
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="pi")
        args = self.reviewer_start()
        args = args[args.index("--") + 1:]
        self.assertEqual(args[args.index("--thinking") + 1], "low")

    def test_a_saved_level_is_never_given_to_claude(self):
        self.store.config.thinking = {"start/one": "xhigh"}
        self.store.config.start_model = "start/one"
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.request(reviewer="claude")
        args = self.reviewer_start()
        self.assertNotIn("--thinking", args)

    def test_the_record_logs_and_worker_follow(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.request(reviewer="pi", model="zai/glm-5.3", then_hunk=True)
        stored = self.records()[self.card.ref]["agent_review"]
        head = self.git(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(stored, {"folder": result["folder"], "pane_id": result["pane_id"], "kind": "pi",
                                  "model": "zai/glm-5.3", "then_hunk": True, "head": head})
        self.assertEqual(self.log(), ["Agent review started: pi (zai/glm-5.3)."])
        self.assertEqual(len(self.workers), 1)
        arguments, log = self.workers[0]
        self.assertEqual(arguments[-4:], ["review-wait", self.card.ref, "--folder", result["folder"]])
        self.assertEqual(log, Path(result["folder"]) / "worker.log")

    def test_a_new_review_removes_the_saved_review(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        records = self.records()
        records[self.card.ref]["agent_reviewed"] = {"head": "abc", "findings": "old.md"}
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        self.request()
        self.assertNotIn("agent_reviewed", self.records()[self.card.ref])

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
        self.assertEqual(self.log()[-1], f"Agent review by codex done: 2 findings. {findings}")
        prompt = self.calls("agent", "prompt")[-1]
        self.assertEqual(prompt[2], "w9:p1")
        for part in (str(findings), "| # | Description | Severity | Agree/Disagree | Reasoning |",
                     "one row for each finding", "what the problem is",
                     "Do not give only the finding's name or number", "herdboard log", "Do not change any code", "Wait for the user"):
            self.assertIn(part, prompt[3])
        notification = self.calls("notification", "show")[-1]
        self.assertEqual(notification[2], f"Agent review of {self.card.ref} done")
        self.assertNotIn("agent_review", self.records()[self.card.ref])
        self.assertEqual(self.calls("plugin", "action", "invoke"), [])

    def test_ready_findings_save_the_reviewed_head(self):
        result = self.request()
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.records()[self.card.ref]["agent_reviewed"],
                         {"head": self.git(self.worktree, "rev-parse", "HEAD"),
                          "findings": str(Path(result["folder"]) / "findings.md")})

    def test_a_failed_review_saves_no_reviewed_head(self):
        self.fake.findings = None
        self.fake.reviewer_states = ["working", None]
        self.request()
        self.assertEqual(self.wait(), 1)
        self.assertNotIn("agent_reviewed", self.records()[self.card.ref])

    def test_a_failed_review_followed_by_a_file_only_retry_delivers_the_second_findings(self):
        self.fake.findings = None
        self.fake.reviewer_states = ["working", None]
        self.request()
        self.assertEqual(self.wait(), 1)
        self.fake.reviewer_states = ["idle"]
        result = self.request(reviewer="pi")
        findings = Path(result["folder"]) / "findings.md"
        findings.write_text("No findings.\n")
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.errors.getvalue(), "")
        self.assertIn(str(findings), self.calls("agent", "prompt")[-1][3])

    def test_a_late_failed_worker_does_not_clear_the_retry_record(self):
        first = self.request()
        self.fake.reviewer_states = [None]
        second = self.request()
        self.fake.reviewer_states = ["working", None]
        self.fake.findings = None
        with contextlib.redirect_stderr(io.StringIO()), \
                patch.object(agent_review, "_wait_for_findings",
                             side_effect=review.ReviewError("Reviewer stopped", "review_failed")):
            self.assertEqual(agent_review.wait(self.store, self.card.ref, Path(first["folder"])), 1)
        self.assertEqual(self.records()[self.card.ref]["agent_review"]["folder"], second["folder"])
        self.fake.reviewer_states = ["idle"]
        (self.folder / "findings.md").write_text("No findings.\n")
        self.assertEqual(self.wait(), 0)
        self.assertIn(str(self.folder / "findings.md"), self.calls("agent", "prompt")[-1][3])

    def test_delivery_failure_is_not_reported_as_success(self):
        self.request()
        real_command = self.fake

        def failed_wait(argv, timeout=120):
            if argv[1:3] == ["agent", "wait"]:
                return self.fake.reply(argv, error="timeout")
            return real_command(argv, timeout)

        with patch.object(start, "command", side_effect=failed_wait):
            self.assertEqual(self.wait(), 1)
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertIn("cannot prompt", self.errors.getvalue())
        self.assertTrue(any("done: 2 findings" in entry for entry in self.log()))
        self.assertIn(str(self.folder / "findings.md"), self.log()[-1])
        self.assertEqual(self.calls("notification", "show")[-1][2],
                         f"Agent review of {self.card.ref} delivery failed")

    def test_a_prompt_failure_reports_completed_findings_and_delivery_failure(self):
        self.request()
        real_command = self.fake

        def failed_prompt(argv, timeout=120):
            if argv[1:3] == ["agent", "prompt"]:
                return self.fake.reply(argv, error="agent_blocked")
            return real_command(argv, timeout)

        with patch.object(start, "command", side_effect=failed_prompt):
            self.assertEqual(self.wait(), 1)
        self.assertIn("cannot prompt", self.errors.getvalue())
        self.assertTrue(any("done: 2 findings" in entry for entry in self.log()))
        self.assertTrue(self.log()[-1].startswith("Agent review delivery failed:"))
        self.assertIn(str(self.folder / "findings.md"), self.log()[-1])
        self.assertEqual(self.calls("notification", "show")[-1][2],
                         f"Agent review of {self.card.ref} delivery failed")

    def test_the_answer_command_uses_the_worker_config_file(self):
        self.request()
        custom = self.root / "custom.toml"
        with contextlib.redirect_stderr(io.StringIO()):
            agent_review.wait(self.store, self.card.ref, self.folder, config_file=custom)
        self.assertIn(f"--config {custom} log {self.card.ref}", self.calls("agent", "prompt")[-1][3])

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
        self.assertTrue(any(entry.startswith("Agent review failed: The codex reviewer stopped before it wrote")
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

    def test_both_on_the_command_line_is_refused(self):
        card = self.run_json("add", "Research corvid memory", directory=self.plain)["card"]
        folder = self.todos / ".herdboard" / "work" / card["ref"]
        folder.mkdir(parents=True)
        start._write(start.state_dir() / "starts.json", {card["ref"]: {
            "ref": card["ref"], "mode": "repoless", "workspace_id": "x1", "pane_id": "x1:p1",
            "worktree_path": str(folder)}})
        data = self.run_json("review", card["ref"], "--both", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "no_repository")

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


LINEAR_ISSUE = {
    "id": "i7", "identifier": "ENG-7", "title": "Map corvid caches",
    "description": "Find where the caches are.\n\nReport the sites.",
    "state": {"name": "In Progress", "type": "started"},
    "labels": [], "project": None, "team": {"key": "ENG", "id": "team-eng"},
    "url": "https://linear.app/acme/issue/ENG-7", "branchName": "nate/eng-7",
    "sortOrder": 1.0, "attachments": [],
}


class FakeIssueWriter:
    """A Linear writer that serves one issue without a network."""

    def __init__(self, issue):
        self._issue = issue

    def online(self):
        return contextlib.nullcontext(self)

    def issue(self, ref):
        return self._issue


class LinearRequestTests(AgentReviewTestCase):
    def test_a_repoless_linear_review_reads_the_issue(self):
        folder = start.work_folder(self.config, "ENG-7")
        folder.mkdir(parents=True)
        start._write(start.state_dir() / "starts.json", {"i7": {
            "ref": "ENG-7", "mode": "repoless", "workspace_id": "x9", "pane_id": "x9:p1",
            "worktree_path": str(folder), "agent": "claude", "agent_name": "hb-eng-7"}})
        self.fake.agent = True
        source = self.root / "findings.md"
        source.write_text("# Findings\n\nCaches are here.\n", encoding="utf-8")
        attachments.add(self.store, "ENG-7", source)
        reviewer = agent_review.AgentReviewer(self.store, self.repo, writer=FakeIssueWriter(LINEAR_ISSUE))
        result = reviewer.request("ENG-7")
        request = (Path(result["folder"]) / "request.md").read_text(encoding="utf-8")
        self.assertIn("Map corvid caches", request)
        self.assertIn("Find where the caches are.", request)
        self.assertIn("Report the sites.", request)
        self.assertIn("no repository", request)


class NoRepositoryAgentReviewTests(AgentReviewTestCase):
    """An agent review of a repoless card reads its attachments instead of a diff."""

    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.store.ensure_inbox(), "Research corvid memory")
        self.result = self.starter.start(self.card.ref, no_repo=True, agent="pi", no_agent=True)
        self.card_folder = start.work_folder(self.config, self.card.ref)
        self.final = ""

    def attach(self, name: str, text: str) -> None:
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        attachments.add(self.store, self.card.ref, source)

    def request(self, **options):
        result = self.reviewer.request(self.card.ref, **options)
        self.fake.review_folder = self.folder = Path(result["folder"])
        return result

    def test_a_repoless_card_without_attachments_has_nothing_to_review(self):
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "nothing_to_review")
        self.assertFalse(self.calls("pane", "split"))

    def test_a_plan_alone_is_not_a_reviewed_result(self):
        self.attach("plan.md", "# Plan\n\nDo the research.\n")
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref)
        self.assertEqual(raised.exception.code, "nothing_to_review")

    def test_both_is_refused_before_the_reviewer_starts(self):
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.request(self.card.ref, then_hunk=True)
        self.assertEqual(raised.exception.code, "no_repository")
        self.assertFalse(self.calls("pane", "split"))

    def test_the_review_lists_the_attachments_and_runs_in_the_card_folder(self):
        self.attach("plan.md", "# Plan\n\nDo the research.\n")
        self.attach("findings.md", "# Findings\n\nCorvids cache food.\n")
        result = self.request()
        self.assertEqual(result["reviewer"], "pi")
        split = self.calls("pane", "split")[0]
        self.assertEqual(split[split.index("--cwd") + 1], str(self.card_folder))
        request = (self.folder / "request.md").read_text(encoding="utf-8")
        attachment = attachments.folder(self.config, self.card.ref) / "findings.md"
        self.assertIn(f"- `{attachment}`", request)
        self.assertIn(f"Working folder: `{self.card_folder}`", request)
        self.assertIn("no repository", request)
        self.assertIn("plan.md", request)
        self.assertIn("When the change has a page", request)
        self.assertIn("terminal-browser open --no-merge --split right", request)
        self.assertIn("terminal-browser action --browser <key> -- close", request)
        self.assertIn("Do not run `terminal-browser shutdown`", request)
        self.assertIn("stays listed", request)
        self.assertIn("do not start a server", request)
        self.assertFalse((self.folder / "branch.patch").exists())
        self.assertFalse((self.folder / "uncommitted.patch").exists())

    def test_the_findings_reach_the_card_agent(self):
        self.attach("findings.md", "# Findings\n\nCorvids cache food.\n")
        self.request()
        code = self.wait()
        self.assertEqual(code, 0, self.errors.getvalue())
        prompt = next(c for c in self.calls("agent", "prompt") if c[2] == self.result["pane_id"])
        self.assertIn("findings.md", prompt[-1])
