import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/resource-usage"
SCRIPT = PLUGIN / "bin/resource_usage.py"
FIXTURES = ROOT / "tests/fixtures/resource-usage"
spec = importlib.util.spec_from_file_location("resource_usage", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load resource usage script")
resource_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resource_usage)

Process = resource_usage.Process
MIB = 1024 * 1024
GIB = 1024 * MIB


def stat_line(pid, comm, ppid, utime, stime, start, rss):
    # Fields 3 to 24 of /proc/<pid>/stat; the ones the plugin does not read are zero.
    fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)] + ["0"] * 6 + [str(start), "0", str(rss)]
    return f"{pid} ({comm}) " + " ".join(fields + ["0"] * 28) + "\n"


def proc(pid, ppid, cpu=0.0, rss=0, start="s", age=None):
    return Process(pid=pid, ppid=ppid, start=f"{start}{pid}", cpu_seconds=cpu, rss_bytes=rss, age_seconds=age)


def table(*processes):
    return {process.pid: process for process in processes}


class LinuxSamplingTests(unittest.TestCase):
    def test_parses_parent_cpu_start_and_resident_size(self):
        text = stat_line(42, "python3", 7, utime=250, stime=50, start=10000, rss=3)
        process = resource_usage.parse_linux_stat(42, text, clock_ticks=100, page_size=4096, uptime=150.0)
        self.assertEqual(process, Process(pid=42, ppid=7, start="10000", cpu_seconds=3.0, rss_bytes=3 * 4096,
                                          age_seconds=50.0))

    def test_age_is_unknown_without_uptime(self):
        text = stat_line(42, "python3", 7, utime=250, stime=50, start=9999, rss=3)
        self.assertIsNone(resource_usage.parse_linux_stat(42, text, clock_ticks=100, page_size=4096).age_seconds)

    def test_command_name_with_spaces_and_parentheses(self):
        text = stat_line(42, "my (odd) name) x", 7, utime=10, stime=0, start=5, rss=1)
        process = resource_usage.parse_linux_stat(42, text, clock_ticks=100, page_size=4096)
        self.assertEqual((process.ppid, process.start, process.cpu_seconds), (7, "5", 0.1))

    def test_collects_proc_table_and_skips_vanished_and_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, line in [(1, stat_line(1, "init", 0, 100, 0, 1, 10)), (2, stat_line(2, "sh", 1, 0, 0, 2, 1))]:
                (root / str(pid)).mkdir()
                (root / str(pid) / "stat").write_text(line)
            (root / "3").mkdir()  # exited between listing and reading: no stat file
            (root / "self").mkdir()
            (root / "4").mkdir()
            (root / "4" / "stat").write_text("")
            (root / "uptime").write_text("12.50 40.00\n")
            real_read = Path.read_text

            def read_text(path, *arguments, **keywords):
                if path.parent.name == "4":
                    raise PermissionError("denied")
                return real_read(path, *arguments, **keywords)

            with patch.object(Path, "read_text", read_text):
                processes = resource_usage.collect_linux_processes(root, clock_ticks=100, page_size=4096)
        self.assertEqual(sorted(processes), [1, 2])
        self.assertEqual(processes[1].cpu_seconds, 1.0)
        self.assertEqual(processes[2].ppid, 1)
        self.assertAlmostEqual(processes[2].age_seconds, 12.48)


class MacSamplingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        replacement = patch.object(resource_usage, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.output = (FIXTURES / "ps-macos.txt").read_text()
        self.fail_ps = False

    def fake_command(self, argv, env=None):
        self.calls.append((argv, env))
        if self.fail_ps:
            raise subprocess.CalledProcessError(1, argv, "", "ps: broken")
        return subprocess.CompletedProcess(argv, 0, self.output, "")

    def test_cumulative_time_formats(self):
        self.assertEqual(resource_usage.parse_ps_time("0:01.50"), 1.5)
        self.assertEqual(resource_usage.parse_ps_time("12:34.56"), 754.56)
        self.assertEqual(resource_usage.parse_ps_time("01:02:03"), 3723.0)
        self.assertEqual(resource_usage.parse_ps_time("1-02:03:04"), 93784.0)
        with self.assertRaises(ValueError):
            resource_usage.parse_ps_time("soon")

    def test_parses_fixture_with_start_identity_and_kib(self):
        processes = resource_usage.collect_macos_processes()
        self.assertEqual(sorted(processes), [1, 501, 502, 503])
        self.assertEqual(processes[502], Process(pid=502, ppid=501, start="Tue Oct 27 09:10:12 2026",
                                                 cpu_seconds=0.25, rss_bytes=1024 * 1024, age_seconds=59.0))
        self.assertEqual(processes[1].age_seconds, 90611.0)
        self.assertEqual(processes[503].cpu_seconds, 93784.0)

    def test_runs_ps_once_with_c_locale_and_explicit_fields(self):
        resource_usage.collect_macos_processes()
        self.assertEqual(len(self.calls), 1)
        argv, env = self.calls[0]
        self.assertEqual(argv, ["ps", "-A", "-o", "pid=,ppid=,lstart=,etime=,time=,rss="])
        self.assertEqual(env["LC_ALL"], "C")

    def test_skips_malformed_lines(self):
        self.output = "garbage line\n" + self.output + "  9  1 Tue\n"
        self.assertEqual(sorted(resource_usage.collect_macos_processes()), [1, 501, 502, 503])

    def test_failed_ps_raises(self):
        self.fail_ps = True
        with self.assertRaises(subprocess.CalledProcessError):
            resource_usage.collect_macos_processes()

    def test_platform_dispatch(self):
        with patch.object(resource_usage.sys, "platform", "darwin"):
            self.assertIn(501, resource_usage.collect_processes())
        with patch.object(resource_usage.sys, "platform", "win32"):
            with self.assertRaises(RuntimeError):
                resource_usage.collect_processes()


class UsageTests(unittest.TestCase):
    def test_all_processors_busy_is_100_percent(self):
        processes = table(proc(1, 0, cpu=18.0))
        usage = resource_usage.measure_usage({1}, processes, {(1, "s1"): 2.0}, elapsed=2.0, cpu_count=8)
        self.assertAlmostEqual(usage.cpu_percent, 100.0)

    def test_one_busy_processor_is_100_over_count(self):
        processes = table(proc(1, 0, cpu=4.0))
        usage = resource_usage.measure_usage({1}, processes, {(1, "s1"): 2.0}, elapsed=2.0, cpu_count=8)
        self.assertAlmostEqual(usage.cpu_percent, 12.5)

    def test_first_sample_is_baseline_without_cpu(self):
        usage = resource_usage.measure_usage({1}, table(proc(1, 0, cpu=4.0, rss=MIB)), None, elapsed=2.0, cpu_count=8)
        self.assertIsNone(usage.cpu_percent)
        self.assertEqual(usage.rss_bytes, MIB)

    def test_zero_elapsed_or_missing_count_has_no_cpu(self):
        processes = table(proc(1, 0, cpu=4.0))
        previous = {(1, "s1"): 2.0}
        self.assertIsNone(resource_usage.measure_usage({1}, processes, previous, elapsed=0.0, cpu_count=8).cpu_percent)
        self.assertIsNone(resource_usage.measure_usage({1}, processes, previous, elapsed=2.0, cpu_count=None).cpu_percent)

    def test_reused_pid_counts_new_process_from_zero(self):
        processes = table(proc(1, 0, cpu=0.5, start="new", age=0.8))
        usage = resource_usage.measure_usage({1}, processes, {(1, "s1"): 500.0}, elapsed=1.0, cpu_count=1)
        self.assertAlmostEqual(usage.cpu_percent, 50.0)

    def test_old_process_missing_from_last_sample_is_a_baseline(self):
        # It was unreadable in the last sample; its lifetime time is not this interval's.
        processes = table(proc(1, 0, cpu=0.5), proc(2, 0, cpu=900.0, age=3600.0), proc(3, 0, cpu=900.0))
        previous = {(1, "s1"): 0.0}
        usage = resource_usage.measure_usage({1, 2, 3}, processes, previous, elapsed=1.0, cpu_count=1)
        self.assertAlmostEqual(usage.cpu_percent, 50.0)

    def test_cpu_never_exceeds_100_percent(self):
        processes = table(proc(1, 0, cpu=3.0))
        usage = resource_usage.measure_usage({1}, processes, {(1, "s1"): 0.0}, elapsed=1.0, cpu_count=2)
        self.assertEqual(usage.cpu_percent, 100.0)

    def test_counter_reset_counts_zero(self):
        processes = table(proc(1, 0, cpu=1.0))
        usage = resource_usage.measure_usage({1}, processes, {(1, "s1"): 5.0}, elapsed=1.0, cpu_count=1)
        self.assertEqual(usage.cpu_percent, 0.0)

    def test_exited_process_in_set_is_ignored(self):
        processes = table(proc(1, 0, cpu=2.0, rss=MIB))
        usage = resource_usage.measure_usage({1, 99}, processes, {(1, "s1"): 1.0}, elapsed=1.0, cpu_count=1)
        self.assertEqual((usage.cpu_percent, usage.rss_bytes), (100.0, MIB))

    def test_memory_sums_resident_size(self):
        processes = table(proc(1, 0, rss=MIB), proc(2, 1, rss=2 * MIB))
        self.assertEqual(resource_usage.measure_usage({1, 2}, processes, None, 1.0, 1).rss_bytes, 3 * MIB)


class ProcessSetTests(unittest.TestCase):
    def setUp(self):
        self.processes = table(
            proc(1, 0), proc(10, 1), proc(11, 10), proc(12, 11), proc(20, 1), proc(21, 20), proc(30, 1),
        )

    def test_descendants_include_roots_and_all_children(self):
        self.assertEqual(resource_usage.descendants([10], self.processes), {10, 11, 12})

    def test_overlapping_roots_count_once(self):
        self.assertEqual(resource_usage.descendants([10, 11], self.processes), {10, 11, 12})

    def test_cycle_terminates(self):
        processes = table(proc(5, 6), proc(6, 5))
        self.assertEqual(resource_usage.descendants([5], processes), {5, 6})

    def test_root_missing_from_table_is_skipped(self):
        self.assertEqual(resource_usage.descendants([99], self.processes), set())

    def test_workspace_sets_join_panes_and_skip_unrelated(self):
        workspaces = [{"workspace_id": "w1"}, {"workspace_id": "w2"}, {"workspace_id": "w3"}]
        panes = [
            {"pane_id": "w1:p1", "workspace_id": "w1"},
            {"pane_id": "w1:p2", "workspace_id": "w1"},
            {"pane_id": "w2:p1", "workspace_id": "w2"},
            {"pane_id": "w3:p1", "workspace_id": "w3"},
        ]
        roots = {"w1:p1": 10, "w1:p2": 20, "w2:p1": 11, "w3:p1": 99}
        sets = resource_usage.workspace_process_sets(workspaces, panes, roots, self.processes)
        self.assertEqual(sets["w1"], {10, 11, 12, 20, 21})
        self.assertEqual(sets["w2"], {11, 12})
        self.assertIsNone(sets["w3"])


def workspace(workspace_id, repo_key=None, linked=False):
    entry = {"workspace_id": workspace_id, "label": workspace_id}
    if repo_key:
        entry["worktree"] = {"repo_key": repo_key, "is_linked_worktree": linked}
    return entry


def worktree_list(repo_key, source, entries):
    return {
        "source": {"repo_key": repo_key, "source_workspace_id": source},
        "worktrees": [{"open_workspace_id": ws, "is_linked_worktree": linked} for ws, linked in entries],
    }


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.lookups = []
        self.lists = {}

    def lookup(self, workspace_id):
        self.lookups.append(workspace_id)
        return self.lists.get(workspace_id)

    def parents(self, workspaces):
        return resource_usage.family_parents(workspaces, self.lookup)

    def test_linked_worktrees_attach_to_main(self):
        workspaces = [workspace("m", "/r/.git"), workspace("a", "/r/.git", True), workspace("b", "/r/.git", True)]
        self.assertEqual(self.parents(workspaces), {"a": "m", "b": "m"})
        self.assertEqual(self.lookups, [])

    def test_equal_labels_in_unrelated_repositories_stay_apart(self):
        workspaces = [workspace("m1", "/one/.git"), workspace("c1", "/one/.git", True),
                      workspace("m2", "/two/.git"), workspace("c2", "/two/.git", True)]
        for entry in workspaces:
            entry["label"] = "same"
        self.assertEqual(self.parents(workspaces), {"c1": "m1", "c2": "m2"})

    def test_closed_main_leaves_children_standalone(self):
        workspaces = [workspace("a", "/r/.git", True), workspace("b", "/r/.git", True)]
        self.assertEqual(self.parents(workspaces), {})

    def test_several_main_workspaces_use_source_workspace(self):
        workspaces = [workspace("m1", "/r/.git"), workspace("m2", "/r/.git"), workspace("a", "/r/.git", True)]
        self.lists["m1"] = worktree_list("/r/.git", "m2", [("m1", False), ("m2", False), ("a", True)])
        self.assertEqual(self.parents(workspaces), {"a": "m2"})
        self.assertEqual(self.lookups, ["m1"])

    def test_several_main_workspaces_without_known_source_stay_standalone(self):
        workspaces = [workspace("m1", "/r/.git"), workspace("m2", "/r/.git"), workspace("a", "/r/.git", True)]
        self.assertEqual(self.parents(workspaces), {})

    def test_missing_metadata_uses_worktree_list_once_per_repository(self):
        workspaces = [workspace("m"), workspace("a"), workspace("b", "/r/.git", True), workspace("plain")]
        self.lists["m"] = worktree_list("/r/.git", "m", [("m", False), ("a", True), ("b", True)])
        self.assertEqual(self.parents(workspaces), {"a": "m", "b": "m"})
        self.assertEqual(self.lookups, ["m", "plain"])

    def test_family_sets_are_unions_and_children_keep_their_own(self):
        sets = {"m": {1, 2}, "a": {2, 3}, "b": {4}, "x": {9}}
        families = resource_usage.family_process_sets(sets, {"a": "m", "b": "m"})
        self.assertEqual(families, {"m": {1, 2, 3, 4}})
        self.assertEqual(sets["a"], {2, 3})

    def test_family_with_unavailable_member_is_unavailable(self):
        families = resource_usage.family_process_sets({"m": {1}, "a": None}, {"a": "m"})
        self.assertEqual(families, {"m": None})


class FormatTests(unittest.TestCase):
    def test_formats_match_mockup(self):
        usage = resource_usage.Usage(cpu_percent=12.4, rss_bytes=int(1.1 * GIB))
        self.assertEqual(resource_usage.format_usage(usage), "CPU 12%  MEM 1.1 GiB")
        self.assertEqual(resource_usage.format_usage(usage, total=True), "CPU 12%  MEM 1.1 GiB  total")

    def test_memory_below_one_gib_is_whole_mib(self):
        self.assertEqual(resource_usage.format_memory(240 * MIB + 1000), "240 MiB")
        self.assertEqual(resource_usage.format_memory(GIB), "1.0 GiB")

    def test_unavailable_values(self):
        self.assertEqual(resource_usage.format_usage(None), "CPU --  MEM --")
        self.assertEqual(resource_usage.format_usage(None, total=True), "CPU --  MEM --  total")
        baseline = resource_usage.Usage(cpu_percent=None, rss_bytes=240 * MIB)
        self.assertEqual(resource_usage.format_usage(baseline), "CPU --  MEM 240 MiB")

    def test_nerd_icons_replace_labels(self):
        usage = resource_usage.Usage(cpu_percent=12.4, rss_bytes=int(1.1 * GIB))
        self.assertEqual(resource_usage.format_usage(usage, icon_style="nerd"), "\uf4bc 12%  \U000f035b 1.1 GiB")
        self.assertEqual(resource_usage.format_usage(None, total=True, icon_style="nerd"),
                         "\uf4bc --  \U000f035b --  total")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, text):
        (Path(self.directory.name) / "config.toml").write_text(text)

    def test_missing_config_is_plain(self):
        self.assertEqual(resource_usage.load_icon_style(), ("plain", None))

    def test_missing_config_dir_is_plain(self):
        os.environ.pop("HERDR_PLUGIN_CONFIG_DIR")
        self.assertEqual(resource_usage.load_icon_style(), ("plain", None))

    def test_nerd_style(self):
        self.write('icon_style = "nerd"\n')
        self.assertEqual(resource_usage.load_icon_style(), ("nerd", None))

    def test_bad_value_is_plain_with_error(self):
        self.write('icon_style = "emoji"\n')
        style, error = resource_usage.load_icon_style()
        self.assertEqual(style, "plain")
        self.assertIn("icon_style", error)

    def test_non_string_value_is_plain_with_error(self):
        self.write('icon_style = ["nerd"]\n')
        style, error = resource_usage.load_icon_style()
        self.assertEqual(style, "plain")
        self.assertIn("icon_style", error)

    def test_file_that_is_not_utf8_is_plain_with_error(self):
        (Path(self.directory.name) / "config.toml").write_bytes(b"icon_style = \"\xff\"\n")
        style, error = resource_usage.load_icon_style()
        self.assertEqual(style, "plain")
        self.assertIn("config.toml", error)

    def test_bad_toml_is_plain_with_error(self):
        self.write("icon_style = \n")
        style, error = resource_usage.load_icon_style()
        self.assertEqual(style, "plain")
        self.assertIn("config.toml", error)


class HerdrTestCase(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_BIN_PATH", None)
        self.calls = []
        self.workspaces = [workspace("w1", "/r/.git"), workspace("w2", "/r/.git", True), workspace("w3")]
        self.panes = [
            {"pane_id": "w1:p1", "workspace_id": "w1"},
            {"pane_id": "w2:p1", "workspace_id": "w2"},
            {"pane_id": "w3:p1", "workspace_id": "w3"},
        ]
        self.shells = {"w1:p1": 10, "w2:p1": 20, "w3:p1": 30}
        self.closed_panes = set()
        self.failing_panes = set()
        self.worktree_results = []
        self.plugins = [{"plugin_id": "nulluminati.resource-usage", "enabled": True}]
        self.snapshot_fails = False
        replacement = patch.object(resource_usage, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fake_command(self, argv, env=None):
        self.calls.append(argv)
        if argv[1:3] == ["api", "snapshot"]:
            if self.snapshot_fails:
                raise subprocess.CalledProcessError(1, argv, "", "server gone")
            snapshot = {"workspaces": self.workspaces, "panes": self.panes}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"snapshot": snapshot}}), "")
        if argv[1:3] == ["pane", "process-info"]:
            pane_id = argv[4]
            if pane_id in self.closed_panes:
                error = json.dumps({"error": {"code": "pane_not_found", "message": "pane not found"}})
                raise subprocess.CalledProcessError(1, argv, error, "")
            if pane_id in self.failing_panes:
                raise subprocess.TimeoutExpired(argv, 10)
            info = {"pane_id": pane_id, "shell_pid": self.shells[pane_id]}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"process_info": info}}), "")
        if argv[1:3] == ["worktree", "list"]:
            result = self.worktree_results.pop(0) if self.worktree_results else None
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")
            error = json.dumps({"error": {"code": "not_git_worktree"}})
            raise subprocess.CalledProcessError(1, argv, error, "")
        if argv[1:3] == ["workspace", "report-metadata"]:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[1:3] == ["plugin", "list"]:
            result = {"result": {"plugins": self.plugins}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
        raise AssertionError(f"unexpected command {argv}")

    def reports(self):
        return [argv for argv in self.calls if argv[1:3] == ["workspace", "report-metadata"]]


class CollectorTests(HerdrTestCase):
    def setUp(self):
        super().setUp()
        self.tables = []
        self.clock = [100.0]
        self.collector = resource_usage.Collector(
            collect=self.next_table, clock=lambda: self.clock[0], cpu_count=lambda: 2,
        )

    def next_table(self):
        result = self.tables.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def sample(self, cpu_10=0.0, cpu_20=0.0, cpu_30=0.0, shell_20_start="s"):
        return table(
            proc(1, 0), proc(10, 1, cpu=cpu_10, rss=100 * MIB), proc(20, 1, cpu=cpu_20, rss=200 * MIB, start=shell_20_start),
            proc(21, 20, rss=MIB), proc(30, 1, cpu=cpu_30, rss=GIB), proc(40, 1, cpu=999.0, rss=GIB),
        )

    def test_first_cycle_reports_memory_and_baseline(self):
        self.tables = [self.sample()]
        self.assertEqual(self.collector.cycle(), {
            "w1": "CPU --  MEM 301 MiB  total",
            "w2": "CPU --  MEM 201 MiB",
            "w3": "CPU --  MEM 1.0 GiB",
        })

    def test_second_cycle_reports_cpu_and_family_total(self):
        self.tables = [self.sample(), self.sample(cpu_10=1.0, cpu_20=2.0, cpu_30=0.5)]
        self.collector.cycle()
        self.clock[0] += 2.0
        texts = self.collector.cycle()
        self.assertEqual(texts["w1"], "CPU 75%  MEM 301 MiB  total")
        self.assertEqual(texts["w2"], "CPU 50%  MEM 201 MiB")
        self.assertEqual(texts["w3"], "CPU 12%  MEM 1.0 GiB")

    def test_icon_style_is_read_every_cycle(self):
        styles = [("plain", None), ("nerd", None)]
        self.collector.icon_style = lambda: styles.pop(0)
        self.tables = [self.sample(), self.sample()]
        self.assertEqual(self.collector.cycle()["w3"], "CPU --  MEM 1.0 GiB")
        self.clock[0] += 2.0
        self.assertEqual(self.collector.cycle()["w3"], "\uf4bc 0%  \U000f035b 1.0 GiB")

    def test_config_error_is_logged_once(self):
        self.collector.icon_style = lambda: ("plain", "config.toml: bad icon_style")
        self.tables = [self.sample(), self.sample()]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.collector.cycle()
            self.collector.cycle()
        self.assertEqual(stderr.getvalue().count("bad icon_style"), 1)

    def test_roots_are_cached_and_refreshed_when_shell_changes(self):
        self.tables = [self.sample(), self.sample(), self.sample(shell_20_start="t")]
        self.collector.cycle()
        self.collector.cycle()
        process_info = [argv for argv in self.calls if argv[1:3] == ["pane", "process-info"]]
        self.assertEqual(len(process_info), 3)
        self.collector.cycle()
        process_info = [argv for argv in self.calls if argv[1:3] == ["pane", "process-info"]]
        self.assertEqual(process_info[-1][4], "w2:p1")
        self.assertEqual(len(process_info), 4)

    def test_relationships_refresh_only_when_topology_changes(self):
        self.tables = [self.sample(), self.sample(), self.sample()]
        self.collector.cycle()
        self.clock[0] += 3600.0
        self.collector.cycle()
        lookups = [argv for argv in self.calls if argv[1:3] == ["worktree", "list"]]
        self.assertEqual(len(lookups), 1)  # w3 has no worktree metadata
        self.workspaces = self.workspaces[:2]
        self.panes = self.panes[:2]
        self.collector.cycle()
        self.assertEqual(len([argv for argv in self.calls if argv[1:3] == ["worktree", "list"]]), 1)

    def test_failed_worktree_lookup_is_retried_after_a_delay(self):
        self.workspaces[1] = workspace("w2")
        self.worktree_results = [
            subprocess.TimeoutExpired(["herdr"], 10),  # w2, first cycle
            None,  # w3: not a Git workspace
            worktree_list("/r/.git", "w1", [("w1", False), ("w2", True)]),  # w2, retry
            None,  # w3 again
        ]
        self.tables = [self.sample(), self.sample(), self.sample()]
        self.assertEqual(self.collector.cycle()["w1"], "CPU --  MEM 100 MiB")
        self.clock[0] += 2.0
        self.assertEqual(self.collector.cycle()["w1"], "CPU 0%  MEM 100 MiB")
        self.clock[0] += resource_usage.RELATIONSHIP_RETRY_SECONDS
        self.assertEqual(self.collector.cycle()["w1"], "CPU 0%  MEM 301 MiB  total")
        self.assertEqual(len([argv for argv in self.calls if argv[1:3] == ["worktree", "list"]]), 4)

    def test_pane_with_failed_lookup_makes_workspace_unavailable(self):
        self.panes.append({"pane_id": "w2:p2", "workspace_id": "w2"})
        self.failing_panes = {"w2:p2"}
        self.tables = [self.sample()]
        texts = self.collector.cycle()
        self.assertEqual(texts["w2"], "CPU --  MEM --")
        self.assertEqual(texts["w1"], "CPU --  MEM --  total")
        self.assertEqual(texts["w3"], "CPU --  MEM 1.0 GiB")

    def test_closing_pane_is_skipped(self):
        self.closed_panes = {"w3:p1"}
        self.panes.append({"pane_id": "w2:p2", "workspace_id": "w2"})
        self.closed_panes.add("w2:p2")
        self.tables = [self.sample()]
        texts = self.collector.cycle()
        self.assertEqual(texts["w3"], "CPU --  MEM --")
        self.assertEqual(texts["w2"], "CPU --  MEM 201 MiB")

    def test_failed_process_table_marks_all_unavailable_and_resets_baseline(self):
        self.tables = [self.sample(), OSError("no proc"), self.sample(cpu_10=1.0)]
        self.collector.cycle()
        self.clock[0] += 2.0
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            texts = self.collector.cycle()
        self.assertEqual(texts, {"w1": "CPU --  MEM --  total", "w2": "CPU --  MEM --", "w3": "CPU --  MEM --"})
        self.assertIn("cannot read processes: no proc", stderr.getvalue())
        self.clock[0] += 2.0
        self.assertEqual(self.collector.cycle()["w1"], "CPU --  MEM 301 MiB  total")

    def test_repeated_process_table_failure_is_logged_once(self):
        self.tables = [OSError("no proc"), OSError("no proc"), OSError("other")]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for _ in range(3):
                self.collector.cycle()
        self.assertEqual(stderr.getvalue().count("no proc"), 1)
        self.assertEqual(stderr.getvalue().count("other"), 1)

    def test_failed_snapshot_raises(self):
        self.snapshot_fails = True
        self.tables = [self.sample()]
        with self.assertRaises(subprocess.CalledProcessError):
            self.collector.cycle()


class ReportTests(HerdrTestCase):
    def test_report_uses_source_token_sequence_and_expiry(self):
        resource_usage.report_usage("w1", "CPU 1%  MEM 2 MiB")
        resource_usage.report_usage("w1", "CPU 1%  MEM 2 MiB")
        first, second = self.reports()
        self.assertEqual(first[:4], ["herdr", "workspace", "report-metadata", "w1"])
        options = dict(zip(first[4::2], first[5::2]))
        self.assertEqual(options["--source"], "nulluminati.resource-usage")
        self.assertEqual(options["--token"], "resource_usage=CPU 1%  MEM 2 MiB")
        self.assertEqual(options["--ttl-ms"], "8000")
        self.assertLess(int(options["--seq"]), int(dict(zip(second[4::2], second[5::2]))["--seq"]))

    def test_clear_touches_only_this_token(self):
        resource_usage.clear_usage("w1")
        (argv,) = self.reports()
        self.assertIn("--clear-token", argv)
        self.assertEqual(argv[argv.index("--clear-token") + 1], "resource_usage")
        self.assertNotIn("--token", argv)

    def test_herdr_bin_path_is_used(self):
        os.environ["HERDR_BIN_PATH"] = "/opt/herdr"
        resource_usage.report_usage("w1", "x")
        self.assertEqual(self.calls[0][0], "/opt/herdr")


class WorkerTests(HerdrTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.socket = self.state / "herdr.sock"
        self.socket.write_text("")
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.state)
        os.environ["HERDR_SOCKET_PATH"] = str(self.socket)
        self.spawned = []
        replacement = patch.object(resource_usage, "spawn_worker", lambda: self.spawned.append(True))
        replacement.start()
        self.addCleanup(replacement.stop)
        self.stderr = io.StringIO()

    def run_worker(self, texts, cycles=None, on_sleep=None):
        sequence = list(texts)

        class FakeCollector:
            def cycle(self):
                result = sequence.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if on_sleep:
                on_sleep(len(sleeps))

        with contextlib.redirect_stderr(self.stderr):
            resource_usage.run_worker(FakeCollector(), sleep=sleep, max_cycles=cycles)
        return sleeps

    def test_ensure_starts_one_worker_when_enabled(self):
        self.assertTrue(resource_usage.ensure_running())
        self.assertEqual(self.spawned, [True])

    def test_ensure_does_nothing_while_worker_holds_lock(self):
        with resource_usage.worker_lock() as held:
            self.assertTrue(held)
            self.assertFalse(resource_usage.ensure_running())
            with resource_usage.worker_lock() as second:
                self.assertFalse(second)
        self.assertEqual(self.spawned, [])
        self.assertTrue(resource_usage.ensure_running())

    def test_sessions_have_independent_locks(self):
        with resource_usage.worker_lock():
            os.environ["HERDR_SOCKET_PATH"] = str(self.state / "other.sock")
            self.assertTrue(resource_usage.ensure_running())

    def test_disabled_preference_survives_ensure(self):
        resource_usage.disable()
        self.assertFalse(resource_usage.ensure_running())
        self.assertEqual(self.spawned, [])
        resource_usage.enable()
        self.assertEqual(self.spawned, [True])
        self.assertEqual((self.state / "preference").read_text().strip(), "enabled")

    def test_worker_reports_each_cycle_then_clears_on_disable(self):
        def on_sleep(count):
            if count == 2:
                resource_usage.disable()

        sleeps = self.run_worker([{"w1": "a"}, {"w1": "a"}], on_sleep=on_sleep)
        self.assertEqual(sleeps, [2.0, 2.0])
        tokens = [argv[argv.index("--token") + 1] if "--token" in argv else "clear" for argv in self.reports()]
        self.assertEqual(tokens, ["resource_usage=a", "resource_usage=a", "clear"])

    def test_worker_exits_when_socket_disappears(self):
        self.socket.unlink()
        self.run_worker([{"w1": "a"}])
        self.assertEqual(self.reports(), [])

    def test_worker_exits_when_plugin_is_unregistered(self):
        self.plugins = []
        self.run_worker([{"w1": "a"}])
        self.assertEqual(self.reports(), [])

    def test_worker_exits_when_plugin_is_disabled_in_herdr(self):
        self.plugins = [{"plugin_id": "nulluminati.resource-usage", "enabled": False}]
        self.run_worker([{"w1": "a"}])
        self.assertEqual(self.reports(), [])

    def test_worker_does_not_run_twice(self):
        with resource_usage.worker_lock():
            self.run_worker([{"w1": "a"}])
        self.assertEqual(self.reports(), [])

    def test_lock_is_released_after_worker_exits(self):
        self.run_worker([{"w1": "a"}], cycles=1)
        with resource_usage.worker_lock() as held:
            self.assertTrue(held)

    def test_closed_workspace_token_is_cleared(self):
        self.run_worker([{"w1": "a", "w2": "b"}, {"w1": "a"}], cycles=2)
        w2 = [argv for argv in self.reports() if argv[3] == "w2"]
        self.assertIn("--clear-token", w2[-1])

    def test_clearing_a_closed_workspace_is_not_a_failure(self):
        real = self.fake_command

        def closed_w2(argv, env=None):
            if argv[1:4] == ["workspace", "report-metadata", "w2"] and "--clear-token" in argv:
                self.calls.append(argv)
                raise subprocess.CalledProcessError(1, argv, '{"error":{"code":"workspace_not_found"}}', "")
            return real(argv, env)

        with patch.object(resource_usage, "command", closed_w2):
            self.run_worker([{"w1": "a", "w2": "b"}, {"w1": "a"}], cycles=2)
        self.assertEqual(self.stderr.getvalue(), "")

    def test_failures_are_logged_once_and_retried(self):
        error = subprocess.CalledProcessError(1, ["herdr"], "", "server busy")
        sleeps = self.run_worker([error, error, {"w1": "a"}], cycles=3)
        self.assertEqual(self.stderr.getvalue().count("server busy"), 1)
        self.assertEqual(len(sleeps), 3)
        self.assertTrue(any("resource_usage=a" in argv for argv in self.reports()))

    def test_same_failure_after_a_success_is_not_logged_again(self):
        error = subprocess.CalledProcessError(1, ["herdr"], "", "server busy")
        self.run_worker([error, {"w1": "a"}, error], cycles=3)
        self.assertEqual(self.stderr.getvalue().count("server busy"), 1)

    def test_log_is_emptied_when_it_passes_the_limit(self):
        path = self.state / "worker.log"
        with patch.object(resource_usage, "LOG_LIMIT_BYTES", 100):
            with open(path, "a") as stream, contextlib.redirect_stderr(stream):
                for number in range(20):
                    resource_usage.log(f"failure {number}")
        self.assertLessEqual(path.stat().st_size, 100 + len("resource-usage: failure 19\n"))
        self.assertTrue(path.read_text().endswith("resource-usage: failure 19\n"))

    def test_worker_gives_up_after_repeated_failures(self):
        error = OSError("broken")
        sleeps = self.run_worker([error] * (resource_usage.MAX_FAILURES + 5))
        self.assertEqual(len(sleeps), resource_usage.MAX_FAILURES - 1)
        self.assertIn("giving up", self.stderr.getvalue())

    def test_main_creates_missing_state_dir(self):
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(self.state / "missing")
        self.assertEqual(resource_usage.main(["--ensure-running"]), 0)
        self.assertEqual(self.spawned, [True])

    def test_main_requires_state_dir(self):
        os.environ.pop("HERDR_PLUGIN_STATE_DIR")
        with contextlib.redirect_stderr(self.stderr):
            self.assertEqual(resource_usage.main(["--ensure-running"]), 1)
        self.assertIn("HERDR_PLUGIN_STATE_DIR", self.stderr.getvalue())


class ManifestTests(unittest.TestCase):
    def test_manifest_commands(self):
        manifest = tomllib.loads((PLUGIN / "herdr-plugin.toml").read_text())
        self.assertEqual(manifest["id"], "nulluminati.resource-usage")
        self.assertEqual(manifest["platforms"], ["linux", "macos"])
        ensure = ["python3", "bin/resource_usage.py", "--ensure-running"]
        self.assertEqual([hook["command"] for hook in manifest["startup"]], [ensure])
        self.assertEqual([(hook["on"], hook["command"]) for hook in manifest["events"]], [("workspace.focused", ensure)])
        actions = {action["id"]: action["command"] for action in manifest["actions"]}
        self.assertEqual(actions, {
            "enable": ["python3", "bin/resource_usage.py", "--enable"],
            "disable": ["python3", "bin/resource_usage.py", "--disable"],
        })


if __name__ == "__main__":
    unittest.main()
