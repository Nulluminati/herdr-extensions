import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

import linear_data  # noqa: E402
from herdboard.board import (HELP_KEYS, CardMenuScreen, PromoteScreen, StateScreen,  # noqa: E402
                             card_actions, promote_refusal)
from herdboard.linear_links import LabelLink, ProjectLink  # noqa: E402
from herdboard.linear_source import state_dir  # noqa: E402
from herdboard.store import CardView  # noqa: E402
from textual.widgets import OptionList  # noqa: E402
from test_herdboard_board import SIZE, settle  # noqa: E402
from test_herdboard_linear_write_board import LinearWriteBoardCase  # noqa: E402
from test_herdboard_mouse import content_offset  # noqa: E402

TITLE = "Renovate the bathroom #home 📅 2026-11-02"


def todo(**fields):
    return CardView(**{"ref": "t1", "board": "api", "path": None, "column": "Todo", "title": "Task",
                       "checked": False, "due": None, "tags": [], "body": [], **fields})


def prompts(options: OptionList) -> list[str]:
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


class MenuRuleTests(unittest.TestCase):
    def actions(self, card):
        return [action for _label, action in card_actions(card)]

    def test_a_top_level_todo_offers_promotion(self):
        self.assertIn("promote", self.actions(todo()))
        self.assertIn("T  Promote to Linear", [label for label, _ in card_actions(todo())])

    def test_a_checked_todo_offers_promotion(self):
        self.assertIn("promote", self.actions(todo(checked=True)))

    def test_cards_that_cannot_be_promoted_do_not_offer_it(self):
        cards = {
            "linear": todo(source="linear", ref="ENG-1", read_only=True),
            "read only": todo(read_only=True),
            "archived": todo(archived=True),
            "sub-card": todo(parent="t9"),
            "promoted": todo(title="Task → ENG-7"),
            "worktree": todo(worktree=True),
        }
        for name, card in cards.items():
            with self.subTest(card=name):
                self.assertNotIn("promote", self.actions(card))
                self.assertIsNotNone(promote_refusal(card))
        self.assertIsNone(promote_refusal(todo()))

    def test_help_lists_T(self):
        self.assertIn("T", [key for key, _what in HELP_KEYS])


class PromoteBoardCase(LinearWriteBoardCase):
    def setUp(self):
        super().setUp()
        self.fixtures["projects"].append({"id": "p-plat2", "name": "platform", "teams": [linear_data.ENG]})
        self.target = self.store.add(self.repo_path, TITLE, "Todo", body=["Ask for two quotes"])

    def card(self, app, title):
        return next(c for d in app.columns for c in d.cards if title in c.title and c.source == "todo")

    async def wait_for(self, pilot, predicate, what):
        for _ in range(100):
            await pilot.pause(0.05)
            if predicate():
                return
        self.fail(f"Timed out: {what}")

    async def open_form(self, pilot, title="Renovate"):
        self.select(pilot.app, self.card(pilot.app, title).ref)
        await pilot.press("T")
        await self.wait_for(pilot, lambda: isinstance(pilot.app.screen, PromoteScreen), "the form opens")
        screen = pilot.app.screen
        await self.wait_for(pilot, lambda: screen.teams is not None or screen.failed, "teams load")
        return screen

    async def choose(self, pilot, selector, index):
        options = pilot.app.screen.query_one(selector, OptionList)
        options.focus()
        options.highlighted = index
        await pilot.press("enter")
        await pilot.pause()

    async def choose_team(self, pilot, key):
        screen = pilot.app.screen
        index = [team["key"] for team in screen.teams].index(key)
        await self.choose(pilot, "#promote-teams", index)
        await self.wait_for(pilot, lambda: screen.projects is not None or screen.failed, "projects load")

    async def choose_project(self, pilot, project_id):
        screen = pilot.app.screen
        index = [None, *(p["id"] for p in screen.projects)].index(project_id)
        await self.choose(pilot, "#promote-projects", index)

    async def promote(self, pilot, team="ENG", project="p-plat"):
        await self.open_form(pilot)
        await self.choose_team(pilot, team)
        await self.choose_project(pilot, project)
        await pilot.press("ctrl+s")
        await settle(pilot)
        await settle(pilot)

    def creates(self):
        return [r["variables"]["input"] for r in self.requests("IssueCreate")]

    def board_text(self):
        return self.repo_path.read_text(encoding="utf-8")


class FormTests(PromoteBoardCase):
    async def test_T_and_the_menu_open_the_same_form(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.open_form(pilot)
            await pilot.press("escape")
            await pilot.pause()
            app.open_card_menu(self.card(app, "Renovate"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, CardMenuScreen), "the menu opens")
            menu = app.screen.query_one("#card-menu", OptionList)
            menu.highlighted = prompts(menu).index("T  Promote to Linear")
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(app.screen, PromoteScreen), "the form opens")

    async def test_nothing_starts_selected_and_promote_needs_both_choices(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            # The board links the Platform project; the form still starts empty.
            self.assertEqual(prompts(screen.query_one("#promote-teams", OptionList)),
                             ["ENG  Engineering", "OPS  Operations"])
            self.assertIsNone(screen.team)
            self.assertFalse(screen.project_chosen)
            self.assertFalse(screen.key_applies("promote"))
            self.assertIn("creates Linear issues", screen.query_one("#promote-note").plain)
            self.assertIn("Done", screen.query_one("#promote-note").plain)
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIs(app.screen, screen)
            await self.choose_team(pilot, "ENG")
            self.assertFalse(screen.key_applies("promote"))
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIs(app.screen, screen)
            await self.choose_project(pilot, None)
            self.assertTrue(screen.key_applies("promote"))
            self.assertEqual(self.creates(), [])

    async def test_projects_belong_to_the_team_and_names_stay_apart(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            labels = prompts(screen.query_one("#promote-projects", OptionList))
            self.assertEqual(labels[0], "No project")
            self.assertEqual([p["id"] for p in screen.projects], ["p-plat", "p-mob", "p-dup1", "p-plat2"])
            platforms = [label for label in labels if label.casefold().startswith("platform")]
            self.assertEqual(len(platforms), 2)
            self.assertNotEqual(platforms[0], platforms[1])
            self.assertIn("p-plat2", platforms[1])
            await self.choose_project(pilot, "p-plat2")
            self.assertEqual(screen.project["id"], "p-plat2")

    async def test_a_team_change_clears_the_project(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            await self.choose_project(pilot, "p-plat")
            self.assertTrue(screen.key_applies("promote"))
            await self.choose_team(pilot, "OPS")
            self.assertIsNone(screen.project)
            self.assertFalse(screen.project_chosen)
            self.assertFalse(screen.key_applies("promote"))
            self.assertEqual([p["id"] for p in screen.projects], ["p-mob", "p-dup2"])

    async def test_an_obsolete_project_list_does_not_replace_the_current_one(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            gate = threading.Event()
            load = screen.load_projects

            def slow_for_eng(team_id):
                if team_id == "t-eng":
                    gate.wait(5)
                return load(team_id)

            screen.load_projects = slow_for_eng
            await self.choose(pilot, "#promote-teams", 0)
            await self.choose_team(pilot, "OPS")
            gate.set()
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(screen.team["key"], "OPS")
            self.assertEqual([p["id"] for p in screen.projects], ["p-mob", "p-dup2"])

    async def test_the_mouse_chooses_team_and_project(self):
        app = self.make_linear_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            teams = screen.query_one("#promote-teams", OptionList)
            await pilot.click(teams, offset=content_offset(teams, 1, 1))
            await self.wait_for(pilot, lambda: screen.projects is not None, "projects load")
            self.assertEqual(screen.team["key"], "OPS")
            projects = screen.query_one("#promote-projects", OptionList)
            await pilot.click(projects, offset=content_offset(projects, 1, 0))
            await pilot.pause()
            self.assertTrue(screen.project_chosen)
            self.assertIsNone(screen.project)

    async def test_a_team_without_projects_says_so_and_allows_no_project(self):
        self.fixtures["projects"] = [p for p in self.fixtures["projects"]
                                     if linear_data.OPS not in p["teams"]]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            await self.choose_team(pilot, "OPS")
            self.assertEqual(prompts(screen.query_one("#promote-projects", OptionList)), ["No project"])
            self.assertIn("no projects", screen.query_one("#promote-status").plain)
            await self.choose_project(pilot, None)
            self.assertTrue(screen.key_applies("promote"))

    async def test_no_teams_says_so(self):
        self.fixtures["teams"] = []
        app = self.make_linear_app(active=False)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            self.assertEqual(screen.teams, [])
            self.assertIn("no Linear teams", screen.query_one("#promote-status").plain)
            self.assertFalse(screen.key_applies("promote"))

    async def test_a_rejected_key_is_an_error_in_the_form(self):
        app = self.make_linear_app(active=False)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.fake.api_key = "another-key"
            screen = await self.open_form(pilot)
            self.assertTrue(screen.failed)
            status = screen.query_one("#promote-status")
            self.assertIn("error", status.classes)
            self.assertIn("API key", status.plain)
            self.assertFalse(screen.key_applies("promote"))

    async def test_escape_closes_the_form_and_creates_nothing(self):
        app = self.make_linear_app()
        before = self.board_text()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            await self.choose_project(pilot, "p-plat")
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, PromoteScreen)
            self.assertEqual(self.creates(), [])
            self.assertEqual(self.board_text(), before)


class RefusalTests(PromoteBoardCase):
    async def assert_refused(self, pilot, ref, message):
        app = pilot.app
        self.select(app, ref)
        app._notifications.clear()
        await pilot.press("T")
        await settle(pilot)
        self.assertNotIsInstance(app.screen, PromoteScreen)
        warnings = [m for m, s in self.notifications(app) if s == "warning"]
        self.assertTrue(any(message in m for m in warnings), warnings)

    async def test_T_refuses_linear_cards_sub_cards_and_cards_with_worktrees(self):
        child = self.store.add(self.repo_path, "Choose tiles", "Todo", parent=self.target.ref)
        started = self.store.add(self.repo_path, "Started todo", "Todo")
        self.start_record(started.ref)
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.assert_refused(pilot, "ENG-1", "local todo")
            await self.assert_refused(pilot, child.ref, "sub-card")
            await self.assert_refused(pilot, started.ref, "worktree")
            self.assertEqual(self.creates(), [])
            self.assertEqual(self.requests("Teams"), [])

    async def test_a_sub_card_with_a_worktree_blocks_its_parent(self):
        child = self.store.add(self.repo_path, "Choose tiles", "Todo", parent=self.target.ref)
        self.start_record(child.ref)
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.assert_refused(pilot, self.target.ref, "Choose tiles")

    async def test_without_a_writer_the_form_does_not_open(self):
        app = self.make_linear_app()
        app.linear_writer = None
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.assert_refused(pilot, self.target.ref, "Linear writes are not available")

    async def test_submit_checks_the_card_again(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            await self.choose_project(pilot, "p-plat")
            # The card gets a worktree while the form is open.
            self.start_record(self.target.ref)
            await pilot.press("ctrl+s")
            await settle(pilot)
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("worktree" in m for m in errors), errors)
            self.assertEqual(self.creates(), [])

    def start_record(self, ref):
        worktree = self.root / f"worktree-{ref}"
        worktree.mkdir()
        path = state_dir() / "starts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = json.loads(path.read_text()) if path.exists() else {}
        records[ref] = {"ref": ref, "worktree_path": str(worktree), "branch": f"feat/{ref}",
                        "base": "main", "agent": "claude"}
        path.write_text(json.dumps(records))


class PromoteTests(PromoteBoardCase):
    async def test_promotion_creates_the_issues_and_completes_the_todo(self):
        self.store.update_links(self.repo_path, lambda _: [
            ProjectLink("p-plat", "Platform", (LabelLink("l-api", "repo:api"),))])
        self.store.add(self.repo_path, "Choose tiles", "Todo", parent=self.target.ref)
        self.store.edit(self.target.ref, title=TITLE, body=["Ask for two quotes", "- [ ] Book the plumber"])
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            fetches = len(self.requests("ProjectIssues"))
            await self.promote(pilot, "ENG", "p-plat")
            parent, checklist, sub_card = self.creates()
            self.assertEqual(parent["title"], "Renovate the bathroom")
            self.assertEqual((parent["teamId"], parent["projectId"]), ("t-eng", "p-plat"))
            self.assertEqual(parent["assigneeId"], "u1")
            self.assertEqual(parent["dueDate"], "2026-11-02")
            self.assertEqual(parent["description"], "Ask for two quotes\n\n#home")
            # The linked project's labels are not added: the form chose the project.
            self.assertNotIn("labelIds", parent)
            self.assertEqual(checklist["title"], "Book the plumber")
            self.assertEqual(sub_card["title"], "Choose tiles")
            self.assertEqual(checklist["parentId"], parent["id"])
            self.assertEqual(sub_card["parentId"], parent["id"])
            card = self.card(app, "Renovate")
            self.assertTrue(card.title.endswith("→ ENG-7"), card.title)
            self.assertEqual(card.column, "Done")
            self.assertTrue(card.checked)
            self.assertEqual(app.current_card().ref, card.ref)
            notices = [m for m, s in self.notifications(app) if s == "information"]
            self.assertTrue(any("ENG-7" in m and "2 sub-issues" in m for m in notices), notices)
            self.assertGreater(len(self.requests("ProjectIssues")), fetches)
            self.assertFalse(app.writing)

    async def test_no_project_creates_an_issue_without_one(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.promote(pilot, "OPS", None)
            (parent,) = self.creates()
            self.assertEqual(parent["teamId"], "t-ops")
            self.assertNotIn("projectId", parent)
            self.assertIn("→ OPS-", self.card(app, "Renovate").title)

    async def test_a_project_that_left_the_team_is_refused(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            await self.choose_project(pilot, "p-plat")
            self.fixtures["projects"][0]["teams"] = [linear_data.OPS]
            await pilot.press("ctrl+s")
            await settle(pilot)
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("Platform" in m and "ENG" in m for m in errors), errors)
            self.assertEqual(self.creates(), [])

    async def test_a_second_submit_while_busy_promotes_once(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            screen = await self.open_form(pilot)
            await self.choose_team(pilot, "ENG")
            await self.choose_project(pilot, "p-plat")
            choice = (screen.team, screen.project)
            ref = screen.ref
            app.submit_promote(ref, choice)
            app.submit_promote(ref, choice)
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(len(self.creates()), 1)
            self.assertIn((f"Busy: {ref} is writing", "warning"), self.notifications(app))

    async def test_a_failure_is_a_notification_and_T_finishes_the_recorded_promotion(self):
        self.store.edit(self.target.ref, title=TITLE, body=["- [ ] Book the plumber"])
        self.fake.mutation_failures = [None, "http_500"]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            fetches = len(self.requests("ProjectIssues"))
            await self.promote(pilot, "ENG", "p-plat")
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("Press T" in m for m in errors), errors)
            self.assertNotIn("→", self.card(app, "Renovate").title)
            self.assertGreater(len(self.requests("ProjectIssues")), fetches)
            app._notifications.clear()
            # Other choices on the retry: the recorded plan wins, with a warning.
            await self.promote(pilot, "OPS", "p-mob")
            self.assertEqual(len([i for i in self.fixtures["issues"]
                                  if i["title"] == "Renovate the bathroom"]), 1)
            self.assertTrue(self.card(app, "Renovate").title.endswith("→ ENG-7"))
            warnings = [m for m, s in self.notifications(app) if s == "warning"]
            self.assertTrue(any("team OPS" in m and "ENG" in m for m in warnings), warnings)

    async def test_no_project_on_the_retry_warns_against_the_recorded_project(self):
        self.store.edit(self.target.ref, title=TITLE, body=["- [ ] Book the plumber"])
        self.fake.mutation_failures = [None, "http_500"]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.promote(pilot, "ENG", "p-plat")
            app._notifications.clear()
            await self.promote(pilot, "ENG", None)
            self.assertTrue(self.card(app, "Renovate").title.endswith("→ ENG-7"))
            self.assertEqual(self.creates()[-1]["projectId"], "p-plat")
            warnings = [m for m, s in self.notifications(app) if s == "warning"]
            self.assertIn("project none is ignored: the recorded plan uses project Platform", warnings)


class StateChoiceTests(PromoteBoardCase):
    def setUp(self):
        super().setUp()
        linear_data.add_state(self.fixtures, "ENG", "Planned", "unstarted")

    async def submit(self, pilot):
        await self.open_form(pilot)
        await self.choose_team(pilot, "ENG")
        await self.choose_project(pilot, None)
        await pilot.press("ctrl+s")
        await self.wait_for(pilot, lambda: isinstance(pilot.app.screen, StateScreen), "the state dialog")
        return pilot.app.screen

    async def test_escape_in_the_state_dialog_cancels_the_promotion(self):
        app = self.make_linear_app()
        before = self.board_text()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await self.submit(pilot)
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(self.creates(), [])
            self.assertEqual(self.board_text(), before)
            warnings = [m for m, s in self.notifications(app) if s == "warning"]
            self.assertTrue(any("cancelled" in m for m in warnings), warnings)
            self.assertFalse(app.writing)

    async def test_the_chosen_state_is_used(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            dialog = await self.submit(pilot)
            options = dialog.query_one(OptionList)
            options.highlighted = prompts(options).index("Planned")
            await pilot.press("enter")
            await settle(pilot)
            await settle(pilot)
            (parent,) = self.creates()
            self.assertEqual(parent["stateId"], "ws-ENG-Planned")


if __name__ == "__main__":
    unittest.main()
