"""Pull request reviews: an agent reviews a GitHub PR in its own worktree workspace and leaves notes in Hunk."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import agent_review, pr_reviews, start  # noqa: E402

REPO = "acme/api"
KEY = "acme/api#7"
HEAD = "a" * 40


def pr_json(number=7, **changes):
    data = {"number": number, "title": "Fix login timeout", "url": f"https://github.com/acme/api/pull/{number}",
            "author": {"login": "octo"}, "isDraft": False, "headRefOid": HEAD, "headRefName": "fix-login",
            "baseRefName": "main", "reviewRequests": []}
    data.update(changes)
    return data


def session(session_id, cwd, number=7, repo=REPO):
    return {"sessionId": session_id, "cwd": str(cwd), "launchedAt": "2026-10-16T19:00:36.858Z",
            "review": {"id": f"#{number}", "repository": repo, "provider": "GitHub"}}


class Fake:
    """Answers `gh`, `git`, `hunk` and `herdr` by the longest matching argument prefix, and records each call."""

    def __init__(self):
        self.calls = []
        self.replies = {}

    def on(self, *prefix, out="", code=0, err="", result=None, error=None, then=None):
        """Queue a reply. The last reply for a prefix repeats. Herdr replies take `result` or an error code."""
        if result is not None or error is not None:
            body = {"error": {"code": error, "message": error}} if error else {"result": result}
            out, code = json.dumps(body), 1 if error else 0
        elif not isinstance(out, str):
            out = json.dumps(out)
        self.replies.setdefault(tuple(prefix), []).append((out, code, err, then))

    def __call__(self, argv, timeout=120, **options):
        argv = ["herdr" if argv[0].endswith("herdr") else argv[0], *argv[1:]]
        self.calls.append((argv, options))
        matches = [p for p in self.replies if tuple(argv[:len(p)]) == p]
        if not matches:
            out = json.dumps({"result": {"type": "ok"}}) if argv[0] == "herdr" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        queue = self.replies[max(matches, key=len)]
        out, code, err, then = queue.pop(0) if len(queue) > 1 else queue[0]
        if then:
            then(argv, options)
        return subprocess.CompletedProcess(argv, code, out, err)

    def made(self, *prefix):
        return [argv for argv, _ in self.calls if tuple(argv[:len(prefix)]) == prefix]

    def options(self, *prefix):
        return [options for argv, options in self.calls if tuple(argv[:len(prefix)]) == prefix]


class PrReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                                  "HERDR_BIN_PATH": "herdr"}))
        self.checkout = self.root / "api"
        self.checkout.mkdir()
        self.worktree = self.root / "worktrees" / "pr-7"
        self.fake = Fake()
        self.enterContext(patch.object(pr_reviews, "run", self.fake))
        self.enterContext(patch.object(start, "command", self.fake))
        self.enterContext(patch.object(pr_reviews, "POLL_SECONDS", 0))
        self.workers = []
        self.enterContext(patch.object(agent_review, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))
        pr_reviews._logins.clear()
        self.fake.on("git", "-C", str(self.checkout), "remote", "-v",
                     out="origin\tgit@github.com:acme/api.git (fetch)\norigin\tgit@github.com:acme/api.git (push)\n")
        self.fake.on("gh", "auth", "token", out="secret-token\n")

    def pr(self, **changes):
        return pr_reviews.PullRequest.from_json(pr_json(**changes), "me")

    def new_workspace(self):
        """Herdr answers a new worktree workspace, and Git has no `pr/7` branch yet."""
        self.fake.on("git", "-C", str(self.checkout), "rev-parse", "--verify", "--quiet", code=1)
        self.fake.on("herdr", "worktree", "create", result={
            "workspace": {"workspace_id": "w9"}, "root_pane": {"pane_id": "w9:p1"},
            "worktree": {"path": str(self.worktree)}})

    def new_session(self, session_id="s-new"):
        """Hunk lists one old session, then also the new one for the PR."""
        old = session("s-old", self.root / "elsewhere")
        self.fake.on("hunk", "session", "list", out={"sessions": [old]})
        self.fake.on("hunk", "session", "list", out={"sessions": [old, session(session_id, self.worktree)]})

    def record(self, **changes):
        record = {"checkout": str(self.checkout), "repo": REPO, "number": 7, "title": "Fix login timeout",
                  "branch": "pr/7", "worktree_path": str(self.worktree), "workspace_id": "w9", "pane_id": "w9:p1",
                  "hunk_pane": "w9:p2", "hunk_session": "s-new", "agent": "claude", "agent_name": "pr-api-7",
                  "model": "sonnet", "thinking": None, "reviewed_sha": HEAD, "status": "reviewing", "notes": None,
                  "error": None}
        record.update(changes)
        pr_reviews.save(KEY, record)
        return record


class ListTests(PrReviewTestCase):
    def test_list_prs_reads_gh_and_sets_the_markers(self):
        prs = [pr_json(7), pr_json(8, isDraft=True, author={"login": "Me"}),
               pr_json(9, reviewRequests=[{"__typename": "Team", "name": "core"}, {"login": "ME"}])]
        self.fake.on("gh", "api", "user", out="me\n")
        self.fake.on("gh", "pr", "list", out=prs)
        found = pr_reviews.list_prs(self.checkout, REPO)
        self.assertEqual([p.number for p in found], [7, 8, 9])
        self.assertEqual([(p.draft, p.mine, p.requested) for p in found],
                         [(False, False, False), (True, True, False), (False, False, True)])
        self.assertEqual(found[0].head_sha, HEAD)
        self.assertEqual(found[0].author, "octo")
        listing = self.fake.made("gh", "pr", "list")[0]
        self.assertIn("--repo", listing)
        self.assertIn(REPO, listing)

    def test_the_login_is_read_once(self):
        self.fake.on("gh", "api", "user", out="me\n")
        self.fake.on("gh", "pr", "list", out=[])
        pr_reviews.list_prs(self.checkout, REPO)
        pr_reviews.list_prs(self.checkout, REPO)
        self.assertEqual(len(self.fake.made("gh", "api", "user")), 1)

    def test_a_gh_failure_is_an_error(self):
        self.fake.on("gh", "api", "user", out="me\n")
        self.fake.on("gh", "pr", "list", code=1, err="HTTP 401")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.list_prs(self.checkout, REPO)
        self.assertEqual(raised.exception.code, "gh_failed")
        self.assertIn("HTTP 401", str(raised.exception))

    def test_base_repository(self):
        self.fake.on("gh", "repo", "view", out={"nameWithOwner": REPO})
        self.assertEqual(pr_reviews.base_repository(self.checkout), REPO)


class RemoteTests(PrReviewTestCase):
    def remotes(self, text):
        self.fake.replies.pop(("git", "-C", str(self.checkout), "remote", "-v"))
        self.fake.on("git", "-C", str(self.checkout), "remote", "-v", out=text)

    def test_ssh_url(self):
        self.assertEqual(pr_reviews.base_remote(self.checkout, REPO), "origin")

    def test_https_url_without_git_suffix_and_another_case(self):
        self.remotes("fork\thttps://github.com/me/api.git (fetch)\nupstream\thttps://github.com/Acme/API (fetch)\n")
        self.assertEqual(pr_reviews.base_remote(self.checkout, REPO), "upstream")

    def test_no_remote_names_the_repository(self):
        self.remotes("origin\thttps://github.com/acme/api-docs.git (fetch)\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.base_remote(self.checkout, REPO)
        self.assertEqual(raised.exception.code, "remote_unknown")


class WorkspaceTests(PrReviewTestCase):
    def test_a_new_review_fetches_the_pr_head_into_a_local_branch(self):
        self.new_workspace()
        record = pr_reviews.open_workspace(self.checkout, REPO, 7, "Fix login timeout " + "x" * 60)
        self.assertIn(["git", "-C", str(self.checkout), "fetch", "origin", "pull/7/head:refs/heads/pr/7"],
                      self.fake.made("git"))
        create = self.fake.made("herdr", "worktree", "create")[0]
        self.assertEqual(create[create.index("--branch") + 1], "pr/7")
        label = create[create.index("--label") + 1]
        self.assertTrue(label.startswith("PR #7 Fix login timeout"))
        self.assertEqual(len(label), len("PR #7 ") + 40)
        self.assertIn("--no-focus", create)
        self.assertEqual((record["workspace_id"], record["pane_id"], record["worktree_path"]),
                         ("w9", "w9:p1", str(self.worktree)))
        self.assertEqual(pr_reviews.records()[KEY]["branch"], "pr/7")

    def test_a_branch_that_no_review_owns_is_refused(self):
        self.fake.on("git", "-C", str(self.checkout), "rev-parse", "--verify", "--quiet", out="abc\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.open_workspace(self.checkout, REPO, 7, "Fix")
        self.assertEqual(raised.exception.code, "branch_exists")
        self.assertEqual(self.fake.made("git", "-C", str(self.checkout), "fetch"), [])

    def test_a_live_workspace_is_reused(self):
        self.worktree.mkdir(parents=True)
        self.record()
        self.fake.on("herdr", "workspace", "get", result={"workspace_id": "w9", "root_pane": {"pane_id": "w9:p1"}})
        record = pr_reviews.open_workspace(self.checkout, REPO, 7, "Fix")
        self.assertEqual(record["workspace_id"], "w9")
        self.assertEqual(self.fake.made("herdr", "worktree"), [])
        self.assertEqual(self.fake.made("git", "-C", str(self.checkout), "fetch"), [])

    def test_a_closed_workspace_of_a_kept_worktree_opens_again(self):
        self.worktree.mkdir(parents=True)
        self.record()
        self.fake.on("herdr", "workspace", "get", error="workspace_not_found")
        self.fake.on("herdr", "worktree", "open", result={
            "workspace": {"workspace_id": "w10"}, "root_pane": {"pane_id": "w10:p1"},
            "worktree": {"path": str(self.worktree)}})
        record = pr_reviews.open_workspace(self.checkout, REPO, 7, "Fix")
        opened = self.fake.made("herdr", "worktree", "open")[0]
        self.assertEqual(opened[opened.index("--path") + 1], str(self.worktree))
        self.assertEqual((record["workspace_id"], record["hunk_pane"], record["hunk_session"]), ("w10", None, None))


class HunkTests(PrReviewTestCase):
    def test_the_token_goes_to_the_pane_environment_not_the_command(self):
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.new_session()
        record = pr_reviews.open_hunk(self.record(hunk_pane=None, hunk_session=None))
        split = self.fake.made("herdr", "pane", "split")[0]
        for pair in ("GH_TOKEN=secret-token", "GH_PR_NUMBER=7", f"GH_PR_REPO={REPO}"):
            self.assertIn(pair, split)
            self.assertEqual(split[split.index(pair) - 1], "--env")
        self.assertEqual(split[split.index("--cwd") + 1], str(self.worktree))
        run = self.fake.made("herdr", "pane", "run")[0]
        self.assertEqual(run[3:], ["w9:p2", f"hunk gh pr '{REPO}#7'"])
        self.assertNotIn("secret-token", " ".join(run))
        self.assertEqual((record["hunk_pane"], record["hunk_session"]), ("w9:p2", "s-new"))
        self.assertEqual(pr_reviews.records()[KEY]["hunk_session"], "s-new")

    def test_no_session_is_an_error(self):
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.fake.on("hunk", "session", "list", out={"sessions": []})
        with patch.object(pr_reviews, "HUNK_WAIT_SECONDS", 0):
            with self.assertRaises(pr_reviews.PrReviewError) as raised:
                pr_reviews.open_hunk(self.record(hunk_pane=None, hunk_session=None))
        self.assertEqual(raised.exception.code, "hunk_unavailable")


class ReviewerTests(PrReviewTestCase):
    def test_claude_gets_the_prompt_before_its_allowed_tools(self):
        arguments = pr_reviews.reviewer_arguments("claude", "Read it", "sonnet", None)
        self.assertEqual(arguments[0], "Read it")
        self.assertLess(arguments.index("--model"), arguments.index("--allowedTools"))
        tools = arguments[arguments.index("--allowedTools") + 1:]
        self.assertIn("Bash(hunk session:*)", tools)
        self.assertIn("Bash(gh pr diff:*)", tools)
        self.assertNotIn("Bash", tools)
        self.assertNotIn("Edit", tools)

    def test_pi_gets_bash_and_its_thinking_level(self):
        arguments = pr_reviews.reviewer_arguments("pi", "Read it", "zai/glm-5.3", "high")
        self.assertEqual(arguments[:2], ["--tools", "read,grep,find,ls,bash"])
        self.assertEqual(arguments[-2:], ["--", "Read it"])
        self.assertIn("high", arguments)

    def test_the_prompt_names_the_session_and_forbids_a_submit(self):
        text = pr_reviews.prompt_text(self.record(), self.pr())
        for part in ("s-new", "hunk skill path", f"gh pr diff 7 --repo {REPO}", HEAD, str(self.worktree),
                     "--author Claude", "Do not submit"):
            self.assertIn(part, text)

    def test_start_reviewer_starts_the_agent_and_its_worker(self):
        record = self.record(status=None, reviewed_sha=None)
        result = pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        agent = self.fake.made("herdr", "agent", "start")[0]
        self.assertEqual(agent[3:8], ["pr-api-7", "--kind", "claude", "--pane", "w9:p1"])
        message = agent[agent.index("--") + 1]
        self.assertNotIn("\n", message)
        saved = pr_reviews.records()[KEY]
        self.assertEqual((saved["status"], saved["reviewed_sha"], saved["model"]), ("reviewing", HEAD, "sonnet"))
        self.assertEqual(self.workers[0][0], ["pr-review-wait", KEY])
        self.assertEqual(result["agent_name"], "pr-api-7")

    def test_a_running_agent_of_the_same_kind_gets_the_new_request(self):
        record = self.record(status="reviewed")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        self.assertEqual(self.fake.made("herdr", "agent", "start"), [])
        prompt = self.fake.made("herdr", "agent", "prompt")[0]
        self.assertEqual(prompt[3], "w9:p1")

    def test_a_running_agent_of_another_kind_is_refused(self):
        record = self.record(status="reviewed")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.start_reviewer(KEY, record, self.pr(), "pi", None, None)
        self.assertEqual(raised.exception.code, "agent_running")


class RequestTests(PrReviewTestCase):
    def test_request_opens_the_workspace_hunk_and_the_agent(self):
        self.new_workspace()
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.new_session()
        self.fake.on("gh", "pr", "view", out=pr_json())
        self.fake.on("herdr", "agent", "get", error="agent_not_found")
        result = pr_reviews.request(self.checkout, REPO, 7, "pi", "zai/glm-5.3", "high")
        self.assertEqual((result["key"], result["workspace_id"], result["hunk_pane"]), (KEY, "w9", "w9:p2"))
        agent = self.fake.made("herdr", "agent", "start")[0]
        self.assertIn("--thinking", agent)

    def test_thinking_works_only_with_pi(self):
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.request(self.checkout, REPO, 7, "claude", None, "high")
        self.assertEqual(raised.exception.code, "usage")


class WaitTests(PrReviewTestCase):
    def wait(self):
        self.errors = io.StringIO()
        with contextlib.redirect_stderr(self.errors):
            return pr_reviews.wait(KEY)

    def test_an_agent_that_worked_and_went_idle_is_reviewed(self):
        self.record()
        for state in ("idle", "working", "working", "idle"):
            self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": state}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "1"}, {"noteId": "2"}]})
        self.assertEqual(self.wait(), 0)
        saved = pr_reviews.records()[KEY]
        self.assertEqual((saved["status"], saved["notes"]), ("reviewed", 2))
        listing = self.fake.made("hunk", "session", "comment", "list")[0]
        self.assertEqual(listing[4:7], ["s-new", "--type", "agent"])
        notice = self.fake.made("herdr", "notification", "show")[0]
        self.assertEqual(notice[3], "PR #7 reviewed")
        self.assertIn("2 notes from claude", notice[notice.index("--body") + 1])

    def test_a_timeout_fails(self):
        self.record()
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "working"}})
        with patch.object(pr_reviews, "REVIEW_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 1)
        saved = pr_reviews.records()[KEY]
        self.assertEqual(saved["status"], "failed")
        self.assertIn("did not finish", saved["error"])
        self.assertEqual(self.fake.made("herdr", "notification", "show")[0][3], "PR #7 review failed")

    def test_an_agent_that_is_gone_fails(self):
        self.record()
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "working"}})
        self.fake.on("herdr", "agent", "get", error="agent_not_found")
        self.assertEqual(self.wait(), 1)
        self.assertEqual(pr_reviews.records()[KEY]["status"], "failed")

    def test_a_finished_review_ends_the_worker(self):
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.fake.made("herdr", "agent", "get"), [])


class AnnotateTests(PrReviewTestCase):
    def test_new_commits_after_a_review(self):
        self.record(status="reviewed", notes=3)
        moved = self.pr(headRefOid="b" * 40)
        other = pr_reviews.PullRequest.from_json(pr_json(8), "me")
        pr_reviews.annotate([moved, other], REPO, pr_reviews.records())
        self.assertEqual((moved.status, moved.new_commits, moved.notes), ("reviewed", True, 3))
        self.assertEqual((other.status, other.new_commits), (None, False))

    def test_the_same_head_has_no_new_commits(self):
        self.record(status="reviewed")
        pr = self.pr()
        pr_reviews.annotate([pr], REPO, pr_reviews.records())
        self.assertFalse(pr.new_commits)

    def test_marks(self):
        pr = pr_reviews.PullRequest.from_json(pr_json(isDraft=True, reviewRequests=[{"login": "me"}]), "me")
        pr.status, pr.new_commits = "reviewed", True
        self.assertEqual(pr.marks(), ["draft", "review requested", "reviewed", "new commits"])


class RefreshTests(PrReviewTestCase):
    def setUp(self):
        super().setUp()
        self.worktree.mkdir(parents=True)

    def test_a_dirty_worktree_is_refused(self):
        self.record()
        self.fake.on("git", "-C", str(self.worktree), "status", "--porcelain", out=" M a.py\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.refresh(KEY)
        self.assertEqual(raised.exception.code, "worktree_dirty")
        self.assertEqual(self.fake.made("git", "-C", str(self.worktree), "fetch"), [])

    def test_refresh_moves_the_branch_reopens_hunk_and_applies_the_notes(self):
        self.record()
        notes = [{"noteId": "1", "source": "agent", "filePath": "a.py", "newRange": [3, 3], "body": "Bug here",
                  "author": "Claude"},
                 {"noteId": "2", "source": "user", "filePath": "b.py", "oldRange": [9, 9], "body": "Why?"},
                 {"noteId": "3", "source": "user", "filePath": "gone.py", "newRange": [1, 1], "body": "Old"}]
        self.fake.on("hunk", "session", "comment", "list", out={"comments": notes})
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        applied = []
        self.fake.on("hunk", "session", "comment", "apply", code=1, err="no line",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", out="{}",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", out="{}",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", code=1, err="no line",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        result = pr_reviews.refresh(KEY)
        git = self.fake.made("git", "-C", str(self.worktree))
        self.assertIn(["git", "-C", str(self.worktree), "fetch", "origin", "pull/7/head"], git)
        self.assertIn(["git", "-C", str(self.worktree), "reset", "--keep", "FETCH_HEAD"], git)
        self.assertIn(["herdr", "pane", "close", "w9:p2"], self.fake.made("herdr", "pane", "close"))
        self.assertEqual(len(applied[0]["comments"]), 3)
        self.assertEqual(applied[1]["comments"], [{"filePath": "a.py", "newLine": 3, "summary": "Bug here",
                                                   "author": "Claude"}])
        self.assertEqual(applied[2]["comments"][0]["oldLine"], 9)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["dropped"], ["gone.py:1: Old"])
        self.assertEqual(pr_reviews.records()[KEY]["hunk_session"], "s-two")
        self.assertEqual(self.fake.made("hunk", "session", "comment", "apply")[0][4], "s-two")


class FinishTests(PrReviewTestCase):
    def setUp(self):
        super().setUp()
        self.fake.on("hunk", "session", "list", out={"sessions": [session("s-new", self.worktree)]})

    def test_unsent_notes_are_refused_without_force(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "u1", "source": "user"}]})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "unsent_notes")
        self.assertIn(KEY, pr_reviews.records())
        self.assertEqual(self.fake.made("herdr", "worktree"), [])

    def test_finish_removes_the_workspace_branch_and_record(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        result = pr_reviews.finish(KEY)
        self.assertEqual(self.fake.made("herdr", "pane", "close")[0][3], "w9:p2")
        remove = self.fake.made("herdr", "worktree", "remove")[0]
        self.assertEqual(remove[3:], ["--workspace", "w9", "--trust-repository"])
        self.assertIn(["git", "-C", str(self.checkout), "branch", "-D", "pr/7"], self.fake.made("git"))
        self.assertNotIn(KEY, pr_reviews.records())
        self.assertEqual(result["key"], KEY)

    def test_force_removes_with_notes(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "u1", "source": "user"}]})
        pr_reviews.finish(KEY, force=True)
        self.assertIn("--force", self.fake.made("herdr", "worktree", "remove")[0])

    def test_a_closed_workspace_removes_the_worktree_with_git(self):
        self.worktree.mkdir(parents=True)
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("herdr", "worktree", "remove", error="workspace_not_found")
        pr_reviews.finish(KEY)
        self.assertIn(["git", "-C", str(self.checkout), "worktree", "remove", str(self.worktree)],
                      self.fake.made("git"))

    def test_an_unknown_review(self):
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "not_found")


class GoTests(PrReviewTestCase):
    def test_go_focuses_the_workspace(self):
        self.record()
        self.assertEqual(pr_reviews.go(KEY)["workspace_id"], "w9")
        self.assertEqual(self.fake.made("herdr", "workspace", "focus")[0][3], "w9")

    def test_a_closed_workspace(self):
        self.record()
        self.fake.on("herdr", "workspace", "focus", error="workspace_not_found")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.go(KEY)
        self.assertEqual(raised.exception.code, "workspace_closed")


if __name__ == "__main__":
    unittest.main()
