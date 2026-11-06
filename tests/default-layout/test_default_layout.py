import contextlib
import fcntl
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support.paths import ROOT
from support.scripts import load_script

EXAMPLE = ROOT / "plugins/default-layout/examples/layout.toml"
default_layout = load_script("default-layout", "default_layout")
# The real socket transport, which the base test case replaces with a fake.
REAL_CALL = default_layout.call


def wrapped(program):
    return ["sh", "-c", program + "; exec ${SHELL:-/bin/sh}"]


class FakeHerdr:
    """A Herdr server in memory: CLI commands and socket requests both land here."""

    def __init__(self):
        self.workspaces = []
        self.tabs = []
        self.panes = []
        self.busy = set()
        self.no_job_control = set()
        self.no_shell_pid = set()
        self.shell_name = "fish"
        self.commands = []
        self.requests = []
        self.own_panes = set()
        self.failing_workspace_labels = set()
        self.failing_process_info = False
        self.failing_session_list = False
        self.session_name = "default"
        self.session_dir = None
        self.counter = 0

    def next_id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def add_workspace(self, label, tabs, worktree=None):
        """Add a workspace; `tabs` maps tab label to a list of pane dicts."""
        workspace_id = self.next_id("w")
        workspace = {"workspace_id": workspace_id, "label": label}
        if worktree:
            workspace["worktree"] = {"checkout_path": worktree}
        self.workspaces.append(workspace)
        for tab_label, panes in tabs.items():
            tab_id = self.add_tab(workspace_id, tab_label)
            for pane in panes:
                self.panes.append(
                    {
                        "pane_id": self.next_id(f"{workspace_id}:p"),
                        "tab_id": tab_id,
                        "workspace_id": workspace_id,
                        **pane,
                    }
                )
        return workspace_id

    def add_tab(self, workspace_id, label):
        tab_id = self.next_id(f"{workspace_id}:t")
        # Herdr numbers the tabs of a workspace from 1, in tab order.
        number = 1 + len([tab for tab in self.tabs if tab["workspace_id"] == workspace_id])
        self.tabs.append({"tab_id": tab_id, "workspace_id": workspace_id, "label": label, "number": number})
        return tab_id

    def tab_ids(self, workspace_id):
        return [tab["tab_id"] for tab in self.tabs if tab["workspace_id"] == workspace_id]

    def tab_labels(self, workspace_id):
        return [tab["label"] for tab in self.tabs if tab["workspace_id"] == workspace_id]

    def snapshot(self):
        return {
            "workspaces": [dict(w) for w in self.workspaces],
            "tabs": [dict(t) for t in self.tabs],
            "panes": [dict(p) for p in self.panes],
        }

    def command(self, argv):
        argv = list(argv)
        self.commands.append(argv)
        args = argv[1:]
        if args == ["api", "snapshot"]:
            return self.ok({"type": "session_snapshot", "snapshot": self.snapshot()})
        if args == ["workspace", "list"]:
            return self.ok({"type": "workspace_list", "workspaces": [dict(w) for w in self.workspaces]})
        if args == ["session", "list", "--json"]:
            if self.failing_session_list:
                return subprocess.CompletedProcess(argv, 1, "", "session list failed")
            sessions = {
                "sessions": [
                    {
                        "name": self.session_name,
                        "session_dir": str(self.session_dir),
                        "socket_path": "/nowhere/herdr.sock",
                    }
                ]
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(sessions), "")
        if args[:2] == ["workspace", "create"]:
            label = args[args.index("--label") + 1]
            if label in self.failing_workspace_labels:
                return subprocess.CompletedProcess(
                    argv, 1, json.dumps({"error": {"code": "invalid_cwd", "message": f"cannot create {label}"}}), ""
                )
            cwd = args[args.index("--cwd") + 1]
            workspace_id = self.add_workspace(label, {"shell": [{"cwd": cwd}]})
            tab_id = self.tab_ids(workspace_id)[0]
            return self.ok(
                {
                    "type": "workspace_created",
                    "workspace": {"workspace_id": workspace_id, "label": label},
                    "tab": {"tab_id": tab_id, "workspace_id": workspace_id},
                    "root_pane": {"pane_id": f"{workspace_id}:p0"},
                }
            )
        if args[:3] == ["pane", "process-info", "--pane"]:
            pane_id = args[3]
            if pane_id in self.own_panes:
                # This pane runs the plugin process itself.
                return self.ok(
                    {
                        "type": "pane_process_info",
                        "process_info": {
                            "shell_pid": 10,
                            "foreground_process_group_id": 11,
                            "foreground_processes": [{"name": "python3", "pid": os.getpid()}],
                        },
                    }
                )
            if self.failing_process_info:
                error = {"error": {"code": "pane_not_found", "message": f"no pane {pane_id}"}}
                return subprocess.CompletedProcess(argv, 1, json.dumps(error), "")
            if pane_id in self.busy:
                # A program in the foreground keeps the shell's process group
                # when the shell has no job control, as `sh -c` has none.
                pgid = 10 if pane_id in self.no_job_control else 11
                foreground = [{"name": "nvim", "pid": 11}]
            else:
                pgid = 10
                foreground = [{"name": self.shell_name, "pid": 10}]
            shell_pid = None if pane_id in self.no_shell_pid else 10
            if shell_pid is None:
                pgid = None
            return self.ok(
                {
                    "type": "pane_process_info",
                    "process_info": {
                        "shell_pid": shell_pid,
                        "foreground_process_group_id": pgid,
                        "foreground_processes": foreground,
                    },
                }
            )
        raise AssertionError(f"unexpected command {argv}")

    def ok(self, result):
        return subprocess.CompletedProcess([], 0, json.dumps({"id": "cli", "result": result}), "")

    def call(self, method, params):
        self.requests.append((method, params))
        if method == "layout.apply":
            if params.get("tab_id"):
                old = next(t for t in self.tabs if t["tab_id"] == params["tab_id"])
                self.tabs.remove(old)
                self.panes = [p for p in self.panes if p["tab_id"] != old["tab_id"]]
                workspace_id = old["workspace_id"]
            else:
                workspace_id = params["workspace_id"]
            # Herdr appends the built tab to the end of the tab row.
            tab_id = self.add_tab(workspace_id, params.get("tab_label"))
            return {"type": "layout_applied", "layout": {"tab_id": tab_id}}
        if method == "tab.move":
            tab = next(t for t in self.tabs if t["tab_id"] == params["tab_id"])
            self.tabs.remove(tab)
            others = [t for t in self.tabs if t["workspace_id"] == tab["workspace_id"]]
            index = params["insert_index"]
            if index < len(others):
                self.tabs.insert(self.tabs.index(others[index]), tab)
            else:
                self.tabs.append(tab)
            return {"type": "ok"}
        raise AssertionError(f"unexpected request {method}")


class DefaultLayoutTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.config_dir = self.dir / "config"
        self.config_dir.mkdir()
        self.state_dir = self.dir / "state"
        self.session_dir = self.dir / "session"
        self.session_dir.mkdir()
        self.session_file = self.session_dir / "session.json"
        self.project_a = self.dir / "alpha"
        self.project_a.mkdir()
        self.project_b = self.dir / "beta"
        self.project_b.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
                "HERDR_SOCKET_PATH": str(self.session_dir / "herdr.sock"),
                "HERDR_BIN_PATH": "herdr",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in (
            "HERDR_WORKSPACE_ID",
            "HERDR_TAB_ID",
            "HERDR_PANE_ID",
            "HERDR_PLUGIN_CONTEXT_JSON",
            "HERDR_SESSION",
            "XDG_CONFIG_HOME",
        ):
            os.environ.pop(name, None)
        self.herdr = FakeHerdr()
        self.herdr.session_dir = self.session_dir
        for target, fake in (
            ("command", self.herdr.command),
            ("call", self.herdr.call),
            ("SETTLE_SECONDS", 0),
            ("SETTLE_POLL_SECONDS", 0),
        ):
            replacement = patch.object(default_layout, target, fake)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def write_layout(self, text):
        (self.config_dir / "layout.toml").write_text(textwrap.dedent(text))

    def two_projects(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "agents"
            [[projects.tabs.panes]]
            agent = "pi"
            [[projects.tabs.panes]]
            split = "right"

            [[projects.tabs]]
            name = "editor"
            command = "nvim"

            [[projects.tabs]]
            name = "shell"

            [[projects]]
            label = "beta"
            path = "{self.project_b}"

            [[projects.tabs]]
            name = "editor"
            command = "nvim"
            """)

    def run_main(self, *argv):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return default_layout.main(list(argv))

    def created(self):
        return [argv for argv in self.herdr.commands if argv[1:3] == ["workspace", "create"]]

    def applies(self):
        return [params for method, params in self.herdr.requests if method == "layout.apply"]

    def fresh_home(self, cwd=None):
        return self.herdr.add_workspace("home", {"shell": [{"cwd": str(cwd or self.dir)}]})

    def saved_session(self):
        """Write the session file that Herdr restores a session from."""
        self.session_file.write_text("{}")

    def project_key(self, number, path):
        """The claim key of the project at `number` in the layout file."""
        return default_layout.claim_key(number, {"path": str(path)})

    def aged_marker(self, key):
        """A marker left behind by a run that holds no lock on it, such as an older plugin."""
        marker = default_layout.claim_path(key)
        marker.touch()
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(marker, (old, old))
        return marker

    def other_run_holds(self, key):
        """A claim that another live run holds: a marker locked by its own open file."""
        marker = default_layout.claim_path(key)
        fd = os.open(marker, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, b"0" * 32)
        return marker

    def a_live_run_holds(self, key):
        """Start a second process that holds the claim, and wait until it has it.

        A lock in this process proves exclusion, but only a second process shows
        that the kernel drops the lock when its owner dies.
        """
        marker = default_layout.claim_path(key)
        script = textwrap.dedent(f"""
            import fcntl, os, sys
            fd = os.open({str(marker)!r}, os.O_CREAT | os.O_RDWR, 0o600)
            os.write(fd, b"0" * 32)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("held", flush=True)
            sys.stdin.read()  # Hold the lock until the test ends.
            """)
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.stdin.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def race_once(self, method, marker, error):
        """Make the first call of `method` on `marker` fail, as a racing release would."""
        real = getattr(Path, method)
        raced = []

        def fail_once(path, *args, **kwargs):
            if Path(path) == marker and not raced:
                raced.append(path)
                raise error
            return real(path, *args, **kwargs)

        return patch.object(Path, method, fail_once)


class SpecTests(DefaultLayoutTestCase):
    def problems(self, text):
        import tomllib

        return default_layout.validate(tomllib.loads(textwrap.dedent(text)))

    def test_example_file_loads_and_is_valid(self):
        import tomllib

        spec = tomllib.loads(EXAMPLE.read_text())
        self.assertEqual(default_layout.validate(spec), [])
        self.assertEqual([p["label"] for p in spec["projects"]], ["herdr-extensions"])
        self.assertEqual([t["name"] for t in spec["projects"][0]["tabs"]], ["agents", "editor", "git", "shell"])

    def test_good_file_loads(self):
        self.two_projects()
        spec = default_layout.load_layout()
        self.assertEqual([p["label"] for p in spec["projects"]], ["alpha", "beta"])

    def test_a_duplicate_label_is_an_error(self):
        problems = self.problems("""
            [[projects]]
            label = "work"
            path = "/tmp/dup/a"
            [[projects.tabs]]
            name = "shell"

            [[projects]]
            label = "work"
            path = "/tmp/dup/b"
            [[projects.tabs]]
            name = "shell"
            """)
        self.assertEqual(problems, ["work: label is already used by project 1"])

    def test_two_tabs_may_share_a_name(self):
        problems = self.problems("""
            [[projects]]
            label = "work"
            path = "/tmp/dup/a"
            [[projects.tabs]]
            name = "shell"
            command = "htop"
            [[projects.tabs]]
            name = "shell"
            command = "nvim"
            """)
        self.assertEqual(problems, [])

    def test_a_tab_named_with_a_bare_number_is_an_error(self):
        problems = self.problems("""
            [[projects]]
            label = "work"
            path = "/tmp/dup/a"
            [[projects.tabs]]
            name = "1"
            command = "nvim"
            [[projects.tabs]]
            name = "logs"
            """)
        self.assertEqual(problems, ["work/1: name must not be a bare number, which Herdr gives to an unnamed tab"])

    def test_a_label_with_surrounding_space_is_an_error(self):
        problems = self.problems("""
            [[projects]]
            label = " work "
            path = "/tmp/one"
            [[projects.tabs]]
            name = "shell"
            """)
        self.assertEqual(problems, ["work: label must not start or end with a space"])

    def test_labels_that_differ_only_by_a_space_are_a_duplicate(self):
        problems = self.problems("""
            [[projects]]
            label = "work"
            path = "/tmp/one"
            [[projects.tabs]]
            name = "shell"

            [[projects]]
            label = "work "
            path = "/tmp/two"
            [[projects.tabs]]
            name = "shell"
            """)
        self.assertEqual(
            problems,
            [
                "work: label must not start or end with a space",
                "work: label is already used by project 1",
            ],
        )

    def test_command_with_panes_is_an_error(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            command = "nvim"
            [[projects.tabs.panes]]
            command = "ls"
            """)
        self.assertEqual(problems, ["a/t: set either command or panes, not both"])

    def test_a_label_on_a_tab_with_panes_is_an_error(self):
        # `label` names a pane, and a tab with panes is named by `name`.
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            label = "shared label"
            [[projects.tabs.panes]]
            command = "ls"
            """)
        self.assertEqual(problems, ["a/t: label is a pane key; name the tab with name"])

    def test_a_label_on_a_tab_without_panes_is_accepted(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "editor"
            label = "solo"
            command = "nvim"
            """)
        self.assertEqual(problems, [])

    def test_a_split_or_ratio_on_a_tab_with_panes_is_an_error(self):
        # Both keys describe the pane that splits, never the tab.
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "dev"
            split = "right"
            ratio = 0.3
            [[projects.tabs.panes]]
            agent = "pi"
            [[projects.tabs.panes]]
            """)
        self.assertEqual(
            problems,
            [
                "a/dev: split belongs to a pane; set it on the pane that splits",
                "a/dev: ratio belongs to a pane; set it on the pane that splits",
            ],
        )

    def test_a_tab_working_dir_is_the_default_for_its_panes(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "dev"
            working_dir = "backend"
            [[projects.tabs.panes]]
            agent = "pi"
            [[projects.tabs.panes]]
            split = "right"
            """)
        self.assertEqual(problems, [])

    def test_a_tab_working_dir_must_be_a_string(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "dev"
            working_dir = " "
            [[projects.tabs.panes]]
            agent = "pi"
            """)
        self.assertEqual(problems, ["a/dev: working_dir must be a non-empty string"])

    def test_an_empty_pane_list_is_a_one_pane_tab(self):
        # `tab_tree` builds one pane from the tab's own keys, so validate must
        # read the tab the same way.
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "t"
            command = "nvim"
            working_dir = "docs"
            panes = []
            """)
        self.assertEqual(problems, [])

    def test_five_panes_is_an_error(self):
        panes = "[[projects.tabs.panes]]\n" * 5
        problems = self.problems('[[projects]]\nlabel = "a"\npath = "/tmp"\n[[projects.tabs]]\nname = "t"\n' + panes)
        self.assertEqual(problems, ["a/t: at most 4 panes, found 5"])

    def test_bad_split_and_bad_ratio_are_errors(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            [[projects.tabs.panes]]
            [[projects.tabs.panes]]
            split = "left"
            [[projects.tabs.panes]]
            ratio = 1.5
            """)
        self.assertEqual(
            problems,
            [
                'a/t: pane 2: split must be "down" or "right"',
                "a/t: pane 3: ratio must be between 0 and 1",
            ],
        )

    def test_a_split_or_ratio_on_the_first_pane_is_an_error(self):
        # Only the panes that split after the first one use these two keys.
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            [[projects.tabs.panes]]
            split = "right"
            ratio = 0.3
            [[projects.tabs.panes]]
            command = "ls"
            """)
        self.assertEqual(
            problems,
            [
                "a/t: pane 1: split has no effect on the first pane",
                "a/t: pane 1: ratio has no effect on the first pane",
            ],
        )

    def test_a_split_or_ratio_on_a_one_pane_tab_is_an_error(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            split = "right"
            ratio = 0.3
            """)
        self.assertEqual(
            problems,
            [
                "a/t: split has no effect on the first pane",
                "a/t: ratio has no effect on the first pane",
            ],
        )

    def test_bad_agent_forms_are_errors(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp"
            [[projects.tabs]]
            name = "t"
            [[projects.tabs.panes]]
            agent = "claude --model opus"
            [[projects.tabs.panes]]
            agent = "pi"
            command = "pi"
            """)
        self.assertEqual(
            problems,
            [
                "a/t: pane 1: agent must be a plain name such as claude or pi",
                "a/t: pane 2: set either agent or command, not both",
            ],
        )

    def test_a_working_dir_that_leaves_the_project_is_not_a_problem(self):
        # A typo that climbs out of the project falls back at build time, so it
        # must not stop the whole file from building.
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "t"
            working_dir = "../etc"
            [[projects.tabs]]
            name = "panes"
            [[projects.tabs.panes]]
            [[projects.tabs.panes]]
            working_dir = "../../../etc"
            [[projects.tabs.panes]]
            working_dir = ".."
            """)
        self.assertEqual(problems, [])

    def test_a_working_dir_inside_the_project_is_accepted(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "one"
            working_dir = "docs"
            [[projects.tabs]]
            name = "two"
            [[projects.tabs.panes]]
            [[projects.tabs.panes]]
            working_dir = "docs/../plugins"
            """)
        self.assertEqual(problems, [])

    def test_an_absolute_working_dir_may_point_outside_the_project(self):
        problems = self.problems("""
            [[projects]]
            label = "a"
            path = "/tmp/proj"
            [[projects.tabs]]
            name = "t"
            working_dir = "/elsewhere/plugins"
            """)
        self.assertEqual(problems, [])

    def test_every_problem_is_reported_in_one_run(self):
        self.write_layout("""
            [[projects]]
            label = ""
            [[projects.tabs]]
            command = "nvim"
            """)
        self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        lines = self.stderr.getvalue().splitlines()
        self.assertIn("default-layout: invalid layout file", lines[0])
        self.assertEqual(
            lines[1:],
            [
                "  project 1: label is required",
                "  project 1: path is required",
                "  project 1/tab 1: name is required",
            ],
        )
        self.assertEqual(self.herdr.commands, [])

    def test_missing_file_apply_exits_zero_with_a_message(self):
        os.environ["HERDR_WORKSPACE_ID"] = "w1"
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertIn("no layout file at", self.stdout.getvalue())
        self.assertEqual(self.herdr.requests, [])

    def test_missing_file_startup_does_nothing(self):
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.herdr.requests, [])

    def test_a_layout_path_that_is_a_directory_is_reported(self):
        (self.config_dir / "layout.toml").mkdir()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        lines = self.stderr.getvalue().splitlines()
        self.assertIn("invalid layout file", lines[0])
        self.assertIn("Is a directory", lines[1])
        self.assertEqual(self.herdr.commands, [])

    def test_an_unreadable_layout_file_is_reported(self):
        self.two_projects()
        denied = PermissionError(13, "Permission denied", str(self.config_dir / "layout.toml"))
        with self.race_once("read_text", self.config_dir / "layout.toml", denied):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        lines = self.stderr.getvalue().splitlines()
        self.assertIn("invalid layout file", lines[0])
        self.assertIn("Permission denied", lines[1])
        self.assertEqual(self.herdr.commands, [])

    def test_a_layout_file_that_is_not_utf8_is_reported(self):
        (self.config_dir / "layout.toml").write_bytes(b"\xff\xfe[[projects]]\n")
        self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        lines = self.stderr.getvalue().splitlines()
        self.assertIn("invalid layout file", lines[0])
        self.assertIn("utf-8", lines[1])
        self.assertEqual(self.herdr.commands, [])

    def test_layout_path_falls_back_to_the_home_config_dir(self):
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR")
        self.assertEqual(
            default_layout.layout_path(),
            Path.home() / ".config/herdr/plugins/config/nulluminati.default-layout/layout.toml",
        )


class TreeTests(unittest.TestCase):
    def test_single_pane_tab_is_a_bare_pane_node(self):
        tree = default_layout.tab_tree({"name": "editor", "command": "nvim"}, "/p")
        self.assertEqual(tree, {"type": "pane", "cwd": "/p", "command": wrapped("nvim")})

    def test_two_panes_make_one_split_with_default_ratio(self):
        tree = default_layout.tab_tree({"name": "t", "panes": [{}, {"split": "right"}]}, "/p")
        self.assertEqual(
            tree,
            {
                "type": "split",
                "direction": "right",
                "ratio": 0.5,
                "first": {"type": "pane", "cwd": "/p"},
                "second": {"type": "pane", "cwd": "/p"},
            },
        )

    def test_three_panes_nest_left_to_right(self):
        tree = default_layout.tab_tree(
            {
                "name": "t",
                "panes": [
                    {"label": "one"},
                    {"label": "two", "ratio": 0.3},
                    {"label": "three", "split": "right"},
                ],
            },
            "/p",
        )
        self.assertEqual(
            tree,
            {
                "type": "split",
                "direction": "right",
                "ratio": 0.5,
                "first": {
                    "type": "split",
                    "direction": "down",
                    "ratio": 0.3,
                    "first": {"type": "pane", "label": "one", "cwd": "/p"},
                    "second": {"type": "pane", "label": "two", "cwd": "/p"},
                },
                "second": {"type": "pane", "label": "three", "cwd": "/p"},
            },
        )

    def test_command_and_agent_are_wrapped_in_a_shell_fallback(self):
        self.assertEqual(default_layout.pane_command({"command": "nvim"}), wrapped("nvim"))
        self.assertEqual(default_layout.pane_command({"agent": "claude"}), wrapped("claude"))
        self.assertIsNone(default_layout.pane_command({}))
        self.assertNotIn("command", default_layout.pane_node({}, "/p"))

    def test_relative_working_dir_resolves_against_the_project(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "docs").mkdir()
            node = default_layout.pane_node({"working_dir": "docs"}, root)
            self.assertEqual(node["cwd"], str(Path(root) / "docs"))

    def test_an_absolute_working_dir_replaces_the_project(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            node = default_layout.pane_node({"working_dir": elsewhere}, root)
            self.assertEqual(node["cwd"], elsewhere)

    def test_a_missing_working_dir_falls_back_to_the_project(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                node = default_layout.pane_node({"working_dir": "docs"}, root, "alpha/git")
            self.assertEqual(node["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: alpha/git: no such directory {Path(root) / 'docs'}; using {root}"],
            )

    def test_a_working_dir_that_is_a_file_falls_back_to_the_project(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "docs").write_text("")
            with contextlib.redirect_stdout(io.StringIO()):
                node = default_layout.pane_node({"working_dir": "docs"}, root)
            self.assertEqual(node["cwd"], root)

    def test_a_missing_working_dir_moves_only_its_own_pane(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "plugins").mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                tree = default_layout.tab_tree(
                    {"name": "t", "panes": [{"working_dir": "plugins"}, {"working_dir": "docs"}]}, root
                )
            self.assertEqual(tree["first"]["cwd"], str(Path(root) / "plugins"))
            self.assertEqual(tree["second"]["cwd"], root)

    def test_a_tab_working_dir_is_the_default_for_its_panes(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "backend").mkdir()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tree = default_layout.tab_tree(
                    {"name": "dev", "working_dir": "backend", "panes": [{}, {"split": "right"}]}, root
                )
            self.assertEqual(tree["first"]["cwd"], str(Path(root) / "backend"))
            self.assertEqual(tree["second"]["cwd"], str(Path(root) / "backend"))
            self.assertEqual(output.getvalue(), "")

    def test_a_plain_tab_label_reaches_its_pane(self):
        tree = default_layout.tab_tree({"name": "editor", "label": "solo"}, "/p")
        self.assertEqual(tree, {"type": "pane", "label": "solo", "cwd": "/p"})

    def test_a_tab_working_dir_that_no_pane_uses_is_not_reported(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "plugins").mkdir()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tree = default_layout.tab_tree(
                    {"name": "dev", "working_dir": "docs", "panes": [{"working_dir": "plugins"}]}, root
                )
            self.assertEqual(tree["cwd"], str(Path(root) / "plugins"))
            self.assertEqual(output.getvalue(), "")

    def test_a_pane_working_dir_wins_over_the_tab_one(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "backend").mkdir()
            (Path(root) / "plugins").mkdir()
            tree = default_layout.tab_tree(
                {"name": "dev", "working_dir": "backend", "panes": [{"working_dir": "plugins"}, {}]}, root
            )
            self.assertEqual(tree["first"]["cwd"], str(Path(root) / "plugins"))
            self.assertEqual(tree["second"]["cwd"], str(Path(root) / "backend"))

    def test_a_pane_working_dir_starts_at_the_project_not_the_tab(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "backend" / "api").mkdir(parents=True)
            tree = default_layout.tab_tree(
                {"name": "dev", "working_dir": "backend", "panes": [{"working_dir": "backend/api"}, {}]}, root
            )
            self.assertEqual(tree["first"]["cwd"], str(Path(root) / "backend" / "api"))

    def test_a_missing_tab_working_dir_falls_back_once_for_its_panes(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tree = default_layout.tab_tree(
                    {"name": "dev", "working_dir": "docs", "panes": [{}, {"split": "right"}]}, root, "alpha/dev"
                )
            self.assertEqual(tree["first"]["cwd"], root)
            self.assertEqual(tree["second"]["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: alpha/dev: no such directory {Path(root) / 'docs'}; using {root}"],
            )

    def test_a_tab_working_dir_that_leaves_the_project_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tree = default_layout.tab_tree(
                    {"name": "dev", "working_dir": "../etc", "panes": [{}, {"split": "right"}]}, root, "alpha/dev"
                )
            self.assertEqual(tree["first"]["cwd"], root)
            self.assertEqual(tree["second"]["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: alpha/dev: working_dir ../etc is outside the project; using {root}"],
            )

    def test_a_relative_working_dir_that_leaves_the_project_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                node = default_layout.pane_node({"working_dir": "../../../etc"}, root, "alpha/git")
            self.assertEqual(node["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: alpha/git: working_dir ../../../etc is outside the project; using {root}"],
            )

    def test_a_relative_working_dir_that_leaves_a_one_pane_tab_falls_back(self):
        # A tab without [[projects.tabs.panes]] tables is its own pane.
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                tree = default_layout.tab_tree({"name": "t", "working_dir": "../etc"}, root, "alpha/t")
            self.assertEqual(tree["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: alpha/t: working_dir ../etc is outside the project; using {root}"],
            )

    def test_a_symlink_that_leads_out_of_the_project_falls_back(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            (Path(root) / "link").symlink_to(elsewhere)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                node = default_layout.pane_node({"working_dir": "link"}, root)
            self.assertEqual(node["cwd"], root)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"default-layout: working_dir link is outside the project; using {root}"],
            )

    def test_the_project_root_is_a_working_dir(self):
        with tempfile.TemporaryDirectory() as root:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                node = default_layout.pane_node({"working_dir": "."}, root)
            self.assertEqual(node["cwd"], root)
            self.assertEqual(output.getvalue(), "")


class MatchTests(DefaultLayoutTestCase):
    def project(self, label, path):
        return {"label": label, "path": str(path)}

    def test_label_match_wins(self):
        by_dir = self.herdr.add_workspace("other", {"t": [{"cwd": str(self.project_a)}]})
        by_label = self.herdr.add_workspace("alpha", {"t": [{"cwd": "/nowhere"}]})
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], by_label)
        self.assertNotEqual(by_dir, by_label)

    def test_directory_match_without_label(self):
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": "/nowhere"}, {"cwd": str(self.project_a)}]})
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], workspace_id)

    def test_worktree_checkout_path_matches(self):
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": "/nowhere"}]}, worktree=str(self.project_a))
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], workspace_id)

    def test_tilde_and_symlink_still_match(self):
        link = self.dir / "link"
        link.symlink_to(self.project_a)
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": str(link)}]})
        with patch.dict(os.environ, {"HOME": str(self.dir)}):
            found = default_layout.match_project(
                self.project("alpha", "~/alpha"), self.herdr.workspaces, self.herdr.snapshot()
            )
        self.assertEqual(found["workspace_id"], workspace_id)

    def test_no_match_returns_none(self):
        self.herdr.add_workspace("x", {"t": [{"cwd": "/nowhere"}]})
        self.assertIsNone(
            default_layout.match_project(
                self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
            )
        )

    def test_pane_in_a_subdirectory_matches(self):
        sub = self.project_a / "pkg" / "src"
        sub.mkdir(parents=True)
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": str(sub)}]})
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], workspace_id)

    def test_exact_match_wins_over_a_subdirectory_match(self):
        sub = self.project_a / "pkg"
        sub.mkdir()
        self.herdr.add_workspace("sub", {"t": [{"cwd": str(sub)}]})
        root = self.herdr.add_workspace("root", {"t": [{"cwd": str(self.project_a)}]})
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], root)

    def test_the_workspace_nearest_the_root_wins_when_no_pane_sits_at_it(self):
        shallow = self.herdr.add_workspace("shallow", {"t": [{"cwd": str(self.project_a / "docs")}]})
        self.herdr.add_workspace("deep", {"t": [{"cwd": str(self.project_a / "pkg" / "src")}]})
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], shallow)

    def test_a_sibling_with_the_same_name_prefix_does_not_match(self):
        sibling = self.dir / "alpha-notes"
        sibling.mkdir()
        self.herdr.add_workspace("x", {"t": [{"cwd": str(sibling)}]})
        self.assertIsNone(
            default_layout.match_project(
                self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
            )
        )

    def test_worktree_checkout_path_beats_a_pane_below_the_root(self):
        self.herdr.add_workspace("sub", {"t": [{"cwd": str(self.project_a / "pkg")}]})
        worktree = self.herdr.add_workspace("x", {"t": [{"cwd": "/nowhere"}]}, worktree=str(self.project_a))
        found = default_layout.match_project(
            self.project("alpha", self.project_a), self.herdr.workspaces, self.herdr.snapshot()
        )
        self.assertEqual(found["workspace_id"], worktree)

    def test_project_for_workspace_picks_the_nearest_project(self):
        nested = self.project_a / "pkg"
        nested.mkdir()
        projects = [self.project("alpha", self.project_a), self.project("pkg", nested)]
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": str(nested / "src")}]})
        workspace = next(w for w in self.herdr.workspaces if w["workspace_id"] == workspace_id)
        found = default_layout.project_for_workspace(projects, workspace, self.herdr.snapshot())
        self.assertEqual(found["label"], "pkg")

    def test_project_for_workspace_still_prefers_the_label(self):
        projects = [self.project("alpha", self.project_a)]
        workspace_id = self.herdr.add_workspace("alpha", {"t": [{"cwd": "/nowhere"}]})
        workspace = next(w for w in self.herdr.workspaces if w["workspace_id"] == workspace_id)
        found = default_layout.project_for_workspace(projects, workspace, self.herdr.snapshot())
        self.assertEqual(found["label"], "alpha")


class SessionDirTests(DefaultLayoutTestCase):
    def test_the_session_directory_comes_from_herdr(self):
        self.herdr.session_name = "work"
        os.environ["HERDR_SESSION"] = "work"
        self.assertEqual(default_layout.session_file(), self.session_file)

    def test_a_named_session_falls_back_to_the_config_directory(self):
        self.herdr.failing_session_list = True
        os.environ["HERDR_SESSION"] = "work"
        os.environ["XDG_CONFIG_HOME"] = str(self.dir / "xdg")
        expected = self.dir / "xdg/herdr/sessions/work/session.json"
        self.assertEqual(default_layout.session_file(), expected)

    def test_the_default_session_falls_back_to_the_config_directory(self):
        self.herdr.failing_session_list = True
        os.environ["XDG_CONFIG_HOME"] = str(self.dir / "xdg")
        self.assertEqual(default_layout.session_file(), self.dir / "xdg/herdr/session.json")


class IdleShellTests(DefaultLayoutTestCase):
    def pane_id(self):
        self.fresh_home()
        return self.herdr.panes[0]["pane_id"]

    def test_an_idle_shell_of_any_name_is_idle(self):
        pane_id = self.pane_id()
        for name in ("tcsh", "csh", "mksh", "xonsh", "oksh", "busybox", "-mksh", "renamed"):
            self.herdr.shell_name = name
            with self.subTest(name=name):
                self.assertTrue(default_layout.is_idle_shell(pane_id))

    def test_a_foreground_program_is_not_idle(self):
        pane_id = self.pane_id()
        self.herdr.busy.add(pane_id)
        self.assertFalse(default_layout.is_idle_shell(pane_id))

    def test_a_program_in_the_shell_process_group_is_not_idle(self):
        # `sh -c '<program>; exec $SHELL'` has no job control, so the program
        # keeps the shell's process group. Only the pid tells them apart.
        pane_id = self.pane_id()
        self.herdr.busy.add(pane_id)
        self.herdr.no_job_control.add(pane_id)
        self.assertFalse(default_layout.is_idle_shell(pane_id))

    def test_a_pane_with_an_unreadable_process_info_is_not_idle(self):
        pane_id = self.pane_id()
        self.herdr.failing_process_info = True
        self.assertFalse(default_layout.is_idle_shell(pane_id))

    def test_a_pane_without_a_shell_pid_is_not_idle(self):
        # Herdr allows a null shell pid. Without it there is no evidence that
        # the pane is at a prompt, so the plugin leaves the tab alone.
        pane_id = self.pane_id()
        self.herdr.no_shell_pid.add(pane_id)
        self.assertFalse(default_layout.is_idle_shell(pane_id))


class FakeSocketServer:
    """A Unix socket server that answers every request the way `mode` says.

    "reply" writes `reply` as one newline-delimited JSON line. "stall" reads
    the request and then holds the connection without writing, as a stalled
    Herdr does. "drip" writes one byte at a time and never a newline. "close"
    reads the request and ends the connection.
    """

    drip_seconds = 0.05

    def __init__(self, path, mode="reply", reply=None):
        self.mode = mode
        self.reply = None if reply is None else json.dumps(reply).encode() + b"\n"
        self.requests = []
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        # A poll timeout is the only way to leave accept() when close() arrives.
        self.listener.settimeout(0.05)
        self.held = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.requests.append(connection.recv(65536))
            if self.mode in ("stall", "drip"):
                self.held.append(connection)
                if self.mode == "drip":
                    threading.Thread(target=self.drip, args=(connection,), daemon=True).start()
                continue
            with connection:
                if self.mode == "reply":
                    connection.sendall(self.reply)

    def drip(self, connection):
        """Send a byte now and then, so a single read can be held for ever."""
        while not self.closed.is_set():
            try:
                connection.sendall(b" ")
            except OSError:
                return
            self.closed.wait(self.drip_seconds)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=5)
        for connection in self.held:
            connection.close()
        self.listener.close()


class SocketCallTests(DefaultLayoutTestCase):
    """The real socket transport, against a server on a real Unix socket."""

    def setUp(self):
        super().setUp()
        replacement = patch.object(default_layout, "call", REAL_CALL)
        replacement.start()
        self.addCleanup(replacement.stop)

    def server(self, mode="reply", reply=None):
        """A server on the socket path that the environment points at."""
        found = FakeSocketServer(self.session_dir / "herdr.sock", mode, reply)
        self.addCleanup(found.close)
        return found

    def short_timeout(self, seconds=0.5):
        replacement = patch.object(default_layout, "REQUEST_TIMEOUT_SECONDS", seconds)
        replacement.start()
        self.addCleanup(replacement.stop)

    def run_with_watchdog(self, seconds, work):
        """Run `work` in a thread, so a lost timeout fails the test instead of hanging it.

        The repository has no pytest-timeout, so a regression in the socket limit
        would block the whole run until Continuous Integration (CI) gives up.
        """
        found = {}

        def run():
            try:
                found["value"] = work()
            except Exception as error:
                found["error"] = error

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), f"still blocked after {seconds} s")
        return found

    def test_a_reply_comes_back_as_the_result(self):
        self.server(reply={"id": "x", "result": {"layout": {"tab_id": "w1:t2"}}})
        self.assertEqual(default_layout.call("layout.apply", {"workspace_id": "w1"}), {"layout": {"tab_id": "w1:t2"}})

    def test_an_error_reply_raises_herdr_error(self):
        self.server(reply={"id": "x", "error": {"code": "no_such_tab", "message": "no tab w1:t9"}})
        with self.assertRaises(default_layout.HerdrError) as raised:
            default_layout.call("tab.move", {"tab_id": "w1:t9", "insert_index": 0})
        self.assertEqual(str(raised.exception), "tab.move: no tab w1:t9")

    def test_a_server_that_closes_without_a_reply_raises(self):
        self.server(mode="close")
        with self.assertRaises(default_layout.HerdrError) as raised:
            default_layout.call("layout.apply", {"workspace_id": "w1"})
        self.assertEqual(str(raised.exception), "layout.apply: no response from Herdr")

    def test_a_stalled_reply_gives_up_and_raises(self):
        server = self.server(mode="stall")
        self.short_timeout()
        started = time.monotonic()
        found = self.run_with_watchdog(5, lambda: REAL_CALL("layout.apply", {"workspace_id": "w1"}))
        elapsed = time.monotonic() - started
        # Only the timeout path names the limit and the stage that it gave up in.
        self.assertEqual(str(found.get("error")), "layout.apply: Herdr did not answer within 0.5 s (reply)")
        self.assertLess(elapsed, 5)
        self.assertEqual(
            [json.loads(line) for line in server.requests],
            [{"id": "default-layout:layout.apply", "method": "layout.apply", "params": {"workspace_id": "w1"}}],
        )

    def test_a_server_that_drips_gives_up_at_the_deadline(self):
        # One timeout for the whole request, not one per read: bytes that arrive
        # inside each read's limit must not hold the call past the deadline.
        self.server(mode="drip")
        self.short_timeout()
        started = time.monotonic()
        found = self.run_with_watchdog(5, lambda: REAL_CALL("layout.apply", {"workspace_id": "w1"}))
        elapsed = time.monotonic() - started
        self.assertEqual(str(found.get("error")), "layout.apply: Herdr did not answer within 0.5 s (reply)")
        self.assertLess(elapsed, 5)

    def test_a_stalled_reply_does_not_stop_the_next_project(self):
        self.server(mode="stall")
        self.two_projects()
        self.fresh_home()
        self.short_timeout()
        found = self.run_with_watchdog(10, lambda: self.run_main("default_layout.py", "startup"))
        self.assertEqual(found.get("value"), 0)
        self.assertIn("default-layout: alpha failed: layout.apply: Herdr did not answer", self.stdout.getvalue())
        self.assertIn("default-layout: beta failed: layout.apply: Herdr did not answer", self.stdout.getvalue())
        self.assertEqual(self.created()[-1][6], "beta")
        self.assertEqual(list(self.state_dir.glob("building-*")), [])


class StartupTests(DefaultLayoutTestCase):
    def test_fresh_session_creates_every_missing_project(self):
        self.two_projects()
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual(
            self.created(),
            [
                ["herdr", "workspace", "create", "--cwd", str(self.project_a), "--label", "alpha", "--no-focus"],
                ["herdr", "workspace", "create", "--cwd", str(self.project_b), "--label", "beta", "--no-focus"],
            ],
        )
        alpha, beta = self.herdr.workspaces[1:]
        self.assertEqual(
            self.herdr.requests,
            [
                (
                    "layout.apply",
                    {
                        "tab_id": f"{alpha['workspace_id']}:t5",
                        "tab_label": "agents",
                        "focus": False,
                        "root": {
                            "type": "split",
                            "direction": "right",
                            "ratio": 0.5,
                            "first": {"type": "pane", "cwd": str(self.project_a), "command": wrapped("pi")},
                            "second": {"type": "pane", "cwd": str(self.project_a)},
                        },
                    },
                ),
                ("tab.move", {"tab_id": f"{alpha['workspace_id']}:t7", "insert_index": 0}),
                (
                    "layout.apply",
                    {
                        "workspace_id": alpha["workspace_id"],
                        "tab_label": "editor",
                        "focus": False,
                        "root": {"type": "pane", "cwd": str(self.project_a), "command": wrapped("nvim")},
                    },
                ),
                (
                    "layout.apply",
                    {
                        "workspace_id": alpha["workspace_id"],
                        "tab_label": "shell",
                        "focus": False,
                        "root": {"type": "pane", "cwd": str(self.project_a)},
                    },
                ),
                (
                    "layout.apply",
                    {
                        "tab_id": f"{beta['workspace_id']}:t11",
                        "tab_label": "editor",
                        "focus": False,
                        "root": {"type": "pane", "cwd": str(self.project_b), "command": wrapped("nvim")},
                    },
                ),
                ("tab.move", {"tab_id": f"{beta['workspace_id']}:t13", "insert_index": 0}),
            ],
        )
        self.assertEqual(self.herdr.tab_labels(alpha["workspace_id"]), ["agents", "editor", "shell"])
        self.assertIn("default-layout: built alpha (3 tabs)", self.stdout.getvalue())
        self.assertIn("default-layout: built beta (1 tabs)", self.stdout.getvalue())

    def test_fresh_home_workspace_is_reused_for_its_project(self):
        self.two_projects()
        home = self.fresh_home(self.project_a)
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertEqual(self.herdr.tab_labels(home), ["agents", "editor", "shell"])

    def test_fresh_detection(self):
        self.assertTrue(default_layout.is_fresh(self.session_file))
        self.saved_session()
        self.assertFalse(default_layout.is_fresh(self.session_file))

    def test_headless_fresh_session_creates_every_missing_project(self):
        self.two_projects()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])
        self.assertIn("default-layout: built alpha (3 tabs)", self.stdout.getvalue())

    def test_fresh_start_with_a_second_workspace_still_builds(self):
        self.two_projects()
        self.herdr.add_workspace("one", {"t": [{"cwd": "/x"}]})
        self.herdr.add_workspace("two", {"t": [{"cwd": "/x"}]})
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_a_busy_shell_does_not_stop_a_fresh_start(self):
        self.two_projects()
        self.fresh_home()
        self.herdr.busy.add(self.herdr.panes[0]["pane_id"])
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_a_restore_with_no_workspace_still_builds(self):
        self.two_projects()
        self.saved_session()
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_a_socket_outside_the_session_directory_does_not_hide_the_restore(self):
        self.two_projects()
        self.saved_session()
        self.herdr.add_workspace("alpha", {"shell": [{"cwd": str(self.project_a)}]})
        os.environ["HERDR_SOCKET_PATH"] = str(self.dir / "elsewhere" / "herdr.sock")
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])
        self.assertNotIn("default-layout: cannot use the saved session", self.stdout.getvalue())

    def test_an_unreadable_session_file_is_reported_and_treated_as_a_restore(self):
        self.two_projects()
        self.session_file.write_text("{not json")
        self.herdr.add_workspace("alpha", {"shell": [{"cwd": str(self.project_a)}]})
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])
        self.assertIn("default-layout: cannot use the saved session at", self.stdout.getvalue())
        self.assertIn("not valid JSON", self.stdout.getvalue())

    def test_two_workspace_restore_creates_no_workspace(self):
        self.two_projects()
        self.saved_session()
        self.herdr.add_workspace("alpha", {"shell": [{"cwd": str(self.project_a)}]})
        self.herdr.add_workspace("other", {"t": [{"cwd": "/x"}]})
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])

    def test_open_project_on_a_fresh_pass_is_left_alone(self):
        self.two_projects()
        self.herdr.add_workspace(
            "alpha",
            {
                "agents": [{"cwd": str(self.project_a)}],
                "editor": [{"cwd": str(self.project_a)}],
                "shell": [{"cwd": str(self.project_a)}],
            },
        )
        spec = default_layout.load_layout()
        with contextlib.redirect_stdout(self.stdout):
            default_layout.build_missing(spec, self.herdr.workspaces, self.herdr.snapshot())
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertEqual([p["tab_label"] for p in self.applies()], ["editor"])
        self.assertIn("default-layout: alpha already open", self.stdout.getvalue())

    def test_open_project_with_some_tabs_gets_only_the_missing_tabs(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"mine": [{"cwd": "/x"}], "editor": [{"cwd": "/x"}]})
        spec = default_layout.load_layout()
        with contextlib.redirect_stdout(self.stdout):
            default_layout.build_missing(spec, self.herdr.workspaces, self.herdr.snapshot())
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["mine", "editor", "agents", "shell"])

    def test_a_second_tab_with_one_name_is_added(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"
            command = "htop"

            [[projects.tabs]]
            name = "shell"
            command = "nvim"
            """)
        workspace_id = self.herdr.add_workspace("alpha", {"shell": [{"cwd": "/x"}]})
        # The open shell holds work, so the empty-workspace path does not apply.
        self.herdr.busy.add(self.herdr.panes[0]["pane_id"])
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([p["root"]["command"] for p in self.applies()], [wrapped("nvim")])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["shell", "shell"])
        self.assertIn("default-layout: added 1 tab(s) to alpha", self.stdout.getvalue())

    def test_a_tab_labelled_like_its_own_number_is_unnamed(self):
        # Herdr keeps the number of a moved tab, so its label still matches it.
        self.assertTrue(default_layout.unnamed_tab({"label": "1", "number": 1}, 2))
        # A tab labelled "1" in the second place carries a name of its own.
        self.assertFalse(default_layout.unnamed_tab({"label": "1", "number": 2}, 2))

    def test_a_tab_number_counts_within_its_workspace(self):
        self.herdr.add_workspace("first", {"mine": [{"cwd": "/x"}]})
        second = self.herdr.add_workspace("alpha", {"1": [{"cwd": "/x"}], "shell": [{"cwd": "/x"}]})
        tabs = default_layout.workspace_tabs(self.herdr.snapshot(), second)
        self.assertEqual([tab["number"] for tab in tabs], [1, 2])
        self.assertEqual(default_layout.named_tab_counts(tabs), {"shell": 1})

    def test_a_missing_working_dir_falls_back_to_the_project(self):
        (self.project_a / "plugins").mkdir()
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "git"
            working_dir = "docs"

            [[projects.tabs]]
            name = "code"
            working_dir = "plugins"
            """)
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual(
            [(params["tab_label"], params["root"]["cwd"]) for params in self.applies()],
            [("git", str(self.project_a)), ("code", str(self.project_a / "plugins"))],
        )
        self.assertIn(
            f"default-layout: alpha/git: no such directory {self.project_a / 'docs'}; using {self.project_a}",
            self.stdout.getvalue(),
        )
        self.assertIn("default-layout: built alpha (2 tabs)", self.stdout.getvalue())

    def test_a_tab_working_dir_is_used_for_the_panes_it_builds(self):
        (self.project_a / "backend").mkdir()
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "dev"
            working_dir = "backend"

            [[projects.tabs.panes]]
            agent = "pi"

            [[projects.tabs.panes]]
            split = "right"
            """)
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        root = self.applies()[0]["root"]
        self.assertEqual(root["first"]["cwd"], str(self.project_a / "backend"))
        self.assertEqual(root["second"]["cwd"], str(self.project_a / "backend"))

    def test_a_missing_tab_working_dir_still_builds_in_the_project(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "dev"
            working_dir = "docs"

            [[projects.tabs.panes]]
            agent = "pi"

            [[projects.tabs.panes]]
            split = "right"
            """)
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        root = self.applies()[0]["root"]
        self.assertEqual(root["first"]["cwd"], str(self.project_a))
        self.assertEqual(root["second"]["cwd"], str(self.project_a))
        self.assertIn(
            f"default-layout: alpha/dev: no such directory {self.project_a / 'docs'}; using {self.project_a}",
            self.stdout.getvalue(),
        )
        self.assertIn("default-layout: built alpha (1 tabs)", self.stdout.getvalue())

    def test_a_home_directory_that_does_not_resolve_is_a_message(self):
        # Path.expanduser raises RuntimeError for an unknown ~user, and the
        # startup hook has no one to show a traceback to.
        self.write_layout("""
            [[projects]]
            label = "alpha"
            path = "~nosuchuser/alpha"
            [[projects.tabs]]
            name = "shell"
            """)
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        self.assertEqual(self.stderr.getvalue().splitlines(), ["default-layout: Could not determine home directory."])
        self.assertEqual(self.created(), [])

    def test_missing_directory_skips_one_project(self):
        self.two_projects()
        self.project_a.rmdir()
        self.fresh_home()
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertIn(f"default-layout: skip alpha: no such directory {self.project_a}", self.stdout.getvalue())

    def test_one_failing_project_does_not_stop_the_others(self):
        self.two_projects()
        self.fresh_home()
        self.herdr.failing_workspace_labels.add("alpha")
        self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])
        self.assertIn("default-layout: alpha failed: cannot create alpha", self.stdout.getvalue())
        self.assertIn("default-layout: built beta (1 tabs)", self.stdout.getvalue())

    def test_a_claimed_project_is_skipped(self):
        self.two_projects()
        self.fresh_home()
        self.other_run_holds(self.project_key(1, self.project_a))
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertIn("default-layout: alpha is already being built", self.stdout.getvalue())

    def test_a_claim_held_elsewhere_survives_the_skip(self):
        self.two_projects()
        self.fresh_home()
        marker = self.other_run_holds(self.project_key(1, self.project_a))
        self.run_main("default_layout.py", "startup")
        self.assertTrue(marker.exists())

    def test_a_live_claim_holds_a_project_past_the_ttl(self):
        self.two_projects()
        self.fresh_home()
        marker = self.other_run_holds(self.project_key(1, self.project_a))
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(marker, (old, old))
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertIn("default-layout: alpha is already being built", self.stdout.getvalue())

    def test_a_claim_held_by_another_process_skips_the_project(self):
        # The kernel drops the lock when the owner dies, so a separate process
        # proves the exclusion that the whole fix rests on.
        self.two_projects()
        self.fresh_home()
        self.a_live_run_holds(self.project_key(1, self.project_a))
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertIn("default-layout: alpha is already being built", self.stdout.getvalue())

    def test_a_claim_on_one_project_does_not_skip_a_lookalike_label(self):
        self.write_layout(f"""
            [[projects]]
            label = "my proj"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"

            [[projects]]
            label = "my_proj"
            path = "{self.project_b}"

            [[projects.tabs]]
            name = "shell"
            """)
        self.fresh_home()
        # Another run holds the claim on the first project.
        self.other_run_holds(self.project_key(1, self.project_a))
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["my_proj"])
        self.assertIn("default-layout: my proj is already being built", self.stdout.getvalue())

    def test_a_duplicate_label_builds_nothing(self):
        self.write_layout(f"""
            [[projects]]
            label = "work"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"

            [[projects]]
            label = "work"
            path = "{self.project_b}"

            [[projects.tabs]]
            name = "shell"
            """)
        self.fresh_home()
        self.assertEqual(self.run_main("default_layout.py", "startup"), 1)
        self.assertIn("  work: label is already used by project 1", self.stderr.getvalue())
        self.assertEqual(self.created(), [])

    def test_a_failing_claim_does_not_stop_the_others(self):
        self.two_projects()
        self.fresh_home()
        real_claim = default_layout.claim

        def failing_claim(key):
            if key == self.project_key(1, self.project_a):
                raise OSError("no state directory")
            return real_claim(key)

        with patch.object(default_layout, "claim", failing_claim):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])
        self.assertIn("default-layout: alpha failed: no state directory", self.stdout.getvalue())
        self.assertIn("default-layout: built beta (1 tabs)", self.stdout.getvalue())

    def test_a_marker_that_vanishes_during_the_stat_still_builds(self):
        self.two_projects()
        self.fresh_home()
        marker = self.aged_marker(self.project_key(1, self.project_a))
        gone = FileNotFoundError(2, "No such file or directory", str(marker))
        with self.race_once("stat", marker, gone):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_a_marker_released_under_the_lock_is_taken_again(self):
        self.two_projects()
        self.fresh_home()
        marker = self.aged_marker(self.project_key(1, self.project_a))
        real_lock = default_layout.lock_marker
        raced = []

        def release_once(fd):
            # The run holding the aged claim finishes and releases it now.
            if not raced:
                raced.append(fd)
                marker.unlink()
            return real_lock(fd)

        with patch.object(default_layout, "lock_marker", release_once):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_an_unlocked_file_system_does_not_block_the_build(self):
        self.two_projects()
        self.fresh_home()
        self.aged_marker(self.project_key(1, self.project_a))
        denied = PermissionError(13, "Permission denied")
        with patch.object(fcntl, "flock", side_effect=denied):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["alpha", "beta"])

    def test_an_unlocked_file_system_keeps_a_fresh_marker(self):
        self.two_projects()
        self.fresh_home()
        default_layout.claim_path(self.project_key(1, self.project_a)).touch()
        denied = PermissionError(13, "Permission denied")
        with patch.object(fcntl, "flock", side_effect=denied):
            self.assertEqual(self.run_main("default_layout.py", "startup"), 0)
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])


class ClaimTests(DefaultLayoutTestCase):
    def test_two_projects_with_one_label_get_their_own_claim(self):
        first = default_layout.claim_key(1, {"path": "/tmp/a"})
        second = default_layout.claim_key(2, {"path": "/tmp/b"})
        self.assertNotEqual(first, second)
        self.assertIsNotNone(default_layout.claim(first))
        self.assertIsNotNone(default_layout.claim(second))
        self.addCleanup(default_layout.release, first)
        self.addCleanup(default_layout.release, second)

    def test_a_run_with_no_state_directory_keeps_a_private_one(self):
        # The shared temporary directory lets another user plant a marker first.
        with (
            patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": ""}),
            patch.object(tempfile, "tempdir", str(self.dir / "tmp")),
        ):
            marker = default_layout.claim_path("alpha")
        self.assertEqual(marker.parent, self.dir / "tmp" / f"default-layout-{os.getuid()}")
        self.assertEqual(marker.parent.stat().st_mode & 0o777, 0o700)

    def test_the_claim_key_holds_the_project_position(self):
        first = default_layout.claim_key(1, {"path": "/tmp/one"})
        second = default_layout.claim_key(2, {"path": "/tmp/one"})
        self.assertNotEqual(first, second)

    def test_the_claim_key_is_a_plain_file_name(self):
        key = default_layout.claim_key(1, {"path": "~/my proj/api v2"})
        self.assertRegex(key, r"^[A-Za-z0-9._-]+$")
        self.assertEqual(default_layout.claim_path(key).name, f"building-{key}")

    def test_a_claim_key_outside_the_plain_characters_is_refused(self):
        for key in ("../escape", "a/b", "", "my proj"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    default_layout.claim_path(key)
                self.assertIn("claim key", str(caught.exception))

    def test_the_marker_records_the_owner_token(self):
        token = default_layout.claim("alpha")
        self.addCleanup(default_layout.release, "alpha")
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(default_layout.claim_path("alpha").read_text(), token)

    def test_a_fresh_marker_without_a_token_blocks_the_claim(self):
        self.state_dir.mkdir()
        (self.state_dir / "building-alpha").touch()
        self.assertIsNone(default_layout.claim("alpha"))

    def test_an_aged_marker_is_taken_over(self):
        self.aged_marker("alpha")
        self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")

    def test_a_claim_lasts_past_the_ttl_while_its_run_lives(self):
        self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")
        marker = default_layout.claim_path("alpha")
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(marker, (old, old))
        self.assertIsNone(default_layout.claim("alpha"), "a live run keeps its claim")

    def test_a_marker_from_a_dead_run_is_taken_over_at_once(self):
        # The owner died, so the kernel dropped its lock and the age does not matter.
        default_layout.claim_path("alpha").write_text("0" * 32)
        self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")

    def test_a_release_removes_this_run_marker(self):
        self.assertIsNotNone(default_layout.claim("alpha"))
        default_layout.release("alpha")
        self.assertFalse(default_layout.claim_path("alpha").exists())
        self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")

    def test_a_release_leaves_a_marker_that_another_run_owns(self):
        self.assertIsNotNone(default_layout.claim("alpha"))
        marker = default_layout.claim_path("alpha")
        marker.write_text("f" * 32)  # Another run has taken the claim over.
        default_layout.release("alpha")
        self.assertTrue(marker.exists(), "a release must leave another run's marker alone")

    def test_a_planted_symlink_is_not_followed(self):
        target = self.dir / "target"
        target.write_text("keep me")
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(target, (old, old))
        self.state_dir.mkdir()
        marker = self.state_dir / "building-alpha"
        marker.symlink_to(target)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")
        self.assertEqual(target.read_text(), "keep me", "a claim must not truncate the link target")
        self.assertFalse(marker.is_symlink(), "a claim must replace the planted link")
        self.assertIn("removed the stale claim marker", out.getvalue())

    def test_a_marker_that_cannot_be_opened_holds_only_until_the_ttl(self):
        # A directory at the marker path cannot be opened, and cannot be removed.
        marker = default_layout.claim_path("alpha")
        marker.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(default_layout.claim("alpha"), "a young marker holds the claim")
        self.assertIn("cannot read the claim marker", out.getvalue())
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(marker, (old, old))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNotNone(default_layout.claim("alpha"), "an old marker must not block")
        self.addCleanup(default_layout.release, "alpha")
        self.assertIn("cannot remove the claim marker", out.getvalue())

    def test_a_release_removes_a_marker_that_holds_no_token(self):
        # A full disk can make the token write fail, and the claim must still end.
        with patch.object(default_layout, "write_owner", lambda fd, token: None):
            self.assertIsNotNone(default_layout.claim("alpha"))
        marker = default_layout.claim_path("alpha")
        self.assertEqual(marker.read_text(), "")
        default_layout.release("alpha")
        self.assertFalse(marker.exists(), "a marker with no token still belongs to this run")
        self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")

    def test_a_release_leaves_a_marker_that_replaced_its_own(self):
        self.assertIsNotNone(default_layout.claim("alpha"))
        marker = default_layout.claim_path("alpha")
        # A sweep, or an older plugin, unlinks the marker. A later run makes a new
        # file at the same path, and this run's open file no longer names it.
        marker.unlink()
        marker.write_text("f" * 32)
        default_layout.release("alpha")
        self.assertTrue(marker.exists(), "a release must unlink only the file it opened")
        self.assertEqual(marker.read_text(), "f" * 32)

    def test_one_project_never_has_two_builders(self):
        # The card's proof: a run that passes the TTL keeps the project.
        first = default_layout.claim("alpha")
        self.assertIsNotNone(first)
        self.addCleanup(default_layout.release, "alpha")
        marker = default_layout.claim_path("alpha")
        old = time.time() - default_layout.CLAIM_TTL_SECONDS - 10
        os.utime(marker, (old, old))
        self.assertIsNone(default_layout.claim("alpha"), "a second run must not take the claim")
        default_layout.release("alpha")
        self.assertFalse(marker.exists(), "the owner frees the claim when its run ends")

    def test_a_marker_that_vanishes_mid_check_is_a_won_claim(self):
        marker = self.aged_marker("alpha")
        real_stat = Path.stat
        raced = []

        def stat_after_release(path, *args, **kwargs):
            # The run holding the aged claim finishes and releases it now.
            if Path(path) == marker and not raced:
                raced.append(path)
                marker.unlink()
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat_after_release):
            self.assertIsNotNone(default_layout.claim("alpha"))
        self.addCleanup(default_layout.release, "alpha")
        self.assertTrue(marker.exists(), "a won claim must leave a marker")

    def test_a_claim_that_never_gets_its_marker_is_lost(self):
        marker = self.aged_marker("alpha")
        real_stat = Path.stat
        real_open = os.open
        raced = []

        def stat_after_release(path, *args, **kwargs):
            if Path(path) == marker and not raced:
                raced.append(path)
                marker.unlink(missing_ok=True)
            return real_stat(path, *args, **kwargs)

        def open_always_taken(path, *args, **kwargs):
            if Path(path) == marker:
                raise FileExistsError(17, "File exists", str(path))
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "stat", stat_after_release), patch.object(os, "open", open_always_taken):
            self.assertIsNone(default_layout.claim("alpha"))


class RehydrateTests(DefaultLayoutTestCase):
    def restored(self):
        self.two_projects()
        self.saved_session()
        cwd = str(self.project_a)
        workspace_id = self.herdr.add_workspace(
            "alpha",
            {
                "mine": [{"cwd": cwd}],
                "agents": [{"cwd": cwd}, {"cwd": cwd}],
                "editor": [{"cwd": cwd}],
                "shell": [{"cwd": cwd}],
            },
        )
        self.herdr.add_workspace("other", {"t": [{"cwd": "/x"}]})
        return workspace_id

    def tab_panes(self, workspace_id, label):
        tab = next(t for t in self.herdr.tabs if t["workspace_id"] == workspace_id and t["label"] == label)
        return [p for p in self.herdr.panes if p["tab_id"] == tab["tab_id"]]

    def test_idle_program_tabs_are_rebuilt_and_moved_back(self):
        workspace_id = self.restored()
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents", "editor"])
        moves = [params["insert_index"] for method, params in self.herdr.requests if method == "tab.move"]
        self.assertEqual(moves, [1, 2])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["mine", "agents", "editor", "shell"])
        self.assertIn("default-layout: rehydrated alpha/agents", self.stdout.getvalue())
        self.assertIn("default-layout: rehydrated 2 tab(s) in alpha", self.stdout.getvalue())

    def test_agent_tab_is_left_alone(self):
        workspace_id = self.restored()
        self.tab_panes(workspace_id, "agents")[0]["agent_session"] = {"agent": "pi", "value": "x"}
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_label"] for p in self.applies()], ["editor"])
        self.assertIn("default-layout: alpha/agents left alone: agent in", self.stdout.getvalue())

    def test_running_program_tab_is_left_alone(self):
        workspace_id = self.restored()
        self.herdr.busy.add(self.tab_panes(workspace_id, "editor")[0]["pane_id"])
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents"])
        self.assertIn("default-layout: alpha/editor left alone: something is running in", self.stdout.getvalue())

    def test_a_tab_of_any_shell_rehydrates(self):
        self.restored()
        self.herdr.shell_name = "mksh"
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents", "editor"])
        self.assertIn("default-layout: rehydrated alpha/editor", self.stdout.getvalue())

    def test_shell_only_tab_is_skipped(self):
        self.restored()
        self.run_main("default_layout.py", "startup")
        self.assertNotIn("shell", [p["tab_label"] for p in self.applies()])

    def test_closed_project_stays_closed(self):
        self.restored()
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])
        self.assertNotIn("beta", self.stdout.getvalue())

    def test_same_named_tabs_are_rebuilt_from_their_own_entry(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"
            command = "htop"

            [[projects.tabs]]
            name = "shell"
            command = "nvim"
            """)
        self.saved_session()
        cwd = str(self.project_a)
        workspace_id = self.herdr.add_workspace("alpha", {"shell": [{"cwd": cwd}]})
        first_tab = self.herdr.tab_ids(workspace_id)[0]
        second_tab = self.herdr.add_tab(workspace_id, "shell")
        self.herdr.panes.append({"pane_id": "w1:p2", "tab_id": second_tab, "workspace_id": workspace_id, "cwd": cwd})
        self.run_main("default_layout.py", "startup")
        applies = self.applies()
        self.assertEqual([p["tab_id"] for p in applies], [first_tab, second_tab])
        self.assertEqual([p["root"]["command"] for p in applies], [wrapped("htop"), wrapped("nvim")])
        self.assertIn("default-layout: rehydrated 2 tab(s) in alpha", self.stdout.getvalue())

    def test_a_tab_of_shells_still_takes_its_file_entry(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"

            [[projects.tabs]]
            name = "shell"
            command = "nvim"
            """)
        self.saved_session()
        cwd = str(self.project_a)
        workspace_id = self.herdr.add_workspace("alpha", {"shell": [{"cwd": cwd}]})
        second_tab = self.herdr.add_tab(workspace_id, "shell")
        self.herdr.panes.append({"pane_id": "w1:p2", "tab_id": second_tab, "workspace_id": workspace_id, "cwd": cwd})
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_id"] for p in self.applies()], [second_tab])
        self.assertEqual([p["root"]["command"] for p in self.applies()], [wrapped("nvim")])

    def test_a_busy_same_named_tab_still_takes_its_file_entry(self):
        self.write_layout(f"""
            [[projects]]
            label = "alpha"
            path = "{self.project_a}"

            [[projects.tabs]]
            name = "shell"
            command = "htop"

            [[projects.tabs]]
            name = "shell"
            command = "nvim"
            """)
        self.saved_session()
        cwd = str(self.project_a)
        workspace_id = self.herdr.add_workspace("alpha", {"shell": [{"cwd": cwd}]})
        first_tab = self.herdr.tab_ids(workspace_id)[0]
        second_tab = self.herdr.add_tab(workspace_id, "shell")
        self.herdr.panes.append({"pane_id": "w1:p2", "tab_id": second_tab, "workspace_id": workspace_id, "cwd": cwd})
        first_pane = next(p for p in self.herdr.panes if p["tab_id"] == first_tab)
        first_pane["agent_session"] = {"agent": "pi", "value": "x"}
        self.run_main("default_layout.py", "startup")
        self.assertEqual([p["tab_id"] for p in self.applies()], [second_tab])
        self.assertEqual([p["root"]["command"] for p in self.applies()], [wrapped("nvim")])
        self.assertIn(
            f"default-layout: alpha/shell left alone: agent in {first_pane['pane_id']}",
            self.stdout.getvalue(),
        )

    def test_one_workspace_restore_rehydrates_and_builds_no_workspace(self):
        self.two_projects()
        self.saved_session()
        cwd = str(self.project_a)
        workspace_id = self.herdr.add_workspace(
            "alpha",
            {
                "agents": [{"cwd": cwd}, {"cwd": cwd}],
                "editor": [{"cwd": cwd}],
                "shell": [{"cwd": cwd}],
            },
        )
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents", "editor"])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["agents", "editor", "shell"])
        self.assertIn("default-layout: rehydrated alpha/editor", self.stdout.getvalue())
        self.assertNotIn("default-layout: built", self.stdout.getvalue())


class ApplyTests(DefaultLayoutTestCase):
    def focused_tab(self):
        """A workspace for project alpha with a user tab in focus, as apply sees it."""
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"first": [{"cwd": "/x"}], "my-work": [{"cwd": "/x"}]})
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = self.herdr.tab_ids(workspace_id)[1]
        pane = next(p for p in self.herdr.panes if p["tab_id"] == os.environ["HERDR_TAB_ID"])
        return workspace_id, pane

    def only_tab(self):
        """A workspace for project alpha whose one tab runs this command."""
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"my-work": [{"cwd": "/x"}]})
        pane = next(p for p in self.herdr.panes if p["workspace_id"] == workspace_id)
        self.herdr.own_panes.add(pane["pane_id"])
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = pane["tab_id"]
        os.environ["HERDR_PANE_ID"] = pane["pane_id"]
        return workspace_id, pane

    def test_apply_leaves_a_tab_that_runs_a_program_alone(self):
        workspace_id, pane = self.focused_tab()
        self.herdr.busy.add(pane["pane_id"])
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "my-work"])
        self.assertIn(
            f"default-layout: alpha/my-work left alone: something is running in {pane['pane_id']}",
            self.stdout.getvalue(),
        )
        self.assertIn("--force", self.stdout.getvalue())

    def test_apply_leaves_a_tab_that_holds_an_agent_alone(self):
        workspace_id, pane = self.focused_tab()
        pane["agent_session"] = {"agent": "pi", "value": "x"}
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "my-work"])
        self.assertIn(f"default-layout: alpha/my-work left alone: agent in {pane['pane_id']}", self.stdout.getvalue())

    def test_a_run_outside_the_pane_still_refuses_a_busy_focused_tab(self):
        workspace_id, pane = self.focused_tab()
        self.herdr.busy.add(pane["pane_id"])
        # Herdr reports the focused pane on a keybinding press, but no pane runs it.
        os.environ["HERDR_PANE_ID"] = pane["pane_id"]
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertIn("left alone: something is running in", self.stdout.getvalue())

    def test_apply_from_a_pane_rebuilds_a_tab_that_does_not_hold_it(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"mine": [{"cwd": "/x"}], "other": [{"cwd": "/x"}]})
        own_tab, other_tab = self.herdr.tab_ids(workspace_id)
        own_pane = next(p for p in self.herdr.panes if p["tab_id"] == own_tab)["pane_id"]
        self.herdr.own_panes.add(own_pane)
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = own_tab
        os.environ["HERDR_PANE_ID"] = own_pane
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual(self.applies()[0]["tab_id"], other_tab)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["mine", "agents", "editor", "shell"])
        self.assertIn("default-layout: applied alpha (3 tabs)", self.stdout.getvalue())

    def test_apply_from_the_only_tab_refuses(self):
        workspace_id, _ = self.only_tab()
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["my-work"])
        self.assertIn(
            "default-layout: alpha/my-work runs this command; run apply from a pane in another tab",
            self.stdout.getvalue(),
        )

    def test_apply_with_force_from_the_only_tab_refuses(self):
        workspace_id, _ = self.only_tab()
        self.assertEqual(self.run_main("default_layout.py", "apply", "--force"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["my-work"])
        self.assertIn("run apply from a pane in another tab", self.stdout.getvalue())

    def test_apply_with_no_tab_in_the_workspace_says_so(self):
        self.two_projects()
        os.environ["HERDR_WORKSPACE_ID"] = self.herdr.add_workspace("alpha", {})
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertIn("default-layout: no tab in this workspace", self.stdout.getvalue())
        self.assertEqual(self.herdr.requests, [])

    def test_apply_with_force_replaces_a_tab_that_runs_a_program(self):
        workspace_id, pane = self.focused_tab()
        self.herdr.busy.add(pane["pane_id"])
        self.assertEqual(self.run_main("default_layout.py", "apply", "--force"), 0)
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents", "editor", "shell"])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "agents", "editor", "shell"])
        self.assertIn("default-layout: applied alpha (3 tabs)", self.stdout.getvalue())

    def test_apply_replaces_the_focused_tab_and_appends_the_rest(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"first": [{"cwd": "/x"}], "focused": [{"cwd": "/x"}]})
        focused = self.herdr.tab_ids(workspace_id)[1]
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = focused
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        applies = self.applies()
        self.assertEqual(applies[0]["tab_id"], focused)
        self.assertEqual([p.get("workspace_id") for p in applies[1:]], [workspace_id, workspace_id])
        self.assertEqual([p["tab_label"] for p in applies], ["agents", "editor", "shell"])
        self.assertTrue(all(p["focus"] is False for p in applies))
        self.assertEqual(self.herdr.requests[1][0], "tab.move")
        self.assertEqual(self.herdr.requests[1][1]["insert_index"], 1)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "agents", "editor", "shell"])

    def test_a_second_apply_keeps_one_tab_of_each_name(self):
        workspace_id, _ = self.focused_tab()
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "agents", "editor", "shell"])
        # Herdr focuses the rebuilt tab, so the second press lands on it.
        os.environ["HERDR_TAB_ID"] = self.herdr.tab_ids(workspace_id)[1]
        self.herdr.requests.clear()
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual([p["tab_label"] for p in self.applies()], ["agents"])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["first", "agents", "editor", "shell"])

    def test_apply_replaces_the_tab_named_like_the_first_tab_and_adds_only_missing_tabs(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace(
            "alpha", {"my-work": [{"cwd": "/x"}], "agents": [{"cwd": "/x"}], "editor": [{"cwd": "/x"}]}
        )
        user_tab, agents_tab, _ = self.herdr.tab_ids(workspace_id)
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = user_tab
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        applies = self.applies()
        self.assertEqual(applies[0]["tab_id"], agents_tab)
        self.assertEqual([p["tab_label"] for p in applies], ["agents", "shell"])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["my-work", "agents", "editor", "shell"])

    def test_apply_leaves_a_busy_tab_named_like_the_first_tab_alone(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"my-work": [{"cwd": "/x"}], "agents": [{"cwd": "/x"}]})
        user_tab, agents_tab = self.herdr.tab_ids(workspace_id)
        pane = next(p for p in self.herdr.panes if p["tab_id"] == agents_tab)
        pane["agent_session"] = {"agent": "pi", "value": "x"}
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = user_tab
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["my-work", "agents"])
        self.assertIn(f"default-layout: alpha/agents left alone: agent in {pane['pane_id']}", self.stdout.getvalue())

    def test_apply_from_a_pane_in_the_tab_named_like_the_first_tab_refuses(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"agents": [{"cwd": "/x"}], "my-work": [{"cwd": "/x"}]})
        agents_tab, _ = self.herdr.tab_ids(workspace_id)
        own_pane = next(p for p in self.herdr.panes if p["tab_id"] == agents_tab)["pane_id"]
        self.herdr.own_panes.add(own_pane)
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = agents_tab
        os.environ["HERDR_PANE_ID"] = own_pane
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertEqual(self.applies(), [])
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["agents", "my-work"])
        self.assertIn(
            "default-layout: alpha/agents runs this command; run apply from a pane in another tab",
            self.stdout.getvalue(),
        )

    def test_apply_into_a_focused_layout_tab_adds_that_tab_back(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("alpha", {"editor": [{"cwd": "/x"}]})
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        os.environ["HERDR_TAB_ID"] = self.herdr.tab_ids(workspace_id)[0]
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["agents", "editor", "shell"])

    def test_apply_reads_the_workspace_from_the_context(self):
        self.two_projects()
        workspace_id = self.herdr.add_workspace("x", {"t": [{"cwd": str(self.project_b)}]})
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_id": workspace_id})
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["editor"])

    def test_apply_finds_the_project_after_a_cd_into_a_subdirectory(self):
        self.two_projects()
        sub = self.project_a / "pkg"
        sub.mkdir()
        workspace_id = self.herdr.add_workspace("scratch", {"t": [{"cwd": str(sub)}]})
        os.environ["HERDR_WORKSPACE_ID"] = workspace_id
        self.assertEqual(self.run_main("default_layout.py", "apply"), 0)
        self.assertEqual(self.herdr.tab_labels(workspace_id), ["agents", "editor", "shell"])
        self.assertIn("default-layout: applied alpha (3 tabs)", self.stdout.getvalue())

    def test_apply_without_a_matching_project_exits_1(self):
        self.two_projects()
        os.environ["HERDR_WORKSPACE_ID"] = self.herdr.add_workspace("x", {"t": [{"cwd": "/x"}]})
        self.assertEqual(self.run_main("default_layout.py", "apply"), 1)
        self.assertIn("default-layout: no project for this workspace", self.stdout.getvalue())
        self.assertEqual(self.applies(), [])

    def test_an_unknown_option_is_refused_before_any_work(self):
        self.two_projects()
        self.assertEqual(self.run_main("default_layout.py", "apply", "--froce"), 2)
        self.assertIn("default-layout: unknown option --froce for apply", self.stderr.getvalue())
        self.assertEqual(self.herdr.commands, [])
        self.assertEqual(self.herdr.requests, [])

    def test_startup_takes_no_force_option(self):
        self.two_projects()
        self.assertEqual(self.run_main("default_layout.py", "startup", "--force"), 2)
        self.assertIn("default-layout: unknown option --force for startup", self.stderr.getvalue())
        self.assertEqual(self.herdr.commands, [])


if __name__ == "__main__":
    unittest.main()
