"""UI mockup drafts for planning.

The planner asks Codex to generate an image of a screen. The image is a draft in herdboard's state
folder, not an attachment: the user looks at it in a pane next to the planner, and only a draft that
the user approves becomes an attachment through `attach`. The drafts go when the card is finished or
archived.
"""

from __future__ import annotations

import base64
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import start
from .model import HerdboardError

MAX_OPTIONS = 4
MAX_IMAGES = 5
QUALITIES = ("auto", "low", "medium", "high")
CODEX_TIMEOUT_SECONDS = 10 * 60
SLUG_LENGTH = 40

# The kitty graphics query: a 1x1 RGB image that the terminal only checks, then a device attributes
# request. Every terminal answers the second, so the viewer does not wait long for the first.
GRAPHICS_QUERY = b"\033_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\033\\\033[c"
CHUNK = 4096
# Rows below the image: a spare line, the option label, the file name and the keys.
TEXT_ROWS = 4

# Options of one call share a time stamp, so picking a free name and moving the file into it is one step.
_names = threading.Lock()


class MockupError(HerdboardError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def drafts_dir(ref: str) -> Path:
    return start.state_dir() / "mockups" / start.safe_key(ref)


def discard_drafts(ref: str) -> None:
    shutil.rmtree(drafts_dir(ref), ignore_errors=True)


def _codex_home() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


def _slug(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    if len(slug) > SLUG_LENGTH:
        # Cut at a word end, unless the first word alone is too long.
        cut = slug[:SLUG_LENGTH + 1]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut[:SLUG_LENGTH]
    return slug or "mockup"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _instruction(prompt: str, quality: str, images) -> str:
    lines = ["Generate exactly one image with your image generation tool: a UI mockup of the screen below.",
             f"Make it {quality} quality." if quality != "auto" else "Choose the image quality yourself."]
    if images:
        lines.append("Use the attached images: edit them, or follow their layout and style, as the description says.")
    lines += ["Do not write or change any other file. Reply with only the path of the saved image.", "",
              prompt.strip()]
    return "\n".join(lines)


def _check(prompt: str, images, quality: str) -> list[Path]:
    if not prompt or not prompt.strip():
        raise MockupError("The mockup needs a prompt that describes the screen", "usage")
    if quality not in QUALITIES:
        raise MockupError(f"Quality must be one of {', '.join(QUALITIES)}", "usage")
    images = [Path(image) for image in images]
    if len(images) > MAX_IMAGES:
        raise MockupError(f"Give {MAX_IMAGES} images at most", "usage")
    for image in images:
        if not image.is_file():
            raise MockupError(f"No image file {image}", "usage")
    return images


def _events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _error_text(event: dict) -> str | None:
    if event.get("type") == "error" and isinstance(event.get("message"), str):
        return event["message"]
    if event.get("type") == "turn.failed" and isinstance(event.get("error"), dict):
        message = event["error"].get("message")
        return message if isinstance(message, str) else None
    return None


def _saved_image(events: list[dict]) -> tuple[Path | None, bool, str]:
    """The PNG that Codex saved, True when it is in Codex's own folder for this run, and the text that
    explains a missing one."""
    thread = next((event.get("thread_id") for event in events
                   if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str)), None)
    messages = [event["item"]["text"] for event in events
                if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "agent_message" and isinstance(event["item"].get("text"), str)]
    errors = [text for text in map(_error_text, events) if text]
    if thread:
        folder = _codex_home() / "generated_images" / thread
        found = sorted(folder.glob("*.png"), key=lambda path: path.stat().st_mtime) if folder.is_dir() else []
        if found:
            return found[-1], True, ""
    if messages:
        candidate = Path(messages[-1].strip()).expanduser()
        if candidate.suffix.lower() == ".png" and candidate.is_file():
            return candidate, False, ""
    # Codex errors first, unchanged: `model_not_found` tells the user to change mockup_model.
    unique = list(dict.fromkeys(errors))
    return None, False, "; ".join(unique) or (messages[-1].strip() if messages else "")


def generate(config, ref: str, prompt: str, images=(), quality: str = "medium", stamp: str | None = None) -> Path:
    """Run Codex for one image of `prompt` and return the draft's path."""
    images = _check(prompt, images, quality)
    if shutil.which("codex") is None:
        raise MockupError("The codex command is not installed. Install the Codex CLI and log in with a "
                          "ChatGPT account.", "codex_missing")
    folder = drafts_dir(ref)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MockupError(f"{folder}: cannot create the drafts folder: {error}", "io") from error
    argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "-m", config.mockup_model, "-C", str(folder)]
    for image in images:
        argv += ["-i", str(image)]
    # `-i` takes every value after it, so `--` ends it before the instruction.
    argv += ["--", _instruction(prompt, quality, images)]
    try:
        # Without a closed stdin, Codex waits for more input.
        result = start.command(argv, timeout=CODEX_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise MockupError(f"Codex made no image in {CODEX_TIMEOUT_SECONDS // 60} minutes", "timeout") from error
    except OSError as error:
        raise MockupError(f"Cannot run codex: {error}", "codex_missing") from error
    saved, generated, reason = _saved_image(_events(result.stdout))
    if saved is None:
        reason = reason or result.stderr.strip()[-500:] or f"exit code {result.returncode}"
        raise MockupError(f"Codex made no image: {reason}", "no_image")
    stamp = stamp or _stamp()
    slug = _slug(prompt)
    with _names:
        number = 1
        while True:
            draft = folder / f"{stamp}-{slug}{'' if number == 1 else f'-{number}'}.png"
            if not draft.exists():
                break
            number += 1
        # A file that Codex only names can be an input image or an attachment, so it stays where it is.
        try:
            if generated:
                shutil.move(str(saved), draft)
            else:
                shutil.copy2(saved, draft)
        except OSError as error:
            raise MockupError(f"Cannot move {saved} to {draft}: {error}", "io") from error
    return draft


def generate_all(config, ref: str, prompts, images=(), quality: str = "medium"):
    """One draft per prompt, made at the same time. Returns the drafts and the errors, both in prompt
    order, with None in the place of each failed draft and of each error that did not happen."""
    prompts = list(prompts)
    if not prompts or len(prompts) > MAX_OPTIONS:
        raise MockupError(f"Give one to {MAX_OPTIONS} prompts, one per option", "usage")
    for prompt in prompts:
        _check(prompt, images, quality)
    stamp = _stamp()

    def one(prompt):
        try:
            return generate(config, ref, prompt, images, quality, stamp=stamp), None
        except MockupError as error:
            return None, str(error)

    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        results = list(pool.map(one, prompts))
    drafts = [draft for draft, _ in results]
    failed = [error for _, error in results]
    if all(draft is None for draft in drafts):
        raise MockupError("; ".join(dict.fromkeys(failed)), "no_image")
    return drafts, failed


def open_file(path: Path) -> None:
    """Open the file in the system's default application, and do not wait for it."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"herdboard: cannot open {path} with {opener}: {error}", file=sys.stderr)


def show(paths, config_file: Path | None = None, numbers=None, total=None) -> str:
    """Show the drafts of one call in a pane to the right of the caller's, else in the default viewer.

    `numbers` are the option numbers of the drafts and `total` the number of options asked for, so the
    pane's labels match the option numbers in the result when an option failed."""
    paths = [Path(path) for path in paths]
    caller = os.environ.get("HERDR_PANE_ID")
    if caller:
        try:
            pane = start.herdr("pane", "split", caller, "--direction", "right", "--no-focus")["pane"]["pane_id"]
        except (HerdboardError, KeyError, TypeError):
            pane = None
        if pane:
            labels = [f"--option {number}" for number in numbers or []]
            labels += [f"--total {total}"] if total is not None else []
            viewer = " ".join([start.herdboard_command(config_file), "mockup-view", *labels,
                               *(shlex.quote(str(path)) for path in paths)])
            # Herdr prints nothing for `pane run` when it works, so only the exit code counts.
            try:
                result = start.command([os.environ.get("HERDR_BIN_PATH") or "herdr", "pane", "run", pane, viewer])
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode == 0:
                return "pane"
            try:
                start.herdr("pane", "close", pane, tolerated=("pane_not_found",))
            except HerdboardError:
                pass
    for path in paths:
        open_file(path)
    return "xdg-open"


class Terminal:
    """The pane's terminal in raw mode, so the viewer reads single keys and the graphics reply."""

    def __enter__(self):
        import termios
        import tty
        self.input = sys.stdin.fileno()
        self.out = sys.stdout.fileno()
        self.saved = termios.tcgetattr(self.input)
        tty.setraw(self.input)
        self.write(b"\033[?25l")
        return self

    def __exit__(self, *exc):
        import termios
        self.write(b"\033[?25h")
        termios.tcsetattr(self.input, termios.TCSADRAIN, self.saved)

    def write(self, data: bytes) -> None:
        while data:
            data = data[os.write(self.out, data):]

    def read(self, timeout: float | None = None) -> bytes:
        ready, _, _ = select.select([self.input], [], [], timeout)
        return os.read(self.input, 1024) if ready else b""

    def size(self) -> tuple[int, int, int, int]:
        """Columns, rows, and the width and height in pixels (0 when the terminal does not say)."""
        import fcntl
        import struct
        import termios
        try:
            rows, columns, width, height = struct.unpack(
                "HHHH", fcntl.ioctl(self.out, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            columns, rows = shutil.get_terminal_size()
            width = height = 0
        return columns, rows, width, height


def _graphics(terminal) -> bool:
    """True when the terminal answers the kitty graphics query with OK."""
    terminal.write(GRAPHICS_QUERY)
    reply = b""
    deadline = time.monotonic() + 1
    while not re.search(rb"\033\[\?[\d;]*c", reply):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        data = terminal.read(left)
        if not data:
            break
        reply += data
    return b";OK" in reply


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _fit(data: bytes, terminal) -> tuple[int, int]:
    """The image's size in cells: as large as the pane allows above the text, in its own proportions."""
    columns, rows, width, height = terminal.size()
    rows = max(rows - TEXT_ROWS, 1)
    # Without pixel sizes, assume cells twice as high as wide.
    cell_width = width / columns if width and columns else 10
    cell_height = height / (rows + TEXT_ROWS) if height else 20
    size = _png_size(data)
    if not size or not all(size):
        return columns, rows
    scale = min(columns * cell_width / size[0], rows * cell_height / size[1])
    return (max(1, min(columns, round(size[0] * scale / cell_width))),
            max(1, min(rows, round(size[1] * scale / cell_height))))


def _draw(terminal, paths: list[Path], index: int, numbers: list[int], total: int) -> None:
    path = paths[index]
    data = path.read_bytes()
    columns, rows = _fit(data, terminal)
    terminal.write(b"\033[2J\033[H\033_Ga=d\033\\")
    encoded = base64.b64encode(data)
    chunks = [encoded[at:at + CHUNK] for at in range(0, len(encoded), CHUNK)] or [b""]
    for number, chunk in enumerate(chunks):
        more = 1 if number < len(chunks) - 1 else 0
        controls = f"a=T,f=100,c={columns},r={rows},q=2,m={more}" if number == 0 else f"m={more}"
        terminal.write(b"\033_G" + controls.encode() + b";" + chunk + b"\033\\")
    lines = []
    if total > 1:
        lines.append(f"Option {numbers[index]} of {total}")
    lines.append(path.name)
    lines.append("←/→ switch · Enter close" if len(paths) > 1 else "Enter close")
    terminal.write(f"\033[{rows + 2};1H".encode() + "\r\n".join(lines).encode())


CLOSE_KEYS = (b"\r", b"\n", b"q", b"\x03", b"\x04")
NEXT_KEYS = (b"\033[C", b"\033OC", b"l")
PREVIOUS_KEYS = (b"\033[D", b"\033OD", b"h")
# How long the viewer waits for the rest of an escape sequence that one read cut.
SEQUENCE_WAIT_SECONDS = 0.1


def _split_keys(data: bytes) -> tuple[list[bytes], bytes]:
    """The keys in `data`, and the start of an escape sequence that the next read completes."""
    keys = []
    while data:
        if data[:1] != b"\033":
            keys.append(data[:1])
            data = data[1:]
        elif len(data) < 2 or data[1:2] in b"[O" and len(data) < 3:
            break
        elif data[1:2] == b"O":
            keys.append(data[:3])
            data = data[3:]
        elif data[1:2] == b"[":
            sequence = re.match(rb"\033\[[0-9;]*[@-~]", data)
            if sequence is None and re.fullmatch(rb"\033\[[0-9;]*", data):
                break
            end = sequence.end() if sequence else 2
            keys.append(data[:end])
            data = data[end:]
        else:
            keys.append(data[:1])
            data = data[1:]
    return keys, data


def _read_keys(terminal):
    """Yield single keys: a read can hold several keys, or cut an escape sequence. Ends at end of input."""
    pending = b""
    while True:
        data = terminal.read(SEQUENCE_WAIT_SECONDS if pending else None)
        if not data:
            if not pending:
                return
            # The rest of the sequence did not come: it was the Escape key alone, or noise.
            yield pending
            pending = b""
            continue
        keys, pending = _split_keys(pending + data)
        yield from keys


def _keys(terminal, paths: list[Path], numbers: list[int], total: int) -> None:
    index = 0
    _draw(terminal, paths, index, numbers, total)
    for key in _read_keys(terminal):
        if key in CLOSE_KEYS:
            return
        if len(paths) < 2:
            continue
        if key in NEXT_KEYS:
            index = (index + 1) % len(paths)
        elif key in PREVIOUS_KEYS:
            index = (index - 1) % len(paths)
        else:
            continue
        _draw(terminal, paths, index, numbers, total)


def view(paths, terminal=None, numbers=None, total=None) -> int:
    """The viewer that the pane runs: one draft at a time, full size, until Enter. `numbers` and `total`
    label the drafts as `show` describes; by default they count the drafts."""
    paths = [Path(path) for path in paths]
    numbers = list(numbers) if numbers else list(range(1, len(paths) + 1))
    total = total or len(paths)
    with terminal if terminal is not None else Terminal() as screen:
        if _graphics(screen):
            _keys(screen, paths, numbers, total)
        else:
            for path in paths:
                open_file(path)
            lines = ["This terminal shows no images. The drafts open in the default viewer:",
                     *(f"{number}. {path}" for number, path in zip(numbers, paths, strict=False)), "", "Enter close"]
            screen.write("\r\n".join(lines).encode())
            for key in _read_keys(screen):
                if key in CLOSE_KEYS:
                    break
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        try:
            start.herdr("pane", "close", pane, tolerated=("pane_not_found",))
        except HerdboardError:
            pass
    return 0
