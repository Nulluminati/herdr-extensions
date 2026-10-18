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
            "baseRefName": "main", "reviewRequests": [], "additions": 12, "deletions": 3, "changedFiles": 2,
            "updatedAt": "2026-10-16T18:00:00Z", "body": "Sessions end too soon."}
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
        self.fake.on("git", "-C", str(self.checkout), "remote", "-v",
                     out="origin\tgit@github.com:acme/api.git (fetch)\norigin\tgit@github.com:acme/api.git (push)\n")
        self.fake.on("gh", "auth", "token", out="secret-token\n")
        # Herdr prints nothing for `pane run` when it works.
        self.fake.on("herdr", "pane", "run", out="")

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


NO_LINE = "hunk: No new diff hunk in gone.py covers line 1.\n"


def search_json(nodes, total=None, end="c2", more=False, login="me"):
    """A GraphQL search reply, with review requests in GraphQL's shape."""
    for node in nodes:
        requests = node.pop("reviewRequests", [])
        node["reviewRequests"] = {"nodes": [{"requestedReviewer": r} for r in requests]}
        node.pop("body", None)
    return {"data": {"viewer": {"login": login}, "search": {
        "issueCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": more, "endCursor": end}, "nodes": nodes}}}


class ListTests(PrReviewTestCase):
    def query(self):
        call = self.fake.made("gh", "api", "graphql")[-1]
        return {arg.split("=", 1)[0]: arg.split("=", 1)[1] for arg in call if "=" in arg}

    def test_a_page_reads_github_search_and_sets_the_markers(self):
        prs = [pr_json(7), pr_json(8, isDraft=True, author={"login": "Me"}),
               pr_json(9, reviewRequests=[{"name": "core"}, {"login": "ME"}])]
        self.fake.on("gh", "api", "graphql", out=search_json(prs, total=120, more=True, end="c3"))
        page = pr_reviews.list_page(self.checkout, REPO)
        self.assertEqual([p.number for p in page.prs], [7, 8, 9])
        self.assertEqual([(p.draft, p.mine, p.requested) for p in page.prs],
                         [(False, False, False), (True, True, False), (False, False, True)])
        self.assertEqual((page.prs[0].head_sha, page.prs[0].author, page.prs[0].base_branch, page.prs[0].additions,
                          page.prs[0].files, page.prs[0].body), (HEAD, "octo", "main", 12, 2, ""))
        self.assertEqual((page.total, page.next_cursor), (120, "c3"))
        query = self.query()
        self.assertEqual(query["q"], f"repo:{REPO} is:pr is:open sort:created-desc")
        self.assertEqual(query["first"], str(pr_reviews.PAGE_SIZE))
        self.assertNotIn("after", query)
        self.assertNotIn("body", query["query"])

    def test_a_page_has_labels_ci_and_the_github_review_decision(self):
        nodes = [pr_json(7, labels={"nodes": [{"name": "bug", "color": "d73a4a"}]}, reviewDecision="APPROVED",
                         commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}),
                 pr_json(8, reviewDecision="CHANGES_REQUESTED",
                         commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]}),
                 pr_json(9, reviewDecision="REVIEW_REQUIRED",
                         commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}),
                 pr_json(10, commits={"nodes": [{"commit": {"statusCheckRollup": None}}]})]
        self.fake.on("gh", "api", "graphql", out=search_json(nodes))
        prs = pr_reviews.list_page(self.checkout, REPO).prs
        self.assertEqual(prs[0].labels, [{"name": "bug", "color": "d73a4a"}])
        self.assertEqual([p.ci for p in prs], ["failed", "pending", "passed", None])
        self.assertEqual([p.decision for p in prs], ["approved", "changes requested", "review required", None])
        query = self.query()["query"]
        for field in ("labels", "reviewDecision", "statusCheckRollup { state }"):
            self.assertIn(field, query)
        self.assertNotIn("contexts", query)

    def test_the_search_text_and_the_cursor_go_to_github(self):
        self.fake.on("gh", "api", "graphql", out=search_json([], end=None))
        page = pr_reviews.list_page(self.checkout, REPO, "author:octo login", after="c2")
        query = self.query()
        self.assertEqual(query["q"], f"repo:{REPO} is:pr is:open sort:created-desc author:octo login")
        self.assertEqual(query["after"], "c2")
        self.assertEqual((page.prs, page.total, page.next_cursor), ([], 0, None))

    def test_a_gh_failure_is_an_error(self):
        self.fake.on("gh", "api", "graphql", code=1, err="HTTP 401")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.list_page(self.checkout, REPO)
        self.assertEqual(raised.exception.code, "gh_failed")
        self.assertIn("HTTP 401", str(raised.exception))

    def test_a_graphql_error_is_an_error(self):
        self.fake.on("gh", "api", "graphql", out={"errors": [{"message": "Bad search"}]})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.list_page(self.checkout, REPO)
        self.assertIn("Bad search", str(raised.exception))

    def test_the_details_are_read_on_their_own(self):
        checks = [{"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                  {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
                  {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": None},
                  {"__typename": "CheckRun", "name": "label", "status": "COMPLETED", "conclusion": "SKIPPED"},
                  {"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR"}]
        self.fake.on("gh", "api", "graphql", out={"data": {"repository": {"pullRequest": {
            "body": "Sessions end too soon.",
            "closingIssuesReferences": {"nodes": [{"number": 3, "title": "Logout", "url": "u3", "state": "OPEN"}]},
            "latestReviews": {"nodes": [{"author": {"login": "octo"}, "state": "APPROVED",
                                         "submittedAt": "2026-10-16T18:00:00Z"},
                                        {"author": None, "state": "COMMENTED", "submittedAt": None}]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE",
                                                                    "contexts": {"nodes": checks}}}}]}}}}})
        details = pr_reviews.pr_details(self.checkout, REPO, 7)
        self.assertEqual(details["body"], "Sessions end too soon.")
        self.assertEqual(details["issues"], [{"number": 3, "title": "Logout", "url": "u3", "state": "OPEN"}])
        self.assertEqual(details["reviews"], [{"author": "octo", "state": "approved", "at": "2026-10-16T18:00:00Z"},
                                              {"author": "ghost", "state": "commented", "at": ""}])
        self.assertEqual(details["checks"], [{"name": "lint", "state": "passed"}, {"name": "test", "state": "failed"},
                                             {"name": "e2e", "state": "pending"}, {"name": "label", "state": "skipped"},
                                             {"name": "ci/legacy", "state": "failed"}])
        query = self.query()
        self.assertEqual((query["owner"], query["name"], query["number"]), ("acme", "api", "7"))

    def test_base_repository(self):
        self.fake.on("gh", "repo", "view", out={"nameWithOwner": REPO})
        self.assertEqual(pr_reviews.base_repository(self.checkout), REPO)


class CommentCountTests(unittest.TestCase):
    def test_the_comment_count_comes_from_the_search_reply(self):
        pr = pr_reviews.PullRequest.from_json(pr_json(comments={"totalCount": 12}), "me")
        self.assertEqual(pr.comments, 12)
        self.assertEqual(pr.to_dict()["comments"], 12)

    def test_a_reply_without_comments_counts_none(self):
        self.assertEqual(pr_reviews.PullRequest.from_json(pr_json(), "me").comments, 0)
        self.assertEqual(pr_reviews.PullRequest.from_json(pr_json(comments=None), "me").comments, 0)

    def test_the_search_asks_github_for_the_comment_count(self):
        self.assertIn("comments { totalCount }", pr_reviews.SEARCH_QUERY)


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
        self.assertIn(["git", "-C", str(self.checkout), "fetch", "origin", "+pull/7/head:refs/heads/pr/7"],
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

    def test_a_kept_branch_of_a_removed_worktree_gets_the_new_head(self):
        self.record(worktree_path=str(self.root / "gone"))
        self.fake.on("git", "-C", str(self.checkout), "rev-parse", "--verify", "--quiet", out="abc\n")
        self.fake.on("herdr", "worktree", "create", result={
            "workspace": {"workspace_id": "w9"}, "root_pane": {"pane_id": "w9:p1"},
            "worktree": {"path": str(self.worktree)}})
        pr_reviews.open_workspace(self.checkout, REPO, 7, "Fix")
        git = self.fake.made("git", "-C", str(self.checkout))
        fetch = git.index(["git", "-C", str(self.checkout), "fetch", "origin", "+pull/7/head:refs/heads/pr/7"])
        self.assertLess(git.index(["git", "-C", str(self.checkout), "worktree", "prune"]), fetch)

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

    def test_a_failed_pane_run_is_an_error(self):
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.fake.on("hunk", "session", "list", out={"sessions": []})
        self.fake.replies.pop(("herdr", "pane", "run"))
        self.fake.on("herdr", "pane", "run", error="pane_not_found")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.open_hunk(self.record(hunk_pane=None, hunk_session=None))
        self.assertEqual(raised.exception.code, "hunk_unavailable")

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

    def test_agent_names_are_valid_herdr_names(self):
        name = pr_reviews._agent_name("acme/" + "a-very-long-repository-name-for-tests", 123456)
        self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertTrue(name.startswith("pr-a-very-long"))
        self.assertTrue(name.endswith("-123456"))
        self.assertRegex(pr_reviews._agent_name("Acme/API", 7), r"^pr-api-[0-9a-f]{4}-7$")

    def test_a_fork_and_its_upstream_get_different_agent_names(self):
        self.assertNotEqual(pr_reviews._agent_name("acme/api", 7), pr_reviews._agent_name("fork/api", 7))
        self.assertEqual(pr_reviews._agent_name("acme/api", 7), pr_reviews._agent_name("Acme/API", 7))

    def test_a_failed_agent_start_saves_failed(self):
        record = self.record(status=None)
        self.fake.on("herdr", "agent", "start", error="agent_start_failed")
        with self.assertRaises(start.StartError):
            pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        saved = pr_reviews.records()[KEY]
        self.assertEqual(saved["status"], "failed")
        self.assertIn("agent_start_failed", saved["error"])
        self.assertEqual(self.workers, [])

    def test_the_request_remembers_the_notes_that_were_there_before(self):
        record = self.record(status=None)
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "old-1"}, {"noteId": "old-2"}]})
        pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        self.assertEqual(pr_reviews.records()[KEY]["known_notes"], ["old-1", "old-2"])
        listing = self.fake.made("hunk", "session", "comment", "list")[0]
        self.assertEqual(listing[4:7], ["s-new", "--type", "agent"])

    def test_a_running_agent_of_the_same_kind_gets_the_new_request(self):
        record = self.record(status="reviewed")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        self.assertEqual(self.fake.made("herdr", "agent", "start"), [])
        prompt = self.fake.made("herdr", "agent", "prompt")[0]
        self.assertEqual(prompt[3], "w9:p1")

    def test_a_running_pi_without_its_name_is_found_by_its_pane(self):
        record = self.record(status="reviewed", agent="pi", model=None)
        self.fake.on("herdr", "agent", "get", "pr-api-7", error="agent_not_found")
        self.fake.on("herdr", "agent", "get", "w9:p1", result={"agent": {"agent_status": "done"}})
        pr_reviews.start_reviewer(KEY, record, self.pr(), "pi", None, None)
        self.assertEqual(self.fake.made("herdr", "agent", "start"), [])
        self.assertEqual(len(self.fake.made("herdr", "agent", "prompt")), 1)

    def test_a_running_agent_with_another_model_is_refused(self):
        record = self.record(status="reviewed")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "opus", None)
        self.assertEqual(raised.exception.code, "agent_running")
        self.assertIn("sonnet", str(raised.exception))
        self.assertEqual(self.fake.made("herdr", "agent", "prompt"), [])

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

    def test_a_kept_worktree_behind_the_pr_head_is_refused(self):
        self.worktree.mkdir(parents=True)
        self.record(status="reviewed", reviewed_sha="b" * 40)
        self.fake.on("gh", "pr", "view", out=pr_json())
        self.fake.on("herdr", "workspace", "get", result={"workspace_id": "w9", "root_pane": {"pane_id": "w9:p1"}})
        self.fake.on("git", "-C", str(self.worktree), "rev-parse", "HEAD", out="b" * 40 + "\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.request(self.checkout, REPO, 7, "claude", "sonnet", None)
        self.assertEqual(raised.exception.code, "new_commits")
        self.assertIn("pr-review-refresh 7", str(raised.exception))
        self.assertEqual(self.fake.made("herdr", "agent", "start"), [])
        self.assertEqual(pr_reviews.records()[KEY]["reviewed_sha"], "b" * 40)

    def test_a_kept_worktree_at_the_pr_head_is_reviewed_again(self):
        self.worktree.mkdir(parents=True)
        self.record(status="reviewed")
        self.fake.on("gh", "pr", "view", out=pr_json())
        self.fake.on("herdr", "workspace", "get", result={"workspace_id": "w9", "root_pane": {"pane_id": "w9:p1"}})
        self.fake.on("git", "-C", str(self.worktree), "rev-parse", "HEAD", out=HEAD + "\n")
        self.fake.on("hunk", "session", "list", out={"sessions": [session("s-new", self.worktree)]})
        self.fake.on("herdr", "agent", "get", error="agent_not_found")
        self.assertEqual(pr_reviews.request(self.checkout, REPO, 7, "claude", "sonnet", None)["key"], KEY)

    def test_saved_notes_come_back_when_a_review_opens_hunk_again(self):
        self.worktree.mkdir(parents=True)
        self.record(status="failed", hunk_pane=None, hunk_session=None)
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 3, "summary": "Bug"}])
        self.fake.on("gh", "pr", "view", out=pr_json())
        self.fake.on("herdr", "workspace", "get", result={"workspace_id": "w9", "root_pane": {"pane_id": "w9:p1"}})
        self.fake.on("git", "-C", str(self.worktree), "rev-parse", "HEAD", out=HEAD + "\n")
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.new_session()
        self.fake.on("herdr", "agent", "get", error="agent_not_found")
        pr_reviews.request(self.checkout, REPO, 7, "claude", "sonnet", None)
        apply = self.fake.options("hunk", "session", "comment", "apply")
        self.assertEqual(json.loads(apply[0]["input"])["comments"][0]["summary"], "Bug")
        self.assertIsNone(pr_reviews._saved_notes(KEY))

    def test_saved_notes_stay_when_hunk_fails_to_apply_them(self):
        self.worktree.mkdir(parents=True)
        self.record(status="failed", hunk_pane=None, hunk_session=None)
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 3, "summary": "Bug"}])
        self.fake.on("gh", "pr", "view", out=pr_json())
        self.fake.on("herdr", "workspace", "get", result={"workspace_id": "w9", "root_pane": {"pane_id": "w9:p1"}})
        self.fake.on("git", "-C", str(self.worktree), "rev-parse", "HEAD", out=HEAD + "\n")
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p2"}})
        self.new_session()
        self.fake.on("hunk", "session", "comment", "apply", code=1, err="hunk: The targeted session disconnected.\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.request(self.checkout, REPO, 7, "claude", "sonnet", None)
        self.assertEqual(raised.exception.code, "hunk_failed")
        self.assertEqual(pr_reviews._saved_notes(KEY), [{"filePath": "a.py", "newLine": 3, "summary": "Bug"}])
        self.assertEqual(self.fake.made("herdr", "agent", "start"), [])

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
        self.assertIn("2 new notes from claude", notice[notice.index("--body") + 1])

    def test_a_replaced_request_ends_the_worker_quietly(self):
        self.record(review_id=1)
        replace = lambda argv, options: pr_reviews.save(KEY, {**pr_reviews.records()[KEY], "review_id": 2})
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "working"}}, then=replace)
        self.assertEqual(self.wait(), 0)
        self.assertEqual(pr_reviews.records()[KEY]["status"], "reviewing")
        self.assertEqual(self.fake.made("herdr", "notification", "show"), [])

    def test_start_reviewer_gives_each_request_an_id(self):
        record = self.record(status=None)
        pr_reviews.start_reviewer(KEY, record, self.pr(), "claude", "sonnet", None)
        first = pr_reviews.records()[KEY]["review_id"]
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        pr_reviews.start_reviewer(KEY, pr_reviews.records()[KEY], self.pr(), "claude", "sonnet", None)
        self.assertNotEqual(pr_reviews.records()[KEY]["review_id"], first)

    def test_a_pi_agent_without_its_name_is_watched_in_its_pane(self):
        self.record(agent="pi")
        self.fake.on("herdr", "agent", "get", "pr-api-7", error="agent_not_found")
        for state in ("working", "done"):
            self.fake.on("herdr", "agent", "get", "w9:p1", result={"agent": {"agent_status": state}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "1"}]})
        self.assertEqual(self.wait(), 0)
        self.assertEqual(pr_reviews.records()[KEY]["status"], "reviewed")

    def test_done_ends_the_wait_also_when_working_was_not_seen(self):
        self.record()
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "done"}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        with patch.object(pr_reviews, "REVIEW_TIMEOUT_SECONDS", 5):
            self.assertEqual(self.wait(), 0)
        self.assertEqual(pr_reviews.records()[KEY]["notes"], 0)

    def test_a_prompted_agent_must_work_first(self):
        self.record(prompted=True)
        for state in ("done", "working", "done"):
            self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": state}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.assertEqual(self.wait(), 0)
        self.assertEqual(len(self.fake.made("herdr", "agent", "get")), 3)

    def test_only_the_notes_of_this_review_count(self):
        self.record(known_notes=["old-1"])
        for state in ("working", "idle"):
            self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": state}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [{"noteId": "old-1"}, {"noteId": "n1"}]})
        self.assertEqual(self.wait(), 0)
        self.assertEqual(pr_reviews.records()[KEY]["notes"], 1)
        notice = self.fake.made("herdr", "notification", "show")[0]
        self.assertIn("1 new notes from claude", notice[notice.index("--body") + 1])

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
        self.assertEqual((moved.review["agent"], moved.review["workspace_id"]), ("claude", "w9"))
        self.assertEqual((other.status, other.new_commits), (None, False))

    def test_the_same_head_has_no_new_commits(self):
        self.record(status="reviewed")
        pr = self.pr()
        pr_reviews.annotate([pr], REPO, pr_reviews.records())
        self.assertFalse(pr.new_commits)

    def test_marks(self):
        pr = pr_reviews.PullRequest.from_json(pr_json(isDraft=True, reviewRequests=[{"login": "me"}]), "me")
        pr.status, pr.new_commits = "reviewed", True
        pr.ci, pr.decision = "failed", "approved"
        self.assertEqual(pr.marks(), ["draft", "review requested", "CI failed", "GitHub: approved",
                                      "agent: reviewed", "new commits"])


class RefreshTests(PrReviewTestCase):
    def setUp(self):
        super().setUp()
        self.worktree.mkdir(parents=True)

    def test_a_dirty_worktree_is_refused(self):
        self.record(status="reviewed")
        self.fake.on("git", "-C", str(self.worktree), "status", "--porcelain", out=" M a.py\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.refresh(KEY)
        self.assertEqual(raised.exception.code, "worktree_dirty")
        self.assertEqual(self.fake.made("git", "-C", str(self.worktree), "fetch"), [])

    def test_refresh_moves_the_branch_reopens_hunk_and_applies_the_notes(self):
        self.record(status="reviewed")
        notes = [{"noteId": "1", "source": "agent", "filePath": "a.py", "newRange": [3, 3], "body": "Bug here",
                  "author": "Claude"},
                 {"noteId": "2", "source": "user", "filePath": "b.py", "oldRange": [9, 9], "body": "Why?"},
                 {"noteId": "3", "source": "user", "filePath": "gone.py", "newRange": [1, 1], "body": "Old\n\nThe rationale."}]
        self.fake.on("hunk", "session", "comment", "list", out={"comments": notes})
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        applied = []
        self.fake.on("hunk", "session", "comment", "apply", code=1, err=NO_LINE,
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", out="{}",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", out="{}",
                     then=lambda argv, options: applied.append(json.loads(options["input"])))
        self.fake.on("hunk", "session", "comment", "apply", code=1, err=NO_LINE,
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
        # Hunk restores every note as an agent note, so the record keeps which ones were the user's.
        self.assertEqual(pr_reviews.records()[KEY]["user_notes"],
                         [{"filePath": "b.py", "oldLine": 9, "summary": "Why?"}])

    def test_refresh_is_refused_while_the_reviewer_works(self):
        self.record(status="reviewing")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "working"}})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.refresh(KEY)
        self.assertEqual(raised.exception.code, "agent_running")
        self.assertEqual(self.fake.made("git", "-C", str(self.worktree), "fetch"), [])
        self.assertEqual(self.fake.made("herdr", "pane", "close"), [])

    def test_a_stale_reviewing_status_does_not_stop_the_refresh(self):
        # The worker died, so nothing moves the status on.
        self.record(status="reviewing")
        self.fake.on("herdr", "agent", "get", result={"agent": {"agent_status": "idle"}})
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        pr_reviews.refresh(KEY)
        record = pr_reviews.records()[KEY]
        self.assertEqual(record["hunk_session"], "s-two")
        self.assertEqual(record["status"], "failed")
        self.assertIn("stopped", record["error"])

    def test_a_reviewer_that_no_longer_runs_does_not_stop_the_refresh(self):
        self.record(status="reviewing")
        self.fake.on("herdr", "agent", "get", error="agent_not_found")
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        pr_reviews.refresh(KEY)
        self.assertEqual(pr_reviews.records()[KEY]["status"], "failed")

    def test_a_hunk_error_keeps_the_notes_that_did_not_apply(self):
        self.record(status="reviewed", user_notes=[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [
            {"noteId": "1", "source": "user", "filePath": "a.py", "newRange": [3, 3], "body": "Mine"},
            {"noteId": "2", "source": "agent", "filePath": "gone.py", "newRange": [1, 1], "body": "Old"}]})
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        busy = "hunk: The targeted session could not receive the command.\n"
        self.fake.on("hunk", "session", "comment", "apply", code=1, err=busy)
        self.fake.on("hunk", "session", "comment", "apply", code=1, err=busy)
        self.fake.on("hunk", "session", "comment", "apply", code=1, err=NO_LINE)
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.refresh(KEY)
        self.assertEqual(raised.exception.code, "hunk_failed")
        self.assertIn("could not receive the command", str(raised.exception))
        # Only the note that Hunk could not take stays; the one on no diff line is gone.
        self.assertEqual(pr_reviews._saved_notes(KEY), [{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        record = pr_reviews.records()[KEY]
        self.assertEqual(record["hunk_session"], "s-two")
        self.assertEqual(record["user_notes"], [{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])


class RefreshFailureTests(PrReviewTestCase):
    def setUp(self):
        super().setUp()
        self.worktree.mkdir(parents=True)
        self.notes = [{"noteId": "1", "source": "user", "filePath": "a.py", "newRange": [3, 3], "body": "Mine"}]

    def test_a_failed_hunk_keeps_the_notes_for_the_next_refresh(self):
        self.record(status="reviewed")
        self.fake.on("hunk", "session", "comment", "list", out={"comments": self.notes})
        self.fake.on("hunk", "session", "list", out={"sessions": []})
        self.fake.on("herdr", "pane", "split", error="pane_not_found")
        with self.assertRaises(start.StartError):
            pr_reviews.refresh(KEY)
        self.assertEqual(pr_reviews._saved_notes(KEY), [{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        # The old session is gone now, so only the saved file holds the notes.
        self.fake.replies.pop(("hunk", "session", "comment", "list"))
        self.fake.on("hunk", "session", "comment", "list", code=1, err="no session")
        self.fake.replies.pop(("herdr", "pane", "split"))
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        result = pr_reviews.refresh(KEY)
        self.assertEqual(result["kept"], 1)
        apply = self.fake.options("hunk", "session", "comment", "apply")
        self.assertEqual(json.loads(apply[0]["input"])["comments"][0]["summary"], "Mine")
        self.assertIsNone(pr_reviews._saved_notes(KEY))

    def test_a_failed_note_list_of_a_live_session_stops_the_refresh(self):
        self.record(status="reviewed")
        self.fake.on("hunk", "session", "comment", "list", code=1, err="timeout")
        self.fake.on("hunk", "session", "list", out={"sessions": [session("s-new", self.worktree)]})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.refresh(KEY)
        self.assertEqual(raised.exception.code, "hunk_failed")
        self.assertEqual(self.fake.made("herdr", "pane", "close"), [])
        self.assertEqual(pr_reviews.records()[KEY]["hunk_session"], "s-new")

    def test_a_failed_note_list_when_hunk_cannot_list_sessions_stops_the_refresh(self):
        self.record(status="reviewed")
        self.fake.on("hunk", "session", "comment", "list", code=1, err="timeout")
        self.fake.on("hunk", "session", "list", code=1, err="timeout")
        with self.assertRaises(pr_reviews.PrReviewError):
            pr_reviews.refresh(KEY)
        self.assertEqual(self.fake.made("herdr", "pane", "close"), [])

    def test_the_notes_are_saved_before_the_pane_closes(self):
        self.record(status="reviewed")
        self.fake.on("hunk", "session", "comment", "list", out={"comments": self.notes})
        saved_at_close = []
        self.fake.on("herdr", "pane", "close", result={"type": "ok"},
                     then=lambda argv, options: saved_at_close.append(pr_reviews._saved_notes(KEY)))
        self.fake.on("herdr", "pane", "split", result={"pane": {"pane_id": "w9:p3"}})
        self.new_session("s-two")
        pr_reviews.refresh(KEY)
        self.assertEqual(saved_at_close, [[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}]])


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

    def test_a_refused_removal_keeps_hunk_and_the_review(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("herdr", "worktree", "remove", error="worktree_dirty")
        with self.assertRaises(start.StartError):
            pr_reviews.finish(KEY)
        self.assertEqual(self.fake.made("herdr", "pane", "close"), [])
        self.assertIn(KEY, pr_reviews.records())

    def test_hunk_closes_after_the_worktree_is_removed(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        pr_reviews.finish(KEY)
        herdr = [argv[1:3] for argv, _ in self.fake.calls if argv[0] == "herdr"]
        self.assertLess(herdr.index(["worktree", "remove"]), herdr.index(["pane", "close"]))

    def test_user_notes_that_a_refresh_restored_stop_finish(self):
        self.record(user_notes=[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        self.fake.on("hunk", "session", "comment", "list", out={"comments": [
            {"noteId": "9", "source": "agent", "filePath": "a.py", "newRange": [3, 3], "body": "Mine"},
            {"noteId": "8", "source": "agent", "filePath": "a.py", "newRange": [5, 5], "body": "Agent note",
             "author": "Claude"}]})
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "unsent_notes")
        self.assertIn("1 notes of yours", str(raised.exception))
        self.assertIn(KEY, pr_reviews.records())

    def test_user_notes_saved_by_a_failed_refresh_stop_finish(self):
        # A refresh that failed after it closed Hunk left the notes only in the file.
        self.record(hunk_pane=None, hunk_session=None,
                    user_notes=[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 3, "summary": "Mine"},
                                     {"filePath": "a.py", "newLine": 5, "summary": "Agent note"}])
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "unsent_notes")
        self.assertIn("1 notes of yours", str(raised.exception))
        self.assertIn(KEY, pr_reviews.records())
        self.assertIsNotNone(pr_reviews._saved_notes(KEY))
        self.assertEqual(self.fake.made("herdr", "worktree"), [])

    def test_user_notes_that_hunk_refused_stop_finish_while_hunk_is_open(self):
        self.record(user_notes=[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "unsent_notes")
        self.assertIn("1 notes of yours", str(raised.exception))

    def test_saved_agent_notes_do_not_stop_finish(self):
        self.record(hunk_pane=None, hunk_session=None)
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 5, "summary": "Agent note"}])
        pr_reviews.finish(KEY)
        self.assertNotIn(KEY, pr_reviews.records())
        self.assertIsNone(pr_reviews._saved_notes(KEY))

    def test_force_removes_saved_user_notes(self):
        self.record(hunk_pane=None, hunk_session=None,
                    user_notes=[{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        pr_reviews._save_notes(KEY, [{"filePath": "a.py", "newLine": 3, "summary": "Mine"}])
        pr_reviews.finish(KEY, force=True)
        self.assertNotIn(KEY, pr_reviews.records())
        self.assertIsNone(pr_reviews._saved_notes(KEY))

    def test_a_branch_that_git_does_not_delete_keeps_the_review(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("git", "-C", str(self.checkout), "branch", "-D", code=1, err="error: branch is locked")
        self.fake.on("git", "-C", str(self.checkout), "rev-parse", "--verify", "--quiet", out="abc\n")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "git_failed")
        self.assertIn("branch is locked", str(raised.exception))
        record = pr_reviews.records()[KEY]
        self.assertIsNone(record["workspace_id"])
        self.assertIsNone(record["hunk_pane"])
        # A second finish has only the branch left to delete.
        self.fake.replies.pop(("git", "-C", str(self.checkout), "branch", "-D"))
        removes = len(self.fake.made("herdr", "worktree", "remove"))
        result = pr_reviews.finish(KEY)
        self.assertTrue(result["branch_deleted"])
        self.assertNotIn(KEY, pr_reviews.records())
        self.assertEqual(len(self.fake.made("herdr", "worktree", "remove")), removes)

    def test_a_branch_that_is_already_gone_finishes(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("git", "-C", str(self.checkout), "branch", "-D", code=1, err="error: branch not found")
        self.fake.on("git", "-C", str(self.checkout), "rev-parse", "--verify", "--quiet", code=1)
        result = pr_reviews.finish(KEY)
        self.assertFalse(result["branch_deleted"])
        self.assertNotIn(KEY, pr_reviews.records())

    def test_a_dirty_worktree_stops_finish_with_worktree_dirty(self):
        self.record()
        self.fake.on("hunk", "session", "comment", "list", out={"comments": []})
        self.fake.on("herdr", "worktree", "remove", error="dirty_worktree_requires_force")
        with self.assertRaises(pr_reviews.PrReviewError) as raised:
            pr_reviews.finish(KEY)
        self.assertEqual(raised.exception.code, "worktree_dirty")
        self.assertIn("finish with force", str(raised.exception))
        self.assertIn(KEY, pr_reviews.records())
        self.assertEqual(self.fake.made("herdr", "pane", "close"), [])

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
