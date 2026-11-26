"""Session and stream behavior without network access."""
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
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agy_runtime as runtime
import call as client

CONVERSATION = "64a38e8b-3b95-4e4c-911a-aa0fc63172ff"
REPLY = "  正文\n第二行\n"
FAKE = r'''
import json, os, sys, time
from pathlib import Path
CID = os.environ.get("FIXTURE_CID", "64a38e8b-3b95-4e4c-911a-aa0fc63172ff")
def emit(kind, payload):
    print(json.dumps({"event": kind, kind: payload, "conversation_id": CID}), flush=True)
if "--print" in sys.argv:
    print(os.environ.get("FIXTURE_MODEL", "gemini-test") + "\tA model label")
    sys.exit(0)
message = json.loads(sys.stdin.readline())["message"]["content"]
actual_cwd = str(Path.cwd())
if "--add-dir" in sys.argv:
    os.chdir(sys.argv[sys.argv.index("--add-dir") + 1])
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert "GEMINI_API_KEY" not in os.environ
with Path("runs").open("a") as f: f.write("once\n")
Path("received").write_text(message)
Path("command.json").write_text(json.dumps(sys.argv))
mode = os.environ.get("FIXTURE_MODE", "success")
emit("init", {"model": "gemini-test", "cwd": actual_cwd})
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


class CallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace, self.session = self.base / "项目 with spaces", self.base / "private-session"
        self.workspace.mkdir()
        self.cli = self.base / "agy"
        self.cli.write_text(f"#!{sys.executable}\n" + FAKE)
        self.cli.chmod(0o700)
        self.args = SimpleNamespace(session=self.session, workspace=self.workspace, no_project=False,
                                    model="gemini-test", execution_mode=None, no_history=None,
                                    codex_home=self.base / "synthetic-codex", timeout=4)
        self.addCleanup(patch.stopall)
        patch.object(client, "agy_executable", return_value=str(self.cli)).start()
        patch.object(runtime, "configured_model", return_value=None).start()
        patch.dict(os.environ, FIXTURE_MODE="success", GEMINI_API_KEY="unused-test-key").start()
        self.diagnostics = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.diagnostics)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def record(self):
        path = sorted((self.session / "turns").glob("*.json"))[-1]
        return json.loads(path.read_text())

    def test_verbatim_reply_project_scope_and_private_receipts(self):
        message = '/literal\n{"value": "$name 和 `code`"}\n'
        self.assertEqual(client.call(self.args, message), REPLY)
        self.assertTrue((self.session / "received").read_text().endswith(message))
        receipt = self.record()
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(len(receipt["tools"]), 1)
        self.assertEqual(receipt["tools"][0]["output"], "1 test passed\n")
        self.assertEqual(receipt["tools"][0]["parameters"], {"CommandLine": "test"})
        self.assertEqual((self.session / "turns/0001.txt").read_bytes(), REPLY.encode())
        for path in (self.session / "session.json", self.session / "turns/0001.json", self.session / "turns/0001.txt"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        command = json.loads((self.session / "command.json").read_text())
        self.assertEqual(command[command.index("--add-dir") + 1], str(self.session))
        self.assertEqual(receipt["init"]["cwd"], str(self.workspace))
        self.assertEqual(os.environ["GEMINI_API_KEY"], "unused-test-key")

    def test_session_resumes_automatically_and_preserves_model_and_mode(self):
        self.args.execution_mode = "accept-edits"
        client.call(self.args, "first")
        first = (self.session / "turns/0001.json").read_bytes()
        self.args.workspace = self.args.model = self.args.execution_mode = self.args.codex_home = None
        client.call(self.args, "second")
        self.assertEqual(self.record()["requested_conversation_id"], CONVERSATION)
        self.assertEqual(self.record()["model"], "gemini-test")
        self.assertEqual(self.record()["execution_mode"], "accept-edits")
        self.assertEqual(first, (self.session / "turns/0001.json").read_bytes())
        command = json.loads((self.session / "command.json").read_text())
        self.assertEqual(command[command.index("--conversation") + 1], CONVERSATION)

    def test_another_project_or_history_store_requires_new_session(self):
        client.call(self.args, "first")
        self.args.workspace = self.base
        with self.assertRaisesRegex(ValueError, "绑定"):
            client.call(self.args, "must not run")
        self.args.workspace, self.args.codex_home = self.workspace, self.base
        with self.assertRaisesRegex(ValueError, "绑定"):
            client.call(self.args, "must not run")
        self.assertEqual((self.session / "runs").read_text(), "once\n")

    def test_concurrent_send_is_rejected(self):
        with client.session_lock(self.session), self.assertRaisesRegex(ValueError, "正在运行"):
            client.call(self.args, "must not run")
        self.assertFalse((self.session / "runs").exists())

    def test_existing_directory_is_not_overwritten(self):
        self.session.mkdir()
        file = self.session / "keep.txt"
        file.write_text("unrelated data")
        with self.assertRaisesRegex(ValueError, "空的专属目录"):
            client.call(self.args, "must not run")
        self.assertEqual(file.read_text(), "unrelated data")
        self.assertFalse((self.session / ".agents").exists())

    def test_mismatched_id_does_not_replace_saved_conversation(self):
        client.call(self.args, "first")
        with patch.dict(os.environ, FIXTURE_CID="12345678-1234-1234-1234-123456789012"):
            with self.assertRaisesRegex(ValueError, "会话 ID"):
                client.call(self.args, "second")
        self.assertEqual(json.loads((self.session / "session.json").read_text())["conversation_id"], CONVERSATION)

    def test_no_project_and_history_disabled(self):
        self.args.workspace, self.args.no_project, self.args.no_history = None, True, True
        client.call(self.args, "chat")
        self.assertIsNone(self.record()["workspace"])
        config = json.loads((self.session / ".agents/mcp_config.json").read_text())
        self.assertEqual(config["mcpServers"], {})
        self.args.no_project, self.args.no_history = False, None
        client.call(self.args, "continue")
        self.assertFalse(self.record()["history_enabled"])

    def test_failures_keep_inflight_trace_without_replaying(self):
        for mode, expected in (("quota", "额度"), ("empty", "空回复"), ("malformed", "无法解析"),
                               ("array", "JSON 对象"), ("bad_payload", "载荷"), ("timeout", "秒内未完成")):
            with self.subTest(mode=mode), patch.dict(os.environ, FIXTURE_MODE=mode):
                self.args.timeout = 0.25 if mode == "timeout" else 4
                with self.assertRaisesRegex(ValueError, expected):
                    client.call(self.args, mode)
                self.assertEqual(self.record()["status"], "error")
                self.assertEqual(self.record()["conversation_id"], CONVERSATION)
                self.assertTrue(self.record()["tools"])
                if mode == "timeout":
                    self.assertEqual(self.record()["tools"][0]["state"], "ACTIVE")
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((self.session / "pid").read_text()), 0)
        self.assertEqual((self.session / "runs").read_text().splitlines(), ["once"] * 6)

    def test_denials_remain_visible_even_with_success_status(self):
        for mode in ("denied", "denied_actions"):
            with patch.dict(os.environ, FIXTURE_MODE=mode):
                client.call(self.args, mode)
        self.assertIn("soft-denied", self.diagnostics.getvalue())
        self.assertIn("权限策略拒绝", self.record()["warnings"][0])

    def test_invalid_model_and_missing_or_wrong_conversation_id_fail(self):
        self.args.model = None
        with patch.dict(os.environ, FIXTURE_MODEL="claude-test"), self.assertRaisesRegex(ValueError, "Gemini 模型"):
            client.call(self.args, "message")
        self.assertFalse((self.session / "runs").exists())
        for name in ("claude-test", "gemini-api://paid", "", None, "gemini-test\n--auto"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.validate_model(name)
        for result in ({"status": "SUCCESS", "response": "ok"}, {"status": "SUCCESS", "response": "ok", "conversation_id": CONVERSATION}):
            command = [sys.executable, "-c", f"print({json.dumps({'event': 'result', 'result': result})!r})"]
            with self.assertRaisesRegex(ValueError, "会话 ID"):
                runtime.run_turn(command, "message", self.session, runtime.subscription_env(), 3,
                                 expected_conversation="12345678-1234-1234-1234-123456789012")

    def test_input_limits_and_no_receipt_overwrite(self):
        self.assertEqual(runtime.read_message(io.BytesIO(" hello 中文\n".encode())), " hello 中文\n")
        for raw in (b"", b" \n", b"\xff", b"x" * (runtime.INPUT_LIMIT + 1)):
            with self.assertRaises(ValueError):
                runtime.read_message(io.BytesIO(raw))
        path = self.base / "existing.json"
        path.write_text("keep")
        with self.assertRaises(FileExistsError), runtime.Receipt(path, {}):
            self.fail("existing receipt was overwritten")
        self.assertEqual(path.read_text(), "keep")

    def test_exited_process_cleanup_does_not_mask_cli_error(self):
        proc = Mock(pid=12345)
        proc.poll.return_value = 0
        with patch.object(runtime.os, "killpg", side_effect=PermissionError):
            runtime.stop_process(proc)
            proc.poll.return_value = None
            with self.assertRaises(PermissionError):
                runtime.stop_process(proc)

    def test_sigterm_stops_child_and_keeps_session_id(self):
        code = f'''
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import call
call.agy_executable = lambda: {str(self.cli)!r}
sys.argv = ['call', '--workspace', {str(self.workspace)!r}, '--model', 'gemini-test', '--session', {str(self.session)!r}]
try:
    with call.handle_termination(): call.main()
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
                if (self.session / "pid").exists():
                    try:
                        if self.record().get("tools"):
                            break
                    except (IndexError, json.JSONDecodeError):
                        pass
                time.sleep(0.02)
            else:
                self.fail("fixture did not start")
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(out, b"")
            self.assertEqual(proc.returncode, 130, err.decode())
            self.assertEqual(self.record()["status"], "error")
            self.assertEqual(self.record()["tools"][0]["state"], "ACTIVE")
            self.assertEqual(json.loads((self.session / "session.json").read_text())["conversation_id"], CONVERSATION)
            with self.assertRaises(ProcessLookupError):
                os.kill(int((self.session / "pid").read_text()), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


if __name__ == "__main__":
    unittest.main()
