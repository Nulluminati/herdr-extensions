"""Agent-written pull requests: an agent writes the title and description, then a draft PR opens."""
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import activity, merge, pr_write, start  # noqa: E402
from test_herdboard_agent_review import ReviewerHerdr  # noqa: E402
from test_herdboard_merge import PullRequestTestCase  # noqa: E402

PR_TEXT = "fix: keep sessions for 30 minutes\n\n## Why\nSessions ended too soon.\n"


class WriterHerdr(ReviewerHerdr):
    """ReviewerHerdr whose card agent writes pr.md after a prompt, and whose split agent writes pr.md."""

    def __init__(self, root):
        super().__init__(root)
        self.output = "pr.md"
        self.findings = PR_TEXT
        self.prompted = None

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "agent prompt" and "pr.md" in args[3]:
            self.calls.append(args)
            self.prompted = args[2]
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and args[2].endswith("-pr"):
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        if operation == "agent get" and self.agent and args[2] == self.prompted and args[2] != self.reviewer_pane:
            self.calls.append(args)
            if self.findings is not None and self.review_folder:
                (self.review_folder / "pr.md").write_text(self.findings)
            return self.reply(argv, {"agent": {"agent_status": "idle", "pane_id": args[2]}})
        return super().__call__(argv, timeout)


class PrWriteTestCase(PullRequestTestCase):
    def setUp(self):
        super().setUp()
        self.fake.__class__ = WriterHerdr
        WriterHerdr.__init__(self.fake, self.root)
        self.enterContext(patch.object(pr_write, "POLL_SECONDS", 0))
        self.started()
        self.store.move(self.card.ref, "Review")
        self.head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")

    def request(self, **options):
        result = self.merger.merge(self.card.ref, mode="pr", **options)
        self.fake.review_folder = self.folder = Path(result["folder"])
        return result

    def wait(self):
        self.errors = io.StringIO()
        with contextlib.redirect_stderr(self.errors):
            return pr_write.wait(self.store, self.card.ref, self.folder)

    def created(self):
        return [c for c in self.gh.calls if c[:2] == ["pr", "create"]]


class RequestTests(PrWriteTestCase):
    def test_the_card_agent_is_asked_by_the_worker(self):
        result = self.request()
        self.assertEqual(result["writer"], "the card's agent")
        stored = self.records()[self.card.ref]["pr_write"]
        self.assertEqual((stored["folder"], stored["pane_id"], stored["own_pane"], stored["rewrite_url"]),
                         (result["folder"], "w9:p1", False, None))
        self.assertEqual(json.loads((self.folder / "pr_write.json").read_text()), stored)
        self.assertEqual(self.log(), ["PR description requested from the card's agent."])
        arguments, log = self.workers[0]
        self.assertEqual(arguments[-4:], ["pr-wait", self.card.ref, "--folder", result["folder"]])
        self.assertEqual(log, self.folder / "worker.log")
        self.assertEqual(self.calls("pane", "split"), [])
        self.assertIsNone(self.fake.prompted)

    def test_the_request_holds_the_card_the_change_the_log_and_the_rules(self):
        self.store.edit(self.card.ref, body=["Use a 30 minute session."])
        activity.append(self.store, self.card.ref, "Chose a fixed timeout over a setting.", "pi")
        self.request()
        self.assertIn("+a", (self.folder / "branch.patch").read_text())
        text = (self.folder / "request.md").read_text()
        for part in ("Fix login timeout", "Use a 30 minute session.", self.branch, self.main_branch(),
                     str(self.worktree), str(self.folder / "branch.patch"), str(self.folder / "pr.md"),
                     "Chose a fixed timeout over a setting.",
                     "## Why", "## What changed", "## Trade-offs", "## Testing", "humanizer",
                     "Do not name the tools", "Do not add a Linear link", "Do not run `gh`",
                     "Describe only the testing that the notes or the change show", "you did not run"):
            self.assertIn(part, text)

    def test_the_title_always_follows_one_conventional_commit_guide(self):
        self.request()
        text = (self.folder / "request.md").read_text()
        for part in ("<type>(<scope>): <summary>", "`feat`", "`fix`", "`docs`", "`refactor`", "`test`",
                     "`chore`", "imperative mood", "lower case", "72 characters", "no period"):
            self.assertIn(part, text)
        self.assertNotIn("git log", text)

    def test_templates_replace_the_default_format(self):
        for name in (".github/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md",
                     ".github/PULL_REQUEST_TEMPLATE/bug.md", "notes.md"):
            path = self.worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("## Summary\n")
        self.git(self.worktree, "add", ".")
        self.git(self.worktree, "commit", "-qm", "docs: templates")
        self.request()
        text = (self.folder / "request.md").read_text()
        for name in (".github/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md",
                     ".github/PULL_REQUEST_TEMPLATE/bug.md"):
            self.assertIn(str(self.worktree / name), text)
        self.assertNotIn("notes.md", text)
        self.assertNotIn("## Why", text)

    def test_a_gone_card_agent_gets_a_read_only_writer_agent(self):
        self.fake.agent = False
        result = self.request()
        self.assertEqual(result["writer"], "a pi writer agent")
        self.assertEqual(self.calls("pane", "split")[0][2], "w9:p1")
        start_call = next(c for c in self.calls("agent", "start") if c[2].endswith("-pr"))
        self.assertEqual(start_call[2], f"hb-{self.card.ref}-pr")
        self.assertEqual(start_call[start_call.index("--kind") + 1], "pi")
        args = start_call[start_call.index("--") + 1:]
        self.assertEqual(args[:2], ["--tools", "read,grep,find,ls,write"])
        self.assertIn(str(self.folder / "request.md"), args[-1])
        stored = self.records()[self.card.ref]["pr_write"]
        self.assertEqual((stored["pane_id"], stored["own_pane"]), (result["pane_id"], True))
        self.assertEqual(self.log(), ["PR description requested from a pi writer agent."])

    def test_a_running_writer_blocks_a_second_request(self):
        self.request()
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual(raised.exception.code, "pr_writing")

    def test_a_writer_that_is_gone_does_not_block(self):
        self.fake.agent = False
        first = self.request()
        self.fake.reviewer_states = [None]
        second = self.request()
        self.assertNotEqual(second["folder"], first["folder"])

    def test_a_request_older_than_the_timeout_does_not_block(self):
        self.request()
        records = self.records()
        records[self.card.ref]["pr_write"]["started"] -= pr_write.WRITE_TIMEOUT_SECONDS + 120
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        self.request()

    def test_rewrite_asks_for_a_description_of_the_open_pull_request(self):
        self.gh.existing = [self.gh.url]
        result = self.request(rewrite=True)
        self.assertEqual((result["pr_action"], result["pr_url"]), ("writing", self.gh.url))
        self.assertEqual(self.records()[self.card.ref]["pr_write"]["rewrite_url"], self.gh.url)
        self.assertNotIn(self.branch, self.remote_branches().split())

    def test_a_worker_that_cannot_start_closes_the_writer(self):
        self.fake.agent = False

        def broken(arguments, log):
            raise OSError("no python")

        with patch.object(pr_write, "start_worker", broken):
            with self.assertRaises(merge.MergeError) as raised:
                self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual(raised.exception.code, "worker_failed")
        self.assertEqual(len(self.calls("pane", "close")), 1)
        self.assertNotIn("pr_write", self.records()[self.card.ref])

    def test_the_board_shows_that_the_description_is_written(self):
        self.request()
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertTrue(card.pr_writing)


class WaitTests(PrWriteTestCase):
    def test_the_card_agent_writes_then_a_draft_pull_request_opens(self):
        self.request()
        self.assertEqual(self.wait(), 0)
        waits = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["agent", "wait"]]
        prompts = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["agent", "prompt"]]
        self.assertEqual(self.fake.calls[waits[0]][2:5], ["w9:p1", "--until", "idle"])
        self.assertLess(waits[0], prompts[0])
        prompt = self.fake.calls[prompts[0]][3]
        self.assertIn(str(self.folder / "request.md"), prompt)
        self.assertIn(str(self.folder / "pr.md"), prompt)
        [create] = self.created()
        self.assertIn("--draft", create)
        self.assertEqual(create[create.index("--base") + 1], self.main_branch())
        self.assertEqual(create[create.index("--head") + 1], self.branch)
        self.assertEqual(create[create.index("--title") + 1], "fix: keep sessions for 30 minutes")
        self.assertEqual(self.gh.bodies, ["## Why\nSessions ended too soon.\n"])
        self.assertEqual(self.git(self.origin, "rev-parse", self.branch), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", self.branch + "@{u}"), self.main_branch())
        record = self.records()[self.card.ref]
        self.assertEqual((record["pr_url"], record["pr_state"]), (self.gh.url, "open"))
        self.assertNotIn("pr_write", record)
        self.assertEqual(self.log()[-1], f"PR opened: {self.gh.url}")
        notification = self.calls("notification", "show")[-1]
        self.assertEqual(notification[2:], [f"PR for {self.card.ref} opened", "--body", self.gh.url])
        self.assertEqual(self.calls("pane", "close"), [])

    def test_a_writer_agent_pane_closes_after_it_writes(self):
        self.fake.agent = False
        result = self.request()
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls("pane", "close"), [["pane", "close", result["pane_id"]]])
        self.assertIsNone(self.fake.prompted)
        self.assertEqual(len(self.created()), 1)

    def test_a_markdown_heading_title_is_a_plain_title(self):
        self.fake.findings = "\n# Fix the login timeout\n\nBody text.\n"
        self.request()
        self.assertEqual(self.wait(), 0)
        [create] = self.created()
        self.assertEqual(create[create.index("--title") + 1], "Fix the login timeout")
        self.assertEqual(self.gh.bodies, ["Body text.\n"])

    def test_a_linear_link_ends_the_body(self):
        self.request()
        stored = json.loads((self.folder / "pr_write.json").read_text())
        stored["linear_url"] = "https://linear.app/acme/issue/ENG-7"
        (self.folder / "pr_write.json").write_text(json.dumps(stored))
        self.wait()
        self.assertEqual(self.gh.bodies,
                         ["## Why\nSessions ended too soon.\n\nLinear: https://linear.app/acme/issue/ENG-7\n"])

    def test_an_empty_title_opens_no_pull_request(self):
        self.fake.findings = "\n\n"
        self.request()
        self.assertEqual(self.wait(), 1)
        self.assertEqual(self.created(), [])
        self.assertTrue(self.log()[-1].startswith("PR not opened: "))
        self.assertIn("no title", self.errors.getvalue())
        self.assertEqual(self.calls("notification", "show")[-1][2], f"PR for {self.card.ref} not opened")
        self.assertNotIn("pr_write", self.records()[self.card.ref])

    def test_a_failed_push_opens_no_pull_request(self):
        self.request()
        self.git(self.repo, "remote", "remove", "origin")
        self.assertEqual(self.wait(), 1)
        self.assertEqual(self.created(), [])
        self.assertTrue(self.log()[-1].startswith(f"PR not opened: Cannot push {self.branch}"))

    def test_rewrite_edits_the_open_pull_request(self):
        self.gh.existing = [self.gh.url]
        self.request(rewrite=True)
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.created(), [])
        edit = next(c for c in self.gh.calls if c[:2] == ["pr", "edit"])
        self.assertEqual(edit[2], self.gh.url)
        self.assertEqual(edit[edit.index("--title") + 1], "fix: keep sessions for 30 minutes")
        self.assertEqual(self.log()[-1], f"PR description rewritten: {self.gh.url}")
        self.assertEqual(self.git(self.origin, "rev-parse", self.branch), self.head)

    def test_a_writer_that_takes_too_long_fails(self):
        self.fake.agent = False
        self.fake.reviewer_states = ["working"] * 3
        self.fake.findings = None
        self.request()
        with patch.object(pr_write, "WRITE_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 1)
        self.assertIn("did not finish", self.log()[-1])
        self.assertEqual(self.created(), [])

    def test_a_writer_that_stops_without_a_file_fails(self):
        self.fake.agent = False
        self.fake.reviewer_states = ["working", None]
        self.fake.findings = None
        self.request()
        self.assertEqual(self.wait(), 1)
        self.assertIn("stopped before it wrote", self.log()[-1])

    def test_a_finished_card_only_loses_the_writer_pane(self):
        self.fake.agent = False
        result = self.request()
        records = self.records()
        records.pop(self.card.ref)
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls("pane", "close"), [["pane", "close", result["pane_id"]]])
        self.assertEqual(self.gh.calls[-1][:2], ["pr", "list"])
        self.assertEqual(self.errors.getvalue(), "")


class StartedTimeTests(PrWriteTestCase):
    def test_the_request_records_when_it_started(self):
        before = time.time()
        self.request()
        self.assertGreaterEqual(self.records()[self.card.ref]["pr_write"]["started"], before)
