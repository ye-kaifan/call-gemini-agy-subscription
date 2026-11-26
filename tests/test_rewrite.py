"""Project evidence, rewrite receipts, and optional real macOS policy checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import rewrite


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project, self.review, self.run = [self.base / n for n in ("project", "review", "run")]
        for p in (self.project, self.review, self.run): p.mkdir()

    def test_evidence_uses_current_worktree_and_excludes_private_and_raw_files(self):
        (self.project / "current.txt").write_text("working copy")
        (self.project / "raw.h5").write_bytes(b"raw data")
        (self.project / ".env").write_text("private fixture")
        (self.project / "link").symlink_to(self.base / "outside.txt")
        (self.base / "outside.txt").write_text("outside fixture")
        with contextlib.redirect_stderr(io.StringIO()):
            records = rewrite.evidence(self.project, ["current.txt", "raw.h5"], self.review)
        self.assertEqual(Path(records[0]["snapshot"]).read_text(), "working copy")
        self.assertNotIn("snapshot", records[1])
        for name in (".env", "link", "../outside.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                rewrite.evidence(self.project, [name], self.review)

    def test_query_search_stays_in_project_and_filters_exclusions(self):
        (self.project / "nested").mkdir()
        (self.project / "nested/source.txt").write_text("unique needle")
        (self.project / ".env").write_text("unique needle")
        (self.project / "private.key").write_text("unique needle")
        (self.project / "raw.H5").write_text("unique needle")
        (self.project / "external").symlink_to(self.base, target_is_directory=True)
        result = rewrite.query_project(self.project, {"op": "search", "query": "unique needle"},
                                       time.monotonic() + 5)
        self.assertEqual(result["paths"], ["nested/source.txt"])
        for name in ("..", "external"):
            with self.assertRaises(ValueError):
                rewrite.query_project(self.project, {"op": "list", "path": name}, time.monotonic() + 5)

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").is_file(), "rewrite is macOS only")
    def test_rewrite_records_failed_turn_and_preserves_model_preflight_sandbox(self):
        receipt = self.base / "failed.json"
        def fail(*args, trace=None, checkpoint=None, **kwargs):
            trace.update(conversation_id="64a38e8b-3b95-4e4c-911a-aa0fc63172ff",
                         tools=[{"tool": "view_file", "state": "DONE", "output": "source evidence"}])
            checkpoint()
            raise ValueError("Google 返回额度或频率限制")
        with patch.object(rewrite, "agy_executable", return_value="/usr/bin/false"), \
                patch.object(rewrite, "select_model", return_value="gemini-test") as select, \
                patch.object(rewrite, "run_turn", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "额度"):
                rewrite.call_agy(None, "draft", self.review, self.run, 5, receipt=receipt)
            self.assertEqual(select.call_args.kwargs["command_prefix"][0], "/usr/bin/sandbox-exec")
        record = json.loads(receipt.read_text())
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["tools"][0]["output"], "source evidence")
        self.assertEqual(record["turns"][0]["tools"], record["tools"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").is_file(), "rewrite is macOS only")
    def test_query_and_revision_context_and_trace(self):
        replies = ['AGY_PROJECT_QUERY {"op":"find","query":"*.txt"}',
                   "第一版遗漏校验词。", "终稿，保留 CHECK-123。\n"]
        (self.project / "answer.txt").write_text("CHECK-123")
        prompts = []
        def answer(args, prompt, *rest, trace=None, checkpoint=None, **kwargs):
            prompts.append(prompt)
            result = {"status": "SUCCESS", "response": replies[len(prompts) - 1]}
            trace.update(tools=[], result=result, init={})
            checkpoint()
            return {}, result, []
        receipt = self.base / "success.json"
        with patch.object(rewrite, "agy_executable", return_value="/usr/bin/false"), \
                patch.object(rewrite, "select_model", return_value="gemini-test"), \
                patch.object(rewrite, "run_turn", side_effect=answer):
            response = rewrite.call_agy("gemini-test", "original draft", self.review, self.run, 5,
                                        project_root=self.project, receipt=receipt, verbatim=["CHECK-123"])
        self.assertEqual(response, replies[-1])
        self.assertIn("answer.txt", prompts[1])
        self.assertIn("第一版遗漏校验词", prompts[2])
        record = json.loads(receipt.read_text())
        self.assertEqual(len(record["turns"]), 3)
        self.assertEqual(record["tools"][0]["result"]["paths"], ["answer.txt"])
        self.assertEqual(record["result"]["response"], response)

    @unittest.skipUnless(os.environ.get("RUN_MACOS_SANDBOX_TESTS") == "1", "opt-in real Seatbelt check")
    def test_macos_policy_blocks_writes_and_child_commands(self):
        source = self.project / "source.txt"
        source.write_text("unchanged")
        secret = self.project / ".env"
        secret.write_text("synthetic private fixture")
        interpreter = str(Path(sys.executable).resolve())
        code = '''
import pathlib, subprocess, sys
for target in sys.argv[1:3]:
    try: pathlib.Path(target).write_text('should fail')
    except PermissionError: pass
    else: raise SystemExit('unexpected write: ' + target)
try: pathlib.Path(sys.argv[3]).read_text()
except PermissionError: pass
else: raise SystemExit('unexpected private read')
try: subprocess.run(['/usr/bin/true'])
except PermissionError: pass
else: raise SystemExit('unexpected child execution')
pathlib.Path(sys.argv[4]).write_text('runtime allowed')
'''
        subprocess.run(["/usr/bin/sandbox-exec", "-D", f"AGY={interpreter}", "-D", f"RUN={self.run}",
                        "-D", "EXCLUDED=" + rewrite.excluded_pattern(self.project, rewrite.KEY_SUFFIXES | rewrite.RAW),
                        "-f", str(SCRIPTS / "readonly.sb"), interpreter, "-B", "-c", code,
                        str(source), str(self.project / "new.txt"), str(secret), str(self.run / "ok")], check=True)
        self.assertEqual(source.read_text(), "unchanged")
        self.assertFalse((self.project / "new.txt").exists())
        self.assertTrue((self.run / "ok").is_file())


if __name__ == "__main__":
    unittest.main()
