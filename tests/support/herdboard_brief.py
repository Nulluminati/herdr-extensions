from pathlib import Path

from herdboard.store import CardView


def todo(**changes):
    fields = dict(
        ref="t83k",
        board="nulluminati-dotfiles-ansible",
        path=Path("/todos/boards/x"),
        column="In progress",
        title="Move to shared [[Stow notes]]",
        checked=False,
        due="2026-11-01",
        tags=["dots-todo"],
        body=["- bat", "- [ ] fish", "\t- nested"],
    )
    fields.update(changes)
    return CardView(**fields)
