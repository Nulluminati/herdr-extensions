from pathlib import Path

from herdboard.store import CardView


def todo(**changes):
    fields = dict(
        ref="t83k", board="nulluminati-dotfiles-ansible", path=Path("/todos/projects/x.md"),
        column="In progress", title="Move to shared [[Stow notes]] #dots-todo 📅 2026-11-01",
        checked=False, due="2026-11-01", tags=["dots-todo"], body=["\t- bat", "\t- [ ] fish", "\t\t- nested"],
    )
    fields.update(changes)
    return CardView(**fields)
