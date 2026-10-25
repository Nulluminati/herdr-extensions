"""Automations: schedules, storage, spawned cards, runs and the tick, with fake Herdr."""
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import plistlib
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard import activity, attachments, automations, start  # noqa: E402
from herdboard.automations import Automation, parse_schedule  # noqa: E402
from herdboard.brief import brief_text  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.identity import repo_identity  # noqa: E402
from herdboard.model import HerdboardError  # noqa: E402
from herdboard.store import Store  # noqa: E402
from test_herdboard_brief import todo  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_start import FakeHerdr  # noqa: E402


class LocalTime(unittest.TestCase):
    """Run in a fixed time zone with a daylight saving change, so local times are predictable."""

    def setUp(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)


def local(*parts):
    return datetime(*parts).astimezone()


class ScheduleTests(LocalTime):
    def test_every_day_defaults_to_nine(self):
        schedule = parse_schedule("every day")
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 8, 0)), local(2026, 10, 23, 9, 0))
        # Strictly after: a due time equal to the start is not the next one.
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 9, 0)), local(2026, 10, 24, 9, 0))

    def test_at_sets_the_time(self):
        schedule = parse_schedule("Every day at 7:05")
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 8, 0)), local(2026, 10, 24, 7, 5))

    def test_every_weekday_skips_the_weekend(self):
        schedule = parse_schedule("every weekday at 07:30")
        # 2026-10-23 is a Friday.
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 8, 0)), local(2026, 10, 26, 7, 30))

    def test_every_week_on_days(self):
        schedule = parse_schedule("every week on monday, thursday at 10:00")
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 8, 0)), local(2026, 10, 26, 10, 0))
        self.assertEqual(schedule.next_after(local(2026, 10, 26, 11, 0)), local(2026, 10, 29, 10, 0))
        self.assertEqual(parse_schedule("every week on mon and thu").next_after(local(2026, 10, 27, 0, 0)),
                         local(2026, 10, 29, 9, 0))

    def test_every_month_on_a_day_uses_the_last_day_of_a_short_month(self):
        schedule = parse_schedule("every month on the 31st")
        self.assertEqual(schedule.next_after(local(2026, 10, 31, 10, 0)), local(2026, 11, 30, 9, 0))
        self.assertEqual(schedule.next_after(local(2027, 2, 1, 0, 0)), local(2027, 2, 28, 9, 0))
        self.assertEqual(parse_schedule("every month on the 1st").next_after(local(2026, 10, 23, 0, 0)),
                         local(2026, 11, 1, 9, 0))

    def test_every_n_days_counts_from_the_anchor(self):
        schedule = parse_schedule("every 3 days", anchor=date(2026, 10, 20))
        self.assertEqual(schedule.next_after(local(2026, 10, 20, 10, 0)), local(2026, 10, 23, 9, 0))
        self.assertEqual(schedule.latest_at_or_before(local(2026, 10, 25, 10, 0)), local(2026, 10, 23, 9, 0))

    def test_every_n_weeks_counts_from_the_anchor(self):
        schedule = parse_schedule("every 2 weeks at 08:00", anchor=date(2026, 10, 23))
        self.assertEqual(schedule.next_after(local(2026, 10, 23, 9, 0)), local(2026, 11, 6, 8, 0))

    def test_latest_at_or_before_includes_the_time_itself(self):
        schedule = parse_schedule("every day")
        self.assertEqual(schedule.latest_at_or_before(local(2026, 10, 23, 9, 0)), local(2026, 10, 23, 9, 0))
        self.assertEqual(schedule.latest_at_or_before(local(2026, 10, 23, 8, 59)), local(2026, 10, 22, 9, 0))

    def test_count_between_excludes_both_ends(self):
        schedule = parse_schedule("every day")
        self.assertEqual(schedule.count_between(local(2026, 10, 20, 9, 0), local(2026, 10, 23, 9, 0)), 2)

    def test_daylight_saving_keeps_the_local_time(self):
        schedule = parse_schedule("every day at 09:00")
        # Berlin moves to summer time on 2026-03-29.
        before = schedule.next_after(local(2026, 3, 28, 0, 0))
        after = schedule.next_after(before)
        self.assertEqual((after.hour, after.minute), (9, 0))
        self.assertEqual(after - before, timedelta(hours=23))

    def test_other_text_is_refused_with_the_accepted_forms(self):
        for text in ("daily", "every day at 25:00", "every month on the 32nd", "every week on funday",
                     "every 0 days", ""):
            with self.subTest(text=text), self.assertRaises(HerdboardError) as raised:
                parse_schedule(text)
            self.assertEqual(raised.exception.code, "bad_schedule")
            self.assertIn("every weekday", str(raised.exception))


class AutomationTestCase(LocalTime):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.store.ensure_inbox()
        self.fake = FakeHerdr(self.root)
        runner = patch.object(start, "command", self.fake)
        runner.start()
        self.addCleanup(runner.stop)

    def add(self, **fields):
        fields.setdefault("title", "Check security alerts")
        fields.setdefault("schedule", "every day")
        fields.setdefault("board", "inbox")
        fields.setdefault("created", local(2026, 10, 20, 8, 0))
        return automations.add(self.store, **fields)

    def cards(self, board="inbox"):
        return self.store.list_cards([self.store.resolve_board(board)])

    def notifications(self):
        return [c for c in self.fake.calls if c[:2] == ["notification", "show"]]


class StorageTests(AutomationTestCase):
    def test_add_writes_toml_and_reads_it_back(self):
        added = self.add(body="Line one\nLine \"two\"\twith tab", tags=["security", "deps"], type="chore",
                         may_merge_prs=True, agent="pi", model="zai/glm-5.3", thinking="high")
        self.assertRegex(added.id, r"^a[0-9a-z]{3}$")
        data = tomllib.loads(automations.definitions_path(self.config).read_text(encoding="utf-8"))
        entry = data["automation"][0]
        self.assertEqual(entry["body"], "Line one\nLine \"two\"\twith tab")
        self.assertEqual(entry["tags"], ["security", "deps"])
        self.assertIs(entry["may_merge_prs"], True)
        self.assertEqual(automations.load(self.config), [added])

    def test_an_empty_or_missing_file_has_no_automations(self):
        self.assertEqual(automations.load(self.config), [])

    def test_unknown_board_and_bad_schedule_are_refused(self):
        with self.assertRaises(HerdboardError) as raised:
            self.add(board="nowhere")
        self.assertEqual(raised.exception.code, "unknown_board")
        with self.assertRaises(HerdboardError) as raised:
            self.add(schedule="sometimes")
        self.assertEqual(raised.exception.code, "bad_schedule")
        self.assertEqual(automations.load(self.config), [])

    def test_thinking_works_only_with_pi(self):
        with self.assertRaises(HerdboardError) as raised:
            self.add(agent="claude", thinking="high")
        self.assertEqual(raised.exception.code, "usage")

    def test_edit_pause_resume_and_remove(self):
        added = self.add()
        edited = automations.edit(self.store, added.id, title="Check alerts", tags=["sec"])
        self.assertEqual(edited.title, "Check alerts")
        self.assertEqual(edited.schedule, "every day")
        self.assertFalse(automations.set_enabled(self.store, added.id, False).enabled)
        self.assertTrue(automations.set_enabled(self.store, added.id, True).enabled)
        removed = automations.remove(self.store, added.id)
        self.assertEqual(removed.id, added.id)
        self.assertEqual(automations.load(self.config), [])
        with self.assertRaises(HerdboardError) as raised:
            automations.get(self.config, added.id)
        self.assertEqual(raised.exception.code, "not_found")

    def test_automation_ids_take_attachments(self):
        added = self.add()
        source = self.root / "plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        attachments.add(self.store, added.id, source)
        self.assertEqual([e["name"] for e in attachments.listing(self.config, added.id)], ["plan.md"])


class SpawnTests(AutomationTestCase):
    def test_spawn_copies_text_type_tags_details_and_attachments(self):
        added = self.add(body="Look at Dependabot.\n- merge the safe ones", tags=["security"], type="chore")
        source = self.root / "plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        attachments.add(self.store, added.id, source)
        card = automations.spawn(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertEqual(card.board, "inbox")
        self.assertEqual(card.column, self.config.columns[0])
        self.assertEqual(card.title, "Check security alerts #type/chore #security")
        self.assertEqual(card.body, ["\tLook at Dependabot.", "\t- merge the safe ones"])
        self.assertEqual([e["name"] for e in attachments.listing(self.config, card.ref)], ["plan.md"])
        log = activity.read(self.config, card.ref)
        self.assertEqual(log[0]["message"], f"Spawned by automation {added.id} for 2026-10-23 09:00.")
        self.assertEqual(log[0]["author"], "herdboard")


class RunTests(AutomationTestCase):
    def test_an_inbox_automation_starts_without_a_repository(self):
        added = self.add()
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertEqual(result["result"], "started")
        record = json.loads((start.state_dir() / "starts.json").read_text())[result["card"]]
        self.assertEqual(record["mode"], "repoless")
        self.assertEqual(self.store.show(result["card"]).column, self.config.start_column)
        self.assertTrue(any(c[:2] == ["agent", "start"] for c in self.fake.calls))
        self.assertTrue(any(result["card"] in " ".join(c) for c in self.notifications()))
        state = automations.read_state()[added.id]
        self.assertEqual(state["last_card"], result["card"])
        self.assertEqual(state["last_result"], "started")

    def test_the_agent_settings_reach_the_start(self):
        added = self.add(agent="pi", model="zai/glm-5.3", thinking="high")
        automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertIn("pi", call)
        self.assertIn("zai/glm-5.3", call)
        self.assertIn("high", call)

    def test_a_start_failure_keeps_the_card_logs_and_notifies(self):
        added = self.add()
        self.fake.fail = "workspace create"
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertTrue(result["result"].startswith("start_failed: "))
        card = self.store.show(result["card"])
        self.assertEqual(card.column, self.config.columns[0])
        messages = [e["message"] for e in activity.read(self.config, card.ref)]
        self.assertTrue(any(m.startswith("Auto start failed: ") for m in messages))
        self.assertTrue(self.notifications())
        self.assertTrue(automations.read_state()[added.id]["last_result"].startswith("start_failed: "))

    def test_a_spawn_failure_is_recorded(self):
        added = self.add()
        # A board that is gone after the automation was saved.
        broken = Automation(**{**added.__dict__, "board": "gone"})
        result = automations.run(self.store, broken, local(2026, 10, 23, 9, 0))
        self.assertIsNone(result["card"])
        self.assertTrue(result["result"].startswith("spawn_failed: "))

    def test_a_merge_automation_puts_the_merge_section_in_the_brief(self):
        added = self.add(may_merge_prs=True)
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        brief = (start.state_dir() / "briefs" / f"{result['card']}.md").read_text(encoding="utf-8")
        self.assertIn("## GitHub pull requests", brief)
        self.assertIn("gh pr merge", brief)


class RepositoryRunTests(AutomationTestCase):
    def test_a_repository_automation_starts_in_a_worktree(self):
        repo = self.root / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit",
                                      "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        board = self.store.init_board(repo_identity(repo))
        start.register_checkout(repo)
        added = self.add(board=board.stem)
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertEqual(result["result"], "started")
        record = json.loads((start.state_dir() / "starts.json").read_text())[result["card"]]
        self.assertEqual(record["mode"], "repo")
        self.assertEqual(self.store.show(result["card"]).board, board.stem)


class TickTests(AutomationTestCase):
    def test_no_run_before_the_first_due_time(self):
        self.add(created=local(2026, 10, 23, 10, 0))
        result = automations.tick(self.store, now=local(2026, 10, 23, 10, 5))
        self.assertEqual(result["runs"], [])
        self.assertEqual(self.cards(), [])

    def test_one_run_per_due_time(self):
        added = self.add(created=local(2026, 10, 23, 8, 0))
        first = automations.tick(self.store, now=local(2026, 10, 23, 9, 2))
        self.assertEqual([r["id"] for r in first["runs"]], [added.id])
        again = automations.tick(self.store, now=local(2026, 10, 23, 9, 7))
        self.assertEqual(again["runs"], [])
        self.assertEqual(len(self.cards()), 1)
        automations.tick(self.store, now=local(2026, 10, 24, 9, 1))
        self.assertEqual(len(self.cards()), 2)

    def test_catch_up_spawns_one_card_and_logs_the_skipped_times(self):
        self.add(created=local(2026, 10, 20, 8, 0))
        automations.tick(self.store, now=local(2026, 10, 20, 9, 1))
        result = automations.tick(self.store, now=local(2026, 10, 23, 9, 30))
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["skipped"], 2)
        cards = self.cards()
        self.assertEqual(len(cards), 2)
        messages = [e["message"] for e in activity.read(self.config, result["runs"][0]["card"])]
        self.assertTrue(any("skipped 2" in m for m in messages))
        self.assertIn("2026-10-23 09:00", messages[0])

    def test_a_paused_automation_never_runs(self):
        added = self.add()
        automations.set_enabled(self.store, added.id, False)
        self.assertEqual(automations.tick(self.store, now=local(2026, 10, 23, 9, 1))["runs"], [])

    def test_a_resumed_automation_starts_with_the_next_due_time(self):
        added = self.add()
        automations.set_enabled(self.store, added.id, False)
        automations.set_enabled(self.store, added.id, True, now=local(2026, 10, 23, 10, 0))
        self.assertEqual(automations.tick(self.store, now=local(2026, 10, 23, 10, 5))["runs"], [])
        self.assertEqual(len(automations.tick(self.store, now=local(2026, 10, 24, 9, 1))["runs"]), 1)

    def test_a_changed_schedule_starts_with_its_next_due_time(self):
        added = self.add(schedule="every week on friday")
        automations.edit(self.store, added.id, schedule="every day", now=local(2026, 10, 23, 10, 0))
        self.assertEqual(automations.tick(self.store, now=local(2026, 10, 23, 10, 5))["runs"], [])

    def test_a_held_tick_lock_makes_a_second_tick_exit(self):
        self.add()
        with start._lock(automations.TICK_LOCK):
            result = automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        self.assertTrue(result["busy"])
        self.assertEqual(self.cards(), [])

    def test_a_failed_start_is_not_retried(self):
        self.add(created=local(2026, 10, 23, 8, 0))
        self.fake.fail = "workspace create"
        automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        self.fake.calls.clear()
        self.assertEqual(automations.tick(self.store, now=local(2026, 10, 23, 9, 6))["runs"], [])
        self.assertFalse(any(c[:2] == ["agent", "start"] for c in self.fake.calls))

    def test_an_unfinished_last_card_does_not_stop_the_next_run(self):
        self.add(created=local(2026, 10, 22, 8, 0))
        automations.tick(self.store, now=local(2026, 10, 22, 9, 1))
        automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        self.assertEqual(len(self.cards()), 2)

    def test_listing_has_the_next_due_and_the_last_run(self):
        added = self.add(created=local(2026, 10, 23, 8, 0))
        automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        entry = automations.listing(self.store, now=local(2026, 10, 23, 9, 2))[0]
        self.assertEqual(entry["id"], added.id)
        self.assertEqual(entry["next_due"], local(2026, 10, 24, 9, 0).isoformat())
        self.assertEqual(entry["last_result"], "started")
        self.assertTrue(entry["last_card"].startswith("t"))
        self.assertTrue(entry["last_run"])


class BriefMergeTests(unittest.TestCase):
    def brief(self, **options):
        return brief_text(todo(), notes=[], repository="github.com/acme/api", worktree="/w", branch="b",
                          base="main", command="herdboard", **options)

    def test_without_merge_the_brief_is_unchanged(self):
        self.assertEqual(self.brief(may_merge_prs=False), self.brief())
        self.assertNotIn("gh pr merge", self.brief())

    def test_merge_adds_the_section_and_keeps_the_branch_rule(self):
        text = self.brief(may_merge_prs=True)
        self.assertIn("## GitHub pull requests", text)
        self.assertIn("gh pr merge", text)
        self.assertIn("checks pass", text)
        self.assertIn("Do not push or merge your branch", text)


class CliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeHerdr(Path(self.tmp.name))
        self.enterContext(patch.object(start, "command", self.fake))

    def test_add_list_edit_pause_resume_remove(self):
        added = self.run_json("automation", "add", "--title", "Check alerts", "--body", "Look\nclosely",
                              "--tag", "security", "--type", "chore", "--schedule", "every weekday at 07:30",
                              "--may-merge-prs")["automation"]
        self.assertEqual(added["board"], "inbox")
        self.assertEqual(added["body"], "Look\nclosely")
        self.assertIs(added["may_merge_prs"], True)
        listed = self.run_json("automation", "list")["automations"]
        self.assertEqual([a["id"] for a in listed], [added["id"]])
        self.assertIsNotNone(listed[0]["next_due"])
        self.assertIsNone(listed[0]["last_run"])
        edited = self.run_json("automation", "edit", added["id"], "--title", "Check all alerts", "--type", "",
                               "--no-merge-prs")["automation"]
        self.assertEqual(edited["title"], "Check all alerts")
        self.assertIsNone(edited["type"])
        self.assertIs(edited["may_merge_prs"], False)
        self.assertEqual(edited["tags"], ["security"])
        self.assertIs(self.run_json("automation", "pause", added["id"])["automation"]["enabled"], False)
        self.assertIs(self.run_json("automation", "resume", added["id"])["automation"]["enabled"], True)
        self.run_json("automation", "remove", added["id"], expect=1)
        removed = self.run_json("automation", "remove", added["id"], "--yes")["removed"]
        self.assertEqual(removed["id"], added["id"])
        self.assertEqual(self.run_json("automation", "list")["automations"], [])

    def test_text_output(self):
        self.run_cli("automation", "add", "--title", "Check alerts", "--schedule", "every day")
        out, _ = self.run_cli("automation", "list")
        self.assertIn("Check alerts", out)
        self.assertIn("every day", out)

    def test_error_codes_for_bad_schedule_and_unknown_board(self):
        error = self.run_json("automation", "add", "--title", "x", "--schedule", "now and then", expect=1)["error"]
        self.assertEqual(error["code"], "bad_schedule")
        error = self.run_json("automation", "add", "--title", "x", "--schedule", "every day", "--board", "nope",
                              expect=1)["error"]
        self.assertEqual(error["code"], "unknown_board")
        error = self.run_json("automation", "edit", "a000", "--title", "x", expect=1)["error"]
        self.assertEqual(error["code"], "not_found")

    def test_body_file(self):
        body = Path(self.tmp.name) / "body.md"
        body.write_text("From a file\n", encoding="utf-8")
        added = self.run_json("automation", "add", "--title", "x", "--schedule", "every day",
                              "--body-file", str(body))["automation"]
        self.assertEqual(added["body"], "From a file\n")

    def test_run_now_spawns_and_starts(self):
        added = self.run_json("automation", "add", "--title", "Say hello", "--schedule", "every day")["automation"]
        result = self.run_json("automation", "run", added["id"])
        self.assertEqual(result["result"], "started")
        card = self.run_json("show", result["card"])["card"]
        self.assertEqual(card["column"], "In progress")
        again = self.run_json("automation", "run", added["id"])
        self.assertNotEqual(again["card"], result["card"])

    def test_tick_runs_due_automations(self):
        self.run_json("automation", "add", "--title", "Say hello", "--schedule", "every day")
        result = self.run_json("automation", "tick")
        self.assertIs(result["busy"], False)
        self.assertEqual(result["runs"], [])

    def test_attach_and_remove(self):
        added = self.run_json("automation", "add", "--title", "x", "--schedule", "every day")["automation"]
        source = Path(self.tmp.name) / "notes.txt"
        source.write_text("hi", encoding="utf-8")
        attached = self.run_json("automation", "attach", added["id"], str(source), "--name", "plan.md")
        self.assertEqual(attached["attachment"]["name"], "plan.md")
        listed = self.run_json("automation", "list")["automations"][0]
        self.assertEqual([a["name"] for a in listed["attachments"]], ["plan.md"])
        removed = self.run_json("automation", "attach", added["id"], "--remove", "plan.md")
        self.assertEqual(removed["removed"]["name"], "plan.md")
        error = self.run_json("automation", "attach", "a000", str(source), expect=1)["error"]
        self.assertEqual(error["code"], "not_found")


class FakeSystem:
    """Records systemctl and launchctl calls; `is-active` answers with `state`."""

    def __init__(self):
        self.calls = []
        self.state = "active"
        self.loaded = True

    def __call__(self, argv, timeout=120):
        self.calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if self.state == "active" else 3, self.state + "\n", "")
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, "", "")
        if argv[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(argv, 3, "", "Boot-out failed: 3: No such process")
        return subprocess.CompletedProcess(argv, 0, "", "")


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("herdr", "herdboard", "gh", "git"):
            tool = self.bin / name
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
        env = patch.dict(os.environ, {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
                                      "XDG_STATE_HOME": str(self.root / "state"),
                                      "PATH": f"{self.bin}:/usr/bin:/bin"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_BIN_PATH", None)
        os.environ.pop("HERDBOARD_CONFIG", None)
        self.system = FakeSystem()
        runner = patch.object(start, "command", self.system)
        runner.start()
        self.addCleanup(runner.stop)
        # The installed `herdboard`, outside this Python's environment.
        found = patch.object(start, "herdboard_command", lambda config_file=None: "herdboard"
                             + (f" --config {config_file}" if config_file else ""))
        found.start()
        self.addCleanup(found.stop)

    def platform(self, name):
        patcher = patch.object(automations.sys, "platform", name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_linux_install_writes_units_and_enables_the_timer(self):
        self.platform("linux")
        result = automations.timer("install")
        units = self.root / "config" / "systemd" / "user"
        service = (units / "herdboard-automations.service").read_text()
        timer = (units / "herdboard-automations.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn(f'ExecStart="{self.bin / "herdboard"}" "automation" "tick"', service)
        self.assertIn(f'"HERDR_BIN_PATH={self.bin / "herdr"}"', service)
        self.assertIn(f"PATH={self.bin}:", service)
        self.assertIn("OnCalendar=*:0/5", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(["systemctl", "--user", "daemon-reload"], self.system.calls)
        self.assertIn(["systemctl", "--user", "enable", "--now", "herdboard-automations.timer"], self.system.calls)
        self.assertEqual(result["backend"], "systemd")
        self.assertIs(result["active"], True)

    def test_a_config_file_that_is_not_the_default_is_named(self):
        self.platform("linux")
        config = self.root / "other.toml"
        automations.timer("install", config_file=config)
        service = (self.root / "config" / "systemd" / "user" / "herdboard-automations.service").read_text()
        self.assertIn(f'"--config" "{config}"', service)
        self.assertIn(f'"HERDBOARD_CONFIG={config}"', service)

    def test_linux_remove_and_status(self):
        self.platform("linux")
        automations.timer("install")
        self.system.state = "inactive"
        status = automations.timer("status")
        self.assertIs(status["active"], False)
        self.assertIs(status["installed"], True)
        automations.timer("remove")
        units = self.root / "config" / "systemd" / "user"
        self.assertFalse((units / "herdboard-automations.service").exists())
        self.assertFalse((units / "herdboard-automations.timer").exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", "herdboard-automations.timer"], self.system.calls)

    def test_macos_install_writes_the_plist_and_bootstraps_it(self):
        self.platform("darwin")
        result = automations.timer("install")
        path = self.root / "home" / "Library" / "LaunchAgents" / "com.nulluminati.herdboard-automations.plist"
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data["Label"], "com.nulluminati.herdboard-automations")
        self.assertEqual(data["ProgramArguments"], [str(self.bin / "herdboard"), "automation", "tick"])
        self.assertEqual(data["StartInterval"], 300)
        self.assertIs(data["RunAtLoad"], True)
        self.assertEqual(data["EnvironmentVariables"]["HERDR_BIN_PATH"], str(self.bin / "herdr"))
        self.assertTrue(data["EnvironmentVariables"]["PATH"].startswith(str(self.bin)))
        self.assertEqual(data["StandardOutPath"], str(self.root / "state" / "herdboard" / "automations.log"))
        domain = f"gui/{os.getuid()}"
        self.assertIn(["launchctl", "bootout", f"{domain}/com.nulluminati.herdboard-automations"], self.system.calls)
        self.assertIn(["launchctl", "bootstrap", domain, str(path)], self.system.calls)
        self.assertEqual(result["backend"], "launchd")
        self.assertIs(result["active"], True)

    def test_macos_remove_and_status(self):
        self.platform("darwin")
        automations.timer("install")
        self.system.loaded = False
        self.assertIs(automations.timer("status")["active"], False)
        automations.timer("remove")
        path = self.root / "home" / "Library" / "LaunchAgents" / "com.nulluminati.herdboard-automations.plist"
        self.assertFalse(path.exists())

    def test_missing_herdr_fails(self):
        self.platform("linux")
        (self.bin / "herdr").unlink()
        with self.assertRaises(HerdboardError) as raised:
            automations.timer("install")
        self.assertEqual(raised.exception.code, "herdr_not_found")

    def test_other_platforms_fail(self):
        self.platform("win32")
        with self.assertRaises(HerdboardError) as raised:
            automations.timer("install")
        self.assertEqual(raised.exception.code, "unsupported_platform")


if __name__ == "__main__":
    unittest.main()
