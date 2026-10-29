import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/peer-review/bin/peer_review.py"
spec = importlib.util.spec_from_file_location("peer_review", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load peer review script")
peer_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer_review)
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import sandbox  # noqa: E402


def git(repository, *arguments):
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True)


class PeerReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repo with spaces"
        self.repository.mkdir()
        git(self.repository, "init", "--quiet")
        git(self.repository, "config", "user.email", "test@example.com")
        git(self.repository, "config", "user.name", "Test")
        (self.repository / "app.py").write_text("print('one')\n")
        git(self.repository, "add", "app.py")
        git(self.repository, "commit", "--quiet", "-m", "initial")
        self.state = self.base / "state"
        self.environment = patch.dict(os.environ, {
            "HERDR_BIN_PATH": "herdr",
            "HERDR_PANE_ID": "w1:p1",
            "XDG_STATE_HOME": str(self.state),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)
        self.agents = {"w1:p1": {"agent": "claude", "cwd": str(self.repository), "pane_id": "w1:p1"}}
        self.calls = []
        self.workers = []
        self.write_findings = True
        self.start_error = None
        self.reviewer_states = ["working", "idle"]
        self.reviewer_kind = "claude"
        self.review = None

    def fake_command(self, argv, timeout=120):
        self.calls.append(argv)
        if argv[:4] == ["herdr", "agent", "get", "w1:p9"]:
            state = self.reviewer_states.pop(0)
            if state is None:
                raise peer_review.ReviewError('{"error":{"code":"agent_not_found"}}')
            if state == "idle" and self.write_findings:
                (self.review / "findings.md").write_text("No findings.\n")
            agent = {"agent": self.reviewer_kind, "agent_status": state, "pane_id": "w1:p9"}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agent": agent}}), "")
        if argv[:3] == ["herdr", "agent", "get"]:
            agent = self.agents.get(argv[3])
            if agent is None:
                raise peer_review.ReviewError(f"no agent in pane {argv[3]}")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agent": agent}}), "")
        if argv[:3] == ["herdr", "pane", "split"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}}), "")
        if argv[:3] == ["herdr", "agent", "start"]:
            if self.start_error:
                raise peer_review.ReviewError(self.start_error)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[:3] == ["herdr", "agent", "prompt"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[:3] == ["herdr", "agent", "wait"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[:3] == ["herdr", "notification", "show"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[0] == "git":
            try:
                return subprocess.run(argv, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as error:
                raise peer_review.ReviewError(error.stderr.strip()) from error
        self.fail(f"Unexpected command: {argv}")

    def run_request(self, *arguments):
        output = io.StringIO()
        self.errors = io.StringIO()
        with patch.object(peer_review, "command", self.fake_command), \
                patch.object(peer_review, "start_worker", lambda arguments, log: self.workers.append((arguments, log))), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(self.errors):
            status = peer_review.main(["request", *arguments])
        return status, output.getvalue()

    def run_deliver(self, review):
        self.review = review
        with patch.object(peer_review, "command", self.fake_command), \
                patch.object(peer_review.time, "sleep", lambda seconds: None), \
                contextlib.redirect_stderr(io.StringIO()):
            return peer_review.main([
                "deliver", "--review", str(review), "--requester", "w1:p1",
                "--reviewer-pane", "w1:p9", "--reviewer", "pi",
            ])

    def only_review(self):
        reviews = list((self.state / "peer-review").iterdir())
        self.assertEqual(len(reviews), 1)
        return reviews[0]

    def test_agent_reviewer_rotation(self):
        self.assertEqual(peer_review.reviewer_for("claude"), "pi")
        self.assertEqual(peer_review.reviewer_for("pi"), "codex")
        self.assertEqual(peer_review.reviewer_for("codex"), "claude")

    def test_reviewers_get_the_herdboard_sandbox_without_a_browser(self):
        folder, repo = Path('/reviews/a"b\\c'), Path("/repo")
        prompt = peer_review.reviewer_arguments("codex", folder, repo)[-1]
        self.assertIn(str(folder / "request.md"), prompt)
        self.assertEqual(peer_review.reviewer_arguments("codex", folder, repo),
                         [*sandbox.codex_reviewer_options("hb-review", folder, hunk=False), prompt])
        self.assertEqual(peer_review.reviewer_arguments("pi", folder, repo),
                         [*sandbox.pi_reviewer_options(folder, repo, browser=False, hunk=False), "--", prompt])
        self.assertEqual(peer_review.reviewer_arguments("claude", folder, repo),
                         [prompt, *sandbox.claude_reviewer_options(folder, repo, browser=False, hunk=False),
                          *sandbox.claude_reviewer_tools(folder)])

    def test_other_agents_are_rejected(self):
        with self.assertRaisesRegex(peer_review.ReviewError, "unsupported"):
            peer_review.reviewer_for("unsupported")

    def test_request_writes_diff_and_untracked_files(self):
        (self.repository / "app.py").write_text("print('two')\n")
        (self.repository / "new.py").write_text("print('new')\n")

        status, output = self.run_request()

        self.assertEqual(status, 0)
        review = self.only_review()
        self.assertIn("+print('two')", (review / "diff.patch").read_text())
        request = (review / "request.md").read_text()
        self.assertIn(str(self.repository), request)
        self.assertIn("new.py", request)
        self.assertIn(str(review / "diff.patch"), request)
        self.assertIn(str(review), output)
        self.assertIn("You may run any read-only command", request)
        self.assertIn("Do not run the project's code, tests or builds", request)
        self.assertIn("Do not change Git state", request)
        self.assertIn("Do not use `terminal-browser`", request)

    def test_request_starts_a_sandboxed_pi_next_to_claude(self):
        (self.repository / "app.py").write_text("print('two')\n")

        self.run_request()

        split = next(argv for argv in self.calls if argv[:3] == ["herdr", "pane", "split"])
        self.assertEqual(split[3:], ["w1:p1", "--direction", "right", "--cwd", str(self.repository), "--no-focus"])
        start = next(argv for argv in self.calls if argv[:3] == ["herdr", "agent", "start"])
        self.assertIn("--kind", start)
        self.assertEqual(start[start.index("--kind") + 1], "pi")
        self.assertEqual(start[start.index("--pane") + 1], "w1:p9")
        review = self.only_review()
        prompt = start[-1]
        self.assertEqual(start[start.index("--") + 1:], peer_review.reviewer_arguments("pi", review, self.repository))
        self.assertIn(str(review / "request.md"), prompt)
        self.assertIn(str(review / "findings.md"), prompt)

    def test_codex_lost_name_requires_a_matching_live_pane(self):
        self.write_findings = False
        self.agents["w1:p1"]["agent"] = "pi"
        (self.repository / "app.py").write_text("print('two')\n")
        self.start_error = '{"error":{"code":"agent_name_not_found"}}'
        self.reviewer_kind = "codex"
        status, _ = self.run_request()
        self.assertEqual(status, 0, self.errors.getvalue())
        self.assertEqual(len(self.workers), 1)
        self.reviewer_kind = "claude"
        with patch.object(peer_review.time, "strftime", return_value="wrong-kind"):
            status, _ = self.run_request()
        self.assertEqual(status, 1)
        self.assertIn("agent_name_not_found", self.errors.getvalue())
        self.assertEqual(len(self.workers), 1)

    def test_pi_request_starts_a_sandboxed_codex_reviewer(self):
        self.agents["w1:p1"]["agent"] = "pi"
        (self.repository / "app.py").write_text("print('two')\n")
        status, _ = self.run_request()
        self.assertEqual(status, 0)
        review = self.only_review()
        call = next(argv for argv in self.calls if argv[:3] == ["herdr", "agent", "start"])
        self.assertEqual(call[call.index("--kind") + 1], "codex")
        self.assertEqual(call[call.index("--") + 1:],
                         peer_review.reviewer_arguments("codex", review, self.repository))

    def test_claude_reviewer_may_only_write_inside_the_review(self):
        self.agents["w1:p1"]["agent"] = "codex"
        (self.repository / "app.py").write_text("print('two')\n")

        self.run_request()

        review = self.only_review()
        start = next(argv for argv in self.calls if argv[:3] == ["herdr", "agent", "start"])
        self.assertEqual(start[start.index("--kind") + 1], "claude")
        prompt = start[start.index("--") + 1]
        self.assertIn(str(review / "request.md"), prompt)
        self.assertEqual(start[start.index("--") + 2:],
                         peer_review.reviewer_arguments("claude", review, self.repository)[1:])
        settings = json.loads(start[start.index("--settings") + 1])["sandbox"]
        self.assertEqual(settings["filesystem"], {"allowWrite": [str(review)],
                                                  "denyWrite": [str(self.repository)]})
        self.assertIn(f"Edit(/{review}/**)", start)

    def test_two_requests_in_one_second_get_two_review_folders(self):
        (self.repository / "app.py").write_text("print('two')\n")

        with patch.object(peer_review.time, "strftime", return_value="20250101-010101"):
            first, _ = self.run_request()
            second, _ = self.run_request()

        self.assertEqual((first, second), (0, 0))
        reviews = sorted(review.name for review in (self.state / "peer-review").iterdir())
        self.assertEqual(reviews, ["20250101-010101", "20250101-010101-2"])
        starts = [argv[3] for argv in self.calls if argv[:3] == ["herdr", "agent", "start"]]
        self.assertEqual(starts, ["review-20250101-010101", "review-20250101-010101-2"])
        self.assertEqual(len(self.workers), 2)

    def test_request_hands_the_wait_to_a_background_worker(self):
        (self.repository / "app.py").write_text("print('two')\n")

        self.run_request()

        review = self.only_review()
        self.assertEqual(self.workers, [([
            "deliver", "--review", str(review), "--requester", "w1:p1",
            "--reviewer-pane", "w1:p9", "--reviewer", "pi",
        ], review / "worker.log")])
        self.assertFalse(any(argv[:3] == ["herdr", "agent", "prompt"] for argv in self.calls))

    def test_reviewer_waiting_for_input_still_gets_the_review(self):
        (self.repository / "app.py").write_text("print('two')\n")
        self.start_error = '{"error":{"code":"agent_not_ready","message":"agent is blocked during startup"}}'

        status, output = self.run_request()

        self.assertEqual(status, 0)
        self.assertIn("has not reported ready in pane w1:p9", output)
        self.assertEqual(len(self.workers), 1)

    def test_reviewer_busy_with_its_first_message_still_gets_the_review(self):
        (self.repository / "app.py").write_text("print('two')\n")
        self.start_error = '{"error":{"code":"timeout","message":"timed out waiting for agent startup"}}'

        status, _ = self.run_request()

        self.assertEqual(status, 0)
        self.assertEqual(len(self.workers), 1)

    def test_other_start_failures_stop_the_request(self):
        (self.repository / "app.py").write_text("print('two')\n")
        self.start_error = "pane is not at a shell prompt"

        status, _ = self.run_request()

        self.assertEqual(status, 1)
        self.assertIn("pane is not at a shell prompt", self.errors.getvalue())
        self.assertEqual(self.workers, [])

    def test_request_without_changes_does_not_open_a_pane(self):
        status, _ = self.run_request()

        self.assertEqual(status, 1)
        self.assertIn("No uncommitted changes", self.errors.getvalue())
        self.assertFalse(any(argv[:3] == ["herdr", "pane", "split"] for argv in self.calls))
        self.assertEqual(self.workers, [])

    def branch_with_upstream(self):
        """A `feature` branch whose upstream is the first branch, with one commit on it."""
        first = subprocess.run(["git", "-C", str(self.repository), "symbolic-ref", "--short", "HEAD"],
                               check=True, capture_output=True, text=True).stdout.strip()
        git(self.repository, "checkout", "--quiet", "-b", "feature")
        git(self.repository, "branch", f"--set-upstream-to={first}")
        (self.repository / "app.py").write_text("print('committed')\n")
        git(self.repository, "commit", "--quiet", "-am", "feat: commit")
        return first

    def test_committed_work_is_reviewed_against_the_upstream(self):
        upstream = self.branch_with_upstream()

        status, _ = self.run_request()

        self.assertEqual(status, 0)
        review = self.only_review()
        self.assertIn("+print('committed')", (review / "diff.patch").read_text())
        request = (review / "request.md").read_text()
        self.assertIn(f"commits on `feature` since `{upstream}`", request)
        self.assertEqual(len(self.workers), 1)

    def test_uncommitted_changes_win_over_the_upstream(self):
        self.branch_with_upstream()
        (self.repository / "app.py").write_text("print('uncommitted')\n")

        self.run_request()

        diff = (self.only_review() / "diff.patch").read_text()
        self.assertIn("+print('uncommitted')", diff)
        self.assertNotIn("+print('committed')", diff)

    def test_a_branch_level_with_its_upstream_has_nothing_to_review(self):
        self.branch_with_upstream()
        git(self.repository, "branch", "level")
        git(self.repository, "branch", "--set-upstream-to=level")

        status, _ = self.run_request()

        self.assertEqual(status, 1)
        self.assertIn("No uncommitted changes, and no commits on feature since level", self.errors.getvalue())

    def test_a_failed_request_shows_a_herdr_notification(self):
        status, _ = self.run_request()

        self.assertEqual(status, 1)
        notification = next(argv for argv in self.calls if argv[:3] == ["herdr", "notification", "show"])
        self.assertEqual(notification[3:5], ["Peer review failed", "--body"])
        self.assertIn("No uncommitted changes", notification[5])

    def test_a_failed_notification_does_not_hide_the_error(self):
        original = self.fake_command

        def failing(argv, timeout=120):
            if argv[:3] == ["herdr", "notification", "show"]:
                raise peer_review.ReviewError("no server")
            return original(argv, timeout)

        self.fake_command = failing
        status, _ = self.run_request()

        self.assertEqual(status, 1)
        self.assertIn("No uncommitted changes", self.errors.getvalue())

    def test_request_from_a_pane_without_an_agent_fails(self):
        status, _ = self.run_request("--pane", "w1:p2")

        self.assertEqual(status, 1)
        self.assertIn("no agent in pane w1:p2", self.errors.getvalue())
        self.assertEqual(self.workers, [])

    def test_plugin_action_uses_the_focused_pane(self):
        self.agents["w1:p3"] = {"agent": "pi", "cwd": str(self.repository), "pane_id": "w1:p3"}
        (self.repository / "app.py").write_text("print('two')\n")
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_id": "w1:p3"})

        self.run_request()

        self.assertIn(["herdr", "agent", "get", "w1:p3"], self.calls)

    def make_review(self):
        review = self.state / "peer-review/r1"
        review.mkdir(parents=True)
        return review

    def test_deliver_waits_for_findings_then_tells_the_requester(self):
        review = self.make_review()

        status = self.run_deliver(review)

        self.assertEqual(status, 0)
        self.assertEqual(self.reviewer_states, [])
        self.assertFalse(any(argv[:4] == ["herdr", "agent", "prompt", "w1:p9"] for argv in self.calls))
        self.assertEqual(self.calls[-2], ["herdr", "agent", "wait", "w1:p1", "--until", "idle"])
        notify = self.calls[-1]
        self.assertEqual(notify[:4], ["herdr", "agent", "prompt", "w1:p1"])
        self.assertIn(str(review / "findings.md"), notify[4])
        self.assertIn("Do not change any code", notify[4])
        self.assertIn("Wait for the user", notify[4])

    def test_deliver_reports_a_reviewer_that_stopped(self):
        review = self.make_review()
        self.write_findings = False
        self.reviewer_states = ["working", None]

        status = self.run_deliver(review)

        self.assertEqual(status, 1)
        notify = self.calls[-1]
        self.assertEqual(notify[:4], ["herdr", "agent", "prompt", "w1:p1"])
        self.assertIn("stopped before it wrote", notify[4])
        self.assertIn(str(review / "worker.log"), notify[4])

    def test_deliver_waits_for_a_reviewer_that_is_not_detected_yet(self):
        review = self.make_review()
        self.reviewer_states = [None, "working", "idle"]

        status = self.run_deliver(review)

        self.assertEqual(status, 0)
        self.assertIn("is ready", self.calls[-1][4])

    def test_deliver_reports_a_review_that_takes_too_long(self):
        review = self.make_review()
        self.reviewer_states = ["working"]

        with patch.object(peer_review, "REVIEW_TIMEOUT_MS", 0):
            status = self.run_deliver(review)

        self.assertEqual(status, 1)
        self.assertIn("did not finish", self.calls[-1][4])


if __name__ == "__main__":
    unittest.main()
