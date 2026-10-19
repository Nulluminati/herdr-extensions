"""Merge a card branch into its base with a rebase and a fast-forward, then finish the card."""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import activity, merge, pr_write, start  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_review import PaneHerdr, ReviewTestCase  # noqa: E402

IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}


class GitHerdr(PaneHerdr):
    """PaneHerdr whose worktrees are real Git worktrees, removed the way Herdr removes them."""

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "worktree remove":
            self.calls.append(args)
            workspace = args[args.index("--workspace") + 1]
            entry = next((w for w in self.worktrees if w["open_workspace_id"] == workspace), None)
            if entry is None:
                return self.reply(argv, error="workspace_not_found")
            removed = subprocess.run(["git", "-C", entry["path"], "worktree", "remove", entry["path"]],
                                     capture_output=True)
            if removed.returncode:
                return self.reply(argv, error="dirty_worktree_requires_force")
            self.worktrees.remove(entry)
            self.panes.pop(workspace, None)
            self.agent = False
            return self.reply(argv, {"type": "worktree_removed", "workspace_id": workspace})
        if operation == "agent prompt":
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        result = super().__call__(argv, timeout)
        if operation == "worktree create":
            path = json.loads(result.stdout)["result"]["worktree"]["path"]
            branch = args[args.index("--branch") + 1]
            cwd = args[args.index("--cwd") + 1]
            subprocess.run(["git", "-C", cwd, "worktree", "add", path, branch], check=True, capture_output=True)
        return result


class MergeTestCase(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, IDENTITY))
        self.fake.__class__ = GitHerdr
        self.merger = merge.Merger(self.store, self.repo)

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, directory, name, text, message):
        (Path(directory) / name).write_text(text)
        self.git(directory, "add", name)
        self.git(directory, "commit", "-qm", message)
        return self.git(directory, "rev-parse", "HEAD")

    def started(self, base=None):
        result = self.starter.start(self.card.ref, agent="pi", base=base)
        self.worktree = Path(result["worktree_path"])
        self.branch = result["branch"]
        return result

    def main_branch(self):
        return self.git(self.repo, "symbolic-ref", "--short", "HEAD")

    def branch_exists(self, branch):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], capture_output=True).returncode == 0

    def records(self):
        path = start.state_dir() / "starts.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def log(self):
        return [e["message"] for e in activity.read(self.config, self.card.ref)]


class MergeLocalTests(MergeTestCase):
    def test_a_card_that_was_never_started_cannot_merge(self):
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "not_started")

    def test_uncommitted_work_stops_the_merge(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        (self.worktree / "b.txt").write_text("not committed\n")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "worktree_dirty")

    def test_a_branch_without_commits_has_nothing_to_merge(self):
        self.started()
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "nothing_to_merge")

    def test_merge_fast_forwards_the_checked_out_base_and_finishes(self):
        self.started()
        head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.merger.merge(self.card.ref)
        main = self.main_branch()
        self.assertEqual(self.git(self.repo, "rev-parse", main), head)
        self.assertEqual((self.repo / "a.txt").read_text(), "a\n")
        self.assertEqual(result, {"ref": self.card.ref, "mode": "local", "base": main, "commit": head,
                                  "worktree_removed": True, "branch_deleted": True})
        self.assertFalse(self.worktree.exists())
        self.assertFalse(self.branch_exists(self.branch))
        card = self.store.show(self.card.ref)
        self.assertEqual((card.column, card.checked), ("Done", True))
        self.assertEqual(self.log(), [f"Merged into {main} at {head[:7]}.", "Finished."])
        self.assertEqual(self.records(), {})

    def test_merge_rebases_onto_a_base_that_moved(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        moved = self.commit(self.repo, "other.txt", "other\n", "feat: other")
        result = self.merger.merge(self.card.ref)
        main = self.main_branch()
        self.assertEqual(self.git(self.repo, "rev-parse", f"{main}~1"), moved)
        self.assertEqual(result["commit"], self.git(self.repo, "rev-parse", main))
        self.assertTrue((self.repo / "a.txt").exists())

    def test_a_base_that_no_checkout_has_moves_without_a_checkout(self):
        self.git(self.repo, "branch", "feature-x")
        self.started(base="feature-x")
        head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.merger.merge(self.card.ref)
        self.assertEqual(self.git(self.repo, "rev-parse", "feature-x"), head)
        self.assertFalse((self.repo / "a.txt").exists())

    def test_a_dirty_base_checkout_stops_before_the_rebase(self):
        self.started()
        self.commit(self.repo, "shared.txt", "one\n", "feat: shared")
        self.git(self.worktree, "rebase", "-q", self.main_branch())
        head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        (self.repo / "shared.txt").write_text("changed by the user\n")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "base_dirty")
        self.assertEqual(self.git(self.worktree, "rev-parse", "HEAD"), head)

    def test_a_failed_fast_forward_changes_nothing_on_the_card(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        (self.repo / "a.txt").write_text("untracked in the base checkout\n")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "merge_failed")
        self.assertTrue(self.worktree.exists())
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")


class OldRecordTests(MergeTestCase):
    def drop_base(self):
        path = start.state_dir() / "starts.json"
        records = json.loads(path.read_text())
        del records[self.card.ref]["base"]
        path.write_text(json.dumps(records))

    def test_a_record_without_a_base_uses_the_branch_upstream(self):
        self.started()
        head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.drop_base()
        self.assertEqual(self.merger.merge(self.card.ref)["base"], self.main_branch())
        self.assertEqual(self.git(self.repo, "rev-parse", self.main_branch()), head)

    def test_without_a_base_or_an_upstream_the_base_is_unknown(self):
        self.started()
        self.drop_base()
        self.git(self.repo, "branch", "--unset-upstream", self.branch)
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.finish(self.card.ref)
        self.assertEqual(raised.exception.code, "base_unknown")


class ConflictTests(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.commit(self.repo, "shared.txt", "one\n", "feat: shared")
        self.started()
        self.store.move(self.card.ref, "Review")
        self.head = self.commit(self.worktree, "shared.txt", "card\n", "feat: card change")
        self.main_head = self.commit(self.repo, "shared.txt", "main\n", "feat: main change")

    def test_a_conflict_goes_back_to_the_agent(self):
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("rebase_conflict", 2))
        self.assertIn("shared.txt", str(raised.exception))
        self.assertEqual(self.git(self.worktree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git(self.worktree, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.main_head)
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2], "w9:p1")
        self.assertIn("shared.txt", prompts[0][3])
        self.assertIn(f"git rebase {self.main_branch()}", prompts[0][3])
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")
        self.assertEqual(self.log(), ["Rebase conflict in shared.txt. Sent to the agent."])

    def test_a_conflict_starts_a_new_agent_when_the_old_one_is_gone(self):
        self.fake.agent = False
        with self.assertRaises(merge.MergeError):
            self.merger.merge(self.card.ref)
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertEqual(len(self.calls("agent", "start")), 2)
        brief = (start.state_dir() / "briefs" / f"{self.card.ref}.md").read_text()
        self.assertIn("shared.txt", brief)


class FinishTests(MergeTestCase):
    def test_an_unmerged_branch_does_not_finish(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.finish(self.card.ref)
        self.assertEqual(raised.exception.code, "not_merged")
        self.assertTrue(self.worktree.exists())

    def test_force_finishes_but_keeps_an_unmerged_branch(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.merger.finish(self.card.ref, force=True)
        self.assertEqual((result["worktree_removed"], result["branch_deleted"]), (True, False))
        self.assertTrue(self.branch_exists(self.branch))
        self.assertIn(f"Branch {self.branch} kept: it is not merged.", self.log())
        self.assertEqual(self.store.show(self.card.ref).column, "Done")

    def test_uncommitted_work_stops_finish_even_with_force(self):
        self.started()
        (self.worktree / "b.txt").write_text("not committed\n")
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(merge.MergeError) as raised:
                self.merger.finish(self.card.ref, force=force)
            self.assertEqual(raised.exception.code, "worktree_dirty")
        self.assertTrue((self.worktree / "b.txt").exists())

    def test_finish_closes_the_review_pane(self):
        self.started()
        self.store.move(self.card.ref, "Review")
        from herdboard import review
        review.Reviewer(self.store, self.repo).review(self.card.ref)
        self.merger.finish(self.card.ref, force=True)
        self.assertEqual(self.calls("plugin", "pane", "close"), [["plugin", "pane", "close", "w9:p2:hunk"]])

    def test_a_worktree_that_is_already_gone_still_finishes(self):
        self.started()
        self.git(self.repo, "worktree", "remove", str(self.worktree))
        result = self.merger.finish(self.card.ref)
        self.assertEqual((result["worktree_removed"], result["branch_deleted"]), (False, True))
        self.assertEqual(self.calls("worktree", "remove"), [])
        self.assertEqual(self.store.show(self.card.ref).column, "Done")

    def test_a_card_already_in_done_stays_there(self):
        self.started()
        self.store.done(self.card.ref)
        self.merger.finish(self.card.ref)
        card = self.store.show(self.card.ref)
        self.assertEqual((card.column, card.checked), ("Done", True))


class FakeGh:
    """`gh` for pull requests: list, create, edit and view, with the URL of one pull request."""

    def __init__(self):
        self.calls = []
        self.existing = []
        self.state = "OPEN"
        self.missing = False
        self.url = "https://github.com/acme/api/pull/7"
        self.title = "fix: keep sessions for 30 minutes"
        self.body = "## Why\nSessions ended too soon.\n"
        # The text of each --body-file, read when gh runs.
        self.bodies = []

    def __call__(self, directory, *args):
        if self.missing:
            raise merge.MergeError("gh is not installed", "gh_unavailable")
        self.calls.append(list(args))
        if "--body-file" in args:
            self.bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        if args[:2] == ("pr", "list"):
            out = json.dumps([{"url": url} for url in self.existing])
        elif args[:2] == ("pr", "create"):
            out = self.url + "\n"
        elif args[:2] == ("pr", "edit"):
            out = self.url + "\n"
        elif args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
            if "title" in fields or "body" in fields:
                out = json.dumps({"title": self.title, "body": self.body})
            else:
                out = json.dumps({"state": self.state})
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(["gh", *args], 0, out, "")


class PullRequestTestCase(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, capture_output=True)
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "origin", self.main_branch())
        self.gh = FakeGh()
        self.enterContext(patch.object(merge, "_gh", self.gh))
        self.workers = []
        self.enterContext(patch.object(pr_write, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))

    def remote_branches(self):
        return self.git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")


class PullRequestTests(PullRequestTestCase):
    def test_merge_pr_asks_for_a_description_and_pushes_nothing_yet(self):
        self.started()
        self.store.move(self.card.ref, "Review")
        head = self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        result = self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual((result["mode"], result["pr_action"], result["pr_url"]), ("pr", "writing", None))
        self.assertEqual(result["writer"], "the card's agent")
        self.assertEqual(self.remote_branches(), self.main_branch())
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.gh.calls))
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self.worktree.exists())
        self.assertEqual(self.store.show(self.card.ref).column, "Review")
        self.assertNotEqual(self.git(self.repo, "rev-parse", self.main_branch()), head)

    def test_an_open_pull_request_is_reused_and_updated(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.gh.existing = ["https://github.com/acme/api/pull/3"]
        result = self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual((result["pr_url"], result["pr_action"]), ("https://github.com/acme/api/pull/3", "pushed"))
        self.assertEqual(self.log(), ["PR updated: https://github.com/acme/api/pull/3"])
        record = self.records()[self.card.ref]
        self.assertEqual((record["pr_url"], record["pr_state"]), ("https://github.com/acme/api/pull/3", "open"))
        head = self.commit(self.worktree, "b.txt", "b\n", "feat: b")
        self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual(self.git(self.origin, "rev-parse", self.branch), head)
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", self.branch + "@{u}"), self.main_branch())
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in self.gh.calls))
        self.assertEqual(self.workers, [])

    def test_without_gh_the_pull_request_cannot_open(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.gh.missing = True
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual(raised.exception.code, "gh_unavailable")

    def test_a_failed_push_to_an_open_pull_request_stops(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.gh.existing = ["https://github.com/acme/api/pull/3"]
        self.git(self.repo, "remote", "remove", "origin")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref, mode="pr")
        self.assertEqual(raised.exception.code, "push_failed")
        self.assertEqual(self.log(), [])

    def test_rewrite_works_only_for_a_pull_request(self):
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref, mode="local", rewrite=True)
        self.assertEqual(raised.exception.code, "usage")

    def test_the_board_merge_setting_picks_the_pull_request(self):
        text = self.board.read_text(encoding="utf-8")
        self.board.write_text(text.replace("kanban-plugin: board\n", "kanban-plugin: board\nmerge: pr\n", 1),
                              encoding="utf-8")
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.assertEqual(self.merger.merge(self.card.ref)["mode"], "pr")


class PullRequestFinishTests(PullRequestTestCase):
    def setUp(self):
        super().setUp()
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.gh.existing = [self.gh.url]
        self.merger.merge(self.card.ref, mode="pr")

    def test_an_open_pull_request_does_not_finish(self):
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.finish(self.card.ref)
        self.assertEqual(raised.exception.code, "not_merged")
        self.assertIn(self.gh.url, str(raised.exception))

    def test_a_merged_pull_request_finishes_and_deletes_both_branches(self):
        self.gh.state = "MERGED"
        result = self.merger.finish(self.card.ref)
        self.assertEqual((result["worktree_removed"], result["branch_deleted"]), (True, True))
        self.assertFalse(self.branch_exists(self.branch))
        self.assertNotIn(self.branch, self.remote_branches().split())
        self.assertEqual(self.store.show(self.card.ref).column, "Done")
        self.assertEqual(self.log()[-1], "Finished.")

    def test_a_remote_branch_that_github_deleted_is_fine(self):
        self.gh.state = "MERGED"
        self.git(self.origin, "branch", "-D", self.branch)
        self.merger.finish(self.card.ref)
        self.assertFalse(self.branch_exists(self.branch))


class PullRequestStateTests(PullRequestTestCase):
    def setUp(self):
        super().setUp()
        self.started()
        self.commit(self.worktree, "a.txt", "a\n", "feat: a")
        self.gh.existing = [self.gh.url]
        self.merger.merge(self.card.ref, mode="pr")
        self.gh.calls.clear()

    def age_check(self, seconds):
        path = start.state_dir() / "starts.json"
        records = json.loads(path.read_text())
        records[self.card.ref]["pr_checked"] -= seconds
        path.write_text(json.dumps(records))

    def test_states_are_read_at_most_once_a_minute(self):
        self.gh.state = "MERGED"
        merge.refresh_pr_states()
        self.assertEqual(self.gh.calls, [])
        self.age_check(61)
        merge.refresh_pr_states()
        self.assertEqual(len(self.gh.calls), 1)
        self.assertEqual(self.records()[self.card.ref]["pr_state"], "merged")
        self.age_check(61)
        merge.refresh_pr_states()
        self.assertEqual(len(self.gh.calls), 1)

    def test_without_gh_the_state_stays(self):
        self.gh.missing = True
        self.age_check(61)
        merge.refresh_pr_states()
        self.assertEqual(self.records()[self.card.ref]["pr_state"], "open")

    def test_the_board_card_shows_the_state_and_a_left_worktree(self):
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertEqual((card.pr_state, card.worktree_left, card.pr_writing), ("open", False, False))
        done = self.store.done(self.card.ref)
        start.mark_removed([done])
        self.assertTrue(done.worktree_left)


    def test_the_board_card_knows_its_started_agent_while_the_worktree_exists(self):
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertEqual(card.agent, "pi")
        shutil.rmtree(self.worktree)
        start.mark_removed([card])
        self.assertIsNone(card.agent)


class MergeModeTests(MergeTestCase):

    def set_board_merge(self, value):
        text = self.board.read_text(encoding="utf-8")
        self.board.write_text(text.replace("kanban-plugin: board\n", f"kanban-plugin: board\nmerge: {value}\n", 1),
                              encoding="utf-8")

    def test_the_mode_comes_from_the_board_then_the_config(self):
        record = {"repository": "x"}
        card = self.store.show(self.card.ref)
        self.assertEqual(self.merger.mode(card, record), "local")
        self.config.merge = "pr"
        self.assertEqual(self.merger.mode(card, record), "pr")
        self.set_board_merge("local")
        self.assertEqual(self.merger.mode(card, record), "local")

    def test_a_linear_card_uses_the_board_of_its_repository(self):
        self.set_board_merge("pr")
        repository = self.store.board_meta(self.board).repo
        self.assertEqual(self.merger.mode(None, {"repository": repository}), "pr")
        self.assertEqual(self.merger.mode(None, {"repository": "other"}), "local")

    def test_a_bad_board_mode_is_a_board_error(self):
        self.set_board_merge("squash")
        meta = self.store.board_meta(self.board)
        self.assertIsNone(meta.merge)
        self.assertIn("merge must be local or pr", meta.error)


class MergeCliTests(CliTestCase):
    def card(self):
        return self.run_json("add", "Fix it", directory=self.plain)["card"]["ref"]

    def test_merge_pr_text(self):
        ref = self.card()
        writing = {"ref": ref, "mode": "pr", "base": "main", "pr_url": None, "pr_action": "writing",
                   "writer": "the card's agent", "folder": "/f"}
        pushed = {"ref": ref, "mode": "pr", "base": "main", "pr_url": "https://github.com/acme/api/pull/7",
                  "pr_action": "pushed"}
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.merge.return_value = writing
            out, _ = self.run_cli("merge", ref, "--pr", directory=self.plain)
            self.assertEqual(out, f"{ref}: the card's agent writes the pull request description. Files: /f\n"
                                  "The draft pull request opens when the description is ready.\n")
            merger.return_value.merge.return_value = pushed
            out, _ = self.run_cli("merge", ref, "--pr", directory=self.plain)
            self.assertEqual(out, f"{ref}: pushed to the open pull request https://github.com/acme/api/pull/7\n"
                                  "Use --rewrite to rewrite its description.\n")
            merger.return_value.merge.return_value = {**writing, "pr_url": "https://github.com/acme/api/pull/7"}
            out, _ = self.run_cli("merge", ref, "--rewrite", directory=self.plain)
            self.assertIn("rewrites the description of https://github.com/acme/api/pull/7", out)
            merger.return_value.merge.assert_called_with(ref, mode=None, force=False, rewrite=True)

    def test_merge_and_finish_text(self):
        ref = self.card()
        merged = {"ref": ref, "mode": "local", "base": "main", "commit": "abcdef1234",
                  "worktree_removed": True, "branch_deleted": True}
        finished = {"ref": ref, "worktree_removed": True, "branch_deleted": False}
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.merge.return_value = merged
            merger.return_value.finish.return_value = finished
            out, _ = self.run_cli("merge", ref, "--local", directory=self.plain)
            self.assertEqual(out, f"{ref}: merged into main at abcdef1\n{ref}: finished; worktree removed; branch deleted\n")
            merger.return_value.merge.assert_called_with(ref, mode="local", force=False, rewrite=False)
            out, _ = self.run_cli("finish", ref, "--force", directory=self.plain)
            self.assertEqual(out, f"{ref}: finished; worktree removed; branch kept\n")
            merger.return_value.finish.assert_called_with(ref, force=True)

    def test_a_conflict_exits_with_code_2(self):
        ref = self.card()
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.merge.side_effect = merge.MergeError("conflict", "rebase_conflict", exit_code=2)
            data = self.run_json("merge", ref, directory=self.plain, expect=2)
        self.assertEqual(data["error"]["code"], "rebase_conflict")

    def test_local_and_pr_cannot_both_be_given(self):
        data = self.run_json("merge", "t123", "--local", "--pr", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_rewrite_cannot_go_with_local(self):
        data = self.run_json("merge", "t123", "--local", "--rewrite", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_pr_wait_returns_the_worker_exit_code(self):
        with patch.object(pr_write, "wait", return_value=1) as wait:
            self.run_cli("pr-wait", "t123", "--folder", "/f", directory=self.plain, expect=1)
        self.assertEqual(wait.call_args.args[1:], ("t123", Path("/f")))


class PrSyncCliTests(CliTestCase):
    def card(self):
        return self.run_json("add", "Fix it", directory=self.plain)["card"]["ref"]

    def test_pr_sync_text(self):
        ref = self.card()
        url = "https://github.com/acme/api/pull/7"
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.sync_pr.return_value = {"ref": ref, "pr_url": url, "path": "/p/pr.md",
                                                        "action": "updated"}
            out, _ = self.run_cli("pr-sync", ref, directory=self.plain)
            self.assertEqual(out, f"{ref}: updated {url}\n")
            merger.return_value.sync_pr.return_value = {"ref": ref, "pr_url": None, "path": "/p/pr.md",
                                                        "action": "fetched"}
            out, _ = self.run_cli("pr-sync", ref, "--fetch", directory=self.plain)
            self.assertEqual(out, f"{ref}: wrote /p/pr.md. Edit it, then run herdboard pr-sync {ref}.\n")
            merger.return_value.sync_pr.assert_called_with(ref, fetch=True)

    def test_pr_sync_json(self):
        ref = self.card()
        url = "https://github.com/acme/api/pull/7"
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.sync_pr.return_value = {"ref": ref, "pr_url": url, "path": "/p/pr.md",
                                                        "action": "updated"}
            data = self.run_json("pr-sync", ref, directory=self.plain)
            self.assertEqual(data, {"ref": ref, "pr_url": url, "path": "/p/pr.md", "action": "updated"})
            merger.return_value.sync_pr.return_value = {"ref": ref, "pr_url": None, "path": "/p/pr.md",
                                                        "action": "fetched"}
            data = self.run_json("pr-sync", ref, "--fetch", directory=self.plain)
        self.assertEqual(data, {"ref": ref, "pr_url": None, "path": "/p/pr.md", "action": "fetched"})

    def test_a_missing_description_has_its_code(self):
        ref = self.card()
        with patch("herdboard.cli.Merger") as merger:
            merger.return_value.sync_pr.side_effect = merge.MergeError("no description", "no_description")
            data = self.run_json("pr-sync", ref, directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "no_description")


if __name__ == "__main__":
    unittest.main()


class NoRepositoryFinishTests(MergeTestCase):
    """A repoless card finishes by deleting its card folder; it has no branch to merge."""

    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.store.ensure_inbox(), "Research corvid memory")
        result = self.starter.start(self.card.ref, no_repo=True, agent="pi", no_agent=True)
        self.folder = Path(result["worktree_path"])

    def test_merge_refuses_a_repoless_card(self):
        with self.assertRaises(merge.MergeError) as raised:
            self.merger.merge(self.card.ref)
        self.assertEqual(raised.exception.code, "no_repository")
        self.assertIn("--agent", str(raised.exception))
        self.assertIn("finish", str(raised.exception))
        self.assertTrue(self.folder.is_dir())

    def test_finish_closes_the_workspace_and_deletes_a_non_empty_folder(self):
        (self.folder / "scratch.txt").write_text("work in progress\n")
        result = self.merger.finish(self.card.ref)
        self.assertEqual(result, {"ref": self.card.ref, "worktree_removed": True, "branch_deleted": False,
                                  "mode": "repoless"})
        self.assertFalse(self.folder.exists())
        self.assertIn(["workspace", "close", "x1"], self.fake.calls)
        card = self.store.show(self.card.ref)
        self.assertTrue(card.checked)
        self.assertEqual(card.column, "Done")
        self.assertEqual(self.records(), {})
        self.assertIn("Finished.", self.log())

    def test_finish_works_when_the_folder_is_gone(self):
        shutil.rmtree(self.folder)
        result = self.merger.finish(self.card.ref)
        self.assertTrue(result["worktree_removed"])
        self.assertEqual(self.records(), {})

    def test_finish_tolerates_a_closed_workspace(self):
        self.fake.closed.add("x1")
        result = self.merger.finish(self.card.ref)
        self.assertTrue(result["worktree_removed"])
        self.assertFalse(self.folder.exists())
