"""Mockup drafts: Codex generates UI images during planning, and a pane shows them to the user."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import mockup, start  # noqa: E402
from herdboard.config import Config  # noqa: E402

from test_herdboard_cli import CliTestCase  # noqa: E402

# A 1x1 PNG, so the viewer can read a real size from the header.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class FakeCodex:
    """Fake `codex exec`: writes a PNG under CODEX_HOME and prints Codex's JSON lines."""

    def __init__(self, codex_home: Path):
        self.codex_home = codex_home
        self.calls = []
        # Prompt text -> what the fake does: "image", "message" (path only in the agent message),
        # "none" (no image) or "fail" (exit code 1 and an error event).
        self.behavior = {}
        self.count = 0
        self.existing = None

    def __call__(self, argv, timeout=120, stdin=None):
        self.calls.append({"argv": argv, "timeout": timeout, "stdin": stdin})
        instruction = argv[-1]
        behavior = next((value for key, value in self.behavior.items() if key in instruction), "image")
        self.count += 1
        thread = f"thread-{self.count}"
        events = [{"type": "thread.started", "thread_id": thread}]
        if behavior == "image":
            folder = self.codex_home / "generated_images" / thread
            folder.mkdir(parents=True)
            (folder / "ig_1.png").write_bytes(PNG)
            events.append({"type": "item.completed", "item": {"type": "agent_message", "text": "saved"}})
        elif behavior == "message":
            saved = self.codex_home / "elsewhere" / f"{thread}.png"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(PNG)
            events.append({"type": "item.completed", "item": {"type": "agent_message", "text": str(saved)}})
        elif behavior == "existing":
            # Codex makes no image and names a file that was already there, such as an input image.
            events.append({"type": "item.completed", "item": {"type": "agent_message", "text": str(self.existing)}})
        elif behavior == "none":
            events.append({"type": "item.completed", "item": {"type": "agent_message", "text": "I cannot draw that"}})
        else:
            events.append({"type": "error", "message": "model_not_found: gpt-6.1-sol"})
            events.append({"type": "turn.failed", "error": {"message": "model_not_found: gpt-6.1-sol"}})
        stdout = "\n".join(json.dumps(event) for event in events) + "\n"
        return subprocess.CompletedProcess(argv, 1 if behavior == "fail" else 0, stdout, "")


class MockupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex"
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                                       "CODEX_HOME": str(self.codex_home)}))
        os.environ.pop("HERDR_PANE_ID", None)
        self.codex = FakeCodex(self.codex_home)
        self.enterContext(mock.patch.object(start, "command", self.codex))
        self.enterContext(mock.patch.object(mockup.shutil, "which", return_value="/usr/bin/codex"))
        self.config = Config(todos_dir=self.root / "todos")


class GenerateTests(MockupTestCase):
    def test_runs_codex_exec_with_the_configured_model_in_the_drafts_folder(self):
        image = self.root / "screen.png"
        image.write_bytes(PNG)
        self.config.mockup_model = "gpt-7"
        mockup.generate(self.config, "tabc", "Login screen with two fields", images=[image])
        call, = self.codex.calls
        argv = call["argv"]
        folder = mockup.drafts_dir("tabc")
        self.assertEqual(argv[:argv.index("--")],
                         ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                          "-m", "gpt-7", "-C", str(folder), "-i", str(image)])
        self.assertEqual(len(argv), argv.index("--") + 2)
        self.assertIn("Login screen with two fields", argv[-1])
        self.assertIn("medium", argv[-1])
        self.assertIs(call["stdin"], subprocess.DEVNULL)
        self.assertEqual(call["timeout"], 600)

    def test_quality_goes_into_the_instruction(self):
        mockup.generate(self.config, "tabc", "Login screen", quality="high")
        self.assertIn("high quality", self.codex.calls[0]["argv"][-1])

    def test_moves_the_png_into_the_drafts_folder_with_a_slug_name(self):
        draft = mockup.generate(self.config, "tabc", "Login screen, two fields! And a logo on top of the page")
        self.assertEqual(draft.parent, mockup.drafts_dir("tabc"))
        self.assertEqual(draft.read_bytes(), PNG)
        self.assertRegex(draft.name, r"^\d{8}T\d{6}Z-login-screen-two-fields-and-a-logo-on\.png$")
        self.assertLessEqual(len(draft.name.split("Z-", 1)[1]) - len(".png"), 40)
        self.assertFalse(list((self.codex_home / "generated_images").rglob("*.png")))

    def test_falls_back_to_the_path_in_the_agent_message(self):
        self.codex.behavior["Login"] = "message"
        draft = mockup.generate(self.config, "tabc", "Login screen")
        self.assertEqual(draft.read_bytes(), PNG)
        self.assertEqual(draft.parent, mockup.drafts_dir("tabc"))

    def test_a_file_named_in_the_agent_message_is_copied_and_kept(self):
        original = self.root / "approved" / "mockup-login.png"
        original.parent.mkdir()
        original.write_bytes(PNG)
        self.codex.existing = original
        self.codex.behavior["Login"] = "existing"
        draft = mockup.generate(self.config, "tabc", "Login screen", images=[original])
        self.assertEqual(draft.read_bytes(), PNG)
        mockup.discard_drafts("tabc")
        self.assertEqual(original.read_bytes(), PNG)

    def test_no_png_raises_no_image_with_the_agent_message(self):
        self.codex.behavior["Login"] = "none"
        with self.assertRaises(mockup.MockupError) as caught:
            mockup.generate(self.config, "tabc", "Login screen")
        self.assertEqual(caught.exception.code, "no_image")
        self.assertIn("I cannot draw that", str(caught.exception))

    def test_a_codex_error_reaches_the_user_unchanged(self):
        self.codex.behavior["Login"] = "fail"
        with self.assertRaises(mockup.MockupError) as caught:
            mockup.generate(self.config, "tabc", "Login screen")
        self.assertIn("model_not_found: gpt-6.1-sol", str(caught.exception))

    def test_codex_missing(self):
        with mock.patch.object(mockup.shutil, "which", return_value=None), \
                self.assertRaises(mockup.MockupError) as caught:
            mockup.generate(self.config, "tabc", "Login screen")
        self.assertEqual(caught.exception.code, "codex_missing")
        self.assertEqual(self.codex.calls, [])

    def test_refuses_bad_input(self):
        images = []
        for index in range(6):
            images.append(self.root / f"{index}.png")
            images[-1].write_bytes(PNG)
        cases = {"empty prompt": dict(prompt="  "),
                 "six images": dict(prompt="x", images=images),
                 "missing image": dict(prompt="x", images=[self.root / "gone.png"]),
                 "bad quality": dict(prompt="x", quality="ultra")}
        for name, arguments in cases.items():
            with self.subTest(name), self.assertRaises(mockup.MockupError) as caught:
                mockup.generate(self.config, "tabc", **arguments)
            self.assertEqual(caught.exception.code, "usage")
        self.assertEqual(self.codex.calls, [])


class GenerateAllTests(MockupTestCase):
    def test_one_draft_per_prompt_in_prompt_order(self):
        drafts, failed = mockup.generate_all(self.config, "tabc", ["Sidebar layout", "Top tabs layout"])
        self.assertEqual(failed, [None, None])
        self.assertEqual([path.name.split("Z-", 1)[1] for path in drafts],
                         ["sidebar-layout.png", "top-tabs-layout.png"])
        self.assertEqual(len(self.codex.calls), 2)

    def test_one_failure_keeps_the_other_drafts(self):
        self.codex.behavior["Top tabs"] = "fail"
        drafts, failed = mockup.generate_all(self.config, "tabc", ["Sidebar layout", "Top tabs layout"])
        self.assertIsNotNone(drafts[0])
        self.assertTrue(drafts[0].is_file())
        self.assertIsNone(drafts[1])
        self.assertIsNone(failed[0])
        self.assertIn("model_not_found", failed[1])

    def test_raises_when_every_prompt_fails(self):
        self.codex.behavior["layout"] = "fail"
        with self.assertRaises(mockup.MockupError) as caught:
            mockup.generate_all(self.config, "tabc", ["Sidebar layout", "Top tabs layout"])
        self.assertIn("model_not_found", str(caught.exception))

    def test_refuses_zero_or_five_prompts(self):
        for prompts in ([], ["a", "b", "c", "d", "e"]):
            with self.subTest(count=len(prompts)), self.assertRaises(mockup.MockupError) as caught:
                mockup.generate_all(self.config, "tabc", prompts)
            self.assertEqual(caught.exception.code, "usage")
        self.assertEqual(self.codex.calls, [])

    def test_same_slug_and_time_get_distinct_names(self):
        drafts, _ = mockup.generate_all(self.config, "tabc", ["Login", "Login", "Login"])
        names = [path.name for path in drafts]
        self.assertEqual(len(set(names)), 3)
        stamp = names[0].split("-", 1)[0]
        self.assertEqual(set(names), {f"{stamp}-login.png", f"{stamp}-login-2.png", f"{stamp}-login-3.png"})


class FakeHerdr:
    def __init__(self, split_error=None, run_code=0):
        self.calls = []
        self.split_error = split_error
        self.run_code = run_code

    def __call__(self, argv, timeout=120, stdin=None):
        args = argv[1:]
        self.calls.append(args)
        if args[:2] == ["pane", "split"]:
            if self.split_error:
                body = {"error": {"code": self.split_error, "message": self.split_error}}
                return subprocess.CompletedProcess(argv, 1, "", json.dumps(body))
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}}), "")
        if args[:2] == ["pane", "run"]:
            # Herdr prints nothing for `pane run` when it works.
            return subprocess.CompletedProcess(argv, self.run_code, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {}}), "")


class ShowTests(MockupTestCase):
    def setUp(self):
        super().setUp()
        self.paths = [self.root / "a b.png", self.root / "c.png"]
        self.opened = []
        self.enterContext(mock.patch.object(mockup, "open_file", self.opened.append))

    def test_splits_the_callers_pane_and_runs_the_viewer_with_every_draft(self):
        herdr = FakeHerdr()
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), mock.patch.object(start, "command", herdr):
            shown = mockup.show(self.paths, config_file=None)
        self.assertEqual(shown, "pane")
        self.assertEqual(herdr.calls[0], ["pane", "split", "w1:p2", "--direction", "right", "--no-focus"])
        run = herdr.calls[1]
        self.assertEqual(run[:3], ["pane", "run", "w1:p9"])
        command = run[3]
        self.assertIn(f"{start.herdboard_command(None)} mockup-view ", command)
        self.assertTrue(command.endswith(" ".join(shlex.quote(str(path)) for path in self.paths)))
        self.assertEqual(len(herdr.calls), 2)
        self.assertEqual(self.opened, [])

    def test_the_viewer_gets_the_option_numbers_and_the_total(self):
        herdr = FakeHerdr()
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), mock.patch.object(start, "command", herdr):
            mockup.show(self.paths, numbers=[2, 3], total=3)
        command = herdr.calls[1][3]
        self.assertIn(" mockup-view --option 2 --option 3 --total 3 ", command)
        self.assertTrue(command.endswith(" ".join(shlex.quote(str(path)) for path in self.paths)))

    def test_opens_each_draft_without_herdr(self):
        self.assertEqual(mockup.show(self.paths), "xdg-open")
        self.assertEqual(self.opened, self.paths)

    def test_closes_the_pane_and_opens_each_draft_when_the_viewer_does_not_start(self):
        herdr = FakeHerdr(run_code=1)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), mock.patch.object(start, "command", herdr):
            self.assertEqual(mockup.show(self.paths), "xdg-open")
        self.assertEqual(herdr.calls[-1], ["pane", "close", "w1:p9"])
        self.assertEqual(self.opened, self.paths)

    def test_opens_each_draft_when_the_split_fails(self):
        herdr = FakeHerdr(split_error="pane_not_found")
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), mock.patch.object(start, "command", herdr):
            self.assertEqual(mockup.show(self.paths), "xdg-open")
        self.assertEqual(self.opened, self.paths)


class FakeTerminal:
    """A terminal in raw mode: each read takes the next queued input; b"" means no input before the timeout."""

    def __init__(self, inputs, size=(80, 24, 800, 480)):
        self.inputs = list(inputs)
        self.output = []
        self.size_value = size
        self.entered = False
        self.restored = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.restored = True

    def write(self, data: bytes):
        self.output.append(data)

    def read(self, timeout=None) -> bytes:
        return self.inputs.pop(0) if self.inputs else b"\r"

    def size(self):
        return self.size_value

    def text(self) -> str:
        return b"".join(self.output).decode()

    def screens(self) -> list[str]:
        """The output split at each screen clear."""
        return self.text().split("\033[2J")[1:]


OK_REPLY = b"\033_Gi=31;OK\033\\\033[?62;c"


class ViewTests(MockupTestCase):
    def setUp(self):
        super().setUp()
        self.paths = []
        for index in range(3):
            path = self.root / f"option-{index + 1}.png"
            path.write_bytes(PNG)
            self.paths.append(path)
        self.opened = []
        self.enterContext(mock.patch.object(mockup, "open_file", self.opened.append))
        self.herdr = FakeHerdr()
        self.enterContext(mock.patch.object(start, "command", self.herdr))

    def test_graphics_show_the_first_draft_in_kitty_chunks(self):
        terminal = FakeTerminal([OK_REPLY, b"\r"])
        self.assertEqual(mockup.view(self.paths, terminal=terminal), 0)
        text = terminal.text()
        self.assertIn("\033_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\033\\\033[c", text)
        screen, = terminal.screens()
        self.assertIn("\033_Ga=d\033\\", screen)
        chunks = [part for part in screen.split("\033_G")[1:] if not part.startswith("a=d")]
        self.assertTrue(chunks[0].startswith("a=T,f=100,"))
        self.assertIn("m=0;", chunks[-1])
        self.assertIn("Option 1 of 3", screen)
        self.assertIn("option-1.png", screen)
        self.assertIn("←/→ switch · Enter close", screen)
        self.assertTrue(terminal.restored)
        self.assertEqual(self.opened, [])

    def test_large_image_is_sent_in_4096_byte_chunks(self):
        big = self.root / "big.png"
        big.write_bytes(PNG + os.urandom(9000))
        terminal = FakeTerminal([OK_REPLY, b"\r"])
        mockup.view([big], terminal=terminal)
        screen, = terminal.screens()
        chunks = [part for part in screen.split("\033_G")[1:] if not part.startswith("a=d")]
        self.assertGreater(len(chunks), 2)
        payloads = [chunk.split(";", 1)[1].split("\033\\", 1)[0] for chunk in chunks]
        self.assertTrue(all(len(payload) == 4096 for payload in payloads[:-1]))
        self.assertTrue(all("m=1" in chunk.split(";", 1)[0] for chunk in chunks[:-1]))
        self.assertEqual(base64.b64decode("".join(payloads)), big.read_bytes())

    def test_arrows_switch_drafts_and_wrap(self):
        terminal = FakeTerminal([OK_REPLY, b"\033[C", b"\033[D", b"\033[D", b"l", b"h", b"\r"])
        mockup.view(self.paths, terminal=terminal)
        labels = [next(f"Option {i} of 3" for i in (1, 2, 3) if f"Option {i} of 3" in screen)
                  for screen in terminal.screens()]
        self.assertEqual(labels, ["Option 1 of 3", "Option 2 of 3", "Option 1 of 3", "Option 3 of 3",
                                  "Option 1 of 3", "Option 3 of 3"])

    def test_labels_use_the_option_numbers_after_a_failed_option(self):
        terminal = FakeTerminal([OK_REPLY, b"\033[C", b"\r"])
        mockup.view(self.paths[:2], terminal=terminal, numbers=[2, 3], total=3)
        first, second = terminal.screens()
        self.assertIn("Option 2 of 3", first)
        self.assertIn("Option 3 of 3", second)

    def test_one_draft_left_of_several_options_keeps_its_label(self):
        terminal = FakeTerminal([OK_REPLY, b"\r"])
        mockup.view(self.paths[:1], terminal=terminal, numbers=[2], total=2)
        screen, = terminal.screens()
        self.assertIn("Option 2 of 2", screen)
        self.assertNotIn("←/→", screen)

    def test_no_graphics_lists_the_option_numbers(self):
        terminal = FakeTerminal([b"", b"\r"])
        mockup.view(self.paths[:2], terminal=terminal, numbers=[2, 3], total=3)
        self.assertIn(f"2. {self.paths[0]}", terminal.text())
        self.assertIn(f"3. {self.paths[1]}", terminal.text())

    def test_an_arrow_split_over_two_reads_still_switches(self):
        terminal = FakeTerminal([OK_REPLY, b"\033[", b"C", b"\r"])
        mockup.view(self.paths, terminal=terminal)
        self.assertIn("Option 2 of 3", terminal.screens()[-1])

    def test_several_keys_in_one_read_all_count(self):
        terminal = FakeTerminal([OK_REPLY, b"\033[C\033[C", b"\r"])
        mockup.view(self.paths, terminal=terminal)
        self.assertIn("Option 3 of 3", terminal.screens()[-1])

    def test_enter_after_an_arrow_in_one_read_closes(self):
        terminal = FakeTerminal([OK_REPLY, b"\033[C\r", b"\033[C"])
        mockup.view(self.paths, terminal=terminal)
        self.assertEqual(terminal.inputs, [b"\033[C"])

    def test_no_graphics_enter_in_a_longer_read_closes(self):
        terminal = FakeTerminal([b"", b"x\r", b"y"])
        mockup.view(self.paths, terminal=terminal)
        self.assertEqual(terminal.inputs, [b"y"])

    def test_q_closes_too(self):
        terminal = FakeTerminal([OK_REPLY, b"q"])
        self.assertEqual(mockup.view(self.paths, terminal=terminal), 0)
        self.assertEqual(len(terminal.screens()), 1)

    def test_one_draft_shows_no_option_label(self):
        terminal = FakeTerminal([OK_REPLY, b"\r"])
        mockup.view(self.paths[:1], terminal=terminal)
        screen, = terminal.screens()
        self.assertNotIn("Option", screen)
        self.assertNotIn("←/→", screen)
        self.assertIn("Enter close", screen)

    def test_no_reply_opens_every_draft(self):
        terminal = FakeTerminal([b"", b"\r"])
        mockup.view(self.paths, terminal=terminal)
        self.assertEqual(self.opened, self.paths)
        text = terminal.text()
        self.assertNotIn("a=T", text)
        for index, path in enumerate(self.paths, 1):
            self.assertIn(f"{index}. {path}", text)

    def test_closes_its_pane_at_the_end(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p9"}):
            mockup.view(self.paths, terminal=FakeTerminal([OK_REPLY, b"\r"]))
        self.assertEqual(self.herdr.calls, [["pane", "close", "w1:p9"]])

    def test_image_fits_the_terminal(self):
        terminal = FakeTerminal([OK_REPLY, b"\r"], size=(80, 24, 800, 480))
        tall = self.root / "tall.png"
        # A 100x200 PNG header: only the size fields matter to the viewer.
        tall.write_bytes(PNG[:16] + (100).to_bytes(4, "big") + (200).to_bytes(4, "big") + PNG[24:])
        mockup.view([tall], terminal=terminal)
        first = [part for part in terminal.screens()[0].split("\033_G")[1:] if not part.startswith("a=d")][0]
        controls = dict(item.split("=") for item in first.split(";", 1)[0].split(","))
        # Cells are 10x20 pixels; 20 rows are free for the image.
        self.assertEqual(controls["r"], "20")
        self.assertEqual(controls["c"], "20")


class DiscardTests(MockupTestCase):
    def test_removes_the_drafts_folder(self):
        folder = mockup.drafts_dir("tabc")
        folder.mkdir(parents=True)
        (folder / "x.png").write_bytes(PNG)
        mockup.discard_drafts("tabc")
        self.assertFalse(folder.exists())

    def test_missing_folder_is_no_error(self):
        mockup.discard_drafts("tabc")

    def test_drafts_folder_is_in_the_state_folder(self):
        self.assertEqual(mockup.drafts_dir("ACME-12"), self.root / "state" / "herdboard" / "mockups" / "ACME-12")


class MockupCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.codex = FakeCodex(Path(self.tmp.name) / "codex")
        self.enterContext(mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex.codex_home)}))
        os.environ.pop("HERDR_PANE_ID", None)
        self.enterContext(mock.patch.object(start, "command", self.codex))
        self.enterContext(mock.patch.object(mockup.shutil, "which", return_value="/usr/bin/codex"))
        self.opened = []
        self.enterContext(mock.patch.object(mockup, "open_file", self.opened.append))
        self.ref = self.run_json("add", "Login page")["card"]["ref"]

    def test_json_result_lists_the_drafts_and_logs_them(self):
        self.codex.behavior["Top tabs"] = "fail"
        data = self.run_json("mockup", self.ref, "Sidebar layout", "Top tabs layout")
        self.assertEqual(data["ref"], self.ref)
        draft, = data["drafts"]
        self.assertEqual((draft["option"], draft["prompt"]), (1, "Sidebar layout"))
        self.assertTrue(Path(draft["path"]).is_file())
        failed, = data["failed"]
        self.assertEqual((failed["option"], failed["prompt"]), (2, "Top tabs layout"))
        self.assertIn("model_not_found", failed["error"])
        self.assertEqual(data["shown"], "xdg-open")
        self.assertEqual(self.opened, [Path(draft["path"])])
        log = self.run_json("show", self.ref)["log"]
        self.assertEqual(log[-1]["message"], f"Mockup draft 1: {draft['path']}")

    def test_no_show_shows_nothing(self):
        data = self.run_json("mockup", self.ref, "Sidebar layout", "--no-show", "--quality", "high")
        self.assertIsNone(data["shown"])
        self.assertEqual(self.opened, [])
        self.assertIn("high quality", self.codex.calls[0]["argv"][-1])

    def test_relative_images_resolve_from_the_directory(self):
        (self.plain / "shot.png").write_bytes(PNG)
        self.run_json("mockup", self.ref, "Revise it", "--image", "shot.png", "--no-show")
        argv = self.codex.calls[0]["argv"]
        self.assertEqual(argv[argv.index("-i") + 1], str(self.plain / "shot.png"))

    def test_more_than_four_prompts_is_a_usage_error(self):
        data = self.run_json("mockup", self.ref, "a", "b", "c", "d", "e", expect=1)
        self.assertEqual(data["error"]["code"], "usage")
        self.assertEqual(self.codex.calls, [])

    def test_text_output(self):
        out, _ = self.run_cli("mockup", self.ref, "Sidebar layout", "--no-show")
        self.assertRegex(out, rf"^{self.ref}: option 1: .+sidebar-layout\.png\n$")

    def test_mockup_view_runs_the_viewer(self):
        with mock.patch.object(mockup, "view", return_value=0) as view:
            self.run_cli("mockup-view", "/tmp/a.png", "/tmp/b.png")
        view.assert_called_once_with([Path("/tmp/a.png"), Path("/tmp/b.png")], numbers=None, total=None)

    def test_mockup_view_passes_the_option_numbers(self):
        with mock.patch.object(mockup, "view", return_value=0) as view:
            self.run_cli("mockup-view", "--option", "2", "--option", "3", "--total", "3", "/tmp/a.png", "/tmp/b.png")
        view.assert_called_once_with([Path("/tmp/a.png"), Path("/tmp/b.png")], numbers=[2, 3], total=3)

    def test_a_failed_first_option_keeps_the_numbers_in_the_pane(self):
        self.codex.behavior["Sidebar"] = "fail"
        with mock.patch.object(mockup, "show", return_value="pane") as show:
            data = self.run_json("mockup", self.ref, "Sidebar layout", "Top tabs layout", "Card grid")
        self.assertEqual([draft["option"] for draft in data["drafts"]], [2, 3])
        show.assert_called_once_with([draft["path"] for draft in data["drafts"]], mock.ANY, numbers=[2, 3], total=3)


if __name__ == "__main__":
    unittest.main()
