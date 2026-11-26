"""Offline behavior tests. No Google requests or user history are used."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_runtime as runtime
import relay
import rewrite

CONVERSATION = "64a38e8b-3b95-4e4c-911a-aa0fc63172ff"
REPLY = "  正文\n第二行\n"
FAKE = r'''
import json, os, sys, time
from pathlib import Path
CID = "64a38e8b-3b95-4e4c-911a-aa0fc63172ff"
def emit(kind, payload):
    print(json.dumps({"event": kind, kind: payload, "conversation_id": CID}), flush=True)
if "--print" in sys.argv:
    print(os.environ.get("FIXTURE_MODEL", "gemini-test") + "\tA model label")
    sys.exit(0)
message = json.loads(sys.stdin.readline())["message"]["content"]
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert "GEMINI_API_KEY" not in os.environ
with Path("runs").open("a") as f: f.write("once\n")
Path("received").write_text(message)
mode = os.environ.get("FIXTURE_MODE", "success")
emit("init", {"model": "gemini-test", "cwd": str(Path.cwd())})
emit("step_update", {"step_index": 2, "state": "ACTIVE", "tool_name": "run_command",
                     "tool_info": {"parameters": {"CommandLine": "test"}}})
if mode == "timeout":
    Path("pid").write_text(str(os.getpid()))
    time.sleep(30)
if mode == "malformed":
    print("{broken", flush=True)
    time.sleep(30)
if mode == "array":
    print("[]", flush=True)
    sys.exit(0)
if mode == "bad_payload":
    emit("result", [])
    sys.exit(0)
emit("step_update", {"step_index": 2, "state": "DONE", "tool_name": "run_command",
                     "tool_info": {"output": "1 test passed\n"}})
if mode == "denied": print("Tool write_file soft-denied: requires approval", file=sys.stderr)
if mode == "quota":
    emit("result", {"status": "ERROR", "error": "HTTP 429 quota"})
    sys.exit(1)
response = "" if mode == "empty" else "  正文\n第二行\n"
result = {"status": "SUCCESS", "conversation_id": CID, "response": response}
if mode == "denied_actions": result["denied_actions"] = [{"action": "command"}]
emit("result", result)
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "项目 with spaces"
        self.workspace.mkdir()
        self.cli = self.base / "agy"
        self.cli.write_text(f"#!{sys.executable}\n" + FAKE)
        self.cli.chmod(0o700)
        self.args = SimpleNamespace(workspace=self.workspace, no_project=False, model="gemini-test",
                                    history_server=relay.HISTORY_SERVER, conversation=None,
                                    resume_receipt=None, receipt=self.base / "turn.json", timeout=4)
        self.addCleanup(patch.stopall)
        patch.object(relay, "agy_executable", return_value=str(self.cli)).start()
        patch.object(runtime, "configured_model", return_value=None).start()
        patch.dict(os.environ, FIXTURE_MODE="success", GEMINI_API_KEY="unused-test-key").start()
        self.diagnostics = io.StringIO()
        # Hold one redirect context throughout the test.
        self.redirect = contextlib.redirect_stderr(self.diagnostics)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def record(self):
        return json.loads(self.args.receipt.read_text())

    def test_message_reply_and_tool_output_are_preserved(self):
        message = '/literal\n{"value": "$name 和 `code`"}\n'
        self.assertEqual(relay.relay(self.args, message), REPLY)
        self.assertTrue((self.workspace / "received").read_text().endswith(message))
        receipt = self.record()
        self.assertEqual(receipt["result"]["response"].encode(), REPLY.encode())
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(len(receipt["tools"]), 1)
        self.assertEqual(receipt["tools"][0]["output"], "1 test passed\n")
        self.assertEqual(receipt["tools"][0]["parameters"], {"CommandLine": "test"})
        self.assertEqual(self.args.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["GEMINI_API_KEY"], "unused-test-key")

    def test_existing_receipt_prevents_any_cli_start(self):
        self.args.receipt.write_text("keep")
        with patch.object(relay, "select_model") as select:
            with self.assertRaises(FileExistsError):
                relay.relay(self.args, "message")
            select.assert_not_called()
        self.assertFalse((self.workspace / "runs").exists())
        self.assertEqual(self.args.receipt.read_text(), "keep")

    def test_errors_keep_trace_without_replaying_edits(self):
        for mode, expected in (("quota", "额度"), ("empty", "空回复"),
                               ("malformed", "无法解析"), ("array", "JSON 对象"),
                               ("bad_payload", "载荷"), ("timeout", "秒内未完成")):
            with self.subTest(mode=mode), patch.dict(os.environ, FIXTURE_MODE=mode):
                self.args.receipt = self.base / (mode + ".json")
                self.args.timeout = 0.25 if mode == "timeout" else 4
                with self.assertRaisesRegex(ValueError, expected):
                    relay.relay(self.args, mode)
                rec = self.record()
                self.assertEqual(rec["status"], "error")
                self.assertEqual(rec["conversation_id"], CONVERSATION)
                self.assertTrue(rec["tools"])
                if mode == "timeout":
                    self.assertEqual(rec["tools"][0]["state"], "ACTIVE")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((self.workspace / "pid").read_text()), 0)
        self.assertEqual((self.workspace / "runs").read_text().splitlines(), ["once"] * 6)

    def test_denied_tool_diagnostic_survives_success(self):
        with patch.dict(os.environ, FIXTURE_MODE="denied"):
            relay.relay(self.args, "message")
        self.assertIn("soft-denied", self.record()["stderr"])
        self.assertIn("soft-denied", self.diagnostics.getvalue())

    def test_success_with_denied_actions_still_warns(self):
        with patch.dict(os.environ, FIXTURE_MODE="denied_actions"):
            self.assertEqual(relay.relay(self.args, "message"), REPLY)
        self.assertIn("权限策略拒绝", self.record()["warnings"][0])
        self.assertIn("权限策略拒绝", self.diagnostics.getvalue())

    def test_resume_receipt_pins_model_and_rejects_another_project(self):
        relay.relay(self.args, "first")
        self.args.resume_receipt = self.args.receipt
        self.args.receipt = self.base / "second.json"
        self.args.model = None
        self.assertEqual(relay.relay(self.args, "second"), REPLY)
        self.assertEqual(self.record()["requested_conversation_id"], CONVERSATION)
        self.assertEqual(self.record()["model"], "gemini-test")
        self.args.workspace = self.base
        with self.assertRaisesRegex(ValueError, "工作区与回执不一致"):
            relay.relay(self.args, "must not run")
        self.assertFalse((self.base / "runs").exists())

    def test_wrong_conversation_id_fails(self):
        self.args.conversation = UUID("12345678-1234-1234-1234-123456789012")
        with self.assertRaisesRegex(ValueError, "会话 ID 与预期不符"):
            relay.relay(self.args, "message")
        self.assertEqual(self.record()["status"], "error")

    def test_resume_preserves_restricted_execution_mode(self):
        self.args.execution_mode = "accept-edits"
        relay.relay(self.args, "first")
        self.args.resume_receipt = self.args.receipt
        self.args.receipt = self.base / "second.json"
        self.args.execution_mode = None
        relay.relay(self.args, "second")
        self.assertEqual(self.record()["execution_mode"], "accept-edits")

    def test_missing_conversation_id_cannot_pass_as_a_continuation(self):
        command = [sys.executable, "-c",
                   'print(\'{"event":"result","result":{"status":"SUCCESS","response":"ok"}}\')']
        with self.assertRaisesRegex(ValueError, "未返回预期的会话 ID"):
            runtime.run_turn(command, "message", self.workspace, runtime.subscription_env(), 3,
                             expected_conversation=CONVERSATION)

    def test_non_gemini_current_model_is_rejected_in_both_entries(self):
        self.args.model = None
        with patch.dict(os.environ, FIXTURE_MODEL="claude-test"):
            with self.assertRaisesRegex(ValueError, "Gemini 模型"):
                relay.relay(self.args, "message")
        self.assertFalse((self.workspace / "runs").exists())
        for name in ("claude-test", "gemini-api://paid", "", None, "gemini-test\n--auto"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.validate_model(name)
        if Path("/usr/bin/sandbox-exec").is_file():
            review, run = self.base / "review", self.base / "runtime"
            review.mkdir()
            run.mkdir()
            with patch.object(rewrite, "agy_executable", return_value=str(self.cli)):
                with self.assertRaisesRegex(ValueError, "Gemini 模型"):
                    rewrite.call_agy("claude-test", "draft", review, run, 3)
            self.assertFalse((review / "runs").exists())

    def test_rewrite_reserves_receipt_before_model_resolution(self):
        self.args.receipt.write_text("keep")
        with patch.object(rewrite, "select_model") as select:
            with self.assertRaises(FileExistsError):
                rewrite.call_agy(None, "draft", self.workspace, self.base, 4, receipt=self.args.receipt)
            select.assert_not_called()

    def test_input_size_and_utf8(self):
        self.assertEqual(runtime.read_message(io.BytesIO(" hello 中文\n".encode())), " hello 中文\n")
        for raw in (b"", b" \n", b"\xff", b"x" * (runtime.INPUT_LIMIT + 1)):
            with self.assertRaises(ValueError):
                runtime.read_message(io.BytesIO(raw))

    def test_codex_database_directory_is_not_a_project(self):
        codex = self.base / "codex-home"
        (codex / "subdir").mkdir(parents=True)
        with patch.dict(os.environ, CODEX_HOME=str(codex)):
            for path in (codex, codex / "subdir", self.base):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    runtime.project_directory(path)

    def test_sigterm_saves_inflight_trace_and_stops_cli(self):
        code = f'''
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import relay
relay.agy_executable = lambda: {str(self.cli)!r}
sys.argv = ['relay', '--workspace', {str(self.workspace)!r}, '--model', 'gemini-test',
            '--receipt', {str(self.args.receipt)!r}]
try:
    with relay.handle_termination(): relay.main()
except KeyboardInterrupt: sys.exit(130)
'''
        proc = subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(os.environ, FIXTURE_MODE="timeout"))
        try:
            proc.stdin.write(b"synthetic test\n")
            proc.stdin.close()
            proc.stdin = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (self.workspace / "pid").exists() and self.args.receipt.exists():
                    try:
                        if self.record().get("tools"): break
                    except json.JSONDecodeError:
                        pass  # A checkpoint may be in progress.
                time.sleep(0.02)
            else:
                self.fail("fixture did not start")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(out, b"")
            self.assertEqual(proc.returncode, 130, err.decode())
            self.assertEqual(self.record()["status"], "error")
            self.assertEqual(self.record()["tools"][0]["state"], "ACTIVE")
            with self.assertRaises(ProcessLookupError):
                os.kill(int((self.workspace / "pid").read_text()), 0)
        finally:
            if proc.poll() is None: proc.kill()
            proc.communicate()


if __name__ == "__main__":
    unittest.main()
