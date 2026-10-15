import copy
import sys
import unittest
from pathlib import Path
from unittest import mock
import http.client
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.linear import (LinearAuthError, LinearClient, LinearError,  # noqa: E402
                              LinearNetworkError)
from herdboard.linear import LABELS_PAGE_QUERY  # noqa: E402
from herdboard.model import HerdboardError  # noqa: E402
from linear_fake import FakeLinear  # noqa: E402


VIEWER = {"id": "u1", "name": "Nate", "email": "nate@example.com",
          "organization": {"id": "o1", "name": "Workspace", "urlKey": "workspace"}}
STATES = [{"id": f"s{i}", "name": f"State {i}", "type": "started", "position": i}
          for i in range(5)]
TEAMS = [{"id": f"t{i}", "key": f"T{i}", "name": f"Team {i}", "states": STATES}
         for i in range(5)]
PROJECTS = [{"id": f"p{i}", "name": "Platform" if i % 2 else "PLATFORM",
             "teams": [{"id": t["id"], "key": t["key"]} for t in TEAMS]}
            for i in range(5)] + [{"id": "other", "name": "Platform extra", "teams": []}]
LABELS = [{"id": f"l{i}", "name": "Repo:api" if i % 2 else "repo:API",
           "team": {"id": "t0", "key": "T0"} if i % 2 else None}
          for i in range(5)]


def issue(i):
    return {"id": f"i{i}", "identifier": f"T0-{i}", "title": f"Issue {i}",
            "description": "Details" if i else None, "dueDate": "2026-10-20" if i else None,
            "url": f"https://linear.app/workspace/issue/T0-{i}", "branchName": f"nate/t0-{i}",
            "sortOrder": float(i), "priority": i, "updatedAt": "2026-07-01T00:00:00Z",
            "state": {"id": "s1", "name": "Active", "type": "started"},
            "team": {"id": "t0", "key": "T0"},
            "labels": {"nodes": [{"id": f"l{i % 2}", "name": "repo:api"}]},
            "project": {"id": "p0", "name": "PLATFORM"},
            "parent": {"id": "parent", "identifier": "T0-99"} if i else None,
            "children": {"nodes": [{"identifier": "T0-100"}] if i else []},
            "assignee": {"id": "u1", "name": "Nate"},
            "attachments": {"nodes": [{"url": "https://github.com/acme/api/pull/1"}]}}


RECENT_OR = [{"completedAt": {"null": True}}, {"completedAt": {"gte": "-P14D"}}]


class LinearTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = copy.deepcopy({"viewer": VIEWER, "teams": TEAMS,
                                       "projects": PROJECTS, "labels": LABELS,
                                       "issues": [issue(i) for i in range(5)]})
        self.fake = self.enterContext(FakeLinear(self.fixtures))
        self.waits = []
        self.client = LinearClient("test-key", endpoint=self.fake.endpoint, sleep=self.waits.append)

    def assert_pages(self, operation, count=3):
        requests = [r for r in self.fake.requests if r["operationName"] == operation]
        self.assertEqual(len(requests), count)
        self.assertEqual([r["variables"]["after"] for r in requests[:3]], [None, "2", "4"])
        self.assertTrue(all(r["variables"]["first"] > 0 for r in requests))

    def test_error_contract(self):
        self.assertTrue(issubclass(LinearError, HerdboardError))
        self.assertEqual((LinearError.code, LinearError.exit_code), ("linear", 3))

    def test_viewer_and_raw_authorization(self):
        self.assertEqual(self.client.viewer(), VIEWER)
        request = self.fake.requests[0]
        self.assertEqual(request["authorization"], "test-key")
        self.assertNotIn("Bearer", request["authorization"])
        self.assertIn("query Viewer", request["query"])

    def test_teams_and_all_states_paginate(self):
        self.assertEqual(self.client.teams(), TEAMS)
        self.assert_pages("Teams")
        requests = [r for r in self.fake.requests if r["operationName"] == "TeamStates"]
        # The first page of states comes inline; only the rest needs requests.
        self.assertEqual(len(requests), 10)
        for team in TEAMS:
            self.assertEqual([r["variables"]["after"] for r in requests
                              if r["variables"]["id"] == team["id"]], ["2", "4"])

    def test_projects_case_insensitive_and_paginated_teams(self):
        self.assertEqual(self.client.find_projects("platform"), PROJECTS[:5])
        self.assert_pages("Projects")
        requests = [r for r in self.fake.requests if r["operationName"] == "ProjectTeams"]
        self.assertEqual(len(requests), 10)
        self.assertEqual(self.fake.requests[0]["variables"]["filter"],
                         {"name": {"eqIgnoreCase": "platform"}})

    def test_project(self):
        self.assertEqual(self.client.project("p0"), {"id": "p0", "name": "PLATFORM"})

    def test_project_for_write_by_id_has_current_name_and_paginated_teams(self):
        self.fixtures["projects"][0]["name"] = "Renamed"
        found = self.client.project_for_write("p0")
        self.assertEqual(found, {**PROJECTS[0], "name": "Renamed"})
        request = next(r for r in self.fake.requests if r["operationName"] == "ProjectForWrite")
        self.assertEqual(request["variables"]["filter"], {"id": {"eq": "p0"}})
        self.assertEqual(len([r for r in self.fake.requests if r["operationName"] == "ProjectTeams"]), 2)
        self.assertIsNone(self.client.project_for_write("missing"))

    def test_project_not_found_or_inaccessible(self):
        self.assertIsNone(self.client.project("missing"))

    def test_labels_workspace_and_team(self):
        self.assertEqual(self.client.find_labels("repo:api"), LABELS)
        self.assert_pages("Labels")

    def test_issue_fields_and_pagination(self):
        result = self.client.project_issues("p0")
        expected = []
        for original in self.fixtures["issues"]:
            mapped = copy.deepcopy(original)
            mapped["labels"] = original["labels"]["nodes"]
            mapped["children"] = [c["identifier"] for c in original["children"]["nodes"]]
            mapped["attachments"] = [a["url"] for a in original["attachments"]["nodes"]]
            expected.append(mapped)
        self.assertEqual(result, expected)
        self.assert_pages("ProjectIssues")
        for request in self.fake.requests:
            self.assertNotIn("includeArchived", request["query"])
            if request["operationName"] == "ProjectIssues":
                self.assertIn("branchName", request["query"])
            self.assertNotIn("gitBranchName", request["query"])

    def test_attachment_urls_paginate(self):
        urls = [f"https://github.com/acme/api/pull/{i}" for i in range(5)]
        self.fixtures["issues"][0]["attachments"]["nodes"] = [{"url": url} for url in urls]
        result = self.client.project_issues("p0")
        self.assertEqual(result[0]["attachments"], urls)
        requests = [r for r in self.fake.requests if r["operationName"] == "IssueAttachments"
                    and r["variables"]["id"] == "i0"]
        self.assertEqual([r["variables"]["after"] for r in requests], ["2", "4"])

    def test_label_filter_matches_any_label(self):
        result = self.client.project_issues("p0", ["l0", "missing"])
        self.assertEqual([i["id"] for i in result], ["i0", "i2", "i4"])
        self.assertEqual(self.fake.requests[0]["variables"]["filter"],
                         {"project": {"id": {"eq": "p0"}},
                          "labels": {"some": {"id": {"in": ["l0", "missing"]}}},
                          "or": RECENT_OR})

    def test_empty_label_filter_matches_nothing(self):
        self.assertEqual(self.client.project_issues("p0", []), [])

    def test_my_issues_pagination_and_nullable_fields(self):
        self.assertEqual(len(self.client.my_issues()), 5)
        self.assert_pages("MyIssues")
        self.fixtures["issues"][0]["project"] = None
        self.fixtures["issues"][1]["assignee"] = None
        result = self.client.my_issues()
        self.assertEqual(len(result), 4)
        self.assertIsNone(result[0]["project"])
        result = self.client.project_issues("p0")
        self.assertIsNone(result[0]["assignee"])

    def test_rate_limit_header_then_success(self):
        self.fake.rate_limits = 2
        self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [2.0, 2.0])
        self.assertEqual(len(self.fake.requests), 3)

    def test_rate_limit_http_date_header(self):
        self.fake.rate_limits = 1
        self.fake.retry_after = "Wed, 18 May 2033 03:33:25 GMT"
        with mock.patch("herdboard.linear.time.time", return_value=2000000000):
            self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [5.0])

    def test_invalid_retry_after_uses_backoff(self):
        for header in ("invalid", "NaN", "Infinity"):
            with self.subTest(header=header):
                self.fake.rate_limits = 1
                self.fake.retry_after = header
                self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [1, 1, 1])

    def test_rate_limit_backoff(self):
        self.fake.rate_limits = 3
        self.fake.retry_after = None
        self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [1, 2, 4])

    def test_rate_limit_exhausted(self):
        self.fake.rate_limits = 10
        with self.assertRaisesRegex(LinearError, "[Rr]ate limit"):
            self.client.viewer()
        self.assertEqual(len(self.fake.requests), 4)
        self.assertEqual(len(self.waits), 3)

    def test_zero_retries(self):
        self.client.max_retries = 0
        self.fake.rate_limits = 1
        with self.assertRaises(LinearError):
            self.client.viewer()
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.fake.requests), 1)

    def test_graphql_rate_limit_on_200_and_400(self):
        error = {"errors": [{"message": "Limit reached", "extensions": {"code": "RATELIMITED"}}]}
        self.fake.responses = [(200, error, {}), (400, error, {})]
        self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [1, 2])

    def test_authentication_failure(self):
        self.client.api_key = "wrong-key"
        with self.assertRaisesRegex(LinearAuthError, "[Aa]uthentication"):
            self.client.viewer()
        self.assertEqual(len(self.fake.requests), 1)

    def test_graphql_authentication_failure(self):
        self.fake.responses = [(200, {"errors": [{"message": "Invalid token",
                                                "extensions": {"code": "UNAUTHENTICATED"}}]}, {})]
        with self.assertRaisesRegex(LinearAuthError, "[Aa]uthentication"):
            self.client.viewer()

    def test_linear_authentication_error_is_http_400(self):
        body = {"errors": [{"message": "Authentication required, not authenticated",
                            "extensions": {"type": "authentication error", "code": "AUTHENTICATION_ERROR",
                                           "statusCode": 400}}]}
        self.fake.responses = [(400, body, {})]
        with self.assertRaisesRegex(LinearAuthError, "[Aa]uthentication"):
            self.client.viewer()
        self.assertEqual(len(self.fake.requests), 1)

    def test_forbidden_is_authentication_failure(self):
        self.fake.responses = [(403, {"errors": [{"message": "Forbidden"}]}, {})]
        with self.assertRaises(LinearAuthError):
            self.client.viewer()

    def test_other_errors_are_not_auth_or_network_errors(self):
        self.fake.responses = [(503, b"Unavailable", {})]
        with self.assertRaises(LinearError) as caught:
            self.client.viewer()
        self.assertNotIsInstance(caught.exception, (LinearAuthError, LinearNetworkError))

    def test_rate_limit_as_linear_sends_it(self):
        # HTTP 400, RATELIMITED, and reset headers in epoch milliseconds.
        body = {"errors": [{"message": "Rate limit exceeded", "extensions": {
            "type": "ratelimited", "code": "RATELIMITED", "statusCode": 400}}]}
        now = 2000000000
        headers = {"X-RateLimit-Requests-Reset": str((now + 7) * 1000),
                   "X-RateLimit-Complexity-Reset": str((now + 12) * 1000)}
        self.fake.responses = [(400, body, headers)]
        with mock.patch("herdboard.linear.time.time", return_value=now):
            self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [12.0])

    def test_rate_limit_waits_only_for_the_exhausted_bucket(self):
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        now = 2000000000
        headers = {"X-RateLimit-Requests-Remaining": "100",
                   "X-RateLimit-Requests-Reset": str((now + 3000) * 1000),
                   "X-RateLimit-Complexity-Remaining": "0",
                   "X-RateLimit-Complexity-Reset": str((now + 5) * 1000)}
        self.fake.responses = [(400, body, headers)]
        with mock.patch("herdboard.linear.time.time", return_value=now):
            self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [5.0])

    def test_rate_limit_reset_in_the_past_waits_zero(self):
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        headers = {"X-RateLimit-Requests-Reset": "1000"}
        self.fake.responses = [(400, body, headers)]
        self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [0.0])

    def test_rate_limit_far_reset_fails_without_waiting(self):
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        now = 2000000000
        headers = {"X-RateLimit-Requests-Reset": str((now + 3600) * 1000)}
        self.fake.responses = [(400, body, headers)]
        with mock.patch("herdboard.linear.time.time", return_value=now):
            with self.assertRaisesRegex(LinearError, "allowed again at 2033-05-18T04:33:20Z"):
                self.client.viewer()
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.fake.requests), 1)

    def test_rate_limit_at_the_cap_still_waits(self):
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        now = 2000000000
        self.fake.responses = [(400, body, {"X-RateLimit-Requests-Reset": str((now + 60) * 1000)})]
        with mock.patch("herdboard.linear.time.time", return_value=now):
            self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [60.0])

    def test_long_retry_after_fails_without_waiting(self):
        self.fake.rate_limits = 1
        self.fake.retry_after = "3600"
        with self.assertRaisesRegex(LinearError, "allowed again at"):
            self.client.viewer()
        self.assertEqual(self.waits, [])

    def test_reset_headers_win_over_retry_after(self):
        body = {"errors": [{"message": "Rate limit", "extensions": {"code": "RATELIMITED"}}]}
        now = 2000000000
        headers = {"X-RateLimit-Requests-Reset": str((now + 4) * 1000), "Retry-After": "30"}
        self.fake.responses = [(400, body, headers)]
        with mock.patch("herdboard.linear.time.time", return_value=now):
            self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [4.0])

    def test_rate_limit_ignores_invalid_response_body(self):
        for body in (b"not JSON", [], {"errors": [{"extensions": None}]}):
            with self.subTest(body=body):
                self.fake.responses = [(429, body, {"Retry-After": "1"})]
                self.assertEqual(self.client.viewer(), VIEWER)
        self.assertEqual(self.waits, [1.0, 1.0, 1.0])

    def test_graphql_error_with_null_extensions(self):
        self.fake.responses = [(200, {"errors": [{"message": "Denied", "extensions": None}]}, {})]
        with self.assertRaisesRegex(LinearError, "Denied"):
            self.client.viewer()

    def test_graphql_error_even_with_data(self):
        self.fake.responses = [(200, {"data": {"viewer": VIEWER},
                                      "errors": [{"message": "Permission denied"}]}, {})]
        with self.assertRaisesRegex(LinearError, "Permission denied"):
            self.client.viewer()
        self.assertEqual(self.waits, [])

    def test_http_error(self):
        self.fake.responses = [(503, b"Unavailable", {})]
        with self.assertRaisesRegex(LinearError, "503"):
            self.client.viewer()

    def test_malformed_json(self):
        self.fake.responses = [(200, b"not JSON", {})]
        with self.assertRaisesRegex(LinearError, "JSON"):
            self.client.viewer()

    def test_missing_or_invalid_data(self):
        for body in ({}, {"data": None}, [], {"data": []}):
            with self.subTest(body=body):
                self.fake.responses = [(200, body, {})]
                with self.assertRaisesRegex(LinearError, "data|response"):
                    self.client.viewer()

    def test_timeout(self):
        self.fake.delay = 0.3
        self.client.timeout = 0.2
        with self.assertRaisesRegex(LinearNetworkError, "[Tt]imeout|timed out"):
            self.client.viewer()

    def test_network_error(self):
        for failure in (URLError("connection refused"), TimeoutError("slow"), OSError("reset"),
                        http.client.IncompleteRead(b"")):
            with self.subTest(failure=failure):
                with mock.patch.object(self.client._opener, "open", side_effect=failure):
                    with self.assertRaises(LinearNetworkError):
                        self.client.viewer()
        with mock.patch.object(self.client._opener, "open", side_effect=URLError("connection refused")):
            with self.assertRaisesRegex(LinearError, "connection refused"):
                self.client.viewer()

    def test_endpoint_must_be_https_or_local(self):
        for endpoint in ("http://api.linear.app/graphql", "ftp://127.0.0.1/", "api.linear.app",
                         "http://127.0.0.1.evil.example/graphql"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(LinearError):
                    LinearClient("key", endpoint=endpoint)
        for endpoint in ("https://api.linear.app/graphql", "http://127.0.0.1:8080/x",
                         "http://localhost:8080/x"):
            LinearClient("key", endpoint=endpoint)

    def test_negative_retries_rejected(self):
        with self.assertRaises(LinearError):
            LinearClient("key", endpoint=self.fake.endpoint, max_retries=-1)

    def test_redirect_is_not_followed(self):
        with FakeLinear(self.fixtures) as target:
            self.fake.responses = [(302, b"", {"Location": target.endpoint})]
            with self.assertRaisesRegex(LinearError, "302"):
                self.client.viewer()
            self.assertEqual(target.requests, [])

    def test_labels_by_id(self):
        self.assertEqual(self.client.labels(["l1", "l3", "missing"]), [LABELS[1], LABELS[3]])
        request = self.fake.requests[0]
        self.assertEqual(request["variables"]["filter"], {"id": {"in": ["l1", "l3", "missing"]}})
        self.assertEqual(self.client.labels([]), [])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.client.labels(["missing"]), [])

    def test_issue_by_identifier(self):
        found = self.client.issue("T0-2")
        self.assertEqual(found["id"], "i2")
        self.assertEqual(found["labels"], [{"id": "l0", "name": "repo:api"}])
        self.assertEqual(found["attachments"], ["https://github.com/acme/api/pull/1"])
        self.assertEqual(self.fake.requests[0]["variables"], {"id": "T0-2"})

    def test_issue_not_found(self):
        self.assertIsNone(self.client.issue("T0-404"))

    def test_issue_pages_nested_connections(self):
        self.fixtures["issues"][3]["labels"]["nodes"] = [{"id": f"x{i}", "name": f"x{i}"} for i in range(5)]
        self.assertEqual(len(self.client.issue("T0-3")["labels"]), 5)

    def test_vanished_issue_gives_empty_nested_lists(self):
        # The issue disappears between the list request and its follow-up.
        node = {"id": "gone", "labels": {"nodes": [{"id": "x", "name": "x"}],
                                         "pageInfo": {"hasNextPage": True, "endCursor": "1"}}}
        labels = self.client._rest(node, "labels", "IssueLabels", LABELS_PAGE_QUERY, "issues")
        self.assertEqual(labels, [{"id": "x", "name": "x"}])

    def test_empty_nested_nodes_in_state_and_team_lookups(self):
        self.fake.responses = [(200, {"data": {"teams": {"nodes": [{"id": "t", "key": "T", "name": "T",
                                    "states": {"nodes": [], "pageInfo": {"hasNextPage": False,
                                                                         "endCursor": None}}}],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}, {})]
        self.assertEqual(self.client.teams()[0]["states"], [])

    def test_labels_and_children_beyond_one_page_are_all_returned(self):
        self.fake.page_size = 100
        self.fixtures["issues"] = [issue(0)]
        self.fixtures["issues"][0]["labels"]["nodes"] = [{"id": f"l{i}", "name": f"n{i}"} for i in range(150)]
        self.fixtures["issues"][0]["children"]["nodes"] = [{"identifier": f"T0-{i}"} for i in range(150)]
        result = self.client.project_issues("p0")[0]
        self.assertEqual([label["id"] for label in result["labels"]], [f"l{i}" for i in range(150)])
        self.assertEqual(result["children"], [f"T0-{i}" for i in range(150)])
        follow_ups = [r for r in self.fake.requests if r["operationName"] in ("IssueLabels", "IssueChildren")]
        self.assertEqual([(r["operationName"], r["variables"]["after"]) for r in follow_ups],
                         [("IssueLabels", "100"), ("IssueChildren", "100")])

    def test_exactly_one_inline_page_needs_no_follow_up(self):
        self.fake.page_size = 100
        self.fixtures["issues"] = [issue(0)]
        self.fixtures["issues"][0]["labels"]["nodes"] = [{"id": f"l{i}", "name": "n"} for i in range(100)]
        self.assertEqual(len(self.client.project_issues("p0")[0]["labels"]), 100)
        self.assertEqual([r["operationName"] for r in self.fake.requests], ["ProjectIssues"])

    def test_many_issues_cost_only_the_list_requests(self):
        self.fake.page_size = 50
        self.fixtures["issues"] = [issue(i) for i in range(200)]
        self.assertEqual(len(self.client.project_issues("p0")), 200)
        self.assertEqual([r["operationName"] for r in self.fake.requests], ["ProjectIssues"] * 20)
        self.fake.requests.clear()
        self.assertEqual(len(self.client.my_issues()), 200)
        self.assertEqual([r["operationName"] for r in self.fake.requests], ["MyIssues"] * 20)

    def test_teams_and_projects_without_extra_pages_cost_only_the_list(self):
        self.fake.page_size = 50
        self.assertEqual(self.client.teams(), TEAMS)
        self.assertEqual(self.client.find_projects("platform"), PROJECTS[:5])
        self.assertEqual({r["operationName"] for r in self.fake.requests}, {"Teams", "Projects"})

    def test_done_days_filter(self):
        now = datetime.now(timezone.utc)
        ago = lambda days: (now - timedelta(days=days)).isoformat()
        done = lambda i, days, project="p0": {**issue(i), "completedAt": ago(days),
                                              "state": {"id": "s9", "name": "Done", "type": "completed"}}
        self.fixtures["issues"] = [issue(0), done(1, 3), done(2, 40), done(3, 20)]
        self.assertEqual([i["id"] for i in self.client.project_issues("p0")], ["i0", "i1"])
        self.assertEqual(self.fake.requests[0]["variables"]["filter"]["or"], RECENT_OR)
        self.assertEqual([i["id"] for i in self.client.project_issues("p0", done_days=30)],
                         ["i0", "i1", "i3"])
        self.assertEqual(self.fake.requests[-1]["variables"]["filter"]["or"][1],
                         {"completedAt": {"gte": "-P30D"}})
        self.fake.requests.clear()
        self.assertEqual(len(self.client.project_issues("p0", done_days=None)), 4)
        self.assertNotIn("or", self.fake.requests[0]["variables"]["filter"])

    def test_done_days_filter_for_my_issues(self):
        old = {**issue(1), "completedAt": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
        self.fixtures["issues"] = [issue(0), old]
        self.assertEqual([i["id"] for i in self.client.my_issues()], ["i0"])
        self.assertEqual(self.fake.requests[0]["variables"]["filter"], {"or": RECENT_OR})
        self.assertEqual(len(self.client.my_issues(done_days=60)), 2)
        self.assertEqual(len(self.client.my_issues(done_days=None)), 2)
        self.assertEqual(self.fake.requests[-1]["variables"], {"first": 10, "after": None})

    def test_done_days_must_be_a_non_negative_integer(self):
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.client.project_issues("p0", done_days=value)

    def test_issues_by_ids(self):
        old = {**issue(3), "completedAt": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
        self.fixtures["issues"][3] = old
        found = self.client.issues_by_ids(["i1", "i3", "i4", "missing"])
        self.assertEqual([i["id"] for i in found], ["i1", "i4"])
        self.assertEqual(found[0]["labels"], [{"id": "l1", "name": "repo:api"}])
        self.assertEqual(found[0]["children"], ["T0-100"])
        self.assertEqual(self.fake.requests[0]["variables"]["filter"],
                         {"id": {"in": ["i1", "i3", "i4", "missing"]}, "or": RECENT_OR})
        self.assertEqual(len(self.client.issues_by_ids(["i3"], done_days=60)), 1)
        self.fake.requests.clear()
        self.assertEqual(self.client.issues_by_ids([]), [])
        self.assertEqual(self.fake.requests, [])

    def test_search_issues_sends_teams_and_open_states_and_reads_one_page(self):
        self.fake.page_size = 50
        self.fixtures["issues"][2]["team"] = {"id": "t1", "key": "T1"}
        self.fixtures["issues"][3]["state"] = {"id": "s9", "name": "Done", "type": "completed"}
        found = self.client.search_issues("issue", ["t0"])
        self.assertEqual([i["identifier"] for i in found], ["T0-0", "T0-1", "T0-4"])
        self.assertEqual(found[0], {"id": "i0", "identifier": "T0-0", "title": "Issue 0",
                                    "state": {"name": "Active", "type": "started"},
                                    "team": {"key": "T0"}, "project": {"name": "PLATFORM"}})
        request = self.fake.requests[0]
        self.assertEqual(request["operationName"], "SearchIssues")
        self.assertEqual(request["variables"], {
            "term": "issue", "first": 20,
            "filter": {"team": {"id": {"in": ["t0"]}},
                       "state": {"type": {"nin": ["completed", "canceled"]}}}})
        self.assertEqual(len(self.fake.requests), 1)

    def test_search_issues_without_teams_sends_nothing(self):
        self.assertEqual(self.client.search_issues("issue", []), [])
        self.assertEqual(self.fake.requests, [])

    def test_broken_pagination_fails_without_loop(self):
        self.fake.responses = [(200, {"data": {"issueLabels": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}}}, {})]
        with self.assertRaisesRegex(LinearError, "[Pp]agination|cursor"):
            self.client.find_labels("repo:api")


class LinearWriteTests(unittest.TestCase):
    assert_pages = LinearTests.assert_pages
    # Reuse fixtures without running the read tests a second time.
    def setUp(self):
        LinearTests.setUp(self)
        self.client.timeout = 0.2
        self.client.sleep = lambda seconds: None

    def test_team_states(self):
        self.assertEqual(self.client.team_states("t0"), STATES)
        self.assert_pages("TeamStates")
        self.assertEqual(self.client.team_states("missing"), [])

    def test_team_by_key(self):
        self.assertEqual(self.client.team_by_key("t0"),
                         {"id": "t0", "key": "T0", "name": "Team 0"})
        self.assertIsNone(self.client.team_by_key("missing"))

    def test_my_teams(self):
        self.fixtures["my_team_ids"] = ["t0", "t2", "t4"]
        self.assertEqual(self.client.my_teams(),
                         [{k: t[k] for k in ("id", "key", "name")} for t in TEAMS[::2]])
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("viewer", self.fake.requests[0]["query"])

    def test_issue_for_write(self):
        from uuid import uuid4
        current = self.fixtures["issues"][0]
        current["id"] = str(uuid4())
        expected = {k: current[k] for k in
                    ("id", "identifier", "title", "sortOrder", "state", "team", "project")}
        self.assertEqual(self.client.issue_for_write("T0-0"), expected)
        self.assertEqual(self.client.issue_for_write(current["id"]), expected)
        self.assertIsNone(self.client.issue_for_write("missing"))

    def test_state_sort_orders(self):
        self.assertEqual(self.client.state_sort_orders("t0", "s1", None),
                         [0.0, 1.0, 2.0, 3.0, 4.0])
        self.assert_pages("StateSortOrders")
        self.assertNotIn("title", self.fake.requests[0]["query"])
        self.assertNotIn("or", self.fake.requests[0]["variables"]["filter"])
        self.fixtures["issues"][0]["completedAt"] = (
            datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        self.fixtures["issues"][1]["team"] = {"id": "other", "key": "OTHER"}
        self.fixtures["issues"][2]["state"]["id"] = "other"
        self.assertEqual(self.client.state_sort_orders("t0", "s1", 14), [3.0, 4.0])
        self.assertEqual(self.fake.requests[-1]["variables"]["filter"]["or"], RECENT_OR)

    def test_update_only_given_fields(self):
        result = self.client.update_issue("i0", state_id="s2", sort_order=0.0)
        self.assertEqual(result["state"], {k: STATES[2][k] for k in ("id", "name", "type")})
        self.assertEqual(result["sortOrder"], 0.0)
        self.assertEqual(set(result), {"id", "identifier", "sortOrder", "state", "team"})
        self.assertEqual(self.fake.requests[-1]["variables"],
                         {"id": "i0", "input": {"stateId": "s2", "sortOrder": 0.0}})
        self.client.update_issue("i0", sort_order=5)
        self.assertEqual(self.fake.requests[-1]["variables"]["input"], {"sortOrder": 5})
        self.client.update_issue("i0", state_id="s1")
        self.assertEqual(self.fake.requests[-1]["variables"]["input"], {"stateId": "s1"})

    def test_create_given_id_and_optional_fields(self):
        from uuid import uuid4
        issue_id = str(uuid4())
        result = self.client.create_issue(issue_id=issue_id, team_id="t0", title="Create",
                                          state_id="s2", project_id="p1", label_ids=["l1"],
                                          assignee_id="u1", sort_order=-10.5)
        self.assertEqual(result["id"], issue_id)
        self.assertEqual(result["sortOrder"], -10.5)
        self.assertIn("url", result)
        self.assertEqual(self.fake.requests[-1]["variables"]["input"],
                         {"id": issue_id, "teamId": "t0", "title": "Create", "stateId": "s2",
                          "projectId": "p1", "labelIds": ["l1"], "assigneeId": "u1",
                          "sortOrder": -10.5})
        found = self.client.issue(issue_id)
        self.assertEqual(found["project"], {"id": "p1", "name": "Platform"})
        self.assertEqual(found["labels"], [{"id": "l1", "name": LABELS[1]["name"]}])
        self.assertEqual(found["assignee"]["id"], "u1")

    def test_create_sends_description_due_date_and_parent(self):
        from uuid import uuid4
        parent_id = self.fixtures["issues"][0]["id"]
        issue_id = str(uuid4())
        result = self.client.create_issue(issue_id=issue_id, team_id="t0", title="Child",
                                          description="Some text", due_date="2026-10-20",
                                          parent_id=parent_id)
        self.assertEqual(self.fake.requests[-1]["variables"]["input"],
                         {"id": issue_id, "teamId": "t0", "title": "Child",
                          "description": "Some text", "dueDate": "2026-10-20",
                          "parentId": parent_id})
        found = self.client.issue(result["identifier"])
        self.assertEqual((found["description"], found["dueDate"]), ("Some text", "2026-10-20"))
        self.assertEqual(found["parent"]["id"], parent_id)
        parent = self.client.issue(self.fixtures["issues"][0]["identifier"])
        self.assertEqual(parent["children"], [result["identifier"]])

    def test_create_with_an_unknown_parent_is_rejected(self):
        from herdboard.linear import LinearWriteRejectedError
        with self.assertRaises(LinearWriteRejectedError):
            self.client.create_issue(issue_id="id", team_id="t0", title="x", parent_id="missing")

    def test_create_with_a_used_id_is_rejected(self):
        from herdboard.linear import LinearWriteRejectedError
        self.client.create_issue(issue_id="given", team_id="t0", title="One")
        with self.assertRaises(LinearWriteRejectedError):
            self.client.create_issue(issue_id="given", team_id="t0", title="Two")

    def test_create_omits_unspecified_fields(self):
        self.client.create_issue(issue_id="given", team_id="t0", title="Minimal")
        self.assertEqual(self.fake.requests[-1]["variables"]["input"],
                         {"id": "given", "teamId": "t0", "title": "Minimal"})

    def test_rejected_mutations(self):
        from herdboard.linear import LinearWriteRejectedError
        for failure in ("success_false", "graphql_error"):
            with self.subTest(failure=failure):
                self.fake.mutation_failure = failure
                with self.assertRaises(LinearWriteRejectedError) as caught:
                    self.client.update_issue("i0", sort_order=99)
                self.assertEqual((caught.exception.code, caught.exception.exit_code),
                                 ("linear_rejected", 3))
                self.assertEqual(self.fixtures["issues"][0]["sortOrder"], 0.0)

    def test_unknown_state_and_team_rejected(self):
        from herdboard.linear import LinearWriteRejectedError
        with self.assertRaises(LinearWriteRejectedError):
            self.client.update_issue("i0", state_id="missing")
        with self.assertRaises(LinearWriteRejectedError):
            self.client.create_issue(issue_id="id", team_id="missing", title="x")

    def test_mutation_http_500(self):
        from herdboard.linear import LinearWriteUnknownError
        self.fake.mutation_failure = "http_500"
        with self.assertRaises(LinearWriteUnknownError) as caught:
            self.client.update_issue("i0", sort_order=99)
        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("linear_unknown", 3))
        self.assertEqual(len(self.fake.requests), 1)

    def test_mutation_timeout_before_and_after_apply(self):
        from herdboard.linear import LinearWriteUnknownError
        for failure, expected in (("timeout_before", 0.0), ("timeout_after", 99)):
            with self.subTest(failure=failure):
                self.fake.requests.clear()
                self.fake.mutation_failure = failure
                with self.assertRaises(LinearWriteUnknownError):
                    self.client.update_issue("i0", sort_order=99)
                self.assertEqual(len(self.fake.requests), 1)
                self.assertEqual(self.fixtures["issues"][0]["sortOrder"], expected)

    def test_create_timeout_after_apply_lookup(self):
        from herdboard.linear import LinearWriteUnknownError
        self.fake.mutation_failure = "timeout_after"
        with self.assertRaises(LinearWriteUnknownError):
            self.client.create_issue(issue_id="given", team_id="t0", title="Created")
        self.assertEqual(len(self.fake.requests), 1)
        self.fake.mutation_failure = None
        self.assertEqual(self.client.issue_for_write("given")["title"], "Created")

    def test_mutation_rate_limit_not_retried(self):
        self.fake.mutation_failure = "http_429"
        with self.assertRaisesRegex(LinearError, "write.*rate limited.*not applied"):
            self.client.update_issue("i0", sort_order=99)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fixtures["issues"][0]["sortOrder"], 0.0)

    def test_mutation_graphql_rate_limit_with_reset(self):
        body = {"errors": [{"message": "Limit", "extensions": {"code": "RATELIMITED"}}]}
        self.fake.responses = [(400, body, {"X-RateLimit-Requests-Reset": "2000000007000"})]
        with mock.patch("herdboard.linear.time.time", return_value=2000000000):
            with self.assertRaisesRegex(LinearError, "not applied.*2033-05-18T03:33:27Z"):
                self.client.update_issue("i0", sort_order=99)
        self.assertEqual(len(self.fake.requests), 1)

    def test_mutation_authentication(self):
        for status, body in ((401, {}), (403, {}),
                             (200, {"errors": [{"message": "Auth", "extensions": {
                                 "code": "AUTHENTICATION_ERROR"}}]})):
            self.fake.responses = [(status, body, {})]
            with self.assertRaises(LinearAuthError):
                self.client.update_issue("i0", sort_order=99)

    def test_mutation_malformed_or_missing_response(self):
        from herdboard.linear import LinearWriteUnknownError
        for body in (b"invalid", [], {}, {"data": None}, {"errors": "invalid"},
                     {"data": {}}, {"data": {"issueUpdate": {"success": True, "issue": None}}},
                     {"data": {"issueUpdate": {"issue": {}}}}):
            with self.subTest(body=body):
                self.fake.requests.clear()
                self.fake.responses = [(200, body, {})]
                with self.assertRaises(LinearWriteUnknownError):
                    self.client.update_issue("i0", sort_order=99)
                self.assertEqual(len(self.fake.requests), 1)

    def test_mutation_network_error(self):
        from herdboard.linear import LinearWriteUnknownError
        with mock.patch.object(self.client._opener, "open", side_effect=URLError("reset")):
            with self.assertRaises(LinearWriteUnknownError):
                self.client.update_issue("i0", sort_order=99)


if __name__ == "__main__":
    unittest.main()
