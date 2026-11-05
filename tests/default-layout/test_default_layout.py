import contextlib
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from support.paths import ROOT
from support.scripts import load_script

EXAMPLE = ROOT / "plugins/default-layout/examples/layout.toml"
default_layout = load_script("default-layout", "default_layout")


def wrapped(program):
    return ["sh", "-c", program + "; exec ${SHELL:-/bin/sh}"]


class FakeHerdr:
    """A Herdr server in memory: CLI commands and socket requests both land here."""

    def __init__(self):
        self.workspaces = []
        self.tabs = []
        self.panes = []
        self.busy = set()
        self.commands = []
        self.requests = []
        self.failing_workspace_labels = set()
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
        self.tabs.append({"tab_id": tab_id, "workspace_id": workspace_id, "label": label})
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
            name = "nvim" if pane_id in self.busy else "fish"
            return self.ok(
                {
                    "type": "pane_process_info",
                    "process_info": {
                        "shell_pid": 10,
                        "foreground_process_group_id": 11 if pane_id in self.busy else 10,
                        "foreground_processes": [{"name": name, "pid": 10}],
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
        node = default_layout.pane_node({"working_dir": "docs"}, "/p")
        self.assertEqual(node["cwd"], "/p/docs")
        node = default_layout.pane_node({"working_dir": "/elsewhere"}, "/p")
        self.assertEqual(node["cwd"], "/elsewhere")


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
        self.state_dir.mkdir()
        (self.state_dir / "building-alpha").touch()
        self.run_main("default_layout.py", "startup")
        self.assertEqual([argv[6] for argv in self.created()], ["beta"])


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

    def test_shell_only_tab_is_skipped(self):
        self.restored()
        self.run_main("default_layout.py", "startup")
        self.assertNotIn("shell", [p["tab_label"] for p in self.applies()])

    def test_closed_project_stays_closed(self):
        self.restored()
        self.run_main("default_layout.py", "startup")
        self.assertEqual(self.created(), [])
        self.assertNotIn("beta", self.stdout.getvalue())

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


if __name__ == "__main__":
    unittest.main()
