"""Linear GraphQL client using personal API key authentication."""

from __future__ import annotations

import http.client
import json
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
)

from .model import HerdboardError


class LinearError(HerdboardError):
    code = "linear"
    exit_code = 3


class LinearAuthError(LinearError):
    """Linear rejected the API key."""


class LinearNetworkError(LinearError):
    """The request failed before Linear answered, or timed out."""


class LinearWriteUnknownError(LinearError):
    """Linear may or may not have applied the write; do not repeat it blindly."""

    code = "linear_unknown"


class LinearWriteRejectedError(LinearError):
    """Linear answered and rejected the write."""

    code = "linear_rejected"


class _LinearWriteRateLimitError(LinearError):
    """A rate-limited write was not applied and must not be retried."""


class LinearNotFoundError(LinearError):
    """Linear reported that a requested entity does not exist."""


PAGE_SIZE = 10
# Follow-up pages of a nested connection are larger than list pages because
# they select only one connection of one entity.
NESTED_PAGE_SIZE = 50
# Longest single wait for a rate limit; a longer wait raises instead.
MAX_RATE_LIMIT_WAIT = 60.0
LOCAL_HOSTS = ("127.0.0.1", "localhost")
PAGE_INFO = "pageInfo { hasNextPage endCursor }"
ATTACHMENT_FIELDS = "url title subtitle"
# Nested connections are bounded to keep query complexity below Linear's
# per-query limit. The first page comes inline; a follow-up query pages the rest.
ISSUE_FIELDS = f"""
    id identifier title description dueDate url branchName priority updatedAt sortOrder
    state {{ id name type }} team {{ id key }}
    labels(first: 100) {{ nodes {{ id name }} {PAGE_INFO} }}
    project {{ id name }} parent {{ id identifier }}
    children(first: 100) {{ nodes {{ identifier }} {PAGE_INFO} }}
    assignee {{ id name }}
    attachments(first: 50) {{ nodes {{ {ATTACHMENT_FIELDS} }} {PAGE_INFO} }}
"""
STATE_FIELDS = "id name type position"


def list_query(operation, field, filter_type, fields):
    return f"""query {operation}($first: Int!, $after: String, $filter: {filter_type}) {{
        {field}(first: $first, after: $after, filter: $filter) {{
            nodes {{ {fields} }} {PAGE_INFO}
        }}
    }}"""


def nested_query(operation, root, field, fields):
    """Page one connection of one entity found by id through a filtered list."""
    return f"""query {operation}($id: ID!, $first: Int!, $after: String) {{
    {root}(first: 1, filter: {{id: {{eq: $id}}}}) {{ nodes {{
        {field}(first: $first, after: $after) {{ nodes {{ {fields} }} {PAGE_INFO} }}
    }} }}
}}"""


VIEWER_QUERY = """query Viewer {
    viewer { id name email organization { id name urlKey } }
}"""
TEAMS_QUERY = list_query("Teams", "teams", "TeamFilter",
                         f"id key name states(first: 50) {{ nodes {{ {STATE_FIELDS} }} {PAGE_INFO} }}")
PROJECTS_QUERY = list_query("Projects", "projects", "ProjectFilter",
                            f"id name teams(first: 50) {{ nodes {{ id key }} {PAGE_INFO} }}")
PROJECT_QUERY = list_query("Project", "projects", "ProjectFilter", "id name")
PROJECT_FOR_WRITE_QUERY = list_query("ProjectForWrite", "projects", "ProjectFilter",
                                     f"id name teams(first: 50) {{ nodes {{ id key }} {PAGE_INFO} }}")
LABEL_FIELDS = "id name team { id key }"
LABELS_QUERY = list_query("Labels", "issueLabels", "IssueLabelFilter", LABEL_FIELDS)
LABELS_BY_ID_QUERY = list_query("LabelsById", "issueLabels", "IssueLabelFilter", LABEL_FIELDS)
ISSUES_QUERY = list_query("ProjectIssues", "issues", "IssueFilter", ISSUE_FIELDS)
PINNED_ISSUES_QUERY = list_query("PinnedIssues", "issues", "IssueFilter", ISSUE_FIELDS)
# One page of search results; Linear limits `searchIssues` to 30 requests per minute.
SEARCH_ISSUES_QUERY = """query SearchIssues($term: String!, $first: Int!, $filter: IssueFilter) {
    searchIssues(term: $term, first: $first, filter: $filter) {
        nodes { id identifier title state { name type } team { key } project { name } }
    }
}"""
ISSUE_QUERY = f"""query Issue($id: String!) {{
    issue(id: $id) {{ {ISSUE_FIELDS} }}
}}"""
STATES_QUERY = nested_query("TeamStates", "teams", "states", STATE_FIELDS)
PROJECT_TEAMS_QUERY = nested_query("ProjectTeams", "projects", "teams", "id key")
LABELS_PAGE_QUERY = nested_query("IssueLabels", "issues", "labels", "id name")
CHILDREN_QUERY = nested_query("IssueChildren", "issues", "children", "identifier")
ATTACHMENTS_QUERY = nested_query("IssueAttachments", "issues", "attachments", ATTACHMENT_FIELDS)
MY_ISSUES_QUERY = f"""query MyIssues($first: Int!, $after: String, $filter: IssueFilter) {{
    viewer {{ assignedIssues(first: $first, after: $after, filter: $filter) {{
        nodes {{ {ISSUE_FIELDS} }} {PAGE_INFO}
    }} }}
}}"""
TEAM_BY_KEY_QUERY = list_query("TeamByKey", "teams", "TeamFilter", "id key name")
MY_TEAMS_QUERY = f"""query MyTeams($first: Int!, $after: String) {{
    viewer {{ teams(first: $first, after: $after) {{
        nodes {{ id key name }} {PAGE_INFO}
    }} }}
}}"""
WRITE_ISSUE_FIELDS = "id identifier sortOrder state { id name type } team { id key }"
ISSUE_FOR_WRITE_QUERY = f"""query IssueForWrite($id: String!) {{
    issue(id: $id) {{ {WRITE_ISSUE_FIELDS} title project {{ id name }} }}
}}"""
STATE_SORT_ORDERS_QUERY = list_query("StateSortOrders", "issues", "IssueFilter", "id sortOrder")
ISSUE_UPDATE_QUERY = f"""mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {{
    issueUpdate(id: $id, input: $input) {{ success issue {{ {WRITE_ISSUE_FIELDS} }} }}
}}"""
ISSUE_CREATE_QUERY = f"""mutation IssueCreate($input: IssueCreateInput!) {{
    issueCreate(input: $input) {{ success issue {{ {WRITE_ISSUE_FIELDS} url }} }}
}}"""
# Every query and mutation string above, for the schema validation test.
QUERIES = {name: value for name, value in globals().items()
           if name.endswith("_QUERY") and isinstance(value, str)}


def response_field(data, path):
    try:
        for key in path:
            data = data[key]
    except (KeyError, IndexError, TypeError) as error:
        raise LinearError("Linear response is missing expected data fields") from error
    return data


def read_page(connection):
    """Return (nodes, cursor of the next page or None) for a connection."""
    nodes = response_field(connection, ("nodes",))
    info = response_field(connection, ("pageInfo",))
    if not isinstance(nodes, list) or not isinstance(info, dict):
        raise LinearError("Linear returned an invalid pagination response")
    has_next = response_field(info, ("hasNextPage",))
    if not isinstance(has_next, bool):
        raise LinearError("Linear returned an invalid pagination flag")
    if not has_next:
        return nodes, None
    cursor = info.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise LinearError("Linear pagination did not advance its cursor")
    return nodes, cursor


def find_connection(data, path):
    """Walk path; None when a nested lookup found no entity (empty nodes)."""
    for key in path:
        if key == 0 and isinstance(data, list) and not data:
            return None
        data = response_field(data, (key,))
    return data


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_opener():
    """An opener without the redirect handler, so the key never reaches another host."""
    opener = OpenerDirector()
    for handler in (ProxyHandler(), HTTPHandler(), HTTPSHandler(),
                    HTTPDefaultErrorHandler(), HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def error_codes(errors):
    codes = set()
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if isinstance(code, str):
            codes.add(code)
    return codes


def rate_limit_wait(headers, attempt):
    """Seconds until Linear allows requests: reset headers, Retry-After, else backoff."""
    now = time.time()
    waits = []
    for bucket in ("Requests", "Complexity"):
        reset = number(headers.get(f"X-RateLimit-{bucket}-Reset"))
        remaining = number(headers.get(f"X-RateLimit-{bucket}-Remaining"))
        # A bucket with requests left is not the one that blocked this call.
        if reset is not None and not (remaining is not None and remaining > 0):
            waits.append(reset / 1000 - now)
    if waits:
        return max(0.0, max(waits))
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        seconds = number(retry_after)
        if seconds is None:
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - now
            except (ValueError, TypeError, OverflowError):
                seconds = None
        if seconds is not None:
            return max(0.0, seconds)
    return float(2 ** attempt)


class LinearClient:
    def __init__(self, api_key, endpoint="https://api.linear.app/graphql", timeout=20.0,
                 max_retries=3, sleep=time.sleep):
        parts = urlsplit(endpoint)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
            raise LinearError("Linear endpoint must use https://")
        if max_retries < 0:
            raise LinearError("Linear max_retries must not be negative")
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self._opener = build_opener()

    def _send(self, body):
        request = Request(self.endpoint, data=body, headers={
            "Authorization": self.api_key, "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.headers, error.read()
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            raise LinearNetworkError(f"Linear network error or timeout: {error}") from error

    def _request(self, operation, query, variables=None, *, mutation=False):
        try:
            return self._request_response(operation, query, variables, mutation=mutation)
        except (LinearAuthError, LinearWriteRejectedError, LinearWriteUnknownError,
                _LinearWriteRateLimitError):
            raise
        except LinearError as error:
            if mutation:
                raise LinearWriteUnknownError(
                    f"Linear write result is unknown; check the issue in Linear: {error}") from error
            raise

    def _request_response(self, operation, query, variables=None, *, mutation=False):
        body = json.dumps({"operationName": operation, "query": query,
                           "variables": variables or {}}).encode("utf-8")
        for attempt in range(1 if mutation else self.max_retries + 1):
            status, headers, raw = self._send(body)
            if mutation and 500 <= status < 600:
                raise LinearWriteUnknownError(
                    f"Linear write result is unknown (HTTP {status}); check the issue in Linear")
            if status in (401, 403):
                raise LinearAuthError(f"Linear authentication failed (HTTP {status}); check the API key and access")
            if status == 429:
                # The HTTP status is sufficient even when the body is invalid.
                payload, errors, codes = {}, [], {"RATELIMITED"}
            else:
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError) as error:
                    if not 200 <= status < 300:
                        raise LinearError(f"Linear HTTP error {status}") from error
                    raise LinearError("Linear returned malformed JSON") from error
                if not isinstance(payload, dict):
                    raise LinearError(f"Linear returned an invalid response (HTTP {status})")
                errors = payload.get("errors") or []
                if not isinstance(errors, list):
                    raise LinearError("Linear returned an invalid GraphQL errors response")
                codes = error_codes(errors)
            if "RATELIMITED" in codes:
                if mutation:
                    message = "Linear write was rate limited and not applied"
                    if any(headers.get(name) is not None for name in (
                            "X-RateLimit-Requests-Reset", "X-RateLimit-Complexity-Reset", "Retry-After")):
                        allowed = datetime.fromtimestamp(
                            time.time() + rate_limit_wait(headers, attempt), UTC)
                        message += "; requests are allowed again at " + allowed.strftime("%Y-%m-%dT%H:%M:%SZ")
                    # This is a known non-write, not an unknown transport result.
                    raise _LinearWriteRateLimitError(message)
                if attempt >= self.max_retries:
                    raise LinearError(f"Linear rate limit exceeded after {attempt} retries")
                delay = rate_limit_wait(headers, attempt)
                if delay > MAX_RATE_LIMIT_WAIT:
                    allowed = datetime.fromtimestamp(time.time() + delay, UTC)
                    raise LinearError("Linear rate limit exceeded; requests are allowed again at "
                                      + allowed.strftime("%Y-%m-%dT%H:%M:%SZ"))
                self.sleep(delay)
                continue
            if "UNAUTHENTICATED" in codes or "AUTHENTICATION_ERROR" in codes:
                raise LinearAuthError("Linear authentication failed; check the API key")
            if errors:
                messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e)
                                     for e in errors)
                if mutation:
                    raise LinearWriteRejectedError(f"Linear rejected the write: {messages}")
                if "not found" in messages.casefold():
                    raise LinearNotFoundError(f"Linear GraphQL error: {messages}")
                raise LinearError(f"Linear GraphQL error: {messages}")
            if not 200 <= status < 300:
                raise LinearError(f"Linear HTTP error {status}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise LinearError("Linear response is missing valid data")
            return data

    def _pages(self, operation, query, path, variables=None, after=None, page_size=PAGE_SIZE):
        variables = dict(variables or {})
        variables.update(first=page_size, after=after)
        result = []
        cursors = {after} if after else set()
        while True:
            data = self._request(operation, query, variables)
            connection = find_connection(data, path)
            if connection is None:
                return result
            nodes, cursor = read_page(connection)
            result.extend(nodes)
            if cursor is None:
                return result
            if cursor in cursors:
                raise LinearError("Linear pagination did not advance its cursor")
            cursors.add(cursor)
            variables["after"] = cursor

    def _rest(self, owner, field, operation, query, root):
        """Nodes of a nested connection: the inline page, then follow-up pages if any."""
        nodes, cursor = read_page(response_field(owner, (field,)))
        if cursor is None:
            return nodes
        return nodes + self._pages(operation, query, (root, "nodes", 0, field),
                                   {"id": owner["id"]}, after=cursor, page_size=NESTED_PAGE_SIZE)

    def viewer(self) -> dict:
        return response_field(self._request("Viewer", VIEWER_QUERY), ("viewer",))

    def teams(self) -> list[dict]:
        teams = self._pages("Teams", TEAMS_QUERY, ("teams",))
        for team in teams:
            team["states"] = self._rest(team, "states", "TeamStates", STATES_QUERY, "teams")
        return teams

    def team_states(self, team_id) -> list[dict]:
        return self._pages("TeamStates", STATES_QUERY, ("teams", "nodes", 0, "states"),
                           {"id": team_id}, page_size=NESTED_PAGE_SIZE)

    def team_by_key(self, key) -> dict | None:
        teams = self._pages("TeamByKey", TEAM_BY_KEY_QUERY, ("teams",),
                            {"filter": {"key": {"eqIgnoreCase": key}}})
        return teams[0] if teams else None

    def my_teams(self) -> list[dict]:
        return self._pages("MyTeams", MY_TEAMS_QUERY, ("viewer", "teams"))

    def issue_for_write(self, identifier) -> dict | None:
        try:
            data = self._request("IssueForWrite", ISSUE_FOR_WRITE_QUERY, {"id": identifier})
        except LinearNotFoundError:
            return None
        return response_field(data, ("issue",))

    def state_sort_orders(self, team_id, state_id, done_days) -> list[float]:
        filters = {"team": {"id": {"eq": team_id}}, "state": {"id": {"eq": state_id}}}
        filters.update(recent_filter(done_days))
        issues = self._pages("StateSortOrders", STATE_SORT_ORDERS_QUERY, ("issues",),
                             {"filter": filters})
        return [float(issue["sortOrder"]) for issue in issues]

    def _mutate(self, operation, query, field, variables):
        data = self._request(operation, query, variables, mutation=True)
        try:
            payload = response_field(data, (field,))
            success = response_field(payload, ("success",))
            if success is False:
                raise LinearWriteRejectedError("Linear rejected the write (success: false)")
            if success is not True:
                raise LinearError("Linear mutation is missing a valid success flag")
            issue = response_field(payload, ("issue",))
            if not isinstance(issue, dict):
                raise LinearError("Linear mutation is missing its issue")
            for path in (("id",), ("identifier",), ("sortOrder",),
                         ("state", "id"), ("state", "name"), ("state", "type"),
                         ("team", "id"), ("team", "key")):
                if response_field(issue, path) is None:
                    raise LinearError("Linear mutation is missing issue fields")
            if field == "issueCreate" and not isinstance(issue.get("url"), str):
                raise LinearError("Linear mutation is missing its issue URL")
            return issue
        except LinearWriteRejectedError:
            raise
        except LinearError as error:
            raise LinearWriteUnknownError(
                f"Linear write result is unknown; check the issue in Linear: {error}") from error

    def update_issue(self, issue_id, *, state_id=None, sort_order=None) -> dict:
        fields = {"stateId": state_id, "sortOrder": sort_order}
        inputs = {key: value for key, value in fields.items() if value is not None}
        return self._mutate("IssueUpdate", ISSUE_UPDATE_QUERY, "issueUpdate",
                            {"id": issue_id, "input": inputs})

    def create_issue(self, *, issue_id, team_id, title, state_id=None, project_id=None,
                     label_ids=None, assignee_id=None, sort_order=None, description=None,
                     due_date=None, parent_id=None) -> dict:
        fields = {"id": issue_id, "teamId": team_id, "title": title, "stateId": state_id,
                  "projectId": project_id, "labelIds": label_ids, "assigneeId": assignee_id,
                  "sortOrder": sort_order, "description": description, "dueDate": due_date,
                  "parentId": parent_id}
        inputs = {key: value for key, value in fields.items() if value is not None}
        return self._mutate("IssueCreate", ISSUE_CREATE_QUERY, "issueCreate", {"input": inputs})

    def find_projects(self, name) -> list[dict]:
        projects = self._pages("Projects", PROJECTS_QUERY, ("projects",),
                               {"filter": {"name": {"eqIgnoreCase": name}}})
        for project in projects:
            project["teams"] = self._rest(project, "teams", "ProjectTeams",
                                          PROJECT_TEAMS_QUERY, "projects")
        return projects

    def projects_for_team(self, team_id) -> list[dict]:
        """The projects of one team, each with all its teams."""
        projects = self._pages("Projects", PROJECTS_QUERY, ("projects",),
                               {"filter": {"accessibleTeams": {"some": {"id": {"eq": team_id}}}}})
        for project in projects:
            project["teams"] = self._rest(project, "teams", "ProjectTeams",
                                          PROJECT_TEAMS_QUERY, "projects")
        return projects

    def project(self, project_id) -> dict | None:
        # The filtered connection returns no nodes for missing or inaccessible
        # projects, without the non-null direct project query's error response.
        projects = self._pages("Project", PROJECT_QUERY, ("projects",),
                               {"filter": {"id": {"eq": project_id}}})
        return projects[0] if projects else None

    def project_for_write(self, project_id) -> dict | None:
        """Find a project by stable ID, including all its teams."""
        projects = self._pages("ProjectForWrite", PROJECT_FOR_WRITE_QUERY, ("projects",),
                               {"filter": {"id": {"eq": project_id}}})
        if not projects:
            return None
        project = projects[0]
        project["teams"] = self._rest(project, "teams", "ProjectTeams",
                                      PROJECT_TEAMS_QUERY, "projects")
        return project

    def find_labels(self, name) -> list[dict]:
        return self._pages("Labels", LABELS_QUERY, ("issueLabels",),
                           {"filter": {"name": {"eqIgnoreCase": name}}})

    def labels(self, ids: list[str]) -> list[dict]:
        """The labels among ids that exist and are accessible."""
        if not ids:
            return []
        return self._pages("LabelsById", LABELS_BY_ID_QUERY, ("issueLabels",),
                           {"filter": {"id": {"in": list(ids)}}})

    def project_issues(self, project_id, label_ids=None, done_days: int | None = 14) -> list[dict]:
        filters = {"project": {"id": {"eq": project_id}}}
        if label_ids is not None:
            if not label_ids:
                return []
            filters["labels"] = {"some": {"id": {"in": list(label_ids)}}}
        filters.update(recent_filter(done_days))
        issues = self._pages("ProjectIssues", ISSUES_QUERY, ("issues",), {"filter": filters})
        return [self._issue(issue) for issue in issues]

    def issues_by_ids(self, ids: list[str], done_days: int | None = 14) -> list[dict]:
        """The issues among ids that exist, are accessible and are open or recently done."""
        if not ids:
            return []
        filters = {"id": {"in": list(ids)}}
        filters.update(recent_filter(done_days))
        issues = self._pages("PinnedIssues", PINNED_ISSUES_QUERY, ("issues",), {"filter": filters})
        return [self._issue(issue) for issue in issues]

    def search_issues(self, term: str, team_ids: list[str], first: int = 20) -> list[dict]:
        """Open issues of the teams in team_ids that match term, best match first.

        The result has only id, identifier, title, state, team and project fields.
        """
        if not team_ids:
            return []
        filters = {"team": {"id": {"in": list(team_ids)}},
                   "state": {"type": {"nin": ["completed", "canceled"]}}}
        data = self._request("SearchIssues", SEARCH_ISSUES_QUERY,
                             {"term": term, "first": first, "filter": filters})
        nodes = response_field(data, ("searchIssues", "nodes"))
        if not isinstance(nodes, list):
            raise LinearError("Linear returned an invalid search response")
        return nodes

    def my_issues(self, done_days: int | None = 14) -> list[dict]:
        variables = {"filter": recent_filter(done_days)} if done_days is not None else {}
        issues = self._pages("MyIssues", MY_ISSUES_QUERY, ("viewer", "assignedIssues"), variables)
        return [self._issue(issue) for issue in issues]

    def issue(self, identifier: str) -> dict | None:
        """One issue by identifier such as ENG-123; None when Linear does not find it."""
        try:
            data = self._request("Issue", ISSUE_QUERY, {"id": identifier})
        except LinearNotFoundError:
            return None
        found = response_field(data, ("issue",))
        return self._issue(found) if found else None

    def _issue(self, issue):
        issue = dict(issue)
        issue["labels"] = self._rest(issue, "labels", "IssueLabels", LABELS_PAGE_QUERY, "issues")
        children = self._rest(issue, "children", "IssueChildren", CHILDREN_QUERY, "issues")
        issue["children"] = [child["identifier"] for child in children]
        issue["attachments"] = self._rest(issue, "attachments", "IssueAttachments", ATTACHMENTS_QUERY, "issues")
        return issue


def recent_filter(done_days):
    """Issue filter: open issues plus issues completed within done_days days."""
    if done_days is None:
        return {}
    if isinstance(done_days, bool) or not isinstance(done_days, int) or done_days < 0:
        raise ValueError("done_days must be a non-negative integer or None")
    return {"or": [{"completedAt": {"null": True}},
                   {"completedAt": {"gte": f"-P{done_days}D"}}]}
