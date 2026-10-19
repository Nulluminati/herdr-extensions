"""Plan a card with an agent: it reads the code, asks the user, and writes the plan as an attachment."""
import contextlib
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
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import activity, attachments, plan, start  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.identity import repo_identity  # noqa: E402
from herdboard.store import Store  # noqa: E402

from test_herdboard_cli import CliTestCase  # noqa: E402


class PlanHerdr:
    """Fake Herdr: worktree workspaces, a new plan tab, a planner agent on a script, and notifications."""

    def __init__(self, root=None):
        self.root = root
        self.calls = []
        # Each status poll takes the next state; None means the planner pane is gone, and "error" that
        # Herdr did not answer.
        self.states = ["working", "idle", None]
        # Called on each poll, before the state is returned: it can write the plan.
        self.on_poll = None
        # Called while the planner agent starts: it can write the plan.
        self.on_start = None
        # Each pane check takes the next answer; False means the pane is gone. The pane exists by default.
        self.panes = []
        # Panes and names of live work agents, which `start` recorded.
        self.work_agents = set()
        # Worktree workspaces that Herdr has open, as `start` records them.
        self.worktrees = []
        self.branches = {}

    def reply(self, argv, result=None, error=None):
        if error:
            return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": error, "message": error}}))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result or {}}), "")

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        self.calls.append(args)
        operation = " ".join(args[:2])
        if operation == "tab create":
            return self.reply(argv, {"tab": {"tab_id": "w1:t4"}, "root_pane": {"pane_id": "w1:p7"}})
        if operation == "workspace list":
            return self.reply(argv, {"workspaces": []})
        if operation == "worktree list":
            return self.reply(argv, {"worktrees": self.worktrees})
        if operation in ("worktree create", "worktree open"):
            branch = args[args.index("--branch") + 1] if "--branch" in args else args[args.index("--path") + 1]
            if branch not in self.branches:
                self.branches[branch] = len(self.branches)
            index = self.branches[branch]
            path = self.root / ("work" if index == 0 else f"work-{index}")
            if "--path" in args:
                path = Path(args[args.index("--path") + 1])
            workspace = f"w{9 + index}"
            if operation == "worktree create":
                # A real worktree, so `git status` and the upstream work in the tests.
                cwd = args[args.index("--cwd") + 1]
                base = args[args.index("--base") + 1] if "--base" in args else "HEAD"
                subprocess.run(["git", "-C", cwd, "worktree", "add", "-qb", branch, str(path), base],
                               check=True, capture_output=True)
            result = {"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": workspace + ":p1"},
                      "worktree": {"path": str(path)}}
            self.worktrees = [w for w in self.worktrees if w["path"] != str(path)]
            self.worktrees.append({"branch": branch, "path": str(path), "open_workspace_id": workspace})
            return self.reply(argv, result)
        if operation == "workspace get":
            return self.reply(argv, {"workspace": {"workspace_id": args[2]},
                                     "root_pane": {"pane_id": args[2] + ":p1"}})
        if operation == "pane list":
            return self.reply(argv, {"panes": [{"pane_id": args[3] + ":p1"}]})
        if operation == "agent get" and args[2] in self.work_agents:
            return self.reply(argv, {"agent": {"agent_status": "working", "pane_id": args[2]}})
        if operation == "agent get":
            if self.on_poll:
                self.on_poll()
            state = self.states.pop(0) if self.states else None
            if state is None:
                return self.reply(argv, error="agent_not_found")
            if state == "error":
                return self.reply(argv, error="server_unavailable")
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        if operation == "pane get":
            if not (self.panes.pop(0) if self.panes else True):
                return self.reply(argv, error="pane_not_found")
            return self.reply(argv, {"pane": {"pane_id": args[2]}})
        if operation == "agent start" and self.on_start:
            self.on_start()
        return self.reply(argv, {"type": "ok"})

    def find(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit",
                                      "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.board = self.store.init_board(repo_identity(self.repo))
        self.card = self.store.add(self.board, "Fix login timeout #bug")
        self.card = self.store.edit(self.card.ref, body=["\tThe session ends after 5 minutes."])
        self.fake = PlanHerdr(self.root)
        self.enterContext(patch.object(start, "command", self.fake))
        self.workers = []
        self.enterContext(patch.object(plan, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))
        self.enterContext(patch.object(plan, "POLL_SECONDS", 0))
        start.register_checkout(self.repo)
        self.planner = plan.Planner(self.store, self.repo)
        self.plan_file = attachments.plan_path(self.config, self.card.ref)

    def planner_start(self):
        return self.fake.find("agent", "start")[0]

    def record(self):
        return json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]

    def checkout_branch(self):
        return subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "--short", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def upstream(self, branch):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", branch + "@{u}"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def brief(self):
        call = self.planner_start()
        message = call[call.index("--") + 1]
        self.assertNotIn("\n", message)
        return Path(message.split("Read the brief at ")[1].split(" first")[0]).read_text()

    def log(self):
        return [entry["message"] for entry in activity.read(self.config, self.card.ref)]


class RequestTests(PlanTestCase):
    def test_the_planner_creates_the_worktree_and_plans_in_a_tab_in_it(self):
        result = self.planner.plan(self.card.ref)
        creates = self.fake.find("worktree", "create")
        self.assertEqual(len(creates), 1)
        self.assertIn("--no-focus", creates[0])
        self.assertEqual(creates[0][creates[0].index("--base") + 1], self.checkout_branch())
        worktree = self.record()["worktree_path"]
        self.assertEqual(self.fake.find("tab", "create"),
                         [["tab", "create", "--workspace", "w9", "--cwd", worktree,
                           "--label", f"plan {self.card.ref}", "--no-focus"]])
        call = self.planner_start()
        self.assertEqual(call[2], f"hb-{self.card.ref}-plan")
        self.assertEqual(call[call.index("--kind") + 1], "claude")
        self.assertEqual(call[call.index("--pane") + 1], "w1:p7")
        self.assertEqual(result, {"ref": self.card.ref, "agent": "claude", "model": None, "pane_id": "w1:p7",
                                  "checkout": str(self.repo), "plan": str(self.plan_file),
                                  "workspace_id": "w9", "worktree_path": worktree,
                                  "branch": start.todo_branch(self.card), "base": self.checkout_branch()})

    def test_the_start_record_has_the_setup_and_no_agent(self):
        self.planner.plan(self.card.ref)
        record = self.record()
        self.assertEqual(record["repository"], repo_identity(self.repo).identity)
        self.assertEqual(record["checkout"], str(self.repo))
        self.assertEqual(record["branch"], start.todo_branch(self.card))
        self.assertEqual(record["base"], self.checkout_branch())
        self.assertEqual(record["workspace_id"], "w9")
        self.assertEqual(record["worktree_path"], str(self.root / "work"))
        self.assertIn("upstream", record["steps_done"])
        self.assertNotIn("agent_name", record)

    def test_claude_edits_the_worktree_and_the_attachments(self):
        self.planner.plan(self.card.ref, model="opus")
        call = self.planner_start()
        args = call[call.index("--") + 1:]
        self.assertIn("Plan herdboard card", args[0])
        folder = attachments.folder(self.config, self.card.ref)
        worktree = self.record()["worktree_path"]
        self.assertEqual(args[1:], ["--model", "opus", "--allowedTools", "Read", "Grep", "Glob",
                                    f"Edit(/{worktree}/**)", f"Write(/{worktree}/**)",
                                    f"Edit(/{folder}/**)", f"Write(/{folder}/**)"])
        self.assertNotIn("dontAsk", args, "the user is there to answer permission prompts")

    def test_pi_gets_read_write_and_shell_tools(self):
        self.planner.plan(self.card.ref, agent="pi", model="zai/glm-5.3", thinking="high")
        call = self.planner_start()
        args = call[call.index("--") + 1:]
        self.assertEqual(args[:-2], ["--tools", "read,grep,find,ls,write,edit,bash", "--model", "zai/glm-5.3",
                                     "--thinking", "high"])
        self.assertEqual(args[-2], "--")

    def test_the_brief_has_the_card_where_to_write_and_how_to_plan(self):
        self.planner.plan(self.card.ref)
        text = self.brief()
        worktree = self.record()["worktree_path"]
        for part in ("# Plan: Fix login timeout", "The session ends after 5 minutes.",
                     f"Worktree: `{worktree}` on branch `{start.todo_branch(self.card)}` "
                     f"(base `{self.checkout_branch()}`)",
                     "You may change code and run commands here to test ideas", "Do not commit",
                     "revert every change you made", str(self.plan_file), "Ask the user",
                     "## Goal", "## Decisions", "## Steps", "## Testing", "## Open questions",
                     "6. Revert your code changes", "git status"):
            self.assertIn(part, text)
        self.assertNotIn("A plan exists", text)
        self.assertNotIn("## Changes already in the worktree", text)
        self.assertNotIn("work agent works in this worktree", text)

    def test_the_brief_warns_when_a_work_agent_runs_in_the_worktree(self):
        self.planner.plan(self.card.ref)
        start._write(start.state_dir() / "starts.json",
                     {self.card.ref: {"ref": self.card.ref, "pane_id": "w9:p1", "steps_done": [],
                                      "agent_name": f"hb-{self.card.ref}"}})
        self.fake.work_agents = {f"hb-{self.card.ref}", "w9:p1"}
        self.fake.panes = [False]  # the first plan tab is gone
        self.planner.plan(self.card.ref)
        self.assertIn("- A work agent works in this worktree too. It can change files while you plan. "
                      "Revert only your own changes.", self.brief())

    def test_the_brief_lists_changes_that_were_already_in_the_worktree(self):
        self.planner.plan(self.card.ref)
        (Path(self.record()["worktree_path"]) / "scratch.txt").write_text("wip\n")
        self.fake.panes = [False]
        self.planner.plan(self.card.ref)
        text = self.brief()
        self.assertIn("## Changes already in the worktree", text)
        self.assertIn("scratch.txt", text)
        self.assertIn("These changes are not yours", text)
        self.assertIn("do not revert them", text)

    def test_a_card_with_a_worktree_plans_in_its_workspace_without_a_second_worktree(self):
        self.planner.plan(self.card.ref)
        worktree = self.record()["worktree_path"]
        self.fake.panes = [False]  # the plan tab is closed; the worktree workspace stays open
        self.fake.calls.clear()
        self.planner.plan(self.card.ref)
        self.assertEqual(self.fake.find("worktree", "create"), [])
        tab = self.fake.find("tab", "create")[0]
        self.assertEqual(tab[tab.index("--workspace") + 1], "w9")
        self.assertEqual(tab[tab.index("--cwd") + 1], worktree)

    def test_a_closed_workspace_of_the_worktree_reopens(self):
        self.planner.plan(self.card.ref)
        self.fake.worktrees[0]["open_workspace_id"] = None
        self.fake.panes = [False]
        self.fake.calls.clear()
        self.planner.plan(self.card.ref)
        self.assertTrue(any(c[:2] == ["worktree", "open"] for c in self.fake.calls))

    def test_a_chosen_base_is_recorded_and_a_later_start_keeps_it(self):
        subprocess.run(["git", "-C", str(self.repo), "branch", "feature-x"], check=True, capture_output=True)
        result = self.planner.plan(self.card.ref, base="feature-x")
        self.assertEqual(result["base"], "feature-x")
        record = self.record()
        self.assertEqual(record["base"], "feature-x")
        self.assertEqual(self.upstream(record["branch"]), "feature-x")
        self.fake.panes = [False]
        started = start.Starter(self.store, self.repo).start(self.card.ref)
        self.assertEqual(started["base"], "feature-x")
        self.assertEqual(started["worktree_path"], record["worktree_path"])
        self.assertEqual(started["pane_id"], "w9:p1", "the work agent takes the root pane, not the plan tab")

    def test_a_parent_without_a_branch_is_prepared_without_an_agent_or_a_move(self):
        parent = self.store.add(self.board, "Billing v2")
        child = self.store.add(self.board, "Add invoice table", parent=parent.ref)
        self.planner.plan(child.ref)
        records = json.loads((start.state_dir() / "starts.json").read_text())
        self.assertEqual(records[parent.ref]["branch"], start.todo_branch(parent))
        self.assertNotIn("agent_name", records[parent.ref])
        self.assertEqual(self.store.show(parent.ref).column, parent.column)

    def test_the_brief_command_uses_the_planner_config_file(self):
        custom = self.root / "custom.toml"
        plan.Planner(self.store, self.repo, config_file=custom).plan(self.card.ref)
        self.assertIn(f"--config {custom} --json show {self.card.ref}", self.brief())

    def test_an_existing_plan_and_attachments_go_into_the_brief(self):
        source = self.root / "spec.md"
        source.write_text("Old plan.\n")
        attachments.add(self.store, self.card.ref, source, name="plan.md")
        attachments.add(self.store, self.card.ref, source)
        self.planner.plan(self.card.ref)
        text = self.brief()
        self.assertIn("A plan exists", text)
        self.assertIn("Old plan.", text)
        self.assertIn(str(attachments.folder(self.config, self.card.ref) / "spec.md"), text)

    def test_a_plan_that_cannot_be_read_does_not_stop_the_planner(self):
        self.plan_file.parent.mkdir(parents=True)
        self.plan_file.write_bytes(b"\xff\xfe")
        self.planner.plan(self.card.ref)
        self.assertNotIn("A plan exists", self.brief())
        self.assertTrue(self.log()[0].startswith("The agent does not get the plan"))

    def test_the_card_stays_in_its_column(self):
        self.planner.plan(self.card.ref)
        self.assertEqual(self.store.show(self.card.ref).column, self.card.column)

    def test_the_log_and_the_worker_follow(self):
        self.planner.plan(self.card.ref, model="opus")
        self.assertEqual(self.log(), ["Planning started: claude (opus)."])
        self.assertEqual(self.workers[0][0], ["plan-wait", self.card.ref, "--pane", "w1:p7"])
        record = json.loads((start.state_dir() / "plans.json").read_text())[self.card.ref]
        self.assertEqual(record["pane_id"], "w1:p7")

    def test_a_running_planner_blocks_a_second_one(self):
        self.planner.plan(self.card.ref)
        # Herdr shows no agent while Claude waits at its folder trust prompt, but the pane is there.
        self.fake.states = [None]
        with self.assertRaises(plan.PlanError) as raised:
            self.planner.plan(self.card.ref)
        self.assertEqual(raised.exception.code, "plan_running")
        self.assertEqual(len(self.fake.find("tab", "create")), 1)
        self.assertEqual(len(self.fake.find("worktree", "create")), 1)

    def test_a_planner_that_is_gone_does_not_block(self):
        self.planner.plan(self.card.ref)
        self.fake.panes = [False]
        self.planner.plan(self.card.ref)
        self.assertEqual(len(self.fake.find("tab", "create")), 2)

    def test_a_planner_that_cannot_start_closes_its_tab_and_keeps_the_worktree(self):
        original = self.fake.__call__

        def failing(argv, timeout=120):
            if argv[1:3] == ["agent", "start"]:
                self.fake.calls.append(argv[1:])
                return self.fake.reply(argv, error="broken")
            return original(argv, timeout)

        with patch.object(start, "command", failing), self.assertRaises(start.StartError):
            self.planner.plan(self.card.ref)
        self.assertEqual(self.fake.find("pane", "close"), [["pane", "close", "w1:p7"]])
        self.assertFalse((start.state_dir() / "plans.json").exists())
        self.assertEqual(self.workers, [])
        record = self.record()
        self.assertTrue(Path(record["worktree_path"]).exists())
        self.assertEqual(self.fake.find("workspace", "remove"), [])

    def test_the_plan_pane_exports_a_config_file_that_is_not_the_default(self):
        custom = self.root / "custom.toml"
        plan.Planner(self.store, self.repo, config_file=custom).plan(self.card.ref)
        export = self.fake.find("pane", "run")
        self.assertEqual(export, [["pane", "run", "w1:p7", f"export HERDBOARD_CONFIG={custom}"]])
        self.assertLess(self.fake.calls.index(export[0]), self.fake.calls.index(self.planner_start()))
        self.fake.panes = [False]  # the first plan tab is gone
        self.planner.plan(self.card.ref)
        self.assertEqual(len(self.fake.find("pane", "run")), 1, "the default config needs no export")

    def test_a_tab_without_a_root_pane_is_a_herdr_error(self):
        original = self.fake.__call__
        for result in ({"tab": {}}, {"root_pane": {}}, {"root_pane": []}):
            def shaped(argv, timeout=120, result=result):
                if argv[1:3] == ["tab", "create"]:
                    return self.fake.reply(argv, result)
                return original(argv, timeout)

            with self.subTest(result=result), patch.object(start, "command", shaped), \
                    self.assertRaises(start.StartError) as raised:
                self.planner.plan(self.card.ref)
            self.assertEqual(raised.exception.code, "herdr_error")

    def test_thinking_works_only_with_pi(self):
        with self.assertRaises(plan.PlanError) as raised:
            self.planner.plan(self.card.ref, thinking="high")
        self.assertEqual(raised.exception.code, "usage")

    def test_a_card_without_a_repository_needs_one(self):
        inbox = self.store.add(self.store.ensure_inbox(), "Plan the garden")
        with self.assertRaises(start.StartError) as raised:
            self.planner.plan(inbox.ref)
        self.assertEqual(raised.exception.code, "repo_required")
        self.planner.plan(inbox.ref, repo=str(self.repo))
        self.assertEqual(len(self.fake.find("tab", "create")), 1)


class WaitTests(PlanTestCase):
    def wait(self, pane="w1:p7"):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = plan.wait(self.store, self.card.ref, pane)
        return code

    def write_plan(self, text="# Plan\n"):
        self.plan_file.parent.mkdir(parents=True, exist_ok=True)
        self.plan_file.write_text(text)

    def test_each_save_is_logged_once_when_the_planner_is_idle(self):
        self.planner.plan(self.card.ref)
        polls = iter([self.write_plan, None, None, lambda: os.utime(self.plan_file, (1, 1)), None])
        self.fake.on_poll = lambda: (next(polls, None) or (lambda: None))()
        self.fake.states = ["working", "idle", "idle", "idle", None]
        self.assertEqual(self.wait(), 0)
        saved = f"Plan saved: {self.plan_file}"
        self.assertEqual(self.log(), ["Planning started: claude.", saved, saved, "Planning ended."])
        self.assertNotIn(self.card.ref, json.loads((start.state_dir() / "plans.json").read_text()))

    def test_a_save_while_the_planner_works_waits_for_idle(self):
        self.planner.plan(self.card.ref)
        self.fake.on_poll = lambda: self.plan_file.exists() or self.write_plan()
        self.fake.states = ["working", "working", None]
        self.wait()
        self.assertEqual(self.log().count(f"Plan saved: {self.plan_file}"), 1)

    def test_a_planner_that_ends_without_a_plan_logs_that(self):
        self.planner.plan(self.card.ref)
        self.fake.states = ["working", None]
        self.wait()
        self.assertEqual(self.log()[-1], "Planning ended without a plan.")

    def test_a_plan_that_existed_before_is_logged_only_when_it_changes(self):
        self.write_plan()
        os.utime(self.plan_file, (1, 1))
        self.planner.plan(self.card.ref)
        self.fake.states = ["idle", None]
        self.wait()
        self.assertEqual(self.log(), ["Planning started: claude.", "Planning ended without a change to the plan."])

    def test_a_planner_not_detected_yet_is_still_awaited(self):
        self.planner.plan(self.card.ref)
        self.fake.states = [None, "working", None]
        self.wait()
        self.assertEqual(len(self.fake.find("agent", "get")), 3)

    def test_a_planner_that_never_shows_up_times_out(self):
        self.planner.plan(self.card.ref)
        self.fake.states = []
        self.fake.panes = [True, False]
        with patch.object(plan, "PLAN_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 0)
        self.assertEqual(self.log()[-1], "Planning ended without a plan.")

    def test_a_planner_not_shown_in_a_pane_that_exists_keeps_its_record_after_the_watch(self):
        self.planner.plan(self.card.ref)
        self.fake.states = []
        with patch.object(plan, "PLAN_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 0)
        self.assertTrue(self.log()[-1].startswith("The plan saves are no longer logged"))
        self.assertIn(self.card.ref, json.loads((start.state_dir() / "plans.json").read_text()))

    def test_a_planner_not_shown_yet_is_awaited_while_its_pane_exists(self):
        # Herdr can show no agent while Claude waits at its folder trust prompt.
        self.planner.plan(self.card.ref)
        self.fake.states = [None, None, None]
        self.fake.panes = [True, True, False]
        self.assertEqual(self.wait(), 0)
        self.assertEqual(len(self.fake.find("pane", "get")), 3)
        self.assertEqual(self.log()[-1], "Planning ended without a plan.")
        self.assertNotIn(self.card.ref, json.loads((start.state_dir() / "plans.json").read_text()))

    def test_a_failed_status_query_does_not_end_the_watch(self):
        self.planner.plan(self.card.ref)
        polls = iter([None, None, self.write_plan])
        self.fake.on_poll = lambda: (next(polls, None) or (lambda: None))()
        self.fake.states = ["working", "error", "idle", None]
        self.wait()
        self.assertEqual(self.log(), ["Planning started: claude.", f"Plan saved: {self.plan_file}",
                                      "Planning ended."])

    def test_a_plan_written_while_the_planner_starts_is_logged(self):
        self.fake.on_start = self.write_plan
        self.planner.plan(self.card.ref)
        self.fake.states = ["idle", None]
        self.wait()
        self.assertEqual(self.log(), ["Planning started: claude.", f"Plan saved: {self.plan_file}",
                                      "Planning ended."])

    def test_a_planner_that_outlives_the_watch_still_blocks_a_second_one(self):
        self.planner.plan(self.card.ref)
        self.fake.states = ["working"]
        with patch.object(plan, "PLAN_TIMEOUT_SECONDS", 0):
            self.assertEqual(self.wait(), 0)
        self.assertTrue(self.log()[-1].startswith("The plan saves are no longer logged"))
        self.assertIn(self.card.ref, json.loads((start.state_dir() / "plans.json").read_text()))
        self.fake.states = ["working"]
        with self.assertRaises(plan.PlanError) as raised:
            self.planner.plan(self.card.ref)
        self.assertEqual(raised.exception.code, "plan_running")

    def test_a_worker_that_starts_after_another_planner_does_not_watch_it(self):
        self.planner.plan(self.card.ref)
        self.assertEqual(self.wait(pane="w1:p3"), 0)
        self.assertEqual(self.fake.find("agent", "get"), [])
        self.assertEqual(self.log(), ["Planning started: claude."])
        self.assertIn(self.card.ref, json.loads((start.state_dir() / "plans.json").read_text()))

    def start_work_agent(self):
        start._write(start.state_dir() / "starts.json",
                     {self.card.ref: {"ref": self.card.ref, "pane_id": "w9:p1", "steps_done": [],
                                      "agent_name": f"hb-{self.card.ref}"}})
        self.fake.work_agents = {"w9:p1", f"hb-{self.card.ref}"}

    def test_a_changed_plan_goes_to_the_card_agent_when_planning_ends(self):
        self.start_work_agent()
        self.planner.plan(self.card.ref)
        self.fake.on_poll = lambda: self.plan_file.exists() or self.write_plan()
        self.fake.states = ["idle", None]
        self.wait()
        self.assertEqual(self.fake.find("agent", "wait")[0][:3], ["agent", "wait", "w9:p1"])
        prompts = self.fake.find("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2], "w9:p1")
        self.assertIn(str(self.plan_file), prompts[0][3])
        self.assertIn("Wait for the user", prompts[0][3])
        self.assertEqual(self.log()[-2:], ["Planning ended.", "Sent the plan to the card's agent."])

    def test_an_unchanged_plan_is_not_sent_to_the_card_agent(self):
        self.start_work_agent()
        self.planner.plan(self.card.ref)
        self.fake.states = ["idle", None]
        self.wait()
        self.assertEqual(self.fake.find("agent", "prompt"), [])

    def test_a_changed_plan_without_a_live_card_agent_is_not_sent(self):
        self.start_work_agent()
        self.fake.work_agents = set()
        self.planner.plan(self.card.ref)
        self.fake.on_poll = lambda: self.plan_file.exists() or self.write_plan()
        self.fake.states = ["idle", None, None, None]
        self.wait()
        self.assertEqual(self.fake.find("agent", "prompt"), [])
        self.assertEqual(self.log()[-1], "Planning ended.")

    def test_no_record_means_nothing_to_wait_for(self):
        self.assertEqual(self.wait(), 1)


class PlanCliTests(CliTestCase):
    def test_plan_passes_the_options(self):
        calls = []

        def fake_plan(planner, ref, agent=None, model=None, thinking=None, repo=None, checkout=None, base=None):
            calls.append((ref, agent, model, thinking, repo, checkout, base, planner.config_file))
            return {"ref": ref, "agent": agent or "claude", "model": model, "pane_id": "w1:p7",
                    "checkout": "/repo", "workspace_id": "w9", "worktree_path": "/repo/work",
                    "branch": "t123-fix", "base": "main", "plan": "/todos/.herdboard/attachments/t123/plan.md"}

        with patch.object(plan.Planner, "plan", fake_plan):
            data = self.run_json("plan", "t123", "--agent", "pi", "--model", "zai/glm-5.3", "--thinking", "high",
                                 "--repo", "api", directory=self.plain)
            self.assertEqual(data["pane_id"], "w1:p7")
            out, _ = self.run_cli("plan", "t123", "--model", "opus", "--base", "feature-x", directory=self.plain)
        self.assertEqual([c[:7] for c in calls], [("t123", "pi", "zai/glm-5.3", "high", "api", None, None),
                                                  ("t123", None, "opus", None, None, None, "feature-x")])
        self.assertIsNotNone(calls[0][7])
        self.assertEqual(out, "t123: claude (opus) plans in pane w1:p7, in /repo/work.\n"
                              "The plan: /todos/.herdboard/attachments/t123/plan.md\n")

    def test_the_board_gets_the_config_file_for_its_workers(self):
        with patch("herdboard.board.HerdboardApp") as app:
            self.run_cli("board", directory=self.plain)
        self.assertEqual(app.call_args.kwargs["config_file"], self.config)

    def test_plan_wait_returns_the_worker_code(self):
        with patch.object(plan, "wait", return_value=1) as waited:
            self.run_cli("plan-wait", "t123", "--pane", "w1:p7", directory=self.plain, expect=1)
        self.assertEqual(waited.call_args.args[1:], ("t123", "w1:p7"))


if __name__ == "__main__":
    unittest.main()
