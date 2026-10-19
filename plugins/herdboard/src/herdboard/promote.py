"""Promote a todo to a Linear issue, with its checklist and its sub-cards as sub-issues.

The promotion is recorded before the first call (see promotions.py), so a failure at any
point leaves a record that a rerun can finish. The todo changes last, only after Linear
confirmed every issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Callable

from .config import Config
from .linear import LinearClient, LinearError
from .linear_cards import LINEAR_REF
from .linear_states import Ask, StateChoices, refuse, resolve_state
from .linear_writes import LinearWriter, pick_label
from .model import ConflictError, HerdboardError, card_type
from .promote_convert import Conversion, convert, promoted_identifier
from .promotions import PromotionRecords
from .store import TEMP_REF, CardView, Store, TodoSnapshot

CLOSED = ("completed", "canceled")

# Finds the Linear project and the label IDs that the issue gets by default, for a todo.
Target = Callable[[LinearClient, CardView], "tuple[dict | None, list[str]]"]


def no_target(client: LinearClient, view: CardView) -> tuple[dict | None, list[str]]:
    return None, []


class AlreadyPromotedError(HerdboardError):
    code = "already_promoted"


class NotATodoError(HerdboardError):
    code = "not_a_todo"


class ParentNotPromotedError(HerdboardError):
    code = "parent_not_promoted"


@dataclass
class Promotion:
    issue: dict
    sub_issues: list[dict]
    todo: CardView
    warnings: list[str] = field(default_factory=list)


class Promoter:
    def __init__(self, config: Config, store: Store, writer: LinearWriter):
        self.config = config
        self.store = store
        self.writer = writer

    def promote(
        self,
        ref: str,
        *,
        team: str | None = None,
        target: Target = no_target,
        ask: Ask = refuse,
        state_name: str | None = None,
        project: str | None = None,
        again: str | None = None,
    ) -> Promotion:
        """Create the issue and sub-issues for a todo, then move the todo to its done column.

        `target` is called only when a new promotion starts. A todo with a record continues
        that record and creates only what Linear does not have. `team`, `project` and
        `state_name` that differ from the record become warnings; the record wins.
        `again` tells how to finish after a failure; `{ref}` in it is the todo ID.
        """
        if LINEAR_REF.match(ref):
            raise NotATodoError(f"{ref} is a Linear issue, not a todo")
        with self.writer.online() as client:
            store = str(self.config.todos_dir.resolve())
            records = PromotionRecords(self.writer.source.account_dir(), store)
            with records.lock():
                return self._promote(
                    client, records, ref, team, target, ask, state_name, project, again
                )

    def _promote(self, client, records, ref, team, target, ask, state_name, project, again) -> Promotion:
        self._refuse_if_marked(records, self.store.show(ref))
        todo_id = self.store.ensure_id(ref).ref
        snapshot = self.store.todo_snapshot(todo_id)
        # The plan comes from this snapshot, so its marker is the one that counts.
        self._refuse_if_marked(records, snapshot.view)
        if snapshot.view.parent:
            raise ParentNotPromotedError(
                f"{todo_id} is a sub-card of {snapshot.view.parent}. Promote {snapshot.view.parent}: "
                "its sub-cards become sub-issues")
        record = records.get(todo_id)
        resuming = record is not None
        warnings = []
        if record is None:
            record = self._plan(client, snapshot, team, target, ask, state_name)
            records.put(todo_id, record)
        else:
            warnings = self._differences(record, team, project, state_name)
        again = (again or "Run `herdboard promote {ref}` again to finish").format(ref=todo_id)
        try:
            found = self._create_missing(client, records, todo_id, record, resuming)
        except LinearError as error:
            error.args = (f"{error}. {again}",)
            raise
        parent, *children = record["issues"]
        for child in children:
            # A sub-card that a rerun finds marked was finished by the run before.
            if child.get("todo") and not promoted_identifier(self.store.show(child["todo"]).title):
                self.store.finish_promotion(self.store.todo_snapshot(child["todo"]), child["identifier"])
        try:
            todo = self.store.finish_promotion(snapshot, parent["identifier"])
        except ConflictError as error:
            error.args = (
                f"{error}. Linear has {parent['identifier']}. {again} and update the todo",
            )
            raise
        records.delete(todo_id)
        issue = self.writer._full(
            client,
            parent["identifier"],
            {"title": parent["input"]["title"], "labels": [], "project": None, **found[parent["id"]]},
        )
        subs = [
            {"id": c["id"], "identifier": c["identifier"], "title": c["input"]["title"]}
            for c in children
        ]
        return Promotion(issue, subs, todo, warnings)

    def _refuse_if_marked(self, records, view: CardView) -> None:
        """Refuse a todo with a `→ identifier` marker. Its record stays while work is left."""
        done = promoted_identifier(view.title)
        if not done:
            return
        message = f"{view.ref} was already promoted to {done}"
        record = None if TEMP_REF.match(view.ref) else records.get(view.ref)
        if record is not None:
            left = [i for i in record["issues"] if i["identifier"] is None]
            if left:
                parent = record["issues"][0]["identifier"] or "not confirmed"
                raise AlreadyPromotedError(
                    f"{message}, but a promotion is unfinished: recorded parent {parent}, "
                    f"{len(left)} of {len(record['issues'])} issues are not confirmed. "
                    f"To finish it, remove the marker from the todo and run "
                    f"`herdboard promote {view.ref}` again"
                )
            records.delete(view.ref)
        raise AlreadyPromotedError(message)

    def _differences(self, record: dict, team, project, state_name) -> list[str]:
        """Warnings for the options of a rerun that differ from what the record planned."""
        found = []
        for label, given, planned in (
            ("team", team, record.get("team")),
            ("project", project, record.get("project")),
            ("state", state_name, record.get("state")),
        ):
            if given is not None and given.casefold() != (planned or "").casefold():
                found.append(
                    f"--{label} {given} is ignored: the recorded plan uses "
                    f"{label} {planned or 'none'}"
                )
        return found

    # Planning

    def _plan(self, client, snapshot: TodoSnapshot, team_key, target, ask, state_name) -> dict:
        """The issues to create, with a client UUID each. Sends nothing to Linear."""
        converted = convert(snapshot.located.lines)
        project, label_ids = target(client, snapshot.view)
        team = self.writer.resolve_team(client, team_key, project)
        states = client.team_states(team["id"])
        choices = StateChoices(self.writer.source.account_dir())
        completed = self.config.linear.columns["completed"]
        column = completed if snapshot.done else snapshot.view.column
        # A todo in the done column becomes a completed issue, whatever the names say.
        parent_state = resolve_state(
            self.config, choices, team, states, column, ask, state_name,
            "completed" if snapshot.done else None,
        )

        def state_of(sub) -> dict:
            if sub.checked:
                if parent_state["type"] == "completed":
                    return parent_state
                return resolve_state(
                    self.config, choices, team, states, completed, ask, only_type="completed"
                )
            if parent_state["type"] in CLOSED:
                return resolve_state(
                    self.config, choices, team, states, self.config.columns[0], ask
                )
            return parent_state

        sub_states = [state_of(sub) for sub in converted.subs]
        labels, unlabeled = self._labels(client, team, converted.tags)
        description = self._description(snapshot, converted, unlabeled)
        assignee = self.writer.source.account()[1]
        base = {"team_id": team["id"], "assignee_id": assignee}
        if project:
            base["project_id"] = project["id"]
        parent_id = str(uuid.uuid4())
        parent = {**base, "title": converted.title, "state_id": parent_state["id"]}
        for key, value in (
            ("label_ids", list(dict.fromkeys([*label_ids, *labels]))),
            ("description", description),
            ("due_date", converted.due),
        ):
            if value:
                parent[key] = value
        issues = [{"id": parent_id, "input": parent, "identifier": None}]
        for sub, state in zip(converted.subs, sub_states):
            child = {**base, "title": sub.title, "state_id": state["id"], "parent_id": parent_id}
            issues.append({"id": str(uuid.uuid4()), "input": child, "identifier": None})
        for card in self.store.children(snapshot.view.ref):
            if promoted_identifier(card.title):
                continue
            child = self._sub_card(client, card, team, states, choices, ask, base, parent_id)
            issues.append({"id": str(uuid.uuid4()), "input": child, "identifier": None, "todo": card.ref})
        return {
            "team": team["key"],
            "project": project["name"] if project else None,
            "state": parent_state["name"],
            "issues": issues,
        }

    def _sub_card(self, client, card, team, states, choices, ask, base, parent_id) -> dict:
        """The create input of a sub-card's issue: its own title, state, labels, due date and text."""
        sub_snapshot = self.store.todo_snapshot(card.ref)
        converted = convert(sub_snapshot.located.lines)
        completed = self.config.linear.columns["completed"]
        state = resolve_state(
            self.config, choices, team, states, completed if sub_snapshot.done else card.column, ask, None,
            "completed" if sub_snapshot.done else None,
        )
        # Sub-issues go one level deep, so a sub-card's checklist stays text in its description.
        checklist = [f"- [{'x' if sub.checked else ' '}] {sub.title}" for sub in converted.subs]
        if checklist:
            converted.leftover += ([""] if converted.leftover else []) + checklist
            converted.subs = []
        labels, unlabeled = self._labels(client, team, converted.tags)
        child = {**base, "title": converted.title, "state_id": state["id"], "parent_id": parent_id}
        for key, value in (
            ("label_ids", labels),
            ("description", self._description(sub_snapshot, converted, unlabeled)),
            ("due_date", converted.due),
        ):
            if value:
                child[key] = value
        return child

    def _labels(self, client, team: dict, tags: list[str]) -> tuple[list[str], list[str]]:
        """Label IDs for the tags, and the tags that have no label. A team label wins."""
        ids, unlabeled = [], []
        for tag in dict.fromkeys(tags):
            # A `type/<name>` tag becomes the label `<name>`.
            chosen = pick_label(client, card_type([tag]) or tag, team)
            if chosen is None:
                unlabeled.append(tag)
            else:
                ids.append(chosen["id"])
        return ids, unlabeled

    def _description(self, snapshot: TodoSnapshot, converted: Conversion, unlabeled) -> str | None:
        """Notes first, then the leftover lines, then tags and links with nothing to become."""
        notes = [n for n in self.store.notes_for(snapshot.view) or [] if n["content"] is not None]
        parts = [
            n["content"].strip() if len(notes) == 1 else f"## {n['name']}\n{n['content'].strip()}"
            for n in notes
        ]
        if converted.leftover:
            parts.append("\n".join(converted.leftover))
        found = {n["name"] for n in notes}
        rest = [" ".join(f"#{tag}" for tag in unlabeled)] if unlabeled else []
        rest += [f"[[{name}]]" for name in converted.links if name not in found]
        if rest:
            parts.append("\n".join(rest))
        return "\n\n".join(parts) or None

    # Creating

    def _create_missing(
        self, client, records, todo_id: str, record: dict, resuming: bool
    ) -> dict[str, dict]:
        """Create the issues that Linear lacks, parent first, recording each confirmed one.

        A rerun looks every recorded UUID up before it sends a create. Returns the issues
        that Linear confirmed, by UUID.
        """
        confirmed = {}
        for item in record["issues"]:
            found = client.issue_for_write(item["id"]) if resuming else None
            if found is None:
                found = self.writer.send_create(client, item["id"], **item["input"])
            if item["identifier"] != found["identifier"]:
                item["identifier"] = found["identifier"]
                records.put(todo_id, record)
            confirmed[item["id"]] = found
        return confirmed
