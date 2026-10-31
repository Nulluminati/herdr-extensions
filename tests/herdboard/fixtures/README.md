# herdboard fixtures

The `kanban-*.md` boards are in the old Markdown board format, which
herdboard used before board folders. They follow the serializer of the
Obsidian Kanban plugin 2.0.51 (`boardToMd`, `laneToMd`, `itemToMd` and
`settingsToCodeblock` in `mgmeyers/obsidian-kanban`), with Obsidian's default
tab indentation. They were written from that source, not saved by Obsidian.
Tests read them with `kanban_md`, the reader of `convert-store`, or convert
them to board folders with `write_markdown_board` from
`tests/support/herdboard_store.py`.

- `kanban-2.0.51-full.md`: multi-line card, nested items, card limit
  (`Todo (5)`), block IDs, due date, note link, complete marker, archive,
  settings block.
- `kanban-2.0.51-empty.md`: new board with empty columns.
- `kanban-promote.md`: cards for `promote`: tags, due date, checklist with
  checked items, deeper items, plain nested lines, note links, a done card.

## convert/

Old Markdown boards for `herdboard convert-store`.

- `project.md`: a repository board with `repo`, `merge` and Linear
  frontmatter, a Triage column, a card limit, type tags, a due date, a nested
  body, a parent link, a promoted card, a card without an ID, an invalid ID,
  a complete marker on a column that is not named Done, and an archive.
- `inbox-crlf.md`: an inbox board with CRLF line endings, a space-indented
  body, and a done column without a complete marker.

## migrate/

Neutral `TODO.md` files for `herdboard migrate`. Each one reproduces a style
of real todo file with made-up text.

- `atx.md`: `# Heading` sections, tab indents three levels deep, blank lines
  between items, punctuation in a heading (`Pantry & Misc.`).
- `setext-colon.md`: `Heading:` lines underlined with `=====` and `-----`,
  8-space indents.
- `colon-tabs.md`: plain `Heading:` lines with list items below, tab indents.
- `bullets.md`: `-`, `*`, `+`, `1.` and `1)` bullets, with and without
  checkboxes, at top level and nested.
- `indents.md`: 2-space, 4-space, tab and 8-space indents, and an item before
  the first heading.
- `loose-text.md`: text lines that are not list items, before and after
  items, after an ATX heading, and a bare `----` line.
