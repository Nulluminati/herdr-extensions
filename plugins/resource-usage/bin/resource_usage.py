# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Write each open workspace's processor and memory usage to the `$resource_usage` workspace token.

A background worker, one per Herdr session, samples the process table every two
seconds. A workspace's processes are the shells of its panes and all their
descendants. A main workspace with open linked worktrees also shows the total of
its own processes and theirs.

Entry points:
  --ensure-running  start the worker if it is enabled and not running (startup and focus hooks)
  --run             be the worker
  --enable          turn collection on and start the worker
  --disable         turn collection off; the worker clears its tokens and exits
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

TOKEN = "resource_usage"
SOURCE = "nulluminati.resource-usage"
PLUGIN_ID = "nulluminati.resource-usage"
SCRIPT = Path(__file__).resolve()
INTERVAL_SECONDS = 2.0
TTL_MS = 8000
# The worker checks that Herdr still has the plugin enabled every this many cycles.
REGISTRATION_EVERY = 15
# The worker exits after this many failed cycles in a row, about one minute.
MAX_FAILURES = 30
LOG_LIMIT_BYTES = 256 * 1024
# After a failed worktree lookup, the worker asks again after this many seconds.
RELATIONSHIP_RETRY_SECONDS = 30.0
# Herdr errors that mean a workspace or pane is gone or has no Git checkout, not a passing failure.
PANE_GONE = {"pane_not_found"}
NOT_A_WORKTREE = {"not_git_worktree", "workspace_not_found"}
MIB = 1024 * 1024
GIB = 1024 * MIB
# The labels before the processor and memory values: text, or Nerd Font glyphs (nf-oct-cpu, nf-md-memory).
LABELS = {"plain": ("CPU", "MEM"), "nerd": ("\uf4bc", "\U000f035b")}


def command(argv: list[str], env: dict | None = None):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=10, env=env)


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_json(*arguments: str) -> dict:
    return json.loads(command([herdr_bin(), *arguments]).stdout or "{}")


def error_code(error: Exception) -> str | None:
    # Herdr writes a failed request's error as JSON to standard output.
    if not isinstance(error, subprocess.CalledProcessError):
        return None
    try:
        return (json.loads(error.stdout or "{}").get("error") or {}).get("code")
    except (json.JSONDecodeError, AttributeError):
        return None


def log(message: str) -> None:
    # The worker's standard error is its log file. Empty it when it passes the limit.
    try:
        descriptor = sys.stderr.fileno()
        if os.fstat(descriptor).st_size > LOG_LIMIT_BYTES:
            os.ftruncate(descriptor, 0)
    except (OSError, ValueError):
        pass
    print(f"resource-usage: {message}", file=sys.stderr, flush=True)


def failure(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or "").strip() or (error.stdout or "").strip() or str(error)
    return str(error)


# Process sampling


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    # Start time as the platform reports it. With pid it names one process, even after pid reuse.
    start: str
    cpu_seconds: float
    rss_bytes: int
    # Seconds since the process started, when the platform tells us.
    age_seconds: float | None = None


def parse_linux_stat(pid: int, text: str, clock_ticks: int, page_size: int, uptime: float | None = None) -> Process:
    # The command name is in parentheses and can hold spaces and parentheses itself,
    # so the fields start after the last closing parenthesis. fields[0] is field 3 in proc(5).
    fields = text[text.rindex(")") + 2 :].split()
    utime, stime, start = int(fields[11]), int(fields[12]), int(fields[19])
    return Process(
        pid=pid,
        ppid=int(fields[1]),
        start=fields[19],
        cpu_seconds=(utime + stime) / clock_ticks,
        rss_bytes=int(fields[21]) * page_size,
        # The start time counts clock ticks since boot.
        age_seconds=None if uptime is None else uptime - start / clock_ticks,
    )


def collect_linux_processes(
    proc_dir: Path = Path("/proc"), clock_ticks: int | None = None, page_size: int | None = None
) -> dict[int, Process]:
    clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
    page_size = page_size or os.sysconf("SC_PAGE_SIZE")
    try:
        uptime = float((proc_dir / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    processes = {}
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            text = (entry / "stat").read_text()
            processes[pid] = parse_linux_stat(pid, text, clock_ticks, page_size, uptime)
        except (OSError, ValueError, IndexError):
            # The process exited after the listing, or we may not read it.
            continue
    return processes


def parse_ps_time(text: str) -> float:
    # ps prints cumulative time as [[DD-]HH:]MM:SS[.ss]; macOS lets minutes go past 59.
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def collect_macos_processes() -> dict[int, Process]:
    # One ps call per cycle. The C locale fixes the lstart date words and the decimal point.
    env = {**os.environ, "LC_ALL": "C"}
    output = command(["ps", "-A", "-o", "pid=,ppid=,lstart=,etime=,time=,rss="], env=env).stdout
    processes = {}
    for line in output.splitlines():
        # lstart is five words, such as "Tue Oct 27 09:10:12 2026".
        fields = line.split()
        if len(fields) != 10:
            continue
        try:
            pid = int(fields[0])
            processes[pid] = Process(
                pid=pid,
                ppid=int(fields[1]),
                start=" ".join(fields[2:7]),
                cpu_seconds=parse_ps_time(fields[8]),
                rss_bytes=int(fields[9]) * 1024,
                age_seconds=parse_ps_time(fields[7]),
            )
        except ValueError:
            continue
    return processes


def collect_processes() -> dict[int, Process]:
    if sys.platform.startswith("linux"):
        return collect_linux_processes()
    if sys.platform == "darwin":
        return collect_macos_processes()
    raise RuntimeError(f"unsupported platform {sys.platform}")


# Process sets


def descendants(roots, processes: dict[int, Process]) -> set[int]:
    children: dict[int, list[int]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process.pid)
    found: set[int] = set()
    pending = [root for root in roots if root in processes]
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    return found


def workspace_process_sets(
    workspaces: list[dict], panes: list[dict], roots: dict[str, int], processes: dict[int, Process]
) -> dict[str, set[int] | None]:
    # None means no pane of the workspace has a live shell we know: the usage is unknown, not zero.
    pane_roots: dict[str, list[int]] = {}
    for pane in panes:
        root = roots.get(pane.get("pane_id"))
        if root in processes:
            pane_roots.setdefault(pane.get("workspace_id"), []).append(root)
    sets: dict[str, set[int] | None] = {}
    for workspace in workspaces:
        workspace_id = workspace["workspace_id"]
        found = pane_roots.get(workspace_id)
        sets[workspace_id] = descendants(found, processes) if found else None
    return sets


def family_parents(workspaces: list[dict], lookup) -> dict[str, str]:
    """Map each open linked worktree workspace to the open workspace of its main checkout.

    `lookup(workspace_id)` returns the `herdr worktree list` result for a workspace, or None.
    """
    lists: dict[str, dict] = {}

    def listed(workspace_id: str) -> dict | None:
        for result in lists.values():
            if any(entry.get("open_workspace_id") == workspace_id for entry in result.get("worktrees") or []):
                return result
        result = lookup(workspace_id)
        if result:
            lists[(result.get("source") or {}).get("repo_key")] = result
        return result

    repos: dict[str, dict[bool, list[str]]] = {}
    for workspace in workspaces:
        workspace_id = workspace["workspace_id"]
        worktree = workspace.get("worktree") or {}
        repo_key, linked = worktree.get("repo_key"), worktree.get("is_linked_worktree")
        if not repo_key or linked is None:
            result = listed(workspace_id)
            if not result:
                continue
            repo_key = (result.get("source") or {}).get("repo_key")
            entries = [
                entry for entry in result.get("worktrees") or [] if entry.get("open_workspace_id") == workspace_id
            ]
            if not repo_key or not entries:
                continue
            linked = bool(entries[0].get("is_linked_worktree"))
        repos.setdefault(repo_key, {True: [], False: []})[bool(linked)].append(workspace_id)

    parents = {}
    for repo_key, members in repos.items():
        mains, children = members[False], members[True]
        if not children or not mains:
            continue
        parent = mains[0] if len(mains) == 1 else None
        if parent is None:
            # Several workspaces show the main checkout. Herdr names one as the source.
            result = lists.get(repo_key) or listed(mains[0]) or {}
            source = (result.get("source") or {}).get("source_workspace_id")
            parent = source if source in mains else None
        if parent:
            parents.update({child: parent for child in children})
    return parents


def family_process_sets(sets: dict[str, set[int] | None], parents: dict[str, str]) -> dict[str, set[int] | None]:
    # The union counts a process once, even when a parent and a child share it.
    families: dict[str, set[int] | None] = {}
    for child, parent in parents.items():
        if parent not in families:
            families[parent] = set(sets[parent]) if sets.get(parent) is not None else None
        child_set = sets.get(child)
        if families[parent] is None or child_set is None:
            families[parent] = None
        else:
            families[parent] |= child_set
    return families


# Usage


@dataclass(frozen=True)
class Usage:
    cpu_percent: float | None
    rss_bytes: int


def measure_usage(
    pids: set[int], processes: dict[int, Process], previous: dict | None, elapsed: float, cpu_count: int | None
) -> Usage:
    """Processor use over the last interval on a whole-machine scale, and resident memory now.

    `previous` maps (pid, start) to cumulative processor seconds at the last sample;
    None means this is the first sample.
    """
    live = [processes[pid] for pid in pids if pid in processes]
    rss = sum(process.rss_bytes for process in live)
    if previous is None or elapsed <= 0 or not cpu_count:
        return Usage(cpu_percent=None, rss_bytes=rss)
    busy = 0.0
    for process in live:
        before = previous.get((process.pid, process.start))
        if before is not None:
            busy += max(process.cpu_seconds - before, 0.0)
        elif process.age_seconds is not None and process.age_seconds <= elapsed + 1.0:
            # It started during the interval, so all its time counts. The second of slack
            # covers ps, which gives the age in whole seconds.
            busy += process.cpu_seconds
        # Else it is older than the interval but was missing from the last sample:
        # this sample is its baseline.
    return Usage(cpu_percent=min(100 * busy / elapsed / cpu_count, 100.0), rss_bytes=rss)


def format_memory(rss_bytes: int) -> str:
    if rss_bytes < GIB:
        return f"{rss_bytes / MIB:.0f} MiB"
    return f"{rss_bytes / GIB:.1f} GiB"


def format_usage(usage: Usage | None, total: bool = False, icon_style: str = "plain") -> str:
    cpu = "--" if usage is None or usage.cpu_percent is None else f"{usage.cpu_percent:.0f}%"
    memory = "--" if usage is None else format_memory(usage.rss_bytes)
    cpu_label, memory_label = LABELS[icon_style]
    text = f"{cpu_label} {cpu}  {memory_label} {memory}"
    return f"{text}  total" if total else text


# Config


def load_icon_style() -> tuple[str, str | None]:
    """The `icon_style` from the plugin's config.toml, and an error to log when the file is bad.

    A missing file, a bad file or a bad value gives "plain", which needs no special font."""
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        return "plain", None
    path = Path(config_dir) / "config.toml"
    try:
        values = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return "plain", None
    except (OSError, ValueError) as error:
        # ValueError covers bad TOML and a file that is not UTF-8.
        return "plain", f"{path}: {error}; using plain labels"
    style = values.get("icon_style", "plain")
    if not isinstance(style, str) or style not in LABELS:
        return "plain", f"{path}: icon_style must be plain or nerd, not {style!r}; using plain labels"
    return style, None


# Herdr


def collect_workspaces() -> tuple[list[dict], list[dict]]:
    snapshot = herdr_json("api", "snapshot").get("result", {}).get("snapshot") or {}
    return snapshot.get("workspaces") or [], snapshot.get("panes") or []


def pane_root(pane_id: str) -> int | None:
    info = herdr_json("pane", "process-info", "--pane", pane_id).get("result", {}).get("process_info") or {}
    pid = info.get("shell_pid")
    return pid if isinstance(pid, int) else None


def worktree_list(workspace_id: str) -> dict | None:
    """The workspace's `herdr worktree list` result, or None when it has no Git checkout.

    Other failures raise, so the caller can ask again later."""
    try:
        return herdr_json("worktree", "list", "--workspace", workspace_id).get("result")
    except subprocess.CalledProcessError as error:
        if error_code(error) in NOT_A_WORKTREE:
            return None
        raise


def report_usage(workspace_id: str, text: str) -> None:
    # The sequence is a timestamp, so it keeps rising after a worker restart.
    command(
        [
            herdr_bin(),
            "workspace",
            "report-metadata",
            workspace_id,
            "--source",
            SOURCE,
            "--token",
            f"{TOKEN}={text}",
            "--seq",
            str(time.time_ns()),
            "--ttl-ms",
            str(TTL_MS),
        ]
    )


def clear_usage(workspace_id: str) -> None:
    command(
        [
            herdr_bin(),
            "workspace",
            "report-metadata",
            workspace_id,
            "--source",
            SOURCE,
            "--clear-token",
            TOKEN,
            "--seq",
            str(time.time_ns()),
        ]
    )


def clear_closed(workspace_id: str) -> None:
    # The workspace may be gone, and its token with it.
    try:
        clear_usage(workspace_id)
    except (OSError, subprocess.SubprocessError):
        pass


def plugin_registered() -> bool:
    plugins = herdr_json("plugin", "list", "--plugin", PLUGIN_ID, "--json").get("result", {}).get("plugins") or []
    return any(plugin.get("plugin_id") == PLUGIN_ID and plugin.get("enabled") for plugin in plugins)


class Collector:
    """One collection cycle per call. Keeps what the next cycle needs: pane shells,
    worktree relationships and the last processor counters."""

    def __init__(
        self, collect=collect_processes, clock=time.monotonic, cpu_count=os.cpu_count, icon_style=load_icon_style
    ):
        self.collect = collect
        self.clock = clock
        self.cpu_count = cpu_count
        self.icon_style = icon_style
        self.config_error: str | None = None
        self.roots: dict[str, tuple[int, str]] = {}
        self.topology = None
        self.parents: dict[str, str] = {}
        self.previous: dict | None = None
        self.previous_time = 0.0
        self.process_error: str | None = None
        self.relationship_retry_at: float | None = None

    def refresh_roots(self, panes: list[dict], processes: dict[int, Process]) -> tuple[dict[str, int], set[str]]:
        """Each pane's shell, and the workspaces with a pane whose shell we could not ask for."""
        current = {}
        incomplete = set()
        for pane in panes:
            pane_id = pane.get("pane_id")
            cached = self.roots.get(pane_id)
            if cached and cached[0] in processes and processes[cached[0]].start == cached[1]:
                current[pane_id] = cached
                continue
            try:
                pid = pane_root(pane_id)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                # A pane that closed after the snapshot has nothing to count.
                if error_code(error) not in PANE_GONE:
                    incomplete.add(pane.get("workspace_id"))
                continue
            if pid in processes:
                current[pane_id] = (pid, processes[pid].start)
        self.roots = current
        return {pane_id: pid for pane_id, (pid, _start) in current.items()}, incomplete

    def refresh_parents(self, workspaces: list[dict]) -> None:
        topology = sorted(
            (workspace["workspace_id"], json.dumps(workspace.get("worktree"), sort_keys=True))
            for workspace in workspaces
        )
        retry = self.relationship_retry_at is not None and self.clock() >= self.relationship_retry_at
        if topology == self.topology and not retry:
            return
        failed = []

        def lookup(workspace_id: str) -> dict | None:
            try:
                return worktree_list(workspace_id)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                failed.append(workspace_id)
                return None

        self.parents = family_parents(workspaces, lookup)
        self.topology = topology
        self.relationship_retry_at = self.clock() + RELATIONSHIP_RETRY_SECONDS if failed else None

    def read_icon_style(self) -> str:
        # The config is read every cycle, so a change shows without a restart. Log a bad config once.
        style, error = self.icon_style()
        if error and error != self.config_error:
            log(error)
        self.config_error = error
        return style

    def cycle(self) -> dict[str, str]:
        style = self.read_icon_style()
        workspaces, panes = collect_workspaces()
        self.refresh_parents(workspaces)
        try:
            processes = self.collect()
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            # Log a failure once, not every cycle while it lasts.
            if failure(error) != self.process_error:
                self.process_error = failure(error)
                log(f"cannot read processes: {self.process_error}")
            self.previous = None
            families = {parent for parent in self.parents.values()}
            return {
                workspace["workspace_id"]: format_usage(None, workspace["workspace_id"] in families, style)
                for workspace in workspaces
            }
        self.process_error = None
        now = self.clock()
        roots, incomplete = self.refresh_roots(panes, processes)
        sets = workspace_process_sets(workspaces, panes, roots, processes)
        # A workspace with a pane we could not measure would show too little.
        sets.update({workspace_id: None for workspace_id in incomplete if workspace_id in sets})
        families = family_process_sets(sets, self.parents)
        elapsed = now - self.previous_time
        cpu_count = self.cpu_count()

        def measure(pids):
            return None if pids is None else measure_usage(pids, processes, self.previous, elapsed, cpu_count)

        texts = {}
        for workspace_id, pids in sets.items():
            if workspace_id in families:
                texts[workspace_id] = format_usage(measure(families[workspace_id]), total=True, icon_style=style)
            else:
                texts[workspace_id] = format_usage(measure(pids), icon_style=style)
        self.previous = {(process.pid, process.start): process.cpu_seconds for process in processes.values()}
        self.previous_time = now
        return texts


# Worker


def state_dir() -> Path:
    return Path(os.environ["HERDR_PLUGIN_STATE_DIR"])


def enabled() -> bool:
    # No preference file is a fresh install, which collects.
    try:
        return (state_dir() / "preference").read_text().strip() != "disabled"
    except FileNotFoundError:
        return True


def set_preference(value: str) -> None:
    (state_dir() / "preference").write_text(value + "\n")


def session_key() -> str:
    # Each Herdr session has its own socket, so it gets its own worker.
    socket_path = os.environ.get("HERDR_SOCKET_PATH", "")
    return hashlib.sha256(socket_path.encode()).hexdigest()[:16]


def log_path() -> Path:
    return state_dir() / f"worker-{session_key()}.log"


@contextmanager
def worker_lock():
    """Yield True while this process holds the session's worker lock, else False."""
    path = state_dir() / f"worker-{session_key()}.lock"
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def spawn_worker() -> None:
    log = log_path()
    if log.exists() and log.stat().st_size > LOG_LIMIT_BYTES:
        log.write_text("")
    with open(log, "a") as stderr:
        # A new session detaches the worker from the hook that started it.
        subprocess.Popen(
            [sys.executable, str(SCRIPT), "--run"],
            cwd=SCRIPT.parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
        )


def ensure_running() -> bool:
    if not enabled():
        return False
    with worker_lock() as free:
        if not free:
            return False
    # Two hooks can both get here; the second worker finds the lock taken and exits.
    spawn_worker()
    return True


def enable() -> None:
    set_preference("enabled")
    ensure_running()


def disable() -> None:
    # The worker sees this within one cycle, clears its tokens and exits.
    set_preference("disabled")


def server_present() -> bool:
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    return not socket_path or os.path.exists(socket_path)


def run_worker(collector=None, sleep=time.sleep, max_cycles: int | None = None) -> None:
    collector = collector or Collector()
    with worker_lock() as held:
        if not held:
            return
        reported: set[str] = set()
        last_error = None
        failures = 0
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                if not enabled() or not server_present():
                    break
                try:
                    if cycles % REGISTRATION_EVERY == 0 and not plugin_registered():
                        break
                    texts = collector.cycle()
                    for workspace_id, text in texts.items():
                        report_usage(workspace_id, text)
                    for workspace_id in reported - texts.keys():
                        clear_closed(workspace_id)
                    reported = set(texts)
                    failures = 0
                except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                    failures += 1
                    message = failure(error)
                    # Log a failure once, also when it comes back after a success.
                    if message != last_error:
                        log(message)
                        last_error = message
                    if failures >= MAX_FAILURES:
                        log(f"giving up after {failures} failed cycles")
                        break
                cycles += 1
                sleep(INTERVAL_SECONDS)
        finally:
            if server_present():
                for workspace_id in reported:
                    clear_closed(workspace_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Herdr workspace resource usage")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ensure-running", action="store_true")
    group.add_argument("--run", action="store_true")
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    arguments = parser.parse_args(argv)
    if not os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        print("resource-usage: HERDR_PLUGIN_STATE_DIR is not set; run this from Herdr", file=sys.stderr)
        return 1
    state_dir().mkdir(parents=True, exist_ok=True)
    if arguments.run:
        run_worker()
    elif arguments.enable:
        enable()
    elif arguments.disable:
        disable()
    else:
        ensure_running()
    return 0


if __name__ == "__main__":
    sys.exit(main())
