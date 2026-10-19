"""Doctor checks isolated stores, state files and the quiet Linear fixture server."""
import contextlib
import fcntl
import io
import json
import os
import shutil
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from herdboard import cli, doctor, linear_source
from herdboard.linear import LinearClient, LinearError
from herdboard.model import IoError
from herdboard.identity import IdentityError
from herdboard.store import Store
from herdboard.config import load_config
from linear_fake import FakeLinear
from linear_data import fixtures


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.todos = self.root / "todos"
        self.config = self.root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n[linear]\nenabled = false\n')
        self.state = self.root / "state" / "herdboard"
        env = patch.dict(os.environ, {"HOME": str(self.root), "HERDBOARD_CONFIG": str(self.config),
                                     "XDG_STATE_HOME": str(self.root / "state"),
                                     "LINEAR_API_KEY": "", "HERDR_BIN_PATH": str(self.root / "herdr")})
        env.start()
        self.addCleanup(env.stop)

    def board(self, name="inbox", body="- [ ] Task ^t123\n", front="kanban-plugin: board\n", done=True):
        path = self.todos / ("inbox.md" if name == "inbox" else f"projects/{name}.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{front}---\n## Todo\n{body}" + ("## Done\n" if done else ""))
        return path

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

    def links(self, project="p-plat", labels=""):
        return f"kanban-plugin: board\nlinear-projects:\n  - id: {project}\n    name: Platform\n{labels}"

    @contextlib.contextmanager
    def server(self):
        with FakeLinear(fixtures()) as fake:
            with patch.object(linear_source, "build_client", side_effect=lambda key: LinearClient(key, fake.endpoint, timeout=2, max_retries=0)):
                yield fake

    def test_invalid_boards_reported_together(self):
        paths = [self.board("a", front="other: value\n"), self.board("b")]
        paths[1].write_text("---\nkanban-plugin: board\n## Todo\n- [ ] Open ^t222\n")
        bad = self.board("c")
        bad.write_bytes(b"\xff")
        found = self.findings("board_invalid", expect=1)
        self.assertEqual({f["path"] for f in found}, {str(p) for p in [*paths, bad]})

    def test_duplicate_ids_in_one_board_reports_every_duplicate(self):
        self.board(body="- [ ] One ^t123\n- [ ] Two ^t123\n- [ ] Three ^t123\n- [ ] Four ^t456\n- [ ] Five ^t456\n")
        found = self.findings("duplicate_id", expect=1)
        self.assertEqual(len(found), 3)
        self.assertEqual([f["line"] for f in found], [6, 7, 9])
        self.assertTrue(all(f["fix"] == "herdboard doctor --fix-ids" for f in found))

    def test_duplicate_ids_across_boards_and_archive(self):
        self.board(body="- [ ] One ^t123\n***\n## Archive\n- [x] Old ^t123\n")
        self.board("other")
        self.assertEqual(len(self.findings("duplicate_id", expect=1)), 2)

    def test_duplicate_repository_identity(self):
        self.board("a", front="kanban-plugin: board\nrepo: github.com/Acme/API\n")
        self.board("b", front="kanban-plugin: board\nrepo: github.com/acme/api\n")
        self.assertEqual(len(self.findings("duplicate_repo", expect=1)), 1)

    def test_invalid_linear_projects(self):
        path = self.board(front="kanban-plugin: board\nlinear-projects: wrong\n")
        found = self.findings("linear_links_invalid", expect=1)
        self.assertEqual(found[0]["path"], str(path))
        self.assertEqual(found[0]["line"], 3)

    def test_invalid_linear_pins(self):
        path = self.board(front="kanban-plugin: board\nrepo: github.com/acme/api\nlinear-issues:\n  - id: i1\n")
        found = self.findings("linear_pins_invalid", expect=1)
        self.assertEqual(found[0]["path"], str(path))
        self.assertEqual(found[0]["line"], 4)
        self.assertIn("identifier", found[0]["message"])

    def test_missing_pinned_issue(self):
        self.enable_linear()
        self.board(front="kanban-plugin: board\nlinear-issues:\n  - {id: i1, identifier: ENG-1}\n"
                         "  - {id: gone, identifier: ENG-404}\n  - {id: i6, identifier: ENG-6}\n")
        with self.server() as fake:
            found = self.findings("linear_pin_missing")
            self.assertEqual([f["level"] for f in found], ["warning"])
            self.assertIn("ENG-404", found[0]["message"])
            self.assertEqual(found[0]["line"], 3)
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

    def test_complete_marker_counts_as_done(self):
        self.board(body="**Complete**\n- [x] Task ^t123\n", done=False)
        self.assertEqual(self.findings("done_column_missing"), [])

    def test_card_missing_id(self):
        self.board(body="- [ ] Task\n***\n## Archive\n- [x] Old\n")
        found = self.findings("card_id_missing")
        self.assertEqual([f["line"] for f in found], [5, 8])
        self.assertTrue(all(f["level"] == "info" for f in found))

    def test_linear_missing_key(self):
        self.enable_linear("")
        with self.server() as fake:
            self.assertEqual(self.findings("linear_auth", expect=1)[0]["level"], "error")
            self.assertEqual(fake.requests, [])

    def test_linear_refused_key(self):
        self.enable_linear("refused")
        with self.server() as fake:
            self.assertEqual(len(self.findings("linear_auth", expect=1)), 1)
            self.assertEqual(len(fake.requests), 1)

    def test_missing_project(self):
        self.enable_linear()
        self.board(front=self.links("missing"))
        with self.server() as fake:
            self.assertEqual(self.findings("linear_project_missing")[0]["level"], "warning")
            self.assertFalse(any("mutation" in r["query"] for r in fake.requests))

    def test_missing_labels_checks_even_missing_project(self):
        self.enable_linear()
        self.board(front=self.links("missing", "    labels:\n      - id: gone\n        name: Gone\n"))
        with self.server():
            self.assertEqual(len(self.findings("linear_label_missing")), 1)

    def test_linear_unreachable_is_one_warning(self):
        self.enable_linear()
        self.board(front=self.links())
        self.board("b", body="", front=self.links())
        with FakeLinear(fixtures()) as fake:
            endpoint = fake.endpoint
        with patch.object(linear_source, "build_client", side_effect=lambda key: LinearClient(key, endpoint, timeout=0.1, max_retries=0)):
            found = self.findings("linear_unreachable")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["level"], "warning")

    def test_network_failure_after_viewer_stops_link_checks(self):
        self.enable_linear()
        self.board(front=self.links())
        self.board("b", body="", front=self.links())
        with self.server() as fake:
            client = LinearClient("test-key", fake.endpoint, timeout=2, max_retries=0)
            viewer = client.viewer
            def viewer_then_timeout():
                result = viewer()
                fake.delay = 0.2
                client.timeout = 0.05
                return result
            with patch.object(linear_source, "build_client", return_value=client), patch.object(client, "viewer", viewer_then_timeout):
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
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                            f"https://github.com/acme/{origin}.git"], check=True, capture_output=True)
        self.state_file("checkouts.json", {"github.com/acme/api": [
            {"path": str(self.root / name), "last_seen": 1} for name in ("first", "second")]})
        found = self.findings("checkout_stale")
        self.assertEqual(len(found), 1)
        self.assertIn(str(self.root / "second"), found[0]["message"])

    def test_corrupt_state_files_reported_together(self):
        paths = [self.state_file(name, []) for name in ("starts.json", "checkouts.json", "org/user/promotions.json")]
        found = self.findings("state_invalid", expect=1)
        self.assertEqual({f["path"] for f in found}, {str(p) for p in paths})

    def test_malformed_state_records(self):
        self.state_file("starts.json", {"t123": {"worktree_path": 42},
                                      "good": {"worktree_path": str(self.root / "gone")}})
        self.state_file("checkouts.json", {"repo": [{}, {"path": str(self.root / "gone"), "last_seen": 1}],
                                         "good": [{"path": str(self.root / "also-gone"), "last_seen": 1}]})
        store = str(self.todos.resolve())
        self.state_file("org/user/promotions.json", {"bad": 42,
                        f"{store}::good": {"store": store, "issues": []}})
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
        self.assertEqual([(f["level"], f["message"]) for f in found],
                         [("info", "No GitHub CLI `gh` found. Only merge --pr, finish of a pull request and pull request reviews need it.")])

    def test_missing_hunk_is_a_warning(self):
        real = shutil.which
        with patch("herdboard.doctor.shutil.which", side_effect=lambda name: None if name == "hunk" else real(name)):
            found = self.findings("hunk_missing")
        self.assertEqual([f["level"] for f in found], ["warning"])
        self.assertIn("pull request reviews", found[0]["message"])

    def test_hunk_without_the_review_extension_is_a_warning(self):
        listing = subprocess.CompletedProcess([], 0, "other-ext  v1.0.0  https://example.com\n", "")
        with patch("herdboard.doctor.shutil.which", return_value="/usr/bin/hunk"), \
                patch("herdboard.doctor.subprocess.run", return_value=listing) as run:
            found = self.findings("hunk_gh_review_missing")
        self.assertEqual([f["level"] for f in found], ["warning"])
        self.assertIn("hunk extension install phl28/hunk-gh-review", found[0]["fix"])
        self.assertEqual(run.call_args.args[0], ["hunk", "extension", "list"])

    def test_hunk_with_the_review_extension_is_fine(self):
        listing = subprocess.CompletedProcess([], 0, "hunk-gh-review  v0.1.0  https://github.com/phl28/hunk-gh-review\n", "")
        with patch("herdboard.doctor.shutil.which", return_value="/usr/bin/hunk"), \
                patch("herdboard.doctor.subprocess.run", return_value=listing):
            self.assertEqual(self.findings("hunk_gh_review_missing"), [])
            self.assertEqual(self.findings("hunk_missing"), [])

    def test_herdr_environment_path_takes_precedence(self):
        executable = self.root / "herdr"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        self.assertEqual(self.findings("herdr_missing"), [])

    def test_json_shape_and_error_exit(self):
        self.board(body="- [ ] One ^t123\n- [ ] Two ^t123\n")
        result = self.run_doctor(expect=1)
        self.assertEqual(set(result), {"findings", "fixed", "summary"})
        self.assertEqual(set(result["summary"]), {"error", "warning", "info"})
        for finding in result["findings"]:
            self.assertEqual(set(finding), {"level", "code", "path", "line", "message", "fix"})
        self.assertEqual(result["summary"]["error"], 1)

    def test_human_output_grouped_with_summary(self):
        self.board(body="- [ ] One\n", done=False)
        with patch("herdboard.doctor.check_hunk", return_value=[]):
            text = self.run_doctor(text=True)
        self.assertLess(text.index("Warning"), text.index("Info"))
        self.assertIn("0 errors, 1 warnings, 2 info", text)

    def test_fix_ids_order_and_second_run(self):
        first = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n***\n## Archive\n- [x] Third ^t123\n")
        last = self.board("z", body="- [ ] Fourth ^t123\n")
        result = self.run_doctor("--fix-ids")
        self.assertEqual([(f["path"], f["line"], f["old_id"]) for f in result["fixed"]], [(str(first), 6, "t123"), (str(first), 9, "t123"), (str(last), 5, "t123")])
        self.assertEqual(len({f["new_id"] for f in result["fixed"]}), 3)
        self.assertIn("First ^t123", first.read_text())
        self.assertEqual(self.findings("duplicate_id"), [])
        self.assertEqual(self.run_doctor("--fix-ids")["fixed"], [])

    def test_fix_ids_skips_read_only(self):
        self.board()
        path = self.board("z", front="not-kanban: board\n")
        before = path.read_bytes()
        result = self.run_doctor("--fix-ids", expect=1)
        self.assertEqual(result["fixed"], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len([f for f in result["findings"] if f["code"] == "board_invalid"]), 1)

    def test_fix_preserves_crlf_unknown_lines_and_assigns_missing_ids(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n\tBody\nunknown\n- [ ] Missing\n")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.run_doctor("--fix-ids")
        content = path.read_bytes()
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        self.assertIn(b"\tBody\r\nunknown\r\n", content)
        self.assertEqual(self.findings("card_id_missing"), [])

    def test_fix_lock_conflict_exits_two(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n")
        before = path.read_bytes()
        with (self.todos / ".herdboard.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = Store.__init__
            def fast(store, config):
                original(store, config, lock_timeout=0)
            with patch.object(Store, "__init__", fast):
                result = self.run_doctor("--fix-ids", expect=2)
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(path.read_bytes(), before)

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

    def test_fix_rechecks_target_under_lock(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n")
        original = Store._lock
        @contextlib.contextmanager
        def edit_before_lock(store):
            path.write_text(path.read_text().replace("Second", "Changed"))
            with original(store):
                yield
        with patch.object(Store, "_lock", edit_before_lock):
            result = self.run_doctor("--fix-ids", expect=2)
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertIn("Changed ^t123", path.read_text())

    def test_fix_keeps_edits_to_other_cards(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n- [ ] Other ^t456\n")
        original = Store._lock
        @contextlib.contextmanager
        def edit_before_lock(store):
            path.write_text(path.read_text().replace("Other", "Changed"))
            with original(store):
                yield
        with patch.object(Store, "_lock", edit_before_lock):
            self.run_doctor("--fix-ids")
        self.assertIn("Changed ^t456", path.read_text())

    def test_fix_skips_invalid_links_but_repairs_other_boards(self):
        self.board()
        bad = self.board("a", front="kanban-plugin: board\nlinear-projects: wrong\n")
        good = self.board("b")
        before = bad.read_bytes()
        result = self.run_doctor("--fix-ids", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(good)])
        self.assertEqual(bad.read_bytes(), before)

    def test_herdr_path_fallback(self):
        os.environ.pop("HERDR_BIN_PATH")
        with patch("herdboard.doctor.shutil.which", return_value="/example/herdr") as which:
            self.assertEqual(self.findings("herdr_missing"), [])
        self.assertEqual(which.call_args_list[0].args, ("herdr",))

    def test_valid_linear_links_make_only_read_queries(self):
        self.enable_linear()
        self.board(front=self.links("p-plat", "    labels:\n      - id: l-api\n        name: repo:api\n"))
        with self.server() as fake:
            result = self.run_doctor()
            self.assertEqual([r["operationName"] for r in fake.requests], ["Viewer", "Project", "LabelsById"])
        self.assertFalse(any(f["code"].startswith("linear_") for f in result["findings"]))
        self.assertFalse(self.state.exists())

    def test_bad_yaml_scalar_keeps_other_board_findings(self):
        self.enable_linear()
        good = self.board("z", body="- [ ] No ID\n")
        for scalar in ("2001-13-45", "[unclosed"):
            with self.subTest(scalar=scalar):
                bad = self.board(front=f"kanban-plugin: board\nfoo: {scalar}\n")
                with self.server():
                    result = self.run_doctor(expect=1)
                self.assertIn(str(bad), [f["path"] for f in result["findings"] if f["code"] == "linear_links_invalid"])
                self.assertIn(str(good), [f["path"] for f in result["findings"] if f["code"] == "card_id_missing"])

    def test_unexpected_board_check_failure_keeps_other_board(self):
        bad = self.board()
        good = self.board("z", body="- [ ] No ID\n")
        original = doctor.check_board
        def check(store, path, board):
            if path == bad:
                raise ValueError("unexpected board failure")
            return original(store, path, board)
        with patch.object(doctor, "check_board", side_effect=check):
            result = self.run_doctor(expect=1)
        self.assertIn(str(bad), [f["path"] for f in result["findings"] if f["code"] == "board_invalid"])
        self.assertIn(str(good), [f["path"] for f in result["findings"] if f["code"] == "card_id_missing"])

    def test_unknown_user_in_config(self):
        self.config.write_text('todos_dir = "~herdboard-no-such-user-91479/x"')
        self.assertEqual(len(self.findings("config_invalid", expect=1)), 1)

    def test_invalid_config_checks_default_boards(self):
        self.board(body="- [ ] No ID\n")
        self.config.write_text("[")
        result = self.run_doctor(expect=1)
        self.assertTrue(any(f["code"] == "card_id_missing" for f in result["findings"]))
        self.assertIn("default", next(f["message"] for f in result["findings"] if f["code"] == "config_invalid"))

    def test_tolerated_start_records_are_warnings(self):
        self.state_file("starts.json", {"bad": 42, "steps": {"steps_done": 42},
                                      "good": {"worktree_path": str(self.root / "gone")}})
        found = self.findings("state_invalid")
        self.assertEqual(len(found), 2)
        self.assertTrue(all(f["level"] == "warning" for f in found))
        self.assertEqual(len(self.findings("worktree_missing")), 1)

    def test_linear_service_error_stops_checks(self):
        from unittest.mock import Mock
        self.enable_linear()
        self.board(front=self.links(labels="    labels:\n      - id: l-api\n        name: API\n"))
        self.board("z", front=self.links(), body="")
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
        self.state_file("checkouts.json", {"repo": [
            {"path": str(self.root), "last_seen": 1},
            {"path": str(self.root / "gone"), "last_seen": 1},
            {"path": str(self.todos), "last_seen": 1}]})
        self.todos.mkdir()
        with patch.object(doctor, "repo_identity", side_effect=IdentityError("git is not installed")) as identity:
            result = self.run_doctor()
        found = [f for f in result["findings"] if f["code"] == "git_missing"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["level"], "info")
        self.assertEqual(sum(f["code"] == "checkout_stale" for f in result["findings"]), 1)
        identity.assert_called_once()

    def test_missing_yaml_is_warning(self):
        self.board(front=self.links())
        with patch("herdboard.linear_links._yaml", return_value=None):
            found = self.findings("pyyaml_missing")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["level"], "warning")
        self.assertIn("cannot", found[0]["message"])

    def test_fix_rejects_new_duplicate_under_lock(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n- [ ] Other ^t456\n")
        original = Store._lock
        expected = path.read_bytes().replace(b"Other ^t456", b"Other ^t123")
        @contextlib.contextmanager
        def edit_before_lock(store):
            path.write_bytes(expected)
            with original(store):
                yield
        with patch.object(Store, "_lock", edit_before_lock):
            result = self.run_doctor("--fix-ids", expect=2)
        self.assertEqual(result["error"]["code"], "conflict")
        self.assertEqual(path.read_bytes(), expected)

    def test_fix_ignores_changed_read_only_duplicate(self):
        self.board()
        writable = self.board("a")
        path = self.board("z", front="not-kanban: board\n")
        original = Store._lock
        expected = path.read_bytes().replace(b"Task", b"Changed")
        @contextlib.contextmanager
        def edit_before_lock(store):
            path.write_bytes(expected)
            with original(store):
                yield
        with patch.object(Store, "_lock", edit_before_lock):
            result = self.run_doctor("--fix-ids", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(writable)])
        self.assertEqual(path.read_bytes(), expected)

    def test_fix_keeps_report_after_write_failure(self):
        self.board()
        first = self.board("a")
        failed = self.board("b")
        before = failed.read_bytes()
        original = Store._write
        def write(store, path, board):
            if path == failed:
                raise IoError("write failed")
            return original(store, path, board)
        with patch.object(Store, "_write", write):
            result = self.run_doctor("--fix-ids", expect=1)
        self.assertEqual([f["path"] for f in result["fixed"]], [str(first)])
        self.assertEqual(failed.read_bytes(), before)
        self.assertTrue(any(f["path"] == str(failed) and "write failed" in f["message"] for f in result["findings"]))

    def test_fix_preserves_all_other_bytes(self):
        path = self.board(body="- [ ] First ^t123\n- [ ] Second ^t123\n  details\n- [ ] Other ^t456\n",
                          front="kanban-plugin: board\ncustom: value\n")
        path.write_text(path.read_text() + "**Complete**\n- [x] Finished ^t789\n\n%% kanban:settings\n```\n{\"custom\": true}\n```\n%%\n")
        before = path.read_bytes()
        result = self.run_doctor("--fix-ids")
        self.assertEqual(len(result["fixed"]), 1)
        replacement = result["fixed"][0]["new_id"].encode()
        self.assertEqual(path.read_bytes(), before.replace(b"Second ^t123", b"Second ^" + replacement))

    def test_doctor_does_not_register_checkout_or_write_state(self):
        with patch.object(cli, "register_checkout") as register:
            self.run_doctor()
        register.assert_not_called()
        self.assertFalse(self.state.exists())
