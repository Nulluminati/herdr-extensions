import fcntl
import re
import unittest

from herdboard import kanban_md
from herdboard.identity import RepoIdentity
from herdboard.migrate_convert import convert
from herdboard.model import ConflictError, FormatError, HerdboardError, NotFoundError
from support.herdboard_store import BASIC, HEAD, SETTINGS, StoreTestCase, board_text, col

IDENTITY = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
TODO = "# Garden\n- water ferns\n\t- weekly\n- [x] prune roses\n# Shed\n- oil hinges\n"
ID = re.compile(r" \^(t[0-9a-z]{3})$", re.M)


def repo_board(*columns, archive=""):
    text = board_text(*columns, archive=archive)
    return text.replace("kanban-plugin: board\n", "kanban-plugin: board\nrepo: github.com/acme/api\n", 1)


def items(text=TODO):
    return convert(text).items


def blank_ids(text, keep=("taaa", "tbbb", "tccc", "tddd")):
    return ID.sub(lambda m: m.group(0) if m.group(1) in keep else " ^tXXX", text)


class ImportTests(StoreTestCase):
    def test_new_board_like_init(self):
        path = self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual(path, self.todos / "projects/acme-api.md")
        text = path.read_text()
        self.assertIn("repo: github.com/acme/api\n", text)
        board = kanban_md.parse(text)
        titles = [c.title for c in board.columns]
        self.assertEqual(titles, ["Backlog", "Todo", "In progress", "Review", "Done"])
        backlog, done = board.column("Backlog"), board.column("Done")
        self.assertEqual([c.title for c in backlog.cards],
                         ["water ferns #garden", "oil hinges #shed"])
        self.assertEqual(backlog.cards[0].body, ["\t- weekly"])
        self.assertEqual([(c.title, c.checked) for c in done.cards],
                         [("prune roses #garden", True)])
        self.assertTrue(done.has_marker(["Complete"]))

    def test_new_board_matches_init_board_apart_from_cards(self):
        init = self.store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        filled = self.store.import_cards(items(), identity=IDENTITY)
        stripped = "\n".join(
            line for line in filled.read_text().split("\n")
            if not line.startswith(("- [", "\t- ")))
        expected = init.read_text().replace("acme/web", "acme/api")
        self.assertEqual(re.sub(r"\n{3,}", "\n\n", stripped), re.sub(r"\n{3,}", "\n\n", expected))

    def test_every_card_has_a_unique_id(self):
        other = self.put("other", board_text(col("Todo", "- [ ] x ^tabc")))
        path = self.store.import_cards(items(), identity=IDENTITY)
        ids = ID.findall(path.read_text())
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertNotIn("tabc", ids)
        self.assertEqual(other.read_text(), board_text(col("Todo", "- [ ] x ^tabc")))

    def test_round_trip(self):
        path = self.store.import_cards(items(), identity=IDENTITY)
        text = path.read_text()
        self.assertEqual(kanban_md.serialize(kanban_md.parse(text)), text)

    def test_existing_board_gets_cards_at_the_bottom(self):
        old = repo_board(
            col("Backlog", "- [ ] first ^taaa", "\tbody"),
            col("Todo", "- [ ] second ^tbbb"),
            col("Done", "**Complete**", "- [x] finished ^tccc"),
            archive="***\n\n## Archive\n\n- [x] old ^tddd\n\n",
        )
        path = self.put("acme-api", old)
        self.store.import_cards(items(), identity=IDENTITY)
        expected = repo_board(
            col("Backlog", "- [ ] first ^taaa", "\tbody", "- [ ] water ferns #garden ^tXXX",
                "\t- weekly", "- [ ] oil hinges #shed ^tXXX"),
            col("Todo", "- [ ] second ^tbbb"),
            col("Done", "**Complete**", "- [x] finished ^tccc",
                "- [x] prune roses #garden ^tXXX"),
            archive="***\n\n## Archive\n\n- [x] old ^tddd\n\n",
        )
        self.assertEqual(blank_ids(path.read_text()), expected)
        new_ids = ID.findall(path.read_text())  # includes the archive card
        self.assertEqual(len(set(new_ids)), 7)

    def test_open_items_go_to_the_first_non_triage_column(self):
        path = self.put("acme-api", repo_board(
            col("Triage"),
            col("Backlog", "- [ ] first ^taaa"),
            col("Done", "**Complete**"),
        ))
        self.store.import_cards(items("- [ ] fresh\n- [x] finished\n"), identity=IDENTITY)
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.title for c in board.column("Triage").cards], [])
        self.assertEqual([c.title for c in board.column("Backlog").cards], ["first", "fresh"])
        self.assertEqual([c.title for c in board.column("Done").cards], ["finished"])

    def test_existing_cards_keep_their_bytes_when_all_have_ids(self):
        before = repo_board(
            col("Todo", "- [ ] one ^t111", "- [ ] two #tag 📅 2026-01-02 ^t222", "\tbody line"),
            col("Done", "**Complete**", "- [x] four ^t444"),
        )
        path = self.put("acme-api", before)
        self.store.import_cards(items("- [ ] only\n- [x] done only\n"), identity=IDENTITY)
        kept = [line for line in path.read_text().split("\n") if "only" not in line]
        self.assertEqual("\n".join(kept), before)

    def test_existing_cards_without_ids_get_them_by_the_write_rule(self):
        path = self.put("acme-api", board_text(col("Backlog", "- [ ] bare")))
        self.store.import_cards(items("- a\n"), path=path)
        self.assertRegex(path.read_text(), r"- \[ \] bare \^t[0-9a-z]{3}\n")

    def test_crlf_board_keeps_crlf(self):
        path = self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.store.import_cards(items("- a\n\t- b\n"), identity=IDENTITY)
        data = path.read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"- [ ] a ^t", data)

    def test_named_board_and_inbox(self):
        named = self.put("homelab", board_text(col("Backlog")))
        self.assertEqual(self.store.import_cards(items("- a\n"), path=named), named)
        inbox = self.store.import_cards(items("- a\n"), path=self.store.inbox_path)
        self.assertEqual(inbox, self.todos / "inbox.md")
        self.assertIn("- [ ] a ^t", inbox.read_text())

    def test_done_column_without_marker_is_named_done(self):
        path = self.put("acme-api", board_text(col("Backlog"), col("Done")))
        self.store.import_cards(items("- [x] a\n"), path=path)
        self.assertIn("- [x] a ^t", kanban_md.serialize(kanban_md.parse(path.read_text())))
        self.assertEqual(len(kanban_md.parse(path.read_text()).column("Done").cards), 1)

    def test_checked_card_without_done_column_fails_before_writing(self):
        text = board_text(col("Backlog", "- [ ] one ^taaa"))
        path = self.put("acme-api", text)
        with self.assertRaisesRegex(HerdboardError, "no done column"):
            self.store.import_cards(items("- [x] a\n"), path=path)
        self.assertEqual(path.read_text(), text)

    def test_unwritable_board_is_refused(self):
        text = "no frontmatter\n\n## Backlog\n\n"
        path = self.put("acme-api", text)
        with self.assertRaises(FormatError):
            self.store.import_cards(items("- a\n"), path=path)
        self.assertEqual(path.read_text(), text)

    def test_held_lock_is_a_conflict(self):
        self.store.ensure_inbox()
        with open(self.store.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError):
                self.store.import_cards(items("- a\n"), identity=IDENTITY)
        self.assertFalse((self.todos / "projects/acme-api.md").exists())

    def test_board_created_by_another_writer_is_reused(self):
        self.store.init_board(IDENTITY)
        path = self.store.import_cards(items("- a\n"), identity=IDENTITY)
        self.assertEqual(path, self.todos / "projects/acme-api.md")
        self.assertEqual(len(list((self.todos / "projects").glob("*.md"))), 1)

    def test_no_temp_files_left(self):
        self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual([p.name for p in (self.todos / "projects").iterdir()], ["acme-api.md"])


class SourceIdStoreTests(StoreTestCase):
    def test_source_id_used_in_another_board_gets_a_fresh_id(self):
        other = self.put("other", board_text(col("Todo", "- [ ] x ^tabc")))
        path = self.store.import_cards(items("- imported ^tabc\n"), identity=IDENTITY)
        cards = kanban_md.parse(path.read_text()).column("Backlog").cards
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "imported")
        self.assertNotEqual(cards[0].block_id, "tabc")
        self.assertRegex(cards[0].block_id, r"^t[0-9a-z]{3}$")
        self.assertEqual(other.read_text(), board_text(col("Todo", "- [ ] x ^tabc")))
        self.assertEqual(self.store.show("tabc").title, "x")

    def test_repeated_source_ids_get_distinct_fresh_ids(self):
        path = self.store.import_cards(items("- a ^tabc\n- b ^tabc\n"), identity=IDENTITY)
        cards = kanban_md.parse(path.read_text()).column("Backlog").cards
        self.assertEqual([c.title for c in cards], ["a", "b"])
        ids = [c.block_id for c in cards]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotIn("tabc", ids)

    def test_source_id_outside_the_format_is_replaced(self):
        path = self.store.import_cards(items("- a ^long-id-here\n"), identity=IDENTITY)
        card = kanban_md.parse(path.read_text()).column("Backlog").cards[0]
        self.assertEqual(card.title, "a")
        self.assertRegex(card.block_id, r"^t[0-9a-z]{3}$")
        self.assertNotIn("long-id-here", path.read_text())

    def test_source_id_before_a_section_tag_is_replaced(self):
        self.put("other", board_text(col("Todo", "- [ ] x ^tabc")))
        path = self.store.import_cards(items("# Shed\n- a ^tabc\n"), identity=IDENTITY)
        card = kanban_md.parse(path.read_text()).column("Backlog").cards[0]
        self.assertEqual((card.title, card.tags), ("a #shed", ["shed"]))
        self.assertNotEqual(card.block_id, "tabc")


class IdentityKeptTests(StoreTestCase):
    def test_existing_repository_target_keeps_its_identity(self):
        self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        target = self.store.import_target(IDENTITY)
        self.assertTrue(target.exists)
        self.assertEqual(target.identity, IDENTITY)

    def test_explicit_board_has_no_identity(self):
        self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        self.assertIsNone(self.store.import_target(IDENTITY, "acme-api").identity)
        self.assertIsNone(self.store.import_target(IDENTITY, "inbox").identity)

    def test_changed_repo_between_preview_and_write_is_a_conflict(self):
        path = self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        target = self.store.import_target(IDENTITY)
        changed = path.read_text().replace("github.com/acme/api", "github.com/acme/other")
        path.write_text(changed)
        with self.assertRaises(ConflictError):
            self.store.import_cards(items("- a\n"), target.path, target.identity)
        self.assertEqual(path.read_text(), changed)
        self.assertEqual([p.name for p in (self.todos / "projects").iterdir()], ["acme-api.md"])

    def test_removed_repo_line_is_a_conflict(self):
        path = self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        target = self.store.import_target(IDENTITY)
        stripped = path.read_text().replace("repo: github.com/acme/api\n", "")
        path.write_text(stripped)
        with self.assertRaises(ConflictError):
            self.store.import_cards(items("- a\n"), target.path, target.identity)
        self.assertEqual(path.read_text(), stripped)

    def test_same_repo_with_other_case_still_writes(self):
        path = self.put("acme-api", repo_board(col("Backlog", "- [ ] one ^taaa")))
        path.write_text(path.read_text().replace("acme/api", "Acme/API"))
        self.store.import_cards(items("- a\n"), path, IDENTITY)
        self.assertIn("- [ ] a ^t", path.read_text())


class TargetTests(StoreTestCase):
    def test_new_repository_board(self):
        target = self.store.import_target(IDENTITY)
        self.assertEqual(target.path, self.todos / "projects/acme-api.md")
        self.assertFalse(target.exists)
        self.assertEqual((target.open_column, target.done_column), ("Backlog", "Done"))

    def test_existing_repository_board_uses_its_columns(self):
        path = self.put("acme-api", HEAD.replace("\n---\n\n", "repo: github.com/acme/api\n\n---\n\n")
                        + col("Ideas") + col("Shipped", "**Complete**") + "\n\n" + SETTINGS)
        target = self.store.import_target(IDENTITY)
        self.assertEqual(target.path, path)
        self.assertTrue(target.exists)
        self.assertEqual((target.open_column, target.done_column), ("Ideas", "Shipped"))

    def test_named_board(self):
        path = self.put("homelab", BASIC)
        target = self.store.import_target(None, "homelab")
        self.assertEqual((target.path, target.open_column, target.done_column),
                         (path, "Todo", "Done"))
        with self.assertRaises(NotFoundError):
            self.store.import_target(None, "nope")

    def test_inbox_before_it_exists(self):
        target = self.store.import_target(None, "inbox")
        self.assertEqual(target.path, self.todos / "inbox.md")
        self.assertFalse(target.exists)

    def test_no_identity_and_no_name_uses_inbox(self):
        self.assertEqual(self.store.import_target(None).path, self.todos / "inbox.md")

    def test_board_without_done_column_reports_error(self):
        self.put("homelab", board_text(col("Backlog")))
        target = self.store.import_target(None, "homelab")
        self.assertIsNone(target.done_column)
        self.assertIn("no done column", target.done_error)


if __name__ == "__main__":
    unittest.main()
