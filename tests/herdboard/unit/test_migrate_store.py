import fcntl
import unittest

from herdboard.identity import RepoIdentity
from herdboard.migrate_convert import convert
from herdboard.model import ConflictError, FormatError, HerdboardError, NotFoundError
from support.herdboard_store import BASIC, StoreTestCase
from support.herdboard_store import write_board as write_folder

IDENTITY = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
TODO = "# Garden\n- water ferns\n\t- weekly\n- [x] prune roses\n# Shed\n- oil hinges\n"


def write_board(todos, name, columns, cards=None, archive=None, repo=None):
    """A board folder from (column, card IDs, options) and a map of card ID to title."""
    cards = cards or {}
    spec = [(title, [{"id": ref, "title": cards[ref]} for ref in refs], options) for title, refs, options in columns]
    gone = [{"id": ref, "title": title} for ref, title in (archive or {}).items()]
    return write_folder(todos, name, spec, gone, repo=repo)


def items(text=TODO):
    return convert(text).items


class ImportTests(StoreTestCase):
    def titles(self, path, column):
        return [(c.title, c.tags) for c in self.store.list_cards([path]) if c.column == column]

    def test_new_board_like_init(self):
        path = self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual(path, self.todos / "boards/acme-api")
        self.assertEqual(self.store.board_meta(path).repo, "github.com/acme/api")
        self.assertEqual(list(self.columns(path)), ["Backlog", "Todo", "In progress", "Review", "Done"])
        self.assertEqual(self.titles(path, "Backlog"), [("water ferns", ["garden"]), ("oil hinges", ["shed"])])
        self.assertEqual(self.titles(path, "Done"), [("prune roses", ["garden"])])
        ferns = next(c for c in self.store.list_cards([path]) if c.title == "water ferns")
        self.assertEqual(ferns.body, ["- weekly"])
        self.assertTrue(self.store.snapshot(path).columns[-1].completed)

    def test_new_board_matches_init_board_apart_from_cards(self):
        init = self.store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        filled = self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual(list(self.columns(init)), list(self.columns(filled)))

    def test_every_card_has_a_unique_id(self):
        write_board(self.todos, "other", [("Todo", ["tabc"], {})], cards={"tabc": "x"})
        path = self.store.import_cards(items(), identity=IDENTITY)
        ids = [ref for refs in self.columns(path).values() for ref in refs]
        self.assertEqual(len(set(ids)), 3)
        self.assertNotIn("tabc", ids)
        self.assertEqual(self.store.show("tabc").title, "x")

    def test_existing_board_gets_cards_at_the_bottom(self):
        path = write_board(
            self.todos,
            "acme-api",
            [("Backlog", ["taaa"], {}), ("Todo", ["tbbb"], {}), ("Done", ["tccc"], {"done": True})],
            cards={"taaa": "first", "tbbb": "second", "tccc": "finished"},
            archive={"tddd": "old"},
            repo="github.com/acme/api",
        )
        self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual([t for t, _ in self.titles(path, "Backlog")], ["first", "water ferns", "oil hinges"])
        self.assertEqual([t for t, _ in self.titles(path, "Todo")], ["second"])
        self.assertEqual([t for t, _ in self.titles(path, "Done")], ["finished", "prune roses"])
        self.assertEqual([c.ref for c in self.store.archived_cards([path])], ["tddd"])

    def test_open_items_go_to_the_first_non_triage_column(self):
        path = write_board(
            self.todos,
            "acme-api",
            [("Triage", [], {}), ("Backlog", ["taaa"], {}), ("Done", [], {"done": True})],
            cards={"taaa": "first"},
            repo="github.com/acme/api",
        )
        self.store.import_cards(items("- [ ] fresh\n- [x] finished\n"), identity=IDENTITY)
        self.assertEqual(self.titles(path, "Triage"), [])
        self.assertEqual([t for t, _ in self.titles(path, "Backlog")], ["first", "fresh"])
        self.assertEqual([t for t, _ in self.titles(path, "Done")], ["finished"])

    def test_existing_cards_keep_their_bytes(self):
        path = write_board(
            self.todos, "acme-api", [("Todo", ["t111"], {}), ("Done", [], {"done": True})], cards={"t111": "one"}
        )
        before = self.card_text(path, "t111")
        self.store.import_cards(items("- [ ] only\n"), path=path)
        self.assertEqual(self.card_text(path, "t111"), before)

    def test_named_board_and_inbox(self):
        named = write_board(self.todos, "homelab", [("Backlog", [], {})])
        self.assertEqual(self.store.import_cards(items("- a\n"), path=named), named)
        inbox = self.store.import_cards(items("- a\n"), path=self.store.inbox_path)
        self.assertEqual(inbox, self.todos / "boards/inbox")
        self.assertEqual([c.title for c in self.store.list_cards([inbox])], ["a"])

    def test_done_column_without_flag_is_named_done(self):
        path = write_board(self.todos, "acme-api", [("Backlog", [], {}), ("Done", [], {})])
        self.store.import_cards(items("- [x] a\n"), path=path)
        self.assertEqual(len(self.columns(path)["Done"]), 1)

    def test_checked_card_without_done_column_fails_before_writing(self):
        path = write_board(self.todos, "acme-api", [("Backlog", ["taaa"], {})], cards={"taaa": "one"})
        before = self.board_text(path)
        with self.assertRaisesRegex(HerdboardError, "no done column"):
            self.store.import_cards(items("- [x] a\n"), path=path)
        self.assertEqual(self.board_text(path), before)
        self.assertEqual([p.name for p in (path / "cards").iterdir()], ["taaa"])

    def test_unreadable_board_is_refused(self):
        path = self.todos / "boards" / "acme-api"
        path.mkdir(parents=True)
        (path / "board.toml").write_text("not = [toml\n")
        with self.assertRaises(FormatError):
            self.store.import_cards(items("- a\n"), path=path)
        self.assertEqual((path / "board.toml").read_text(), "not = [toml\n")

    def test_held_lock_is_a_conflict(self):
        self.store.ensure_inbox()
        with open(self.store.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError):
                self.store.import_cards(items("- a\n"), identity=IDENTITY)
        self.assertFalse((self.todos / "boards/acme-api").exists())

    def test_board_created_by_another_writer_is_reused(self):
        self.store.init_board(IDENTITY)
        path = self.store.import_cards(items("- a\n"), identity=IDENTITY)
        self.assertEqual(path, self.todos / "boards/acme-api")
        self.assertEqual([p.name for p in (self.todos / "boards").iterdir()], ["acme-api"])

    def test_no_temp_files_left(self):
        path = self.store.import_cards(items(), identity=IDENTITY)
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["board.toml", "cards"])
        for folder in (path / "cards").iterdir():
            self.assertEqual([p.name for p in folder.iterdir()], ["card.md"])


class SourceIdStoreTests(StoreTestCase):
    def test_source_ids_are_dropped_and_fresh_ones_allocated(self):
        write_board(self.todos, "other", [("Todo", ["tabc"], {})], cards={"tabc": "x"})
        path = self.store.import_cards(items("- imported ^tabc\n- b ^tabc\n- c ^long-id-here\n"), identity=IDENTITY)
        cards = self.store.list_cards([path])
        self.assertEqual([c.title for c in cards], ["imported", "b", "c"])
        ids = [c.ref for c in cards]
        self.assertEqual(len(set(ids)), 3)
        self.assertNotIn("tabc", ids)
        self.assertEqual(self.store.show("tabc").title, "x")

    def test_source_id_before_a_section_tag_is_dropped(self):
        path = self.store.import_cards(items("# Shed\n- a ^tabc\n"), identity=IDENTITY)
        card = self.store.list_cards([path])[0]
        self.assertEqual((card.title, card.tags), ("a", ["shed"]))
        self.assertNotEqual(card.ref, "tabc")


class IdentityKeptTests(StoreTestCase):
    def repo_board(self, repo="github.com/acme/api"):
        return write_board(self.todos, "acme-api", [("Backlog", ["taaa"], {})], cards={"taaa": "one"}, repo=repo)

    def test_existing_repository_target_keeps_its_identity(self):
        self.repo_board()
        target = self.store.import_target(IDENTITY)
        self.assertTrue(target.exists)
        self.assertEqual(target.identity, IDENTITY)

    def test_explicit_board_has_no_identity(self):
        self.repo_board()
        self.assertIsNone(self.store.import_target(IDENTITY, "acme-api").identity)
        self.assertIsNone(self.store.import_target(IDENTITY, "inbox").identity)

    def test_changed_repo_between_preview_and_write_is_a_conflict(self):
        path = self.repo_board()
        target = self.store.import_target(IDENTITY)
        board = path / "board.toml"
        changed = board.read_text().replace("github.com/acme/api", "github.com/acme/other")
        board.write_text(changed)
        with self.assertRaises(ConflictError):
            self.store.import_cards(items("- a\n"), target.path, target.identity)
        self.assertEqual(board.read_text(), changed)
        self.assertEqual([p.name for p in (path / "cards").iterdir()], ["taaa"])

    def test_removed_repo_line_is_a_conflict(self):
        path = self.repo_board()
        target = self.store.import_target(IDENTITY)
        board = path / "board.toml"
        board.write_text(board.read_text().replace('repo = "github.com/acme/api"\n', ""))
        with self.assertRaises(ConflictError):
            self.store.import_cards(items("- a\n"), target.path, target.identity)

    def test_same_repo_with_other_case_still_writes(self):
        path = self.repo_board("github.com/Acme/API")
        self.store.import_cards(items("- a\n"), path, IDENTITY)
        self.assertIn("a", [c.title for c in self.store.list_cards([path])])


class TargetTests(StoreTestCase):
    def test_new_repository_board(self):
        target = self.store.import_target(IDENTITY)
        self.assertEqual(target.path, self.todos / "boards/acme-api")
        self.assertFalse(target.exists)
        self.assertEqual((target.open_column, target.done_column), ("Backlog", "Done"))

    def test_existing_repository_board_uses_its_columns(self):
        path = write_board(
            self.todos, "acme-api", [("Ideas", [], {}), ("Shipped", [], {"done": True})], repo="github.com/acme/api"
        )
        target = self.store.import_target(IDENTITY)
        self.assertEqual(target.path, path)
        self.assertTrue(target.exists)
        self.assertEqual((target.open_column, target.done_column), ("Ideas", "Shipped"))

    def test_named_board(self):
        path = self.put("homelab", BASIC)
        target = self.store.import_target(None, "homelab")
        self.assertEqual((target.path, target.open_column, target.done_column), (path, "Todo", "Done"))
        with self.assertRaises(NotFoundError):
            self.store.import_target(None, "nope")

    def test_inbox_before_it_exists(self):
        target = self.store.import_target(None, "inbox")
        self.assertEqual(target.path, self.todos / "boards/inbox")
        self.assertFalse(target.exists)

    def test_no_identity_and_no_name_uses_inbox(self):
        self.assertEqual(self.store.import_target(None).path, self.todos / "boards/inbox")

    def test_board_without_done_column_reports_error(self):
        write_board(self.todos, "homelab", [("Backlog", [], {})])
        target = self.store.import_target(None, "homelab")
        self.assertIsNone(target.done_column)
        self.assertIn("no done column", target.done_error)


if __name__ == "__main__":
    unittest.main()
