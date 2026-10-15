from datetime import datetime, timezone
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

import linear_data  # noqa: E402
from herdboard import linear_source  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.linear import LinearClient, LinearError  # noqa: E402
from herdboard.linear_cards import column_for, has_pull_request, issue_card  # noqa: E402
from herdboard.linear_source import CacheMiss, LinearDisabledError, LinearSource, ProjectNotFound  # noqa: E402
from linear_fake import FakeLinear  # noqa: E402

DEAD = "http://127.0.0.1:1/graphql"


class LinearEnvTestCase(unittest.TestCase):
    """A fake Linear server, a private state directory and a controlled environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        env = {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.state),
               "XDG_CONFIG_HOME": str(self.root / "xdg"), "HERDBOARD_CONFIG": "",
               "LINEAR_API_KEY": "test-key"}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.fixtures = linear_data.fixtures()
        self.fake = self.enterContext(FakeLinear(self.fixtures, page_size=50))
        self.endpoint = self.fake.endpoint
        self.enterContext(mock.patch.object(linear_source, "build_client", self.build_client))

    def build_client(self, key):
        return LinearClient(key, endpoint=self.endpoint, timeout=2.0, sleep=lambda _: None)

    def source(self, **kwargs):
        return LinearSource(Config(todos_dir=self.root / "todos"), **kwargs)

    def requests(self, operation):
        return [r for r in self.fake.requests if r["operationName"] == operation]


class SourceTests(LinearEnvTestCase):
    def test_paused_fetch_cannot_restore_cache_after_concurrent_write(self):
        import threading
        from herdboard.linear_writes import LinearWriter
        linear_data.with_workflow(self.fixtures)
        source = self.source()
        source.account()
        snapshot_ready, resume = threading.Event(), threading.Event()
        results = []

        def load(client):
            snapshot = client.my_issues(source.settings.done_days)
            snapshot_ready.set()
            self.assertTrue(resume.wait(5))
            return snapshot

        thread = threading.Thread(target=lambda: results.append(source._fetch("mine", load)))
        thread.start()
        try:
            self.assertTrue(snapshot_ready.wait(5))
            LinearWriter(Config(), self.source()).move("ENG-1", "In progress")
        finally:
            resume.set()
            thread.join(5)
        self.assertEqual(len(results), 1)
        self.assertFalse((source.account_dir() / "mine.json").exists())
        with self.assertRaises(CacheMiss):
            self.source(offline=True).my_issues()

    def test_invalidation_ignores_undeleted_cache_in_views_and_issue_search(self):
        source = self.source()
        source.my_issues()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            source.drop_cache()
        offline = self.source(offline=True)
        with self.assertRaises(CacheMiss):
            offline.my_issues()
        with self.assertRaises(CacheMiss):
            offline.issue("ENG-1")

    def test_disabled_without_key(self):
        del os.environ["LINEAR_API_KEY"]
        source = self.source()
        self.assertFalse(source.active)
        with self.assertRaises(LinearDisabledError):
            source.my_issues()
        self.assertEqual(self.fake.requests, [])

    def test_fetch_writes_cache_and_key_map(self):
        fetched = self.source().my_issues()
        self.assertFalse(fetched.stale)
        self.assertEqual({i["identifier"] for i in fetched.data}, {"ENG-1", "ENG-2", "ENG-4", "ENG-5", "ENG-6"})
        cache = self.state / "herdboard" / "o1" / "u1" / "mine.json"
        stored = json.loads(cache.read_text())
        self.assertEqual(stored["fetched_at"], fetched.fetched_at.isoformat())
        keys = list((self.state / "herdboard" / "keys").glob("*.json"))
        self.assertEqual(len(keys), 1)
        self.assertRegex(keys[0].stem, r"^[0-9a-f]{16}$")
        self.assertEqual(json.loads(keys[0].read_text()), {"org": "o1", "user": "u1"})
        self.assertNotIn("test-key", keys[0].read_text())

    def test_project_issues_checks_project_and_filters_labels(self):
        fetched = self.source().project_issues("p-plat", ["l-web"])
        self.assertEqual([i["identifier"] for i in fetched.data["issues"]], ["ENG-3"])
        self.assertEqual(fetched.data["project"]["name"], "Platform")

    def test_missing_project_raises_and_is_not_cached(self):
        with self.assertRaises(ProjectNotFound):
            self.source().project_issues("gone")
        self.assertFalse((self.state / "herdboard" / "o1" / "u1").exists())

    def test_network_error_falls_back_to_stale_cache(self):
        first = self.source().my_issues()
        self.endpoint = DEAD
        again = self.source().my_issues()
        self.assertTrue(again.stale)
        self.assertEqual(again.fetched_at, first.fetched_at)
        self.assertEqual(again.data, first.data)

    def test_timeout_falls_back_to_stale_cache(self):
        first = self.source().my_issues()
        self.fake.delay = 0.5
        source = self.source()
        source_client = source.client
        source_client.timeout = 0.1
        self.assertTrue(source.my_issues().stale)
        self.assertEqual(first.data, source.my_issues().data)

    def test_network_error_without_cache_raises(self):
        self.endpoint = DEAD
        with self.assertRaises(LinearError):
            self.source().my_issues()

    def test_network_error_with_known_account_but_no_query_cache(self):
        self.source().my_issues()
        self.endpoint = DEAD
        with self.assertRaises(CacheMiss):
            self.source().project_issues("p-plat")

    def test_offline_reads_cache_only(self):
        self.source().my_issues()
        count = len(self.fake.requests)
        fetched = self.source(offline=True).my_issues()
        self.assertTrue(fetched.stale)
        self.assertEqual(len(self.fake.requests), count)

    def test_offline_without_cache_raises(self):
        with self.assertRaises(CacheMiss):
            self.source(offline=True).my_issues()
        self.assertEqual(self.fake.requests, [])

    def test_authentication_error_does_not_fall_back(self):
        self.source().my_issues()
        os.environ["LINEAR_API_KEY"] = "wrong-key"
        with self.assertRaisesRegex(LinearError, "[Aa]uthentication"):
            self.source().my_issues()

    def test_authentication_error_after_viewer_does_not_fall_back(self):
        source = self.source()
        source.my_issues()
        self.fake.api_key = "rotated"
        with self.assertRaisesRegex(LinearError, "[Aa]uthentication"):
            source.my_issues()

    def test_cache_is_keyed_by_account(self):
        self.source().my_issues()
        other = linear_data.fixtures()
        other["viewer"] = {**linear_data.VIEWER, "id": "u2", "organization": {"id": "o2", "name": "W", "urlKey": "w"}}
        other["issues"] = []
        self.fake.fixtures.clear()
        self.fake.fixtures.update(other)
        self.fake.api_key = "second-key"
        os.environ["LINEAR_API_KEY"] = "second-key"
        self.assertEqual(self.source().my_issues().data, [])
        self.endpoint = DEAD
        self.assertEqual(self.source().my_issues().data, [])
        os.environ["LINEAR_API_KEY"] = "test-key"
        self.assertEqual(len(self.source().my_issues().data), 5)

    def test_unknown_key_offline_has_no_cache(self):
        self.source().my_issues()
        os.environ["LINEAR_API_KEY"] = "someone-else"
        with self.assertRaises(CacheMiss):
            self.source(offline=True).my_issues()

    def test_graphql_error_does_not_fall_back(self):
        self.source().my_issues()
        self.fake.responses = [(200, {"data": {"viewer": linear_data.VIEWER}}, {}),
                               (200, {"errors": [{"message": "Denied"}]}, {})]
        with self.assertRaisesRegex(LinearError, "Denied"):
            self.source().my_issues()


class RefreshTests(LinearEnvTestCase):
    def test_network_down_lasts_for_one_refresh(self):
        source = self.source()
        first = source.my_issues()
        self.assertFalse(first.stale)
        source.client.endpoint = DEAD
        source.begin_refresh()
        self.assertTrue(source.my_issues().stale)
        # Within the refresh Linear is not contacted again.
        source.client.endpoint = self.endpoint
        count = len(self.fake.requests)
        self.assertTrue(source.my_issues().stale)
        self.assertEqual(len(self.fake.requests), count)
        source.begin_refresh()
        again = source.my_issues()
        self.assertFalse(again.stale)
        self.assertGreater(len(self.fake.requests), count)
        self.assertGreater(again.fetched_at, first.fetched_at)

    def test_offline_stays_cache_only_across_refreshes(self):
        self.source().my_issues()
        source = self.source(offline=True)
        count = len(self.fake.requests)
        source.begin_refresh()
        self.assertTrue(source.my_issues().stale)
        self.assertEqual(len(self.fake.requests), count)

    def test_missing_labels_are_reported_and_valid_labels_still_filter(self):
        fetched = self.source().project_issues("p-plat", ["l-web", "gone"])
        self.assertEqual(fetched.data["missing_labels"], ["gone"])
        self.assertEqual([i["identifier"] for i in fetched.data["issues"]], ["ENG-3"])
        none = self.source().project_issues("p-plat", ["gone"])
        self.assertEqual(none.data["missing_labels"], ["gone"])
        self.assertEqual(none.data["issues"], [])
        ok = self.source().project_issues("p-plat", ["l-web"])
        self.assertEqual(ok.data["missing_labels"], [])

    def test_done_days_setting_reaches_the_client(self):
        self.source().my_issues()
        mine = self.requests("MyIssues")[-1]["variables"]["filter"]
        self.assertEqual(mine["or"][1], {"completedAt": {"gte": "-P14D"}})
        config = Config(todos_dir=self.root / "todos")
        config.linear.done_days = 3
        LinearSource(config).project_issues("p-plat")
        issues = self.requests("ProjectIssues")[-1]["variables"]["filter"]
        self.assertEqual(issues["or"][1], {"completedAt": {"gte": "-P3D"}})

    def test_older_result_never_replaces_a_newer_cache_file(self):
        path = self.root / "cache.json"
        newer = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        older = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)
        linear_source.write_cache(path, newer, {"v": "new"})
        linear_source.write_cache(path, older, {"v": "old"})
        self.assertEqual(json.loads(path.read_text())["data"], {"v": "new"})
        linear_source.write_cache(path, newer, {"v": "same time"})
        self.assertEqual(json.loads(path.read_text())["data"], {"v": "same time"})

    def test_fetched_time_is_the_time_the_request_started(self):
        self.fake.delay = 0.3
        fetched = self.source().my_issues()
        waited = datetime.now(timezone.utc) - fetched.fetched_at
        self.assertGreaterEqual(waited.total_seconds(), 0.3)

    def test_issue_is_looked_up_directly_and_cached(self):
        source = self.source()
        fetched = source.issue("ENG-3")
        self.assertEqual(fetched.data["identifier"], "ENG-3")
        self.assertFalse(fetched.stale)
        self.assertEqual(self.requests("ProjectIssues"), [])
        self.assertIsNone(source.issue("ENG-99").data)
        offline = self.source(offline=True).issue("ENG-3")
        self.assertTrue(offline.stale)
        self.assertEqual(offline.data["identifier"], "ENG-3")

    def test_issue_search_covers_every_cached_view(self):
        self.source().project_issues("p-plat")
        self.endpoint = DEAD
        found = self.source(offline=True).issue("eng-3")
        self.assertTrue(found.stale)
        self.assertEqual(found.data["identifier"], "ENG-3")
        with self.assertRaises(CacheMiss):
            self.source(offline=True).issue("ENG-99")


class PinnedSourceTests(LinearEnvTestCase):
    def test_pinned_issues_are_fetched_by_id_and_cached(self):
        fetched = self.source().pinned_issues(["i5", "i1", "i404"])
        self.assertEqual([i["identifier"] for i in fetched.data], ["ENG-1", "ENG-5"])
        self.assertFalse(fetched.stale)
        self.assertEqual(self.requests("PinnedIssues")[0]["variables"]["filter"]["id"],
                         {"in": ["i5", "i1", "i404"]})
        self.endpoint = DEAD
        offline = self.source(offline=True).pinned_issues(["i1", "i404", "i5"])
        self.assertTrue(offline.stale)
        self.assertEqual([i["identifier"] for i in offline.data], ["ENG-1", "ENG-5"])
        with self.assertRaises(CacheMiss):
            self.source(offline=True).pinned_issues(["i1"])

    def test_issue_finds_a_pinned_issue_offline(self):
        self.source().pinned_issues(["i5"])
        self.endpoint = DEAD
        found = self.source(offline=True).issue("ENG-5")
        self.assertEqual(found.data["id"], "i5")

    def test_search_returns_open_issues_of_my_teams(self):
        self.fixtures["issues"].append({**linear_data.make_issue(7), "identifier": "OPS-7",
                                        "team": linear_data.OPS})
        self.fixtures["my_team_ids"] = ["t-eng"]
        found = self.source().search("issue")
        self.assertEqual([i["identifier"] for i in found], ["ENG-1", "ENG-2", "ENG-3", "ENG-5"])
        self.assertEqual(found[0], {"id": "i1", "identifier": "ENG-1", "title": "Issue 1",
                                    "state": {"name": "Todo", "type": "unstarted"},
                                    "team": {"key": "ENG"}, "project": {"name": "Platform"}})
        self.assertEqual(self.requests("SearchIssues")[0]["variables"]["filter"]["team"],
                         {"id": {"in": ["t-eng"]}})

    def test_search_for_an_identifier_fetches_that_issue_from_any_team(self):
        self.fixtures["issues"].append({**linear_data.make_issue(7), "identifier": "OPS-7",
                                        "team": linear_data.OPS, "project": None})
        self.fixtures["my_team_ids"] = ["t-eng"]
        found = self.source().search(" ops-7 ")
        self.assertEqual(found, [{"id": "i7", "identifier": "OPS-7", "title": "Issue 7",
                                  "state": {"name": "Todo", "type": "unstarted"},
                                  "team": {"key": "OPS"}, "project": None}])
        self.assertEqual(self.requests("SearchIssues"), [])
        self.assertEqual(self.source().search("ENG-404"), [])

    def test_search_needs_linear(self):
        with self.assertRaises(LinearDisabledError):
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
                self.source().search("issue")
        with self.assertRaisesRegex(LinearError, "connection"):
            self.source(offline=True).search("issue")
        self.assertEqual(self.requests("SearchIssues"), [])


class CardTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.when = datetime(2026, 10, 10, 12, 3, tzinfo=timezone.utc)

    def card(self, issue, identities=()):
        return issue_card(self.config, issue, set(identities), self.when, False)

    def flat(self, issue):
        issue = dict(issue)
        issue["labels"] = issue["labels"]["nodes"]
        issue["attachments"] = [a["url"] for a in issue["attachments"]["nodes"]]
        return issue

    def test_fields(self):
        card = self.card(self.flat(linear_data.make_issue(2, state=("In Review", "started"),
                                                          labels=("l-api", "l-bug"))))
        self.assertEqual((card.ref, card.board, card.column, card.title, card.state, card.team),
                         ("ENG-2", "Platform", "Review", "Issue 2", "In Review", "ENG"))
        self.assertEqual((card.source, card.read_only, card.path, card.checked), ("linear", True, None, False))
        self.assertEqual((card.tags, card.type), (["repo:api"], "bug"))
        self.assertEqual(card.body, ["Line one", "Line two"])
        self.assertEqual(card.due, "2026-10-20")
        self.assertEqual(card.url, "https://linear.app/workspace/issue/ENG-2")
        self.assertEqual(card.fetched_at, self.when.isoformat())

    def test_a_sub_issue_names_its_parent(self):
        issue = linear_data.make_issue(2)
        issue["parent"] = {"id": "i1", "identifier": "ENG-1"}
        self.assertEqual(self.card(self.flat(issue)).parent, "ENG-1")
        self.assertIsNone(self.card(self.flat(linear_data.make_issue(1))).parent)

    def test_without_project_description_or_due(self):
        card = self.card(self.flat(linear_data.make_issue(6, project=None, description=None, due=None,
                                                          state=("Shipped", "completed"))))
        self.assertEqual((card.board, card.column, card.body, card.due, card.checked),
                         ("Linear", "Done", [], None, True))

    def test_columns_by_type_and_name_override(self):
        types = {"triage": "Backlog", "backlog": "Backlog", "unstarted": "Todo", "started": "In progress",
                 "completed": "Done"}
        for kind, column in types.items():
            self.assertEqual(column_for(self.config, {"name": "X", "type": kind}), column)
        self.assertEqual(column_for(self.config, {"name": "In Review", "type": "started"}), "Review")
        self.assertEqual(column_for(self.config, {"name": "in review", "type": "started"}), "Review")
        self.config.linear.state_names["Doing"] = "Todo"
        self.assertEqual(column_for(self.config, {"name": "Doing", "type": "started"}), "Todo")

    def test_canceled_is_hidden_even_with_a_name_override(self):
        self.config.linear.state_names["Dropped"] = "Done"
        self.assertIsNone(self.card(self.flat(linear_data.make_issue(4, state=("Dropped", "canceled")))))

    def test_pr_marker(self):
        issue = self.flat(linear_data.make_issue(1, attachments=(linear_data.PR_URL,)))
        self.assertTrue(self.card(issue, {"github.com/acme/api"}).pr)
        self.assertFalse(self.card(issue, {"github.com/acme/other"}).pr)
        self.assertFalse(self.card(issue, set()).pr)

    def test_pr_urls(self):
        ident = {"github.com/acme/api"}
        for url, expected in [
            ("https://github.com/acme/api/pull/1", True),
            ("https://GitHub.com/ACME/Api/pull/12/files", True),
            ("https://github.com/acme/api/issues/1", False),
            ("https://github.com/acme/api-extra/pull/1", False),
            ("https://gitlab.com/acme/api/pull/1", False),
        ]:
            with self.subTest(url=url):
                self.assertEqual(has_pull_request([url], ident), expected)
        self.assertFalse(has_pull_request(["https://github.com/acme/api/pull/1"], {"gitlab.com/acme/api"}))
        self.assertTrue(has_pull_request(["x", "https://github.com/acme/api/pull/1"], {"github.com/acme/api"}))


if __name__ == "__main__":
    unittest.main()
