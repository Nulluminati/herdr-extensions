"""Doctor checks isolated stores, state files and the quiet Linear fixture server."""

import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import cli, doctor, linear_source
from herdboard.config import load_config
from herdboard.identity import IdentityError
from herdboard.linear import LinearClient, LinearError
from herdboard.model import IoError
from herdboard.store import Store
from support.linear_data import fixtures
from support.linear_fake import FakeLinear


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.todos = self.root / "todos"
        self.config = self.root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n[linear]\nenabled = false\n')
        self.state = self.root / "state" / "herdboard"
        env = patch.dict(
            os.environ,
            {
                "HOME": str(self.root),
                "HERDBOARD_CONFIG": str(self.config),
                "XDG_STATE_HOME": str(self.root / "state"),
                "LINEAR_API_KEY": "",
                "HERDR_BIN_PATH": str(self.root / "herdr"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def board(self, name="inbox", cards=("t123",), folders=None, archive=(), done=True, top="", columns=None):
        """A board folder. `top` is raw TOML before the columns; `folders` the card folders made."""
        path = self.todos / "boards" / name
        path.mkdir(parents=True, exist_ok=True)
        if columns is None:
            columns = [("Todo", list(cards), {})] + ([("Done", [], {"done": True})] if done else [])
        lines = ["format = 1", *([top.rstrip("\n")] if top else [])]
        for title, ids, options in columns:
            lines += ["", "[[column]]", f"name = {json.dumps(title)}"]
            lines += ["done = true"] if options.get("done") else []
            lines += [f"cards = {json.dumps(ids)}"]
        (path / "board.toml").write_text("\n".join(lines) + "\n")
        for folder, ids in (("cards", cards if folders is None else folders), ("archive", archive)):
            for card_id in ids:
                (path / folder / card_id).mkdir(parents=True, exist_ok=True)
                (path / folder / card_id / "card.md").write_text(f'+++\ntitle = "Card {card_id}"\n+++\n')
        return path

    def lists(self, path):
        """The card lists of the columns in a board folder."""
        return [column["cards"] for column in tomllib.loads((path / "board.toml").read_text())["column"]]

    def run_doctor(self, *args, expect=0, text=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(["-C", str(self.root), "doctor", *args, *([] if text else ["--json"])])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(status, expect, out.getvalue())
        return out.getvalue() if text else json.loads(out.getvalue())

    def findings(self, code, *args, expect=0):
        return [f for f in self.run_doctor(*args, expect=expect)["findings"] if f["code"] == code]

    def state_file(self, name, data):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def enable_linear(self, key="test-key"):
        self.config.write_text(f'todos_dir = "{self.todos}"\n[linear]\nenabled = true\n')
        os.environ["LINEAR_API_KEY"] = key

    def links(self, project="p-plat", labels=()):
        text = f'[[linear-projects]]\nid = "{project}"\nname = "Platform"\n'
        if labels:
            items = ", ".join(f'{{id = "{i}", name = "{n}"}}' for i, n in labels)
            text += f"labels = [{items}]\n"
        return text

    @contextlib.contextmanager
    def server(self):
        with FakeLinear(fixtures()) as fake:
            with patch.object(
                linear_source,
                "build_client",
                side_effect=lambda key: LinearClient(key, fake.endpoint, timeout=2, max_retries=0),
            ):
                yield fake

    def test_invalid_boards_reported_together(self):
        paths = [self.board("a"), self.board("b"), self.board("c")]
        (paths[0] / "board.toml").write_text("format = [\n")
        (paths[1] / "board.toml").write_text("format = 2\n")
        (paths[2] / "board.toml").write_bytes(b"\xff")
        found = self.findings("board_invalid", expect=1)
        self.assertEqual({f["path"] for f in found}, {str(p / "board.toml") for p in paths})

    def test_card_folders_that_herdboard_cannot_read(self):
        path = self.board(cards=["t123", "t456"])
        (path / "cards" / "t123" / "card.md").write_text("no frontmatter\n")
        (path / "cards" / "t456" / "card.md").unlink()
        (path / "cards" / "notes").mkdir()
        result = self.run_doctor(expect=1)
        codes = {f["code"]: f["path"] for f in result["findings"]}
        self.assertEqual(codes["card_invalid"], str(path / "cards" / "t123" / "card.md"))
        self.assertEqual(codes["card_file_missing"], str(path / "cards" / "t456"))
        self.assertEqual(codes["card_folder_invalid"], str(path / "cards" / "notes"))

    def test_the_old_layout_names_convert_store(self):
        self.todos.mkdir()
        (self.todos / "inbox.md").write_text("## Todo\n")
        (found,) = self.findings("old_format", expect=1)
        self.assertEqual(found["fix"], "herdboard convert-store")

    def test_obsolete_config_keys_are_warnings(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\nvault_root = "~/Notes"\n[linear]\nenabled = false\n')
        (found,) = self.findings("config_key_obsolete")
        self.assertEqual(found["level"], "warning")
        self.assertIn("vault_root", found["message"])

    def test_card_lists_that_do_not_match_the_folders(self):
        path = self.board(cards=["t123", "t123", "tgon"], folders=["t123", "t456"])
        result = self.run_doctor()
        found = {f["code"]: f for f in result["findings"] if f["code"].startswith("card_")}
        self.assertIn("t123", found["card_listed_twice"]["message"])
        self.assertIn("tgon", found["card_missing"]["message"])
        self.assertEqual(found["card_orphan"]["path"], str(path / "cards" / "t456"))
        self.assertTrue(all(f["fix"] == "herdboard doctor --fix" for f in found.values()))

    def test_duplicate_ids_across_boards_and_archive(self):
        self.board()
        other = self.board("other", cards=[], archive=["t123"])
        (found,) = self.findings("duplicate_id", expect=1)
        self.assertEqual(found["path"], str(other / "archive" / "t123"))

    def test_duplicate_repository_identity(self):
        self.board("a", top='repo = "github.com/Acme/API"')
        self.board("b", cards=[], top='repo = "github.com/acme/api"')
        self.assertEqual(len(self.findings("duplicate_repo", expect=1)), 1)

    def test_invalid_linear_projects(self):
        path = self.board(top='linear-projects = "wrong"')
        found = self.findings("linear_links_invalid", expect=1)
        self.assertEqual(found[0]["path"], str(path / "board.toml"))

    def test_invalid_linear_pins(self):
        path = self.board(top='repo = "github.com/acme/api"\n\n[[linear-issues]]\nid = "i1"')
        found = self.findings("linear_pins_invalid", expect=1)
        self.assertEqual(found[0]["path"], str(path / "board.toml"))
        self.assertIn("identifier", found[0]["message"])

    def test_missing_pinned_issue(self):
        self.enable_linear()
        pins = [("i1", "ENG-1"), ("gone", "ENG-404"), ("i6", "ENG-6")]
        self.board(top="".join(f'[[linear-issues]]\nid = "{i}"\nidentifier = "{n}"\n' for i, n in pins))
        with self.server() as fake:
            found = self.findings("linear_pin_missing")
            self.assertEqual([f["level"] for f in found], ["warning"])
            self.assertIn("ENG-404", found[0]["message"])
            self.assertEqual([r["operationName"] for r in fake.requests], ["Viewer", "PinnedIssues"])

    def test_invalid_config(self):
        for content in ("[", "columns = 1", "start_agent = 'wrong'"):
            with self.subTest(content=content):
                self.config.write_text(content)
                found = self.findings("config_invalid", expect=1)
                self.assertEqual(found[0]["path"], str(self.config))

    def test_missing_done_column(self):
        self.board(done=False)
        self.assertEqual(self.findings("done_column_missing")[0]["level"], "warning")

    def test_a_board_without_columns_is_an_error(self):
        self.board(columns=[])
        found = self.findings("board_no_columns", expect=1)
        self.assertEqual(found[0]["level"], "error")
        self.assertIn("t123", found[0]["message"])

    def test_a_done_flag_or_the_name_done_counts_as_done(self):
        self.board(columns=[("Todo", ["t123"], {}), ("Shipped", [], {"done": True})])
        self.board("b", cards=[], columns=[("Todo", [], {}), ("done", [], {})])
        self.assertEqual(self.findings("done_column_missing"), [])

    def test_two_done_columns_are_a_warning(self):
        self.board(columns=[("Todo", ["t123"], {}), ("A", [], {"done": True}), ("B", [], {"done": True})])
        self.assertEqual(len(self.findings("done_column_multiple")), 1)

    def test_linear_missing_key(self):
        self.enable_linear("")
        with self.server() as fake:
            self.assertEqual(self.findings("linear_auth", expect=1)[0]["level"], "error")
            self.assertEqual(fake.requests, [])

    def test_linear_command_failure_is_auth_finding(self):
        self.config.write_text('[linear]\napi_key_command = ["missing-key-program"]\n')
        with patch("herdboard.config.subprocess.run", side_effect=FileNotFoundError("missing")):
            found = doctor.check_linear(load_config(), {}, False)
        self.assertEqual([(f["code"], f["level"]) for f in found], [("linear_auth", "error")])
        self.assertIn("missing-key-program", found[0]["message"])

    def test_disabled_or_offline_linear_does_not_run_command(self):
        with patch("herdboard.config.subprocess.run") as run:
            for enabled, offline in ((False, False), (True, True)):
                self.config.write_text(f'[linear]\nenabled = {str(enabled).lower()}\napi_key_command = ["key"]\n')
                self.assertEqual(doctor.check_linear(load_config(), {}, offline), [])
            run.assert_not_called()

    def test_linear_refused_key(self):
        self.enable_linear("refused")
        with self.server() as fake:
            self.assertEqual(len(self.findings("linear_auth", expect=1)), 1)
            self.assertEqual(len(fake.requests), 1)

    def test_missing_project(self):
        self.enable_linear()
        self.board(top=self.links("missing"))
        with self.server() as fake:
            self.assertEqual(self.findings("linear_project_missing")[0]["level"], "warning")
            self.assertFalse(any("mutation" in r["query"] for r in fake.requests))

    def test_missing_labels_checks_even_missing_project(self):
        self.enable_linear()
        self.board(top=self.links("missing", [("gone", "Gone")]))
        with self.server():
            self.assertEqual(len(self.findings("linear_label_missing")), 1)

    def test_linear_unreachable_is_one_warning(self):
        self.enable_linear()
        self.board(top=self.links())
        self.board("b", cards=[], top=self.links())
        with FakeLinear(fixtures()) as fake:
            endpoint = fake.endpoint
        with patch.object(
            linear_source,
            "build_client",
            side_effect=lambda key: LinearClient(key, endpoint, timeout=0.1, max_retries=0),
        ):
            found = self.findings("linear_unreachable")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["level"], "warning")

    def test_network_failure_after_viewer_stops_link_checks(self):
        self.enable_linear()
        self.board(top=self.links())
        self.board("b", cards=[], top=self.links())
        with self.server() as fake:
            client = LinearClient("test-key", fake.endpoint, timeout=2, max_retries=0)
            viewer = client.viewer

            def viewer_then_timeout():
                result = viewer()
                fake.delay = 0.2
                client.timeout = 0.05
                return result

            with (
                patch.object(linear_source, "build_client", return_value=client),
                patch.object(client, "viewer", viewer_then_timeout),
            ):
                self.assertEqual(len(self.findings("linear_unreachable")), 1)
            self.assertEqual([r["operationName"] for r in fake.requests], ["Viewer", "Project"])

    def test_offline_makes_no_request_even_without_key(self):
        self.enable_linear()
        with self.server() as fake:
            self.run_doctor("--offline")
            self.assertEqual(fake.requests, [])
        self.enable_linear("")
        self.assertEqual(self.findings("linear_auth", "--offline"), [])

    def test_unfinished_promotion(self):
        store = str(self.todos.resolve())
        path = self.state_file("org/user/promotions.json", {f"{store}::t123": {"store": store, "issues": []}})
        found = self.findings("promotion_unfinished")
        self.assertEqual(found[0]["fix"], "herdboard promote t123")
        self.assertEqual(found[0]["path"], str(path))
        self.assertEqual(found[0]["level"], "warning")

    def test_missing_start_worktree(self):
        self.state_file("starts.json", {"t123": {"worktree_path": str(self.root / "gone")}})
        self.assertEqual(self.findings("worktree_missing")[0]["level"], "info")

    def test_a_card_folder_without_a_start_record_is_a_warning(self):
        folder = self.todos / ".herdboard" / "work" / "t123"
        folder.mkdir(parents=True)
        self.state_file("starts.json", {})
        found = self.findings("work_folder_unused")[0]
        self.assertEqual(found["level"], "warning")
        self.assertEqual(found["path"], str(folder))
        self.assertIn(str(folder), found["fix"])

    def test_a_repoless_card_folder_belongs_to_its_start_record(self):
        folder = self.todos / ".herdboard" / "work" / "t123"
        folder.mkdir(parents=True)
        self.state_file("starts.json", {"t123": {"mode": "repoless", "worktree_path": str(folder)}})
        self.assertEqual(self.findings("work_folder_unused"), [])
        self.assertFalse(self.findings("worktree_missing"))

    def test_missing_checkout(self):
        self.state_file("checkouts.json", {"github.com/acme/api": [{"path": str(self.root / "gone"), "last_seen": 1}]})
        self.assertEqual(self.findings("checkout_stale")[0]["level"], "info")

    def test_checkout_identity_changed(self):
        for name, origin in (("first", "api"), ("second", "other")):
            repo = self.root / name
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", f"https://github.com/acme/{origin}.git"],
                check=True,
                capture_output=True,
            )
        self.state_file(
            "checkouts.json",
            {"github.com/acme/api": [{"path": str(self.root / name), "last_seen": 1} for name in ("first", "second")]},
        )
        found = self.findings("checkout_stale")
        self.assertEqual(len(found), 1)
        self.assertIn(str(self.root / "second"), found[0]["message"])

    def test_corrupt_state_files_reported_together(self):
        paths = [self.state_file(name, []) for name in ("starts.json", "checkouts.json", "org/user/promotions.json")]
        found = self.findings("state_invalid", expect=1)
        self.assertEqual({f["path"] for f in found}, {str(p) for p in paths})

    def test_malformed_state_records(self):
        self.state_file(
            "starts.json", {"t123": {"worktree_path": 42}, "good": {"worktree_path": str(self.root / "gone")}}
        )
        self.state_file(
            "checkouts.json",
            {
                "repo": [{}, {"path": str(self.root / "gone"), "last_seen": 1}],
                "good": [{"path": str(self.root / "also-gone"), "last_seen": 1}],
            },
        )
        store = str(self.todos.resolve())
        self.state_file("org/user/promotions.json", {"bad": 42, f"{store}::good": {"store": store, "issues": []}})
        result = self.run_doctor(expect=1)
        codes = [f["code"] for f in result["findings"]]
        self.assertEqual(codes.count("state_invalid"), 3)
        self.assertEqual(codes.count("worktree_missing"), 1)
        self.assertEqual(codes.count("checkout_stale"), 2)
        self.assertEqual(codes.count("promotion_unfinished"), 1)

    def test_missing_herdr_does_not_call_server(self):
        with patch("herdboard.start.command") as command:
            self.assertEqual(self.findings("herdr_missing")[0]["level"], "info")
            command.assert_not_called()

    def test_missing_gh_is_info(self):
        real = shutil.which
        with patch("herdboard.doctor.shutil.which", side_effect=lambda name: None if name == "gh" else real(name)):
            found = self.findings("gh_missing")
        self.assertEqual(
            [(f["level"], f["message"]) for f in found],
            [
                (
                    "info",
                    "No GitHub CLI `gh` found. Only merge --pr, finish of a pull request"
                    " and pull request reviews need it.",
                )
            ],
        )

    def test_missing_hunk_is_a_warning(self):
        real = shutil.which
        with patch("herdboard.doctor.shutil.which", side_effect=lambda name: None if name == "hunk" else real(name)):
            found = self.findings("hunk_missing")
        self.assertEqual([f["level"] for f in found], ["warning"])
        self.assertIn("pull request reviews", found[0]["message"])

    def test_hunk_without_the_review_extension_is_a_warning(self):
        listing = subprocess.CompletedProcess([], 0, "other-ext  v1.0.0  https://example.com\n", "")
        with (
            patch("herdboard.doctor.shutil.which", return_value="/usr/bin/hunk"),
            patch("herdboard.doctor.subprocess.run", return_value=listing) as run,
        ):
            found = self.findings("hunk_gh_review_missing")
        self.assertEqual([f["level"] for f in found], ["warning"])
        self.assertIn("hunk extension install phl28/hunk-gh-review", found[0]["fix"])
        self.assertEqual(run.call_args.args[0], ["hunk", "extension", "list"])

    def test_hunk_with_the_review_extension_is_fine(self):
        listing = subprocess.CompletedProcess(
            [], 0, "hunk-gh-review  v0.1.0  https://github.com/phl28/hunk-gh-review\n", ""
        )
        with (
            patch("herdboard.doctor.shutil.which", return_value="/usr/bin/hunk"),
            patch("herdboard.doctor.subprocess.run", return_value=listing),
        ):
            self.assertEqual(self.findings("hunk_gh_review_missing"), [])
            self.assertEqual(self.findings("hunk_missing"), [])

    def test_herdr_environment_path_takes_precedence(self):
        executable = self.root / "herdr"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        self.assertEqual(self.findings("herdr_missing"), [])

    def test_json_shape_and_error_exit(self):
        self.board()
        self.board("other", cards=["t123"])
        result = self.run_doctor(expect=1)
        self.assertEqual(set(result), {"findings", "fixed", "summary"})
        self.assertEqual(set(result["summary"]), {"error", "warning", "info"})
        for finding in result["findings"]:
            self.assertEqual(set(finding), {"level", "code", "path", "line", "message", "fix"})
        self.assertEqual(result["summary"]["error"], 1)

    def test_human_output_grouped_with_summary(self):
        self.board(done=False)
        with (
            patch("herdboard.doctor.check_hunk", return_value=[]),
            patch("herdboard.doctor.check_gh", return_value=[doctor.finding("info", "gh_missing", "no gh")]),
        ):
            text = self.run_doctor(text=True)
        self.assertLess(text.index("Warning"), text.index("Info"))
        self.assertIn("0 errors, 1 warnings, 2 info", text)

    def test_fix_repairs_the_lists_and_a_second_run_does_nothing(self):
        first = self.board(cards=["t123", "t123", "tgon"], folders=["t123", "t456"])
        last = self.board("z", cards=[], folders=["t789"])
        text = self.run_doctor("--fix", text=True)
        self.assertIn(f"Fixed {first / 'board.toml'}: Repaired the card lists.", text)
        result = self.run_doctor("--fix")
        self.assertEqual(result["fixed"], [])
        self.assertFalse([f for f in result["findings"] if f["code"].startswith("card_")])
        self.assertEqual(self.lists(first), [["t123", "t456"], []])
        self.assertEqual(self.lists(last), [["t789"], []])

    def test_fix_skips_an_unreadable_board(self):
        self.board(cards=[], folders=["t123"])
        path = self.board("z", cards=[], folders=["t456"])
        (path / "board.toml").write_text("format = 9\n")
        result = self.run_doctor("--fix", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(self.todos / "boards" / "inbox" / "board.toml")])
        self.assertEqual((path / "board.toml").read_text(), "format = 9\n")

    def test_fix_keeps_comments_and_unknown_keys(self):
        path = self.board(cards=[], folders=["t123"], top='# My board\nowner = "nate"')
        self.run_doctor("--fix")
        text = (path / "board.toml").read_text()
        self.assertTrue(text.startswith('format = 1\n# My board\nowner = "nate"\n'), text)
        self.assertEqual(self.lists(path), [["t123"], []])

    def test_fix_lock_conflict_exits_two(self):
        path = self.board(cards=[], folders=["t123"])
        before = (path / "board.toml").read_bytes()
        with (self.todos / ".herdboard.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = Store.__init__

            def fast(store, config):
                original(store, config, lock_timeout=0)

            with patch.object(Store, "__init__", fast):
                result = self.run_doctor("--fix", expect=2)
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual((path / "board.toml").read_bytes(), before)

    def test_invalid_preset_reports_its_key_path(self):
        with self.config.open("a") as stream:
            stream.write('[factory.presets.cheap]\nbuilder = { agent = "unknown" }\n')
        found = self.findings("config_invalid", expect=1)
        self.assertEqual(len(found), 1)
        self.assertIn("factory.presets.cheap.builder.agent", found[0]["message"])
        self.assertIn(str(self.config), found[0]["message"])

    def test_corrupt_promotions_reported_with_invalid_config(self):
        path = self.state_file("org/user/promotions.json", [])
        self.config.write_text("[")
        result = self.run_doctor(expect=1)
        self.assertEqual(result["summary"]["error"], 2)
        self.assertIn(str(path), [f["path"] for f in result["findings"]])

    def test_invalid_json_and_encoding_in_state(self):
        for name in ("starts.json", "checkouts.json", "org/user/promotions.json"):
            path = self.state_file(name, {})
            path.write_bytes(b"\xff")
        self.assertEqual(len(self.findings("state_invalid", expect=1)), 3)

    def test_fix_reads_the_board_again_under_the_lock(self):
        path = self.board(cards=[], folders=["t123"])
        original = Store._lock

        @contextlib.contextmanager
        def add_before_lock(store):
            (path / "cards" / "t456").mkdir()
            (path / "cards" / "t456" / "card.md").write_text('+++\ntitle = "New"\n+++\n')
            with original(store):
                yield

        with patch.object(Store, "_lock", add_before_lock):
            self.run_doctor("--fix")
        self.assertEqual(self.lists(path), [["t123", "t456"], []])

    def test_fix_repairs_a_board_with_invalid_links(self):
        path = self.board(cards=[], folders=["t123"], top='linear-projects = "wrong"')
        result = self.run_doctor("--fix", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(path / "board.toml")])
        self.assertIn('linear-projects = "wrong"', (path / "board.toml").read_text())

    def test_herdr_path_fallback(self):
        os.environ.pop("HERDR_BIN_PATH")
        with patch("herdboard.doctor.shutil.which", return_value="/example/herdr") as which:
            self.assertEqual(self.findings("herdr_missing"), [])
        self.assertEqual(which.call_args_list[0].args, ("herdr",))

    def test_valid_linear_links_make_only_read_queries(self):
        self.enable_linear()
        self.board(top=self.links("p-plat", [("l-api", "repo:api")]))
        with self.server() as fake:
            result = self.run_doctor()
            self.assertEqual([r["operationName"] for r in fake.requests], ["Viewer", "Project", "LabelsById"])
        self.assertFalse(any(f["code"].startswith("linear_") for f in result["findings"]))
        self.assertFalse(self.state.exists())

    def test_unexpected_board_check_failure_keeps_other_board(self):
        bad = self.board()
        good = self.board("z", cards=[], folders=["t456"])
        original = doctor.check_board

        def check(store, board):
            if board.path == bad:
                raise ValueError("unexpected board failure")
            return original(store, board)

        with patch.object(doctor, "check_board", side_effect=check):
            result = self.run_doctor(expect=1)
        self.assertIn(str(bad / "board.toml"), [f["path"] for f in result["findings"] if f["code"] == "board_invalid"])
        self.assertIn(
            str(good / "cards" / "t456"), [f["path"] for f in result["findings"] if f["code"] == "card_orphan"]
        )

    def test_unknown_user_in_config(self):
        self.config.write_text('todos_dir = "~herdboard-no-such-user-91479/x"')
        self.assertEqual(len(self.findings("config_invalid", expect=1)), 1)

    def test_invalid_config_checks_default_boards(self):
        self.board(cards=[], folders=["t123"])
        self.config.write_text("[")
        result = self.run_doctor(expect=1)
        self.assertTrue(any(f["code"] == "card_orphan" for f in result["findings"]))
        self.assertIn("default", next(f["message"] for f in result["findings"] if f["code"] == "config_invalid"))

    def test_tolerated_start_records_are_warnings(self):
        self.state_file(
            "starts.json", {"bad": 42, "steps": {"steps_done": 42}, "good": {"worktree_path": str(self.root / "gone")}}
        )
        found = self.findings("state_invalid")
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f["level"] == "warning" for f in found))
        self.assertEqual(len(self.findings("worktree_missing")), 1)

    def test_linear_service_error_stops_checks(self):
        from unittest.mock import Mock

        self.enable_linear()
        self.board(top=self.links(labels=[("l-api", "API")]))
        self.board("z", cards=[], top=self.links())
        for method in ("project", "labels"):
            with self.subTest(method=method):
                client = Mock()
                client.viewer.return_value = {"id": "viewer"}
                client.project.return_value = {"id": "p-plat"}
                client.labels.return_value = [{"id": "l-api"}]
                getattr(client, method).side_effect = LinearError("rate limited")
                with patch.object(linear_source, "build_client", return_value=client):
                    found = self.findings("linear_check_failed")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["level"], "warning")
                self.assertEqual(client.project.call_count, 1)
                self.assertEqual(client.labels.call_count, 0 if method == "project" else 1)

    def test_git_missing_keeps_checkout_path_checks(self):
        self.state_file(
            "checkouts.json",
            {
                "repo": [
                    {"path": str(self.root), "last_seen": 1},
                    {"path": str(self.root / "gone"), "last_seen": 1},
                    {"path": str(self.todos), "last_seen": 1},
                ]
            },
        )
        self.todos.mkdir()
        with patch.object(doctor, "repo_identity", side_effect=IdentityError("git is not installed")) as identity:
            result = self.run_doctor()
        found = [f for f in result["findings"] if f["code"] == "git_missing"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["level"], "info")
        self.assertEqual(sum(f["code"] == "checkout_stale" for f in result["findings"]), 1)
        identity.assert_called_once()

    def test_fix_keeps_report_after_write_failure(self):
        first = self.board("a", cards=[], folders=["t123"])
        failed = self.board("b", cards=[], folders=["t456"])
        before = (failed / "board.toml").read_bytes()
        original = Store._write_file

        def write(store, path, data):
            if path == failed / "board.toml":
                raise IoError("write failed")
            return original(store, path, data)

        with patch.object(Store, "_write_file", write):
            result = self.run_doctor("--fix", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(first / "board.toml")])
        self.assertEqual((failed / "board.toml").read_bytes(), before)
        self.assertTrue(
            any(f["code"] == "board_fix_failed" and "write failed" in f["message"] for f in result["findings"])
        )

    def test_doctor_does_not_register_checkout_or_write_state(self):
        with patch.object(cli, "register_checkout") as register:
            self.run_doctor()
        register.assert_not_called()
        self.assertFalse(self.state.exists())
