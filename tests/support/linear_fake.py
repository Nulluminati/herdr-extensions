"""Local, quiet GraphQL fixture server; no Linear account is required."""

import copy
import functools
import gzip
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import graphql
from graphql.execution.values import get_variable_values

from support.paths import ROOT

SCHEMA_PATH = ROOT / "tests/herdboard/fixtures/linear/schema.graphql.gz"


@functools.lru_cache(maxsize=1)
def linear_schema():
    return graphql.build_schema(gzip.decompress(SCHEMA_PATH.read_bytes()).decode("utf-8"))


@functools.cache
def parse_query(query):
    """Parse a query; return (document, error messages from schema validation)."""
    document = graphql.parse(query)
    return document, [e.message for e in graphql.validate(linear_schema(), document)]


def validation_errors(query, variables):
    """Messages for a query or variables that the schema rejects."""
    try:
        document, errors = parse_query(query)
    except graphql.GraphQLError as error:
        return [error.message]
    if errors:
        return errors
    operation = document.definitions[0]
    coerced = get_variable_values(linear_schema(), operation.variable_definitions or [], variables)
    return [e.message for e in coerced] if isinstance(coerced, list) else []


def connection(nodes, variables, page_size):
    start = int(variables.get("after") or 0)
    end = start + min(variables.get("first", 50), page_size)
    page = copy.deepcopy(nodes[start:end])
    return {
        "nodes": page,
        "pageInfo": {
            "hasNextPage": end < len(nodes),
            "endCursor": str(start + len(page)) if page else None,
        },
    }


class FakeLinear:
    def __init__(self, fixtures=None, api_key="test-key", page_size=2, now=None):
        self.now = now or datetime.now(UTC)
        self.fixtures = fixtures or {}
        self.api_key = api_key
        self.page_size = page_size
        self.requests = []
        self.responses = []
        self.rate_limits = 0
        self.retry_after = "2"
        self.delay = 0
        self.mutation_failure = None
        # One failure per mutation, taken in order (None: no failure). When the list is
        # empty, `mutation_failure` applies to every mutation.
        self.mutation_failures = []
        self.mutation_delay = 0.3

    def __enter__(self):
        # Keep schema construction outside client request timeouts.
        linear_schema()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({**request, "authorization": self.headers.get("Authorization")})
                status, body, headers = 200, None, {}
                errors = validation_errors(request["query"], request["variables"])
                if errors:
                    status, body = (
                        400,
                        {
                            "errors": [
                                {"message": m, "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}} for m in errors
                            ]
                        },
                    )
                elif self.headers.get("Authorization") != fake.api_key:
                    status, body = 401, {"errors": [{"message": "Invalid API key"}]}
                elif fake.rate_limits:
                    fake.rate_limits -= 1
                    status, body = 429, {"errors": [{"message": "Rate limited"}]}
                    if fake.retry_after is not None:
                        headers["Retry-After"] = fake.retry_after
                elif fake.responses:
                    status, body, headers = fake.responses.pop(0)
                elif request["operationName"] in ("IssueUpdate", "IssueCreate") and fake.next_failure():
                    failure = fake.failure
                    field = "issueUpdate" if request["operationName"] == "IssueUpdate" else "issueCreate"
                    if failure == "success_false":
                        body = {"data": {field: {"success": False, "issue": None}}}
                    elif failure == "graphql_error":
                        body = {"errors": [{"message": "Write rejected"}]}
                    elif failure == "http_429":
                        status, body = 429, {"errors": [{"message": "Rate limited"}]}
                    elif failure == "http_500":
                        status, body = 500, b"Unavailable"
                    elif failure == "timeout_before":
                        # This request never applies the mutation, even after the delay.
                        time.sleep(fake.mutation_delay)
                        body = {"errors": [{"message": "Write not applied"}]}
                    elif failure == "delayed_commit":
                        time.sleep(fake.mutation_delay)
                        body = fake.respond(request)
                    elif failure == "timeout_after":
                        body = fake.respond(request)
                        time.sleep(fake.mutation_delay)
                    else:
                        raise AssertionError(f"Unknown mutation failure: {failure}")
                else:
                    body = fake.respond(request)
                if fake.delay:
                    time.sleep(fake.delay)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # A timeout test closes the socket before the response arrives.
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/graphql"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def next_failure(self):
        """The failure for the next mutation; it stays in `failure` for the handler."""
        self.failure = self.mutation_failures.pop(0) if self.mutation_failures else self.mutation_failure
        return self.failure

    def respond(self, request):
        try:
            data = self.answer(request)
        except ValueError as error:
            return {"errors": [{"message": str(error)}]}
        if data is None:
            return {"data": None, "errors": [{"message": "Entity not found: Issue"}]}
        document, _ = parse_query(request["query"])
        operation = document.definitions[0]
        return {"data": prune(operation.selection_set, data, request["variables"], self.page_size)}

    def matches(self, issue, filt):
        """Whether an issue fixture passes the filter keys that the client sends."""
        for field in ("team", "state"):
            if field in filt and issue[field]["id"] != filt[field]["id"]["eq"]:
                return False
        if "id" in filt and issue["id"] not in filt["id"]["in"]:
            return False
        if "project" in filt and (issue["project"] is None or issue["project"]["id"] != filt["project"]["id"]["eq"]):
            return False
        if "labels" in filt:
            ids = filt["labels"]["some"]["id"]["in"]
            if not any(label["id"] in ids for label in issue["labels"]["nodes"]):
                return False
        if "or" in filt:
            return any(self.matches_completed(issue.get("completedAt"), option["completedAt"]) for option in filt["or"])
        return True

    def matches_completed(self, completed_at, comparator):
        if "null" in comparator:
            return (completed_at is None) == comparator["null"]
        if completed_at is None:
            return False
        days = int(re.fullmatch(r"-P(\d+)D", comparator["gte"]).group(1))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return completed >= self.now - timedelta(days=days)

    def answer(self, request):
        op, variables = request["operationName"], request["variables"]
        fixtures = self.fixtures

        def page(nodes):
            return connection(nodes, variables, self.page_size)

        if op == "Viewer":
            return {"viewer": copy.deepcopy(fixtures["viewer"])}
        if op == "Teams":
            return {"teams": page(fixtures["teams"])}
        if op == "TeamByKey":
            key = variables["filter"]["key"]["eqIgnoreCase"].casefold()
            return {"teams": page([t for t in fixtures["teams"] if t["key"].casefold() == key])}
        if op == "MyTeams":
            ids = fixtures.get("my_team_ids", [t["id"] for t in fixtures["teams"]])
            return {"viewer": {"teams": page([t for t in fixtures["teams"] if t["id"] in ids])}}
        if op == "TeamStates":
            teams = [t for t in fixtures["teams"] if t["id"] == variables["id"]]
            return {"teams": {"nodes": [{"states": page(t["states"])} for t in teams]}}
        if op in ("Projects", "Project", "ProjectForWrite"):
            nodes = fixtures["projects"]
            filt = variables["filter"]
            if "name" in filt:
                name = filt["name"]["eqIgnoreCase"].casefold()
                nodes = [p for p in nodes if p["name"].casefold() == name]
            elif "accessibleTeams" in filt:
                team = filt["accessibleTeams"]["some"]["id"]["eq"]
                nodes = [p for p in nodes if any(t["id"] == team for t in p["teams"])]
            else:
                nodes = [p for p in nodes if p["id"] == filt["id"]["eq"]]
            return {"projects": page(nodes)}
        if op == "ProjectTeams":
            project = next(p for p in fixtures["projects"] if p["id"] == variables["id"])
            return {"projects": {"nodes": [{"teams": page(project["teams"])}]}}
        if op == "Labels":
            name = variables["filter"]["name"]["eqIgnoreCase"].casefold()
            return {"issueLabels": page([label for label in fixtures["labels"] if label["name"].casefold() == name])}
        if op == "LabelsById":
            ids = variables["filter"]["id"]["in"]
            return {"issueLabels": page([label for label in fixtures["labels"] if label["id"] in ids])}
        if op in ("IssueAttachments", "IssueLabels", "IssueChildren"):
            field = {"IssueAttachments": "attachments", "IssueLabels": "labels", "IssueChildren": "children"}[op]
            nodes = [i for i in fixtures["issues"] if i["id"] == variables["id"]]
            if not nodes:
                return {"issues": {"nodes": []}}
            return {"issues": {"nodes": [{field: page(nodes[0][field]["nodes"])}]}}
        if op == "SearchIssues":
            filt = variables["filter"]
            term = variables["term"].casefold()
            nodes = [
                i
                for i in fixtures["issues"]
                if term in f"{i['identifier']} {i['title']}".casefold()
                and i["team"]["id"] in filt["team"]["id"]["in"]
                and i["state"]["type"] not in filt["state"]["type"]["nin"]
            ]
            return {"searchIssues": {"nodes": nodes}}
        if op in ("ProjectIssues", "StateSortOrders", "PinnedIssues"):
            return {"issues": page([i for i in fixtures["issues"] if self.matches(i, variables["filter"])])}
        if op == "MyIssues":
            nodes = [
                i
                for i in fixtures["issues"]
                if i["assignee"] is not None
                and i["assignee"]["id"] == fixtures["viewer"]["id"]
                and self.matches(i, variables.get("filter") or {})
            ]
            return {"viewer": {"assignedIssues": page(nodes)}}
        if op in ("Issue", "IssueForWrite"):
            found = [i for i in fixtures["issues"] if variables["id"] in (i["identifier"], i["id"])]
            return {"issue": copy.deepcopy(found[0])} if found else None
        if op in ("IssueUpdate", "IssueCreate"):
            inputs = variables["input"]
            if op == "IssueUpdate":
                found = [i for i in fixtures["issues"] if i["id"] == variables["id"]]
                if not found:
                    raise ValueError("Unknown issue id")
                issue = found[0]
                team_id = issue["team"]["id"]
            else:
                team_id = inputs["teamId"]
                issue = None
                if any(i["id"] == inputs["id"] for i in fixtures["issues"]):
                    raise ValueError("Duplicate issue id")
            team = next((t for t in fixtures["teams"] if t["id"] == team_id), None)
            if team is None:
                raise ValueError("Unknown team id")
            state_id = inputs.get("stateId")
            state = next((s for s in team["states"] if s["id"] == state_id), None)
            if state_id is not None and state is None:
                raise ValueError("Unknown state id")
            if issue is None:
                identifier = f"{team['key']}-{len(fixtures['issues']) + 1}"
                issue = {
                    "id": inputs["id"],
                    "identifier": identifier,
                    "title": inputs["title"],
                    "team": {k: team[k] for k in ("id", "key")},
                    "state": {k: team["states"][0][k] for k in ("id", "name", "type")},
                    "sortOrder": float(len(fixtures["issues"])),
                    "url": f"https://linear.app/workspace/issue/{identifier}",
                    "project": None,
                    "labels": {"nodes": []},
                    "assignee": None,
                    "description": None,
                    "dueDate": None,
                    "branchName": None,
                    "priority": 0,
                    "updatedAt": self.now.isoformat(),
                    "parent": None,
                    "children": {"nodes": []},
                    "attachments": {"nodes": []},
                }
                fixtures["issues"].append(issue)
            if state is not None:
                issue["state"] = {k: state[k] for k in ("id", "name", "type")}
            if "sortOrder" in inputs:
                issue["sortOrder"] = inputs["sortOrder"]
            if "projectId" in inputs:
                project = next((p for p in fixtures["projects"] if p["id"] == inputs["projectId"]), None)
                issue["project"] = {k: project[k] for k in ("id", "name")} if project else None
            if "labelIds" in inputs:
                issue["labels"] = {
                    "nodes": [
                        {k: label[k] for k in ("id", "name")}
                        for label in fixtures["labels"]
                        if label["id"] in inputs["labelIds"]
                    ]
                }
            if "assigneeId" in inputs:
                issue["assignee"] = {"id": inputs["assigneeId"], "name": "x"}
            for key in ("description", "dueDate"):
                if key in inputs:
                    issue[key] = inputs[key]
            if "parentId" in inputs:
                parent = next((i for i in fixtures["issues"] if i["id"] == inputs["parentId"]), None)
                if parent is None:
                    raise ValueError("Unknown parent id")
                issue["parent"] = {"id": parent["id"], "identifier": parent["identifier"]}
                parent["children"]["nodes"].append({"identifier": issue["identifier"]})
            field = "issueUpdate" if op == "IssueUpdate" else "issueCreate"
            return {field: {"success": True, "issue": copy.deepcopy(issue)}}
        raise AssertionError(f"Unknown operation: {op}")


def argument(field, name, variables):
    for arg in field.arguments or ():
        if arg.name.value == name:
            value = arg.value
            return variables.get(value.name.value) if isinstance(value, graphql.VariableNode) else int(value.value)
    return None


def prune(selection_set, value, variables, page_size):
    """Keep only the selected fields, like a GraphQL server.

    A fixture connection (a list or a {"nodes": [...]} dict) behind a field with a
    `first` argument and no pageInfo is cut to the first page of min(first, page_size).
    """
    if isinstance(value, list):
        return [prune(selection_set, item, variables, page_size) for item in value]
    result = {}
    for field in selection_set.selections:
        name = field.name.value
        if name not in value:
            raise AssertionError(f"Fixture has no field {name}")
        sub = value[name]
        if field.selection_set is None or sub is None:
            result[name] = sub
            continue
        if argument(field, "first", variables) is not None and not (isinstance(sub, dict) and "pageInfo" in sub):
            nodes = sub["nodes"] if isinstance(sub, dict) else sub
            sub = connection(nodes, {"first": argument(field, "first", variables)}, page_size)
        result[name] = prune(field.selection_set, sub, variables, page_size)
    return result
