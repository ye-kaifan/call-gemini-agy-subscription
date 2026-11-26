"""Real subprocess/stdio behavior against a fake Codex executable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from app_server import AppServer, RpcError, codex_executable

FAKE = r'''
import json,os,sys,time
from pathlib import Path
home=Path(os.environ['CODEX_HOME'])
(home/'pid').write_text(str(os.getpid()))
assert sys.argv[1:] == ['app-server','--listen','stdio://']
mode=os.environ.get('RPC_FIXTURE_MODE','success')
initialized=False
for line in sys.stdin:
    req=json.loads(line)
    with (home/'requests.jsonl').open('a') as f:f.write(json.dumps(req)+'\n')
    method=req.get('method')
    if method == 'initialized':
        initialized=True
        continue
    if method == 'initialize':
        result={'codexHome':str(home if mode != 'wrong_home' else home/'wrong')}
    else:
        assert initialized
        if mode == 'timeout':time.sleep(30)
        if mode == 'exit':sys.exit(2)
        if mode == 'malformed':
            print('{broken',flush=True)
            continue
        if mode == 'array':
            print('[]',flush=True)
            continue
        if mode == 'error':
            print(json.dumps({'id':req['id'],'error':{'code':-32601,'message':'unsupported'}}),flush=True)
            continue
        if mode == 'large_stderr':
            sys.stderr.write('diagnostic\n'*20000);sys.stderr.flush()
        print(json.dumps({'method':'irrelevant/event','params':{}}),flush=True)
        if mode == 'server_request':
            print(json.dumps({'id':'server-action','method':'exec/approval','params':{}}),flush=True)
            reply=json.loads(sys.stdin.readline())
            (home/'declined.json').write_text(json.dumps(reply))
        result={'data':[],'nextCursor':None}
    response={'id':req['id']+100 if mode == 'wrong_id' else req['id'],'result':result}
    raw=json.dumps(response)+'\n'
    if mode == 'fragmented':
        for char in raw:sys.stdout.write(char);sys.stdout.flush()
    else:print(raw,end='',flush=True)
'''


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.executable = self.home / "codex"
        self.executable.write_text(f"#!{sys.executable}\n" + FAKE)
        self.executable.chmod(0o700)
        self.client = AppServer(self.home, executable=str(self.executable), timeout=3)
        self.addCleanup(self.client.close)
        self.env = patch.dict(os.environ, RPC_FIXTURE_MODE="success")
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_handshake_reuses_process_and_shuts_down(self):
        with self.client:
            for _ in range(3):
                self.assertEqual(self.client.request("thread/list", {}), {"data": [], "nextCursor": None})
            proc = self.client.proc
        requests = [json.loads(s) for s in (self.home / "requests.jsonl").read_text().splitlines()]
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized"] + ["thread/list"] * 3)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(proc.returncode, 0)

    def test_non_read_methods_are_rejected_before_start(self):
        for method in ("thread/start", "thread/resume", "turn/start", "thread/delete", "command/exec"):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "仅允许"):
                self.client.request(method, {})
        self.assertFalse((self.home / "pid").exists())

    def test_notifications_partial_frames_and_large_stderr(self):
        for mode in ("fragmented", "large_stderr", "server_request"):
            with self.subTest(mode=mode), patch.dict(os.environ, RPC_FIXTURE_MODE=mode):
                self.client.close()
                self.assertEqual(self.client.request("thread/list", {})["data"], [])
                self.assertLessEqual(len(self.client.stderr), 65536)
        reply = json.loads((self.home / "declined.json").read_text())
        self.assertEqual(reply["error"]["code"], -32601)

    def test_timeout_and_bad_frames_close_child(self):
        for mode in ("timeout", "malformed", "array", "exit", "wrong_home", "wrong_id"):
            with self.subTest(mode=mode), patch.dict(os.environ, RPC_FIXTURE_MODE=mode):
                self.client.close()
                self.client.timeout = 3
                if mode == "timeout":
                    self.client.start()
                    self.client.timeout = .15
                with self.assertRaises(ValueError):
                    self.client.request("thread/list", {})
                self.assertIsNone(self.client.proc)
                pid = int((self.home / "pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_rpc_error_does_not_retry(self):
        with patch.dict(os.environ, RPC_FIXTURE_MODE="error"), self.assertRaises(RpcError):
            self.client.request("thread/items/list", {})
        requests = [json.loads(s) for s in (self.home / "requests.jsonl").read_text().splitlines()]
        self.assertEqual(sum(r["method"] == "thread/items/list" for r in requests), 1)

    def test_missing_binary_and_invalid_override_are_actionable(self):
        with patch.dict(os.environ, CODEX_HISTORY_BIN="relative/path"), self.assertRaises(ValueError):
            codex_executable()
        with patch.dict(os.environ, CODEX_HISTORY_BIN="", PATH=""), self.assertRaisesRegex(ValueError, "Codex CLI"):
            codex_executable()

    def test_mcp_tools_and_read_errors_do_not_break_transport(self):
        messages = [{"id": 1, "method": "initialize"}, {"method": "notifications/initialized"},
                    {"id": 2, "method": "tools/list"},
                    {"id": 3, "method": "tools/call", "params": {"name": "list_codex_threads", "arguments": {}}},
                    {"id": 4, "method": "tools/call", "params": {"name": "execute_sql", "arguments": {}}},
                    {"id": 5, "method": "ping"}]
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "history.py"), "--home", str(self.home), "serve"],
                                input="".join(json.dumps(r) + "\n" for r in messages), text=True,
                                capture_output=True, timeout=5,
                                env=dict(os.environ, CODEX_HISTORY_BIN=str(self.executable)))
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"])["threads"], [])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(replies[4]["result"], {})
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.home / "pid").read_text()), 0)


if __name__ == "__main__":
    unittest.main()
