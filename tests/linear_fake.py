"""Local, quiet GraphQL fixture server; no Linear account is required."""

import copy
import functools
import gzip
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import graphql
from graphql.execution.values import get_variable_values

SCHEMA_PATH = Path(__file__).resolve().parent / "fixtures/linear/schema.graphql.gz"


@functools.lru_cache(maxsize=1)
def linear_schema():
    return graphql.build_schema(gzip.decompress(SCHEMA_PATH.read_bytes()).decode("utf-8"))


@functools.lru_cache(maxsize=None)
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
    return {"nodes": page, "pageInfo": {
        "hasNextPage": end < len(nodes),
        "endCursor": str(start + len(page)) if page else None,
    }}


class FakeLinear:
    def __init__(self, fixtures=None, api_key="test-key", page_size=2, now=None):
        self.now = now or datetime.now(timezone.utc)
        self.fixtures = fixtures or {}
        self.api_key = api_key
        self.page_size = page_size
        self.requests = []
        self.responses = []
        self.rate_limits = 0
        self.retry_after = "2"
        self.delay = 0

    def __enter__(self):
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
                    status, body = 400, {"errors": [{"message": m, "extensions": {
                        "code": "GRAPHQL_VALIDATION_FAILED"}} for m in errors]}
                elif self.headers.get("Authorization") != fake.api_key:
                    status, body = 401, {"errors": [{"message": "Invalid API key"}]}
                elif fake.rate_limits:
                    fake.rate_limits -= 1
                    status, body = 429, {"errors": [{"message": "Rate limited"}]}
                    if fake.retry_after is not None:
                        headers["Retry-After"] = fake.retry_after
                elif fake.responses:
                    status, body, headers = fake.responses.pop(0)
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

    def respond(self, request):
        data = self.answer(request)
        if data is None:
            return {"data": None, "errors": [{"message": "Entity not found: Issue"}]}
        document, _ = parse_query(request["query"])
        operation = document.definitions[0]
        return {"data": prune(operation.selection_set, data, request["variables"], self.page_size)}

    def matches(self, issue, filt):
        """Whether an issue fixture passes the filter keys that the client sends."""
        if "project" in filt and (issue["project"] is None
                                  or issue["project"]["id"] != filt["project"]["id"]["eq"]):
            return False
        if "labels" in filt:
            ids = filt["labels"]["some"]["id"]["in"]
            if not any(label["id"] in ids for label in issue["labels"]["nodes"]):
                return False
        if "or" in filt:
            return any(self.matches_completed(issue.get("completedAt"), option["completedAt"])
                       for option in filt["or"])
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
        page = lambda nodes: connection(nodes, variables, self.page_size)
        if op == "Viewer":
            return {"viewer": copy.deepcopy(fixtures["viewer"])}
        if op == "Teams":
            return {"teams": page(fixtures["teams"])}
        if op == "TeamStates":
            team = next(t for t in fixtures["teams"] if t["id"] == variables["id"])
            return {"teams": {"nodes": [{"states": page(team["states"])}]}}
        if op in ("Projects", "Project"):
            nodes = fixtures["projects"]
            filt = variables["filter"]
            if "name" in filt:
                name = filt["name"]["eqIgnoreCase"].casefold()
                nodes = [p for p in nodes if p["name"].casefold() == name]
            else:
                nodes = [p for p in nodes if p["id"] == filt["id"]["eq"]]
            return {"projects": page(nodes)}
        if op == "ProjectTeams":
            project = next(p for p in fixtures["projects"] if p["id"] == variables["id"])
            return {"projects": {"nodes": [{"teams": page(project["teams"])}]}}
        if op == "Labels":
            name = variables["filter"]["name"]["eqIgnoreCase"].casefold()
            return {"issueLabels": page([label for label in fixtures["labels"]
                                         if label["name"].casefold() == name])}
        if op == "LabelsById":
            ids = variables["filter"]["id"]["in"]
            return {"issueLabels": page([label for label in fixtures["labels"]
                                         if label["id"] in ids])}
        if op in ("IssueAttachments", "IssueLabels", "IssueChildren"):
            field = {"IssueAttachments": "attachments", "IssueLabels": "labels",
                     "IssueChildren": "children"}[op]
            nodes = [i for i in fixtures["issues"] if i["id"] == variables["id"]]
            if not nodes:
                return {"issues": {"nodes": []}}
            return {"issues": {"nodes": [{field: page(nodes[0][field]["nodes"])}]}}
        if op == "ProjectIssues":
            return {"issues": page([i for i in fixtures["issues"]
                                    if self.matches(i, variables["filter"])])}
        if op == "MyIssues":
            nodes = [i for i in fixtures["issues"] if i["assignee"] is not None
                     and i["assignee"]["id"] == fixtures["viewer"]["id"]
                     and self.matches(i, variables.get("filter") or {})]
            return {"viewer": {"assignedIssues": page(nodes)}}
        if op == "Issue":
            found = [i for i in fixtures["issues"] if i["identifier"] == variables["id"]]
            return {"issue": copy.deepcopy(found[0])} if found else None
        raise AssertionError(f"Unknown operation: {op}")


def argument(field, name, variables):
    for arg in field.arguments or ():
        if arg.name.value == name:
            value = arg.value
            return (variables.get(value.name.value) if isinstance(value, graphql.VariableNode)
                    else int(value.value))
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
        if argument(field, "first", variables) is not None and not (
                isinstance(sub, dict) and "pageInfo" in sub):
            nodes = sub["nodes"] if isinstance(sub, dict) else sub
            sub = connection(nodes, {"first": argument(field, "first", variables)}, page_size)
        result[name] = prune(field.selection_set, sub, variables, page_size)
    return result
