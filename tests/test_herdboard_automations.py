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

    def test_intervals_longer_than_a_year_are_refused(self):
        for text in ("every 2000 days", "every 366 days", "every 53 weeks"):
            with self.subTest(text=text), self.assertRaises(HerdboardError) as raised:
                parse_schedule(text)
            self.assertEqual(raised.exception.code, "bad_schedule")
        parse_schedule("every 365 days")
        parse_schedule("every 52 weeks")


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

    def test_codex_takes_its_own_thinking_levels(self):
        added = self.add(agent="codex", model="gpt-5.5", thinking="ultra")
        self.assertEqual((added.agent, added.thinking), ("codex", "ultra"))
        for agent, thinking in (("codex", "off"), ("pi", "ultra")):
            with self.subTest(agent=agent), self.assertRaises(HerdboardError) as raised:
                self.add(agent=agent, thinking=thinking)
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

    def test_a_codex_automation_starts_codex(self):
        added = self.add(agent="codex", model="gpt-5.5", thinking="xhigh")
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertEqual(result["result"], "started")
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertEqual(call[call.index("--kind") + 1], "codex")
        self.assertIn("gpt-5.5", call)

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

    def test_a_failed_agent_launch_moves_the_card_back(self):
        added = self.add()
        self.fake.fail = "agent start"
        result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertTrue(result["result"].startswith("start_failed: "))
        self.assertEqual(self.store.show(result["card"]).column, self.config.columns[0])
        # A later start by hand moves the card again.
        record = json.loads((start.state_dir() / "starts.json").read_text())[result["card"]]
        self.assertNotIn("move", record["steps_done"])
        start.Starter(self.store, self.root).start(result["card"], no_repo=True)
        self.assertEqual(self.store.show(result["card"]).column, self.config.start_column)

    def test_a_spawn_failure_is_recorded(self):
        added = self.add()
        # A board that is gone after the automation was saved.
        broken = Automation(**{**added.__dict__, "board": "gone"})
        result = automations.run(self.store, broken, local(2026, 10, 23, 9, 0))
        self.assertIsNone(result["card"])
        self.assertTrue(result["result"].startswith("spawn_failed: "))

    def test_a_failed_attachment_copy_keeps_the_card_linked_and_logged(self):
        added = self.add()
        source = self.root / "plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        attachments.add(self.store, added.id, source)
        with patch.object(automations.attachments, "add", side_effect=HerdboardError("disk full")):
            result = automations.run(self.store, added, local(2026, 10, 23, 9, 0))
        self.assertTrue(result["result"].startswith("spawn_failed: "))
        self.assertIsNotNone(result["card"])
        self.assertEqual(automations.read_state()[added.id]["last_card"], result["card"])
        messages = [entry["message"] for entry in activity.read(self.config, result["card"])]
        self.assertTrue(any(f"Spawned by automation {added.id}" in message for message in messages), messages)
        self.assertTrue(any("disk full" in message for message in messages), messages)

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

    def test_the_restart_time_is_written_under_the_definitions_lock(self):
        added = self.add()
        held = []

        def restart(automation_id, now):
            try:
                with start._lock(automations.DEFINITIONS_LOCK, wait=0):
                    held.append(False)
            except HerdboardError:
                held.append(True)

        with patch.object(automations, "_restart", side_effect=restart):
            automations.set_enabled(self.store, added.id, False)
            automations.set_enabled(self.store, added.id, True)
            automations.edit(self.store, added.id, schedule="every week on friday")
        self.assertEqual(held, [True, True])

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

    def test_a_broken_automation_does_not_stop_the_others(self):
        path = automations.definitions_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[[automation]]\nid = "a001"\ntitle = "Broken"\nschedule = "every 2000 days"\n'
                        'created = "2026-10-20T08:00:00+02:00"\n'
                        '[[automation]]\nid = "a002"\ntitle = "Good"\nschedule = "every day"\n'
                        'created = "2026-10-20T08:00:00+02:00"\n', encoding="utf-8")
        result = automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        self.assertEqual([run["id"] for run in result["runs"]], ["a002"])
        self.assertEqual([error["id"] for error in result["errors"]], ["a001"])

    def tick_with(self, during_first_run):
        """Tick at 09:01 with two due automations; `during_first_run(second)` runs while the first one starts."""
        first = self.add(title="First", created=local(2026, 10, 22, 8, 0))
        second = self.add(title="Second", created=local(2026, 10, 22, 8, 0))
        real_run = automations.run
        started = []

        def run(store, item, due, **options):
            started.append(item)
            if item.id == first.id:
                during_first_run(second)
            return real_run(store, item, due, **options)

        with patch.object(automations, "run", side_effect=run):
            automations.tick(self.store, now=local(2026, 10, 23, 9, 1))
        return [item for item in started if item.id == second.id]

    def test_a_pause_during_another_run_stops_the_run(self):
        self.assertEqual(self.tick_with(lambda second: automations.set_enabled(self.store, second.id, False)), [])

    def test_a_removal_during_another_run_stops_the_run(self):
        self.assertEqual(self.tick_with(lambda second: automations.remove(self.store, second.id)), [])

    def test_an_edit_during_another_run_reaches_the_run(self):
        runs = self.tick_with(lambda second: automations.edit(self.store, second.id, may_merge_prs=True))
        self.assertEqual([item.may_merge_prs for item in runs], [True])

    def test_a_resume_during_another_run_waits_for_the_next_due_time(self):
        def pause_and_resume(second):
            automations.set_enabled(self.store, second.id, False)
            automations.set_enabled(self.store, second.id, True, now=local(2026, 10, 23, 9, 1))

        self.assertEqual(self.tick_with(pause_and_resume), [])

    def ticks_over(self, start_utc, hours):
        """Tick every 10 minutes for `hours` hours from a UTC time, as the timer does."""
        moment = start_utc
        while moment < start_utc + timedelta(hours=hours):
            automations.tick(self.store, now=moment.astimezone())
            moment += timedelta(minutes=10)

    def test_a_due_time_in_the_skipped_spring_hour_runs_once_an_hour_later(self):
        from datetime import timezone
        self.add(schedule="every day at 02:30", created=local(2026, 3, 28, 12, 0))
        # Berlin skips 02:00 to 03:00 on 2026-03-29.
        self.ticks_over(datetime(2026, 3, 28, 22, 0, tzinfo=timezone.utc), 6)
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("2026-03-29 03:30", activity.read(self.config, cards[0].ref)[0]["message"])

    def test_a_due_time_in_the_repeated_autumn_hour_runs_once(self):
        from datetime import timezone
        self.add(schedule="every day at 02:30", created=local(2026, 10, 24, 12, 0))
        # Berlin repeats 02:00 to 03:00 on 2026-10-25.
        self.ticks_over(datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc), 6)
        self.assertEqual(len(self.cards()), 1)

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

    def test_plan_takes_codex(self):
        added = self.run_json("automation", "add", "--title", "Check alerts", "--schedule", "every day",
                              "--agent", "codex", "--thinking", "ultra")["automation"]
        self.assertEqual((added["agent"], added["thinking"]), ("codex", "ultra"))
        with patch("herdboard.automation_plan.AutomationPlanner.plan",
                   return_value={"id": added["id"], "agent": "codex", "model": None, "pane_id": "w1:p1",
                                 "plan": "x", "workspace_id": "w1", "worktree_path": "/w", "branch": None,
                                 "base": None}) as plan:
            self.run_json("automation", "plan", added["id"], "--agent", "codex", "--thinking", "ultra")
        self.assertEqual(plan.call_args.kwargs["agent"], "codex")
        self.assertEqual(plan.call_args.kwargs["thinking"], "ultra")

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

    def test_the_job_path_has_the_codex_folder(self):
        self.platform("linux")
        codex = self.root / "codex-bin"
        codex.mkdir()
        (codex / "codex").write_text("#!/bin/sh\n")
        (codex / "codex").chmod(0o755)
        os.environ["PATH"] += f":{codex}"
        automations.timer("install")
        service = (self.root / "config" / "systemd" / "user" / "herdboard-automations.service").read_text()
        self.assertIn(str(codex), service)

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

    def test_the_default_config_file_and_custom_xdg_folders_reach_the_job(self):
        self.platform("linux")
        # The CLI always passes the resolved config file, also when it is the default one.
        config = self.root / "config" / "herdboard" / "config.toml"
        automations.timer("install", config_file=config)
        service = (self.root / "config" / "systemd" / "user" / "herdboard-automations.service").read_text()
        self.assertIn(f'"--config" "{config}"', service)
        self.assertIn(f'"HERDBOARD_CONFIG={config}"', service)
        self.assertIn(f'"XDG_CONFIG_HOME={self.root / "config"}"', service)
        self.assertIn(f'"XDG_STATE_HOME={self.root / "state"}"', service)

    def test_unset_xdg_folders_stay_unset(self):
        self.platform("darwin")
        os.environ.pop("XDG_CONFIG_HOME")
        os.environ.pop("XDG_STATE_HOME")
        automations.timer("install")
        path = self.root / "home" / "Library" / "LaunchAgents" / "com.nulluminati.herdboard-automations.plist"
        env = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
        self.assertNotIn("XDG_CONFIG_HOME", env)
        self.assertNotIn("XDG_STATE_HOME", env)

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


class PlanAutomationTests(LocalTime):
    def setUp(self):
        super().setUp()
        from herdboard import automation_plan, plan
        from test_herdboard_plan import PlanHerdr

        self.automation_plan = automation_plan
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=Test", "-c", "user.email=test@example.com",
                                                     "commit", "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.store.ensure_inbox()
        self.board = self.store.init_board(repo_identity(self.repo))
        start.register_checkout(self.repo)
        self.fake = PlanHerdr(self.root)
        self.enterContext(patch.object(start, "command", self.fake))
        self.workers = []
        self.enterContext(patch.object(automation_plan, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))
        self.enterContext(patch.object(plan, "POLL_SECONDS", 0))
        self.planner = automation_plan.AutomationPlanner(self.store)

    def add(self, board):
        return automations.add(self.store, title="Merge safe Dependabot PRs", schedule="every weekday at 07:30",
                               board=board, body="Check the open security alerts.")

    def brief(self):
        call = self.fake.find("agent", "start")[0]
        message = call[call.index("--") + 1]
        return Path(message.split("Read the brief at ")[1].split(" first")[0]).read_text()

    def log(self, automation_id):
        return [entry["message"] for entry in activity.read(self.config, automation_id)]

    def test_a_repository_automation_plans_in_a_temporary_worktree(self):
        added = self.add(self.board.stem)
        result = self.planner.plan(added.id)
        create = self.fake.find("worktree", "create")[0]
        self.assertEqual(create[create.index("--branch") + 1], f"auto/{added.id}-plan")
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(create[create.index("--label") + 1], f"plan {added.id}")
        self.assertEqual(result["plan"], str(attachments.plan_path(self.config, added.id)))
        records = json.loads((start.state_dir() / "plans.json").read_text())
        self.assertEqual(records[added.id]["pane_id"], result["pane_id"])
        self.assertEqual(self.workers[0][0][-4:], ["plan-wait", added.id, "--pane", result["pane_id"]])
        brief = self.brief()
        for part in (added.id, "every weekday at 07:30", self.board.stem, "Check the open security alerts.",
                     "unattended", str(attachments.plan_path(self.config, added.id))):
            self.assertIn(part, brief)
        self.assertIn("Planning started", self.log(added.id)[0])

    def test_an_inbox_automation_plans_in_its_folder(self):
        added = self.add("inbox")
        result = self.planner.plan(added.id)
        create = self.fake.find("workspace", "create")[0]
        folder = self.config.todos_dir / ".herdboard" / "work" / added.id
        self.assertEqual(create[create.index("--cwd") + 1], str(folder))
        self.assertEqual(create[create.index("--label") + 1], f"plan {added.id}")
        self.assertTrue(folder.is_dir())
        self.assertEqual(result["worktree_path"], str(folder))
        self.assertFalse(self.fake.find("worktree", "create"))

    def test_codex_plans_an_automation(self):
        added = self.add("inbox")
        result = self.planner.plan(added.id, agent="codex", model="gpt-5.5", thinking="high")
        self.assertEqual(result["agent"], "codex")
        call = self.fake.find("agent", "start")[0]
        self.assertEqual(call[call.index("--kind") + 1], "codex")
        self.assertIn("gpt-5.5", call)
        with self.assertRaises(HerdboardError) as raised:
            self.planner.plan(self.add("inbox").id, agent="claude", thinking="high")
        self.assertEqual(raised.exception.code, "usage")

    def test_a_running_planner_blocks_a_second_one(self):
        added = self.add("inbox")
        self.planner.plan(added.id)
        with self.assertRaises(HerdboardError) as raised:
            self.planner.plan(added.id)
        self.assertEqual(raised.exception.code, "plan_running")

    def test_the_worker_removes_a_clean_worktree_and_its_branch(self):
        added = self.add(self.board.stem)
        result = self.planner.plan(added.id)
        worktree = Path(result["worktree_path"])
        self.assertTrue(worktree.exists())
        plan_file = attachments.plan_path(self.config, added.id)
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        self.fake.on_poll = lambda: plan_file.write_text("# Plan\n")
        self.assertEqual(self.automation_plan.wait(self.store, added.id, result["pane_id"]), 0)
        self.assertFalse(worktree.exists())
        branches = subprocess.run(["git", "-C", str(self.repo), "branch", "--list", f"auto/{added.id}-plan"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")
        log = self.log(added.id)
        self.assertTrue(any(m.startswith("Plan saved") for m in log))
        self.assertIn("Removed the planning worktree", log[-1])

    def test_the_worker_keeps_a_worktree_with_changes(self):
        added = self.add(self.board.stem)
        result = self.planner.plan(added.id)
        worktree = Path(result["worktree_path"])
        (worktree / "scratch.txt").write_text("left over")
        self.automation_plan.wait(self.store, added.id, result["pane_id"])
        self.assertTrue(worktree.exists())
        self.assertIn("Kept the planning worktree", self.log(added.id)[-1])

    def test_the_worker_closes_an_inbox_workspace(self):
        added = self.add("inbox")
        result = self.planner.plan(added.id)
        self.automation_plan.wait(self.store, added.id, result["pane_id"])
        self.assertTrue(self.fake.find("workspace", "close"))

    def test_the_worker_keeps_a_worktree_that_a_later_planner_opened(self):
        added = self.add(self.board.stem)
        result = self.planner.plan(added.id)
        worktree = Path(result["worktree_path"])
        real_wait = self.automation_plan.plan_module.wait
        later = []

        def wait(store, ref, pane):
            code = real_wait(store, ref, pane)
            # A second planner starts after the first one ended, before the worker cleans up.
            later.append(self.planner.plan(added.id))
            return code

        with patch.object(self.automation_plan.plan_module, "wait", side_effect=wait):
            self.automation_plan.wait(self.store, added.id, result["pane_id"])
        self.assertEqual(later[0]["worktree_path"], str(worktree))
        self.assertNotEqual(later[0]["pane_id"], result["pane_id"])
        self.assertTrue(worktree.exists())
        self.assertFalse(self.fake.find("worktree", "remove"))
        self.assertIn("A later planner uses", self.log(added.id)[-1])


class DoctorTests(unittest.TestCase):
    def setUp(self):
        from herdboard import doctor

        self.doctor = doctor
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.todos = self.root / "todos"
        self.config_file = self.root / "config.toml"
        self.config_file.write_text(f'todos_dir = "{self.todos}"\n[linear]\nenabled = false\n')
        env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
                                      "XDG_CONFIG_HOME": str(self.root / "config"), "LINEAR_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        self.system = FakeSystem()
        self.enterContext(patch.object(start, "command", self.system))
        self.enterContext(patch.object(automations.sys, "platform", "linux"))
        self.store = Store(Config(todos_dir=self.todos))
        self.store.ensure_inbox()

    def codes(self):
        return [f["code"] for f in self.doctor.diagnose(self.config_file, offline=True)["findings"]
                if f["code"].startswith("automation")]

    def write(self, text):
        path = automations.definitions_path(self.store.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_no_automations_no_findings(self):
        self.assertEqual(self.codes(), [])
        self.assertFalse(any(c[:1] == ["systemctl"] for c in self.system.calls))

    def test_a_good_automation_with_an_active_timer_has_no_findings(self):
        automations.add(self.store, title="x", schedule="every day")
        self.assertEqual(self.codes(), [])

    def test_an_inactive_timer_is_a_warning(self):
        automations.add(self.store, title="x", schedule="every day")
        self.system.state = "inactive"
        self.assertEqual(self.codes(), ["automation_timer_inactive"])

    def test_invalid_toml_is_an_error(self):
        self.write("[[automation]\n")
        self.assertEqual(self.codes(), ["automations_invalid"])

    def test_bad_schedule_missing_board_and_duplicate_ids(self):
        self.write('[[automation]]\nid = "a001"\ntitle = "x"\nschedule = "sometimes"\nboard = "inbox"\n'
                   '[[automation]]\nid = "a001"\ntitle = "y"\nschedule = "every day"\nboard = "gone"\n')
        self.assertEqual(sorted(self.codes()), ["automation_board_missing", "automation_duplicate_id",
                                                "automation_schedule_invalid"])


if __name__ == "__main__":
    unittest.main()
