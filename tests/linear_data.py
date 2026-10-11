"""Shared Linear fixture data for the herdboard Linear tests."""

import copy

VIEWER = {"id": "u1", "name": "Nate", "email": "nate@example.com",
          "organization": {"id": "o1", "name": "Workspace", "urlKey": "workspace"}}
ENG = {"id": "t-eng", "key": "ENG"}
OPS = {"id": "t-ops", "key": "OPS"}
STATES = [{"id": "s1", "name": "Todo", "type": "unstarted", "position": 1}]
TEAMS = [{**ENG, "name": "Engineering", "states": STATES},
         {**OPS, "name": "Operations", "states": STATES}]
PROJECTS = [
    {"id": "p-plat", "name": "Platform", "teams": [ENG]},
    {"id": "p-mob", "name": "Mobile", "teams": [ENG, OPS]},
    {"id": "p-dup1", "name": "Twin", "teams": [ENG]},
    {"id": "p-dup2", "name": "twin", "teams": [OPS]},
]
LABELS = [
    {"id": "l-api", "name": "repo:api", "team": None},
    {"id": "l-bug", "name": "bug", "team": ENG},
    {"id": "l-bug-ops", "name": "bug", "team": OPS},
    {"id": "l-web", "name": "repo:web", "team": None},
]
PR_URL = "https://github.com/Acme/API/pull/7"


def make_issue(number, project="p-plat", state=("Todo", "unstarted"), labels=(), assignee="u1",
               attachments=(), title=None, description="Line one\nLine two", due="2026-10-20"):
    projects = {p["id"]: {"id": p["id"], "name": p["name"]} for p in PROJECTS}
    return {
        "id": f"i{number}", "identifier": f"ENG-{number}", "title": title or f"Issue {number}",
        "description": description, "dueDate": due,
        "url": f"https://linear.app/workspace/issue/ENG-{number}", "branchName": f"nate/eng-{number}",
        "sortOrder": float(number), "priority": 0, "updatedAt": "2026-07-01T00:00:00Z",
        "state": {"id": f"st-{state[0]}", "name": state[0], "type": state[1]},
        "team": ENG,
        "labels": {"nodes": [{"id": label["id"], "name": label["name"]}
                             for label in LABELS if label["id"] in labels]},
        "project": projects.get(project),
        "parent": None, "children": {"nodes": []},
        "assignee": {"id": assignee, "name": "x"} if assignee else None,
        "attachments": {"nodes": [{"url": url} for url in attachments]},
    }


def default_issues():
    return [
        make_issue(1, labels=("l-api",), attachments=(PR_URL,)),
        make_issue(2, state=("In Review", "started"), labels=("l-api", "l-bug")),
        make_issue(3, labels=("l-web",), assignee="other"),
        make_issue(4, state=("Dropped", "canceled")),
        make_issue(5, project="p-mob", state=("Doing", "started")),
        make_issue(6, project=None, state=("Shipped", "completed")),
    ]


def fixtures():
    return copy.deepcopy({"viewer": VIEWER, "teams": TEAMS, "projects": PROJECTS,
                          "labels": LABELS, "issues": default_issues()})


WORKFLOW = [("Backlog", "backlog"), ("Triage", "triage"), ("Todo", "unstarted"),
            ("Doing", "started"), ("In Review", "started"), ("Done", "completed"),
            ("Cancelled", "canceled")]
# Fixture issues whose state name the workflow does not have.
RENAMED_STATES = {"Shipped": "Done", "Dropped": "Cancelled"}


def add_state(data, team_key, name, state_type):
    """Add a workflow state to a team and return it."""
    team = next(t for t in data["teams"] if t["key"] == team_key)
    state = {"id": f"ws-{team_key}-{name}", "name": name, "type": state_type,
             "position": float(len(team["states"]))}
    team["states"].append(state)
    return state


def with_workflow(data):
    """Give the fixture teams a full workflow and point every issue at one of its states.

    Only the Doing state is started and mapped to "In progress", so no column is ambiguous.
    """
    for team in data["teams"]:
        team["states"] = []
        for name, state_type in WORKFLOW:
            add_state(data, team["key"], name, state_type)
    for issue in data["issues"]:
        name = RENAMED_STATES.get(issue["state"]["name"], issue["state"]["name"])
        team = next(t for t in data["teams"] if t["id"] == issue["team"]["id"])
        state = next(s for s in team["states"] if s["name"] == name)
        issue["state"] = {k: state[k] for k in ("id", "name", "type")}
    return data
