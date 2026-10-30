import contextlib
import fcntl
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support.paths import ROOT
from support.scripts import load_script

FIXTURES = ROOT / "tests/auto-title/fixtures"
auto_title = load_script("auto-title", "auto_title")

CONFIG = {"max_length": 40, "poll_seconds": 2.0}


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def agent_pane(pane_id, agent="claude", label=None, tokens=None, focused=False, tab_id="w1:t1"):
    pane = {"agent": agent, "focused": focused, "pane_id": pane_id, "tab_id": tab_id, "tokens": tokens or {}}
    if label is not None:
        pane["label"] = label
    return pane


def shell_pane(pane_id, focused=False, tab_id="w1:t1"):
    return {"focused": focused, "pane_id": pane_id, "tab_id": tab_id, "tokens": {}}


class CleanTests(unittest.TestCase):
    def test_normalizes_whitespace(self):
        self.assertEqual(auto_title.clean("  fix \t the\n  bug  ", 40), "fix the bug")

    def test_removes_control_characters(self):
        self.assertEqual(auto_title.clean("fix\x1b[31m the\x07 bug", 40), "fix[31m the bug")

    def test_cuts_long_text_with_ellipsis(self):
        text = "a" * 50
        self.assertEqual(auto_title.clean(text, 40), "a" * 39 + "…")
        self.assertEqual(len(auto_title.clean(text, 40)), 40)

    def test_keeps_text_at_the_limit(self):
        self.assertEqual(auto_title.clean("a" * 40, 40), "a" * 40)

    def test_custom_max_length(self):
        self.assertEqual(auto_title.clean("abcdefghij", 5), "abcd…")

    def test_cuts_before_a_trailing_space(self):
        self.assertEqual(auto_title.clean("abc defgh", 5), "abc…")


class CardTitleTests(unittest.TestCase):
    def test_open_card(self):
        self.assertEqual(auto_title.card_title("▶ Improve tab titles · Review"), "Improve tab titles")

    def test_checked_card(self):
        self.assertEqual(auto_title.card_title("✓ Ship it · Done"), "Ship it")

    def test_title_with_separator_inside(self):
        self.assertEqual(auto_title.card_title("▶ Parse a · b · In progress"), "Parse a · b")

    def test_empty_token(self):
        self.assertEqual(auto_title.card_title(""), "")
        self.assertEqual(auto_title.card_title(None), "")


class SourceOrderTests(unittest.TestCase):
    def test_session_name_beats_label_card_and_program(self):
        pane = agent_pane(
            "w1:p1", label="label", tokens={"claude_session_name": "session", "herdboard_card": "▶ card · Review"}
        )
        self.assertEqual(auto_title.pane_title(pane, "claude"), "session")
        self.assertEqual(auto_title.context_title(pane), (1, "claude_session_name", "session"))

    def test_pi_session_name(self):
        pane = agent_pane("w1:p1", agent="pi", tokens={"pi_session_name": "pi session"})
        self.assertEqual(auto_title.pane_title(pane, "pi"), "pi session")

    def test_agent_label_beats_card_and_program(self):
        pane = agent_pane("w1:p1", label="label", tokens={"herdboard_card": "▶ card · Review"})
        self.assertEqual(auto_title.context_title(pane), (2, "pane label", "label"))

    def test_shell_label_is_not_a_source(self):
        pane = shell_pane("w1:p1")
        pane["label"] = "my notes"
        self.assertIsNone(auto_title.context_title(pane))
        self.assertEqual(auto_title.pane_title(pane, "fish"), "fish")

    def test_card_beats_program(self):
        pane = agent_pane("w1:p1", tokens={"herdboard_card": "▶ card · Review"})
        self.assertEqual(auto_title.context_title(pane), (3, "herdboard_card", "card"))

    def test_program_is_the_last_source(self):
        self.assertEqual(auto_title.pane_title(agent_pane("w1:p1"), "claude"), "claude")

    def test_no_source_gives_an_empty_title(self):
        self.assertEqual(auto_title.pane_title(shell_pane("w1:p1"), None), "")

    def test_blank_session_name_falls_through(self):
        pane = agent_pane("w1:p1", label="label", tokens={"claude_session_name": "  "})
        self.assertEqual(auto_title.context_title(pane), (2, "pane label", "label"))


class SourcePaneTests(unittest.TestCase):
    def test_main_agent_beats_a_review_split(self):
        main = agent_pane("w1:p1", tokens={"claude_session_name": "main", "herdboard_card": "▶ card · Review"})
        review = agent_pane("w1:p2", agent="pi", tokens={"pi_session_name": "review"}, focused=True)
        self.assertIs(auto_title.source_pane([main, review]), main)
        self.assertIs(auto_title.source_pane([review, main]), main)

    def test_agent_with_a_session_beats_a_bare_agent(self):
        bare = agent_pane("w1:p1", focused=True)
        named = agent_pane("w1:p2", tokens={"claude_session_name": "named"})
        self.assertIs(auto_title.source_pane([bare, named]), named)

    def test_focused_agent_breaks_a_tie(self):
        first = agent_pane("w1:p1", tokens={"claude_session_name": "one"})
        second = agent_pane("w1:p2", tokens={"claude_session_name": "two"}, focused=True)
        self.assertIs(auto_title.source_pane([first, second]), second)

    def test_agent_beats_a_focused_shell(self):
        shell = shell_pane("w1:p1", focused=True)
        agent = agent_pane("w1:p2")
        self.assertIs(auto_title.source_pane([shell, agent]), agent)

    def test_no_agent_uses_the_focused_pane(self):
        first = shell_pane("w1:p1")
        second = shell_pane("w1:p2", focused=True)
        self.assertIs(auto_title.source_pane([first, second]), second)

    def test_background_tab_uses_the_first_pane(self):
        first = shell_pane("w1:p1")
        second = shell_pane("w1:p2")
        self.assertIs(auto_title.source_pane([first, second]), first)


class OwnedTests(unittest.TestCase):
    def state(self, **tabs):
        return {"tabs": tabs, "reset": []}

    def test_digits_are_owned(self):
        self.assertTrue(auto_title.owned({"tab_id": "w1:t1", "label": "12"}, self.state()))

    def test_herdboard_plan_label_is_owned(self):
        self.assertTrue(auto_title.owned({"tab_id": "w1:t1", "label": "plan tveq"}, self.state()))

    def test_linear_plan_label_is_owned(self):
        self.assertTrue(auto_title.owned({"tab_id": "w1:t1", "label": "plan ENG-123"}, self.state()))

    def test_plan_label_must_match_exactly(self):
        self.assertFalse(auto_title.owned({"tab_id": "w1:t1", "label": "plan tveq now"}, self.state()))

    def test_own_last_label_is_owned(self):
        state = self.state(**{"w1:t1": {"written": "fix bug"}})
        self.assertTrue(auto_title.owned({"tab_id": "w1:t1", "label": "fix bug"}, state))

    def test_manual_label_is_not_owned(self):
        state = self.state(**{"w1:t1": {"written": "fix bug"}})
        self.assertFalse(auto_title.owned({"tab_id": "w1:t1", "label": "my tab"}, state))

    def test_reset_mark_makes_a_manual_label_owned(self):
        state = {"tabs": {}, "reset": ["w1:t1"]}
        self.assertTrue(auto_title.owned({"tab_id": "w1:t1", "label": "my tab"}, state))


class FakeHerdr:
    def __init__(self):
        self.tabs = fixture("tab_list.json")
        self.panes = fixture("pane_list.json")
        self.workspaces = fixture("workspace_list.json")
        self.programs = {"w1:p2": "fish", "w1:p3": "nvim", "w1:p5": "herdboard"}
        self.calls = []
        self.error = None
        self.plugins = [{"plugin_id": "nulluminati.auto-title", "enabled": True}]

    def tab(self, tab_id):
        return next(tab for tab in self.tabs["result"]["tabs"] if tab["tab_id"] == tab_id)

    def renames(self):
        return [call[3:] for call in self.calls if call[1:3] == ["tab", "rename"]]

    def __call__(self, argv, timeout=30):
        self.calls.append(argv)
        if self.error:
            raise auto_title.TitleError(self.error)
        arguments = argv[1:]

        def reply(data):
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

        if arguments == ["tab", "list"]:
            return reply(self.tabs)
        if arguments == ["pane", "list"]:
            return reply(self.panes)
        if arguments == ["workspace", "list"]:
            return reply(self.workspaces)
        if arguments[:3] == ["pane", "process-info", "--pane"]:
            program = self.programs.get(arguments[3])
            if program is None:
                raise auto_title.TitleError('{"error":{"code":"pane_not_found"}}')
            info = fixture("process_info_fish.json")
            info["result"]["process_info"]["foreground_processes"][0]["name"] = program
            return reply(info)
        if arguments[:2] == ["tab", "rename"]:
            self.tab(arguments[2])["label"] = arguments[3]
            return reply({"result": {}})
        if arguments[:2] == ["notification", "show"]:
            return reply({"result": {}})
        if arguments == ["plugin", "list", "--plugin", "nulluminati.auto-title", "--json"]:
            return reply({"result": {"plugins": self.plugins, "type": "plugin_list"}})
        raise AssertionError(f"unexpected command {argv}")


class HerdrTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.config_dir = Path(self.temporary.name) / "config"
        self.state_dir.mkdir()
        self.config_dir.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "HERDR_BIN_PATH": "herdr",
                "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)
        self.herdr = FakeHerdr()
        command = patch.object(auto_title, "command", self.herdr)
        command.start()
        self.addCleanup(command.stop)


class ReconcileTests(HerdrTestCase):
    def test_names_owned_tabs_and_leaves_manual_labels(self):
        state = {"tabs": {}, "reset": []}
        self.assertTrue(auto_title.reconcile(state, CONFIG))
        self.assertEqual(
            self.herdr.renames(),
            [
                ["w1:t1", "Herdboard tveq auto tab titles"],
                ["w1:t2", "nvim"],
                ["w1:t3", "Planning: Codex support plan"],
            ],
        )
        self.assertEqual(state["tabs"]["w1:t2"], {"written": "nvim"})
        self.assertEqual(self.herdr.tab("w1:t4")["label"], "board")

    def test_no_process_info_call_when_context_gives_a_title(self):
        auto_title.reconcile({"tabs": {}, "reset": []}, CONFIG)
        asked = [call[4] for call in self.herdr.calls if call[1:3] == ["pane", "process-info"]]
        self.assertEqual(asked, ["w1:p3"])

    def test_no_rename_when_the_title_equals_the_label(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.herdr.calls.clear()
        self.assertFalse(auto_title.reconcile(state, CONFIG))
        self.assertEqual(self.herdr.renames(), [])

    def test_follows_a_changed_session_name(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        pane = self.herdr.panes["result"]["panes"][0]
        pane["tokens"]["claude_session_name"] = "Renamed session"
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t1")["label"], "Renamed session")

    def test_manual_rename_stops_updates(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.herdr.tab("w1:t2")["label"] = "mine"
        self.herdr.programs["w1:p3"] = "htop"
        self.herdr.calls.clear()
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.renames(), [])

    def test_renaming_to_digits_hands_the_tab_back(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.herdr.tab("w1:t2")["label"] = "mine"
        auto_title.reconcile(state, CONFIG)
        self.herdr.tab("w1:t2")["label"] = "2"
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t2")["label"], "nvim")

    def test_reset_mark_renames_a_manual_label_and_clears_the_mark(self):
        state = {"tabs": {}, "reset": ["w1:t4"]}
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t4")["label"], "herdboard")
        self.assertEqual(state["reset"], [])
        self.assertEqual(state["tabs"]["w1:t4"], {"written": "herdboard"})

    def test_planner_tab_keeps_its_prefix_after_the_plan_label_is_gone(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(state["tabs"]["w1:t3"], {"written": "Planning: Codex support plan", "planning": True})
        pane = next(pane for pane in self.herdr.panes["result"]["panes"] if pane["tab_id"] == "w1:t3")
        pane["tokens"]["pi_session_name"] = "Renamed plan"
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t3")["label"], "Planning: Renamed plan")

    def test_linear_planner_tab_gets_the_prefix(self):
        self.herdr.tab("w1:t3")["label"] = "plan ENG-123"
        auto_title.reconcile({"tabs": {}, "reset": []}, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t3")["label"], "Planning: Codex support plan")

    def test_tab_in_a_planner_workspace_gets_the_prefix(self):
        self.herdr.workspaces["result"]["workspaces"].append({"label": "plan tauto", "workspace_id": "w2"})
        self.herdr.tabs["result"]["tabs"].append({"label": "1", "tab_id": "w2:t1", "workspace_id": "w2"})
        self.herdr.panes["result"]["panes"].append(
            agent_pane("w2:p1", tab_id="w2:t1", tokens={"claude_session_name": "Weekly cleanup"})
        )
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.tab("w2:t1")["label"], "Planning: Weekly cleanup")

    def test_prefix_counts_toward_max_length(self):
        auto_title.reconcile({"tabs": {}, "reset": []}, {"max_length": 15, "poll_seconds": 2.0})
        self.assertEqual(self.herdr.tab("w1:t3")["label"], "Planning: Code…")

    def test_manual_rename_of_a_planner_tab_stops_updates(self):
        state = {"tabs": {}, "reset": []}
        auto_title.reconcile(state, CONFIG)
        self.herdr.tab("w1:t3")["label"] = "mine"
        self.herdr.calls.clear()
        auto_title.reconcile(state, CONFIG)
        self.assertEqual(self.herdr.renames(), [])

    def test_stale_tab_ids_are_removed(self):
        state = {"tabs": {"w9:t1": {"written": "gone"}}, "reset": ["w9:t2"]}
        auto_title.reconcile(state, CONFIG)
        self.assertNotIn("w9:t1", state["tabs"])
        self.assertEqual(state["reset"], [])

    def test_max_length_cuts_the_title(self):
        auto_title.reconcile({"tabs": {}, "reset": []}, {"max_length": 10, "poll_seconds": 2.0})
        self.assertEqual(self.herdr.tab("w1:t1")["label"], "Herdboard…")

    def test_no_title_leaves_the_tab(self):
        self.herdr.programs.pop("w1:p3")
        auto_title.reconcile({"tabs": {}, "reset": []}, CONFIG)
        self.assertEqual(self.herdr.tab("w1:t2")["label"], "2")


class StateTests(HerdrTestCase):
    def test_round_trip(self):
        state = {"tabs": {"w1:t1": {"written": "x"}}, "reset": ["w1:t2"]}
        auto_title.save_state(self.state_dir, state)
        self.assertEqual(auto_title.load_state(self.state_dir), state)

    def test_missing_or_bad_state_is_empty(self):
        self.assertEqual(auto_title.load_state(self.state_dir), {"tabs": {}, "reset": []})
        (self.state_dir / "tabs.json").write_text("{not json")
        self.assertEqual(auto_title.load_state(self.state_dir), {"tabs": {}, "reset": []})

    def test_tick_saves_the_state(self):
        auto_title.tick(self.state_dir, CONFIG)
        state = auto_title.load_state(self.state_dir)
        self.assertEqual(state["tabs"]["w1:t2"], {"written": "nvim"})

    def test_tick_waits_for_the_state_lock(self):
        with (self.state_dir / "state.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            worker = threading.Thread(target=auto_title.tick, args=(self.state_dir, CONFIG))
            worker.start()
            worker.join(0.3)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.herdr.renames(), [])
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.herdr.renames()), 3)

    def test_paused_tick_renames_nothing(self):
        (self.state_dir / "paused").touch()
        auto_title.tick(self.state_dir, CONFIG)
        self.assertEqual(self.herdr.renames(), [])


class ConfigTests(HerdrTestCase):
    def test_defaults_without_a_file(self):
        self.assertEqual(auto_title.load_config(self.config_dir, self.state_dir), CONFIG)

    def test_reads_the_keys(self):
        (self.config_dir / "config.toml").write_text("max_length = 24\npoll_seconds = 1\n")
        self.assertEqual(
            auto_title.load_config(self.config_dir, self.state_dir), {"max_length": 24, "poll_seconds": 1.0}
        )

    def test_bad_value_logs_and_uses_the_default(self):
        (self.config_dir / "config.toml").write_text('max_length = "long"\npoll_seconds = 3\n')
        self.assertEqual(
            auto_title.load_config(self.config_dir, self.state_dir), {"max_length": 40, "poll_seconds": 3.0}
        )
        self.assertIn("max_length", (self.state_dir / "auto-title.log").read_text())

    def test_bad_file_logs_and_uses_the_defaults(self):
        (self.config_dir / "config.toml").write_text("max_length = \n")
        self.assertEqual(auto_title.load_config(self.config_dir, self.state_dir), CONFIG)
        self.assertIn("config.toml", (self.state_dir / "auto-title.log").read_text())


class LogTests(HerdrTestCase):
    def test_log_is_truncated_when_it_grows(self):
        log = self.state_dir / "auto-title.log"
        log.write_text("x" * (auto_title.LOG_LIMIT + 1))
        auto_title.log(self.state_dir, "fresh line")
        self.assertEqual(log.read_text().splitlines()[-1].split(" ", 2)[-1], "fresh line")
        self.assertLess(log.stat().st_size, 1000)


class PollerTests(HerdrTestCase):
    def test_second_poller_exits_when_the_lock_is_held(self):
        with (self.state_dir / "poller.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(auto_title, "tick") as tick:
                self.assertEqual(auto_title.run(self.state_dir, CONFIG), 0)
        tick.assert_not_called()
        self.assertFalse((self.state_dir / "poller.pid").exists())

    def test_run_exits_after_the_server_is_gone(self):
        self.herdr.error = '{"error":{"code":"server_not_running"}}'
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            patch.object(auto_title.time, "sleep", sleep),
            patch.object(auto_title.time, "monotonic", lambda: clock[0]),
        ):
            self.assertEqual(auto_title.run(self.state_dir, CONFIG), 0)
        self.assertGreaterEqual(clock[0], 130.0)
        self.assertLess(clock[0], 140.0)
        self.assertEqual((self.state_dir / "poller.pid").read_text(), str(os.getpid()))

    def test_run_logs_other_errors_and_continues(self):
        passes = []

        def tick(state_dir, config):
            passes.append(1)
            if len(passes) == 1:
                raise auto_title.TitleError("boom")
            raise KeyboardInterrupt

        with patch.object(auto_title, "tick", tick), patch.object(auto_title.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                auto_title.run(self.state_dir, CONFIG)
        self.assertEqual(len(passes), 2)
        self.assertIn("boom", (self.state_dir / "auto-title.log").read_text())

    def test_run_exits_when_the_plugin_is_disabled(self):
        self.herdr.plugins[0]["enabled"] = False
        with patch.object(auto_title, "tick") as tick:
            self.assertEqual(auto_title.run(self.state_dir, CONFIG), 0)
        tick.assert_not_called()

    def test_run_exits_when_the_plugin_is_removed(self):
        self.herdr.plugins.clear()
        with patch.object(auto_title, "tick") as tick:
            self.assertEqual(auto_title.run(self.state_dir, CONFIG), 0)
        tick.assert_not_called()

    def test_start_detaches_a_run_child(self):
        with patch.object(auto_title.subprocess, "Popen") as popen:
            self.assertEqual(auto_title.main(["start"]), 0)
        arguments, options = popen.call_args
        self.assertEqual(arguments[0][-1], "run")
        self.assertTrue(options["start_new_session"])


class ActionTests(HerdrTestCase):
    def notifications(self):
        return [call[3:] for call in self.herdr.calls if call[1:3] == ["notification", "show"]]

    def setUp(self):
        super().setUp()
        # doctor prints its report for the plugin log.
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def test_reset_marks_the_focused_tab(self):
        self.herdr.tab("w1:t1")["focused"] = False
        self.herdr.tab("w1:t4")["focused"] = True
        self.assertEqual(auto_title.main(["reset"]), 0)
        self.assertEqual(self.herdr.tab("w1:t4")["label"], "herdboard")
        self.assertEqual(self.notifications(), [["Auto title", "--body", "Tab herdboard is automatic again"]])

    def test_reset_uses_the_pane_from_the_context(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_id": "w1:p5"})
        auto_title.main(["reset"])
        self.assertEqual(self.herdr.tab("w1:t4")["label"], "herdboard")

    def test_pause_and_resume(self):
        with patch.object(auto_title.subprocess, "Popen") as popen:
            auto_title.main(["pause"])
            self.assertTrue((self.state_dir / "paused").exists())
            auto_title.main(["resume"])
        self.assertFalse((self.state_dir / "paused").exists())
        popen.assert_called_once()
        self.assertEqual(len(self.notifications()), 2)

    def test_doctor_explains_the_focused_tab(self):
        self.assertEqual(auto_title.main(["doctor"]), 0)
        [[title, _, body]] = self.notifications()
        self.assertEqual(title, "Auto title")
        self.assertIn("Label: 1", body)
        self.assertIn("Owner: plugin (default number label)", body)
        self.assertIn("Source: w1:p1, claude_session_name", body)
        self.assertIn("Title: Herdboard tveq auto tab titles", body)
        self.assertIn("Poller: not running", body)

    def test_doctor_names_a_manual_label(self):
        self.herdr.tab("w1:t1")["focused"] = False
        self.herdr.tab("w1:t4")["focused"] = True
        auto_title.main(["doctor"])
        [[_, _, body]] = self.notifications()
        self.assertIn("Owner: user (manual label)", body)
        self.assertIn("Source: w1:p5, program", body)

    def test_doctor_shows_a_paused_poller(self):
        (self.state_dir / "paused").touch()
        auto_title.main(["doctor"])
        [[_, _, body]] = self.notifications()
        self.assertIn("Poller: not running, paused", body)

    def test_failed_action_reports_the_error(self):
        self.herdr.error = "boom"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(auto_title.main(["reset"]), 1)
        self.assertIn("boom", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
