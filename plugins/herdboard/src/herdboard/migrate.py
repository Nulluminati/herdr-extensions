"""Preview for `herdboard migrate`: which cards go to which column, and which lines need a look."""

from __future__ import annotations

from .migrate_convert import Conversion, Item
from .store import ImportTarget


def _card_json(item: Item) -> dict:
    return {"title": item.title, "tag": item.tag, "checked": item.checked, "body": item.body}


def build_preview(conversion: Conversion, target: ImportTarget) -> dict:
    """The cards by column in board order (open cards first), and the non-list lines."""
    items = conversion.items
    if target.done_column == target.open_column:
        groups = [(target.open_column, items)]
    else:
        groups = [(target.open_column, [i for i in items if not i.checked])]
        if target.done_column is not None:
            groups.append((target.done_column, [i for i in items if i.checked]))
    return {
        "columns": [{"column": title, "cards": [_card_json(i) for i in cards]} for title, cards in groups if cards],
        "non_list": [
            {
                "line": n.number,
                "text": n.text,
                "action": n.action,
                "section": n.section,
                **({"parent": n.parent} if n.action == "ambiguous" else {}),
            }
            for n in conversion.non_list
        ],
        "dropped_ids": [{"card": i.title, "id": i.dropped_id} for i in items if i.dropped_id],
    }


def preview_text(target: ImportTarget, preview: dict, count: int) -> str:
    lines = [f"Board: {target.path}" + ("" if target.exists else " (new)"), f"Cards: {count}"]
    for column in preview["columns"]:
        lines.append(f"## {column['column']} ({len(column['cards'])})")
        for card in column["cards"]:
            mark = "x" if card["checked"] else " "
            tag = f" #{card['tag']}" if card["tag"] else ""
            lines.append(f"  - [{mark}] {card['title']}{tag}")
            lines.extend("  " + line.replace("\t", "  ") for line in card["body"])
    if preview["non_list"]:
        lines.append(f"Non-list lines ({len(preview['non_list'])}):")
        for entry in preview["non_list"]:
            under = f" (under: {entry['parent'] or 'no parent, new card'})" if entry["action"] == "ambiguous" else ""
            lines.append(f"  line {entry['line']}: {entry['action']}: {entry['text']}{under}")
    if preview["dropped_ids"]:
        lines.append(f"Dropped source IDs ({len(preview['dropped_ids'])}), new IDs are allocated:")
        lines.extend(f"  ^{entry['id']}: {entry['card']}" for entry in preview["dropped_ids"])
    return "\n".join(lines)
