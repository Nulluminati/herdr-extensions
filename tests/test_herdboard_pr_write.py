"""Agent-written pull requests: an agent writes the title and description, then a draft PR opens."""
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import activity, merge, pr_write, start  # noqa: E402
from herdboard.agent_review import reviewer_arguments  # noqa: E402
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
    def test_codex_writer_keeps_valid_config_thinking(self):
        records = self.records()
        records[self.card.ref]["agent"] = "codex"
        start._write(start.state_dir() / "starts.json", records)
        self.config.start_thinking = "ultra"
        self.fake.agent = False
        self.request()
        call = next(c for c in self.calls("agent", "start") if c[2].endswith("-pr"))
        self.assertIn('model_reasoning_effort="ultra"', call)
        self.assertIn('default_permissions="hb-review"', call)

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

    def test_a_typed_card_sets_the_title_type(self):
        self.store.edit(self.card.ref, title="Fix login timeout #type/bug")
        self.request()
        text = (self.folder / "request.md").read_text()
        self.assertIn("The card is a bug: the title type is `fix`.", text)

    def test_a_card_without_a_type_leaves_the_title_type_open(self):
        self.request()
        self.assertNotIn("The card is a", (self.folder / "request.md").read_text())

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

    def test_a_claude_writer_starts_without_the_browser_rule(self):
        self.fake.agent = False
        path = start.state_dir() / "starts.json"
        records = json.loads(path.read_text())
        records[self.card.ref]["agent"] = "claude"
        path.write_text(json.dumps(records))
        self.request()
        start_call = next(c for c in self.calls("agent", "start") if c[2].endswith("-pr"))
        self.assertEqual(start_call[start_call.index("--kind") + 1], "claude")
        args = start_call[start_call.index("--") + 1:]
        self.assertNotIn("Bash(terminal-browser:*)", args)
        self.assertNotIn("--tools", args)

    def test_a_read_only_writer_gets_no_bash_and_no_browser(self):
        for kind in ("claude", "pi"):
            args = reviewer_arguments(kind, "Read it", Path("/tmp/ask"), None, None)
            self.assertNotIn("Bash(terminal-browser:*)", args, kind)
            if kind == "pi":
                self.assertEqual(args[1], "read,grep,find,ls,write", kind)

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

    def test_the_request_tells_the_agent_how_to_sync_later(self):
        self.request()
        text = (self.folder / "request.md").read_text()
        self.assertIn(f"herdboard pr-sync {self.card.ref}", text)
        self.assertIn("Do not run `gh`", text)
        prompt = pr_write.prompt_text(self.folder, self.card.ref)
        self.assertIn(f"herdboard pr-sync {self.card.ref}", prompt)
        self.assertIn("Do not run gh", prompt)


class SyncTests(PrWriteTestCase):
    def opened(self):
        """Open the draft pull request, then forget what opening it called."""
        self.request()
        self.assertEqual(self.wait(), 0)
        self.gh.calls.clear()
        self.gh.bodies.clear()
        return self.records()[self.card.ref]

    def sync(self, fetch=False):
        return pr_write.sync(self.merger, self.card.ref, fetch=fetch)

    def stored(self, **changes):
        records = self.records()
        record = records[self.card.ref]
        for name, value in changes.items():
            if value is None:
                record.pop(name, None)
            else:
                record[name] = value
        (start.state_dir() / "starts.json").write_text(json.dumps(records))

    def test_the_worker_keeps_the_folder_of_the_written_description(self):
        record = self.opened()
        self.assertEqual(record["pr_folder"], str(self.folder))

    def test_a_sync_pushes_the_branch_and_replaces_the_title_and_body(self):
        self.opened()
        (self.folder / "pr.md").write_text("feat: new title\n\nNew body.\n")
        head = self.commit(self.worktree, "b.txt", "b\n", "feat: b")
        result = self.sync()
        self.assertEqual(result["ref"], self.card.ref)
        self.assertEqual(result["action"], "updated")
        self.assertEqual(result["pr_url"], self.gh.url)
        self.assertEqual(result["path"], str(self.folder / "pr.md"))
        [edit] = [c for c in self.gh.calls if c[:2] == ["pr", "edit"]]
        self.assertEqual(edit[2], self.gh.url)
        self.assertEqual(edit[edit.index("--title") + 1], "feat: new title")
        self.assertEqual(self.gh.bodies, ["New body.\n"])
        self.assertEqual(self.git(self.origin, "rev-parse", self.branch), head)
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", self.branch + "@{u}"),
                         self.main_branch())
        self.assertEqual(self.log()[-1], f"PR description updated: {self.gh.url}")
        record = self.records()[self.card.ref]
        self.assertEqual((record["pr_url"], record["pr_state"]), (self.gh.url, "open"))
        self.assertGreaterEqual(record["pr_checked"], time.time() - 5)

    def test_a_linear_card_gets_the_linear_link_once(self):
        self.opened()
        (self.folder / "pr.md").write_text("feat: x\n\nBody.\n")
        with self.linear():
            self.sync()
        self.assertEqual(self.gh.bodies,
                         ["Body.\n\nLinear: https://linear.app/acme/issue/ENG-7\n"])

    def linear(self):
        """`sync` sees a Linear card with the issue URL, as a real ENG- ref does."""
        card = SimpleNamespace(source="linear", url="https://linear.app/acme/issue/ENG-7")
        key, stored = self.card.ref, self.records()[self.card.ref]
        return patch.object(merge.Merger, "_started", lambda merger, ref: (card, ref, key, stored))

    def test_a_todo_card_syncs_without_a_linear_issue_read(self):
        self.opened()
        (self.folder / "pr.md").write_text("feat: x\n\nBody.\n")
        with patch.object(merge.Merger, "_card", side_effect=AssertionError("no Linear read")):
            self.sync()
        self.assertEqual(self.gh.bodies, ["Body.\n"])

    def test_a_fetch_keeps_a_linear_line_that_is_not_last(self):
        self.opened()
        self.gh.body = "First.\n\nLinear: https://linear.app/acme/issue/ENG-1\n\nLast.\n"
        result = self.sync(fetch=True)
        self.assertEqual(Path(result["path"]).read_text(),
                         "fix: keep sessions for 30 minutes\n\nFirst.\n\n"
                         "Linear: https://linear.app/acme/issue/ENG-1\n\nLast.\n")

    def test_the_pull_request_is_found_by_its_branch(self):
        self.opened()
        self.stored(pr_url=None)
        self.gh.existing = ["https://github.com/acme/api/pull/9"]
        result = self.sync()
        self.assertEqual(result["pr_url"], "https://github.com/acme/api/pull/9")
        self.assertEqual(self.records()[self.card.ref]["pr_url"], "https://github.com/acme/api/pull/9")

    def test_a_closed_pull_request_gives_way_to_the_open_one_of_the_branch(self):
        self.opened()
        self.stored(pr_url="https://github.com/acme/api/pull/6", pr_state="merged")
        self.gh.existing = ["https://github.com/acme/api/pull/9"]
        result = self.sync()
        self.assertEqual(result["pr_url"], "https://github.com/acme/api/pull/9")
        self.assertEqual(self.gh.calls[0][:2], ["pr", "list"])
        [edit] = [c for c in self.gh.calls if c[:2] == ["pr", "edit"]]
        self.assertEqual(edit[2], "https://github.com/acme/api/pull/9")
        self.assertEqual(self.records()[self.card.ref]["pr_state"], "open")

    def test_a_fetch_writes_the_github_text_without_pushing_or_editing(self):
        self.opened()
        head = self.git(self.origin, "rev-parse", self.branch)
        self.gh.title = "fix: old title"
        self.gh.body = "## Why\nSessions ended too soon.\n\nLinear: https://linear.app/acme/issue/ENG-7\n"
        result = self.sync(fetch=True)
        self.assertEqual(result["action"], "fetched")
        self.assertEqual(result["ref"], self.card.ref)
        written = Path(result["path"])
        self.assertEqual(written.parent.parent, start.state_dir() / "prs")
        self.assertEqual(written.read_text(),
                         "fix: old title\n\n## Why\nSessions ended too soon.\n")
        self.assertEqual(self.records()[self.card.ref]["pr_folder"], str(written.parent))
        self.assertFalse(any(c[:2] == ["pr", "edit"] for c in self.gh.calls))
        self.assertEqual(self.git(self.origin, "rev-parse", self.branch), head)

    def test_a_fetched_description_syncs_once_with_the_linear_link(self):
        self.opened()
        self.gh.body = "Body.\n\nLinear: https://linear.app/acme/issue/ENG-7\n"
        with self.linear():
            result = self.sync(fetch=True)
            self.gh.calls.clear()
            self.gh.bodies.clear()
            self.sync()
        self.assertEqual(self.gh.bodies,
                         ["Body.\n\nLinear: https://linear.app/acme/issue/ENG-7\n"])
        self.assertNotIn("Linear:", Path(result["path"]).read_text())

    def test_without_an_open_pull_request_the_sync_stops(self):
        self.opened()
        self.stored(pr_url=None, pr_state=None)
        self.gh.existing = []
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "no_pr")
        self.assertIn("m", str(raised.exception))

    def test_without_a_written_description_the_sync_stops(self):
        self.opened()
        self.stored(pr_folder=None)
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "no_description")
        self.assertIn("--fetch", str(raised.exception))

    def test_a_missing_description_file_stops_the_sync(self):
        self.opened()
        (self.folder / "pr.md").unlink()
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "no_description")

    def test_a_dirty_worktree_stops_the_sync(self):
        self.opened()
        (self.worktree / "b.txt").write_text("uncommitted\n")
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "worktree_dirty")
        self.assertEqual(self.gh.calls, [])

    def test_a_writer_that_still_runs_stops_the_sync(self):
        self.request()
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "pr_writing")

    def test_a_writer_that_starts_under_the_lock_stops_the_sync(self):
        # The sync reads the record before it takes the lock. A rewrite that starts in between is
        # invisible to that read, so the check must run again inside the lock.
        self.opened()
        key, stale = self.card.ref, dict(self.records()[self.card.ref])
        racing = dict(stale, pr_write={"pane_id": "w9:p1", "started": time.time(), "own_pane": False})
        (start.state_dir() / "starts.json").write_text(json.dumps({key: racing}))
        with patch.object(merge.Merger, "_started", lambda merger, ref: (None, ref, key, stale)):
            with self.assertRaises(merge.MergeError) as raised:
                self.sync()
        self.assertEqual(raised.exception.code, "pr_writing")
        self.assertEqual([c for c in self.gh.calls if c[:2] == ["pr", "edit"]], [])

    def test_an_empty_title_stops_the_sync(self):
        self.opened()
        (self.folder / "pr.md").write_text("\n\n")
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "pr_failed")

    def test_a_card_without_a_worktree_cannot_sync(self):
        self.opened()
        records = self.records()
        records[self.card.ref]["worktree_path"] = str(self.root / "gone")
        (start.state_dir() / "starts.json").write_text(json.dumps(records))
        with self.assertRaises(merge.MergeError) as raised:
            self.sync()
        self.assertEqual(raised.exception.code, "not_started")
