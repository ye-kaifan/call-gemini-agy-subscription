"""Synthetic history only. Tests do not read a user's Codex store."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from history import History, cursor_decode, scope_key


class Store:
    def __init__(self, root):
        self.root = root
        root.mkdir(exist_ok=True)
        with sqlite3.connect(root / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,title TEXT,cwd TEXT,archived INTEGER,source TEXT,history_mode TEXT)")
        with sqlite3.connect(root / "thread_history_1.sqlite") as db:
            db.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT,item_type TEXT)")

    def add(self, texts, *, archived=False, paginated=False, indexed=True, title="Synthetic history", source="cli"):
        ident, turn = str(uuid4()), str(uuid4())
        folder = self.root / ("archived_sessions" if archived else "sessions/2026/11/26")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-11-26T00-00-00-{ident}.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": ident, "cwd": "/synthetic/another-project", "source": source}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}]
        for n, text in enumerate(texts):
            role = "user" if n % 2 == 0 else "assistant"
            rows += [{"type": "response_item", "payload": {"type": "message", "role": role,
                      "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}},
                     {"type": "event_msg", "payload": {"type": "user_message" if role == "user" else "agent_message", "message": text}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if indexed:
            with sqlite3.connect(self.root / "state_5.sqlite") as db:
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (ident, str(path), title,
                           "/synthetic/another-project", int(archived), source, "paginated" if paginated else "legacy"))
        if paginated:
            with sqlite3.connect(self.root / "thread_history_1.sqlite") as db:
                for n, text in enumerate(texts):
                    kind = "userMessage" if n % 2 == 0 else "agentMessage"
                    item = {"type": kind, "id": str(n), "content": [{"type": "text", "text": text}]} if n % 2 == 0 else {
                        "type": kind, "id": str(n), "text": text, "phase": "final_answer"}
                    db.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (ident, turn, str(n), n, json.dumps(item), kind))
        return ident, path


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "codex")
        self.reader = History(self.store.root)

    def all_pages(self, op, args, key):
        values = []
        for _ in range(100):
            page = self.reader.call(op, args)
            values.extend(page[key])
            if not page["next_cursor"]:
                return values
            args = dict(args, cursor=page["next_cursor"])
        self.fail("pagination did not terminate")

    def test_all_sources_and_archive_and_unindexed_rollouts(self):
        active, _ = self.store.add(["active"])
        archived, _ = self.store.add(["archived"], archived=True)
        unindexed, _ = self.store.add(["unindexed"], indexed=False, source={"subagent": {"thread_spawn": {}}})
        rows = self.all_pages("list", {"limit": 1}, "threads")
        self.assertEqual({r["id"] for r in rows}, {active, archived, unindexed})
        self.assertEqual([r["id"] for r in self.reader.call("list", {"archive": "archived"})["threads"]], [archived])

    def test_legacy_mirrors_are_not_duplicated(self):
        ident, _ = self.store.add(["question", "answer", "question again", "answer"])
        rows = self.all_pages("read", {"thread_id": ident, "limit": 1}, "records")
        self.assertEqual([r["text"] for r in rows], ["question", "answer", "question again", "answer"])

    def test_paginated_sqlite_long_messages_are_not_truncated(self):
        texts = ["prompt", "中文 exact\n" * 200, "last request", "end"]
        ident, path = self.store.add(texts, paginated=True)
        path.unlink()  # New projected history remains readable without a legacy body.
        rows = self.all_pages("read", {"thread_id": ident, "limit": 2, "max_chars": 256}, "records")
        self.assertEqual("".join(r["text"] for r in rows), "".join(texts))
        self.assertTrue(any(not r["record_complete"] for r in rows))

    def test_search_reads_full_body_and_resumes_across_archives(self):
        ids = {self.store.add(["start", "not-in-title UNIQUE evidence"], archived=archive, paginated=paged)[0]
               for archive, paged in [(False, False), (True, False), (False, True), (True, True)]}
        rows = self.all_pages("search", {"query": "unique", "limit": 1}, "matches")
        self.assertEqual({r["thread"]["id"] for r in rows}, ids)
        self.assertEqual(self.reader.call("list", {"title": "UNIQUE"})["threads"], [])

    def test_tool_outputs_are_opt_in_and_searchable(self):
        ident, _ = self.store.add(["request", "response"], paginated=True)
        with sqlite3.connect(self.store.root / "thread_history_1.sqlite") as db:
            db.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (ident, "turn", "tool", 10,
                       json.dumps({"type": "commandExecution", "command": "synthetic", "aggregatedOutput": "TOOL-ONLY-MARKER"}), "commandExecution"))
        self.assertEqual(self.reader.call("search", {"query": "TOOL-ONLY"})["matches"], [])
        self.assertEqual(len(self.reader.call("search", {"query": "TOOL-ONLY", "include_tools": True})["matches"]), 1)

    def test_projection_lag_reads_newly_persisted_rollout_messages(self):
        ident, path = self.store.add(["request", "old answer"], paginated=True)
        offset = path.stat().st_size
        with sqlite3.connect(self.store.root / "thread_history_1.sqlite") as db:
            db.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT,next_rollout_byte_offset INTEGER)")
            db.execute("INSERT INTO thread_history_projection_state VALUES(?,?)", (ident, offset))
        with path.open("a") as stream:
            for item in [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "fresh saved answer"}]}},
                         {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "agent_message", "id": "new", "text": "fresh saved answer"}}}]:
                stream.write(json.dumps(item) + "\n")
        self.assertEqual([r["text"] for r in self.reader.call("read", {"thread_id": ident})["records"]],
                         ["request", "old answer", "fresh saved answer"])

    def test_persisted_realtime_messages_are_included(self):
        ident, _ = self.store.add(["request"], paginated=True)
        with sqlite3.connect(self.store.root / "thread_history_1.sqlite") as db:
            db.execute("CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT,item_type TEXT)")
            db.execute("INSERT INTO thread_realtime_items VALUES(?,?,?,?,?)", (ident, "realtime", 100,
                       json.dumps({"type": "agentMessage", "text": "saved realtime text"}), "agentMessage"))
        self.assertEqual(self.reader.call("read", {"thread_id": ident})["records"][-1]["text"], "saved realtime text")

    def test_recent_wal_commits_are_visible_without_checkpoint(self):
        ident, _ = self.store.add(["request"], paginated=True)
        path = self.store.root / "thread_history_1.sqlite"
        with sqlite3.connect(path) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (ident, "turn", "fresh", 101,
                           json.dumps({"type": "agentMessage", "text": "committed in WAL"}), "agentMessage"))
            writer.commit()
            wal = Path(str(path) + "-wal")
            before = wal.read_bytes()
            self.assertEqual(self.reader.call("read", {"thread_id": ident})["records"][-1]["text"], "committed in WAL")
            self.assertEqual(wal.read_bytes(), before)

    def test_cursor_cannot_be_reused_with_another_query(self):
        self.store.add(["a", "a", "b"], paginated=True)
        cursor = self.reader.call("search", {"query": "a", "limit": 1})["next_cursor"]
        with self.assertRaisesRegex(ValueError, "游标"):
            self.reader.call("search", {"query": "b", "limit": 1, "cursor": cursor})
        for value in ("!bad", "e30=", "W10=", "bnVsbA=="):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cursor_decode(value, scope_key("list", {}), "")

    def test_readonly_source_and_private_files_not_exposed(self):
        ident, _ = self.store.add(["safe"], paginated=True)
        auth = self.store.root / "auth.json"
        auth.write_text('{"secret":"never-read"}')
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob("*") if p.is_file()}
        self.reader.call("read", {"thread_id": ident})
        self.reader.call("search", {"query": "safe"})
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        with self.assertRaises(ValueError):
            self.reader.permitted_path(auth)
        linked = self.store.root / "sessions" / "escape.jsonl"
        linked.symlink_to(auth)
        with self.assertRaises(ValueError):
            self.reader.permitted_path(linked)

    def test_corrupt_or_missing_body_reports_incomplete_search(self):
        _, path = self.store.add(["question"])
        path.write_text(path.read_text() + '{"unfinished"')
        result = self.reader.call("search", {"query": "question"})
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])

    def test_compressed_only_history(self):
        ident, path = self.store.add(["question", "compressed answer"], archived=True)
        try:
            import zstandard
        except ImportError:
            if not shutil.which("zstd"):
                self.skipTest("zstd codec unavailable")
            with path.with_suffix(".jsonl.zst").open("wb") as output:
                subprocess.run(["zstd", "-q", "-c", str(path)], stdout=output, check=True)
        else:
            path.with_suffix(".jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
        path.unlink()
        self.assertEqual([r["text"] for r in self.reader.call("read", {"thread_id": ident})["records"]],
                         ["question", "compressed answer"])

    @unittest.skipUnless(shutil.which("zstd"), "zstd command unavailable")
    def test_zstd_command_fallback_and_corruption(self):
        ident, path = self.store.add(["question", "zstd command answer"])
        compressed = path.with_suffix(".jsonl.zst")
        with compressed.open("wb") as output:
            subprocess.run(["zstd", "-q", "-c", str(path)], stdout=output, check=True)
        path.unlink()
        with patch.dict(sys.modules, {"zstandard": None, "compression": None}):
            self.assertEqual(self.reader.call("read", {"thread_id": ident})["records"][-1]["text"], "zstd command answer")
            compressed.write_bytes(b"not a zstd stream")
            with self.assertRaisesRegex(ValueError, "zstd"):
                self.reader.call("read", {"thread_id": ident})

    @unittest.skipUnless(shutil.which("zstd"), "zstd command unavailable")
    def test_zstd_unindexed_header_and_early_page_do_not_require_full_decompression(self):
        ident, path = self.store.add(["large " * 100000], indexed=False)
        with path.with_suffix(".jsonl.zst").open("wb") as output:
            subprocess.run(["zstd", "-q", "-c", str(path)], stdout=output, check=True)
        path.unlink()
        with patch.dict(sys.modules, {"zstandard": None, "compression": None}):
            self.assertEqual(self.reader.call("list", {})["threads"][0]["id"], ident)
            first = self.reader.call("read", {"thread_id": ident, "max_chars": 256})
            self.assertIsNotNone(first["next_cursor"])
            self.assertEqual(len(first["records"][0]["text"]), 256)

    def test_mcp_roundtrip_text_results_and_error_isolation(self):
        ident, _ = self.store.add(["synthetic MCP answer"])
        messages = [
            {"id": 1, "method": "initialize"},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "read_codex_thread", "arguments": {"thread_id": ident}}},
            {"id": 4, "method": "tools/call", "params": {"name": "execute_sql", "arguments": {}}},
            {"id": 5, "method": "ping"},
        ]
        run = subprocess.run([sys.executable, "-B", str(SCRIPTS / "history.py"), "--home", str(self.store.root), "serve"],
                             input="".join(json.dumps(m) + "\n" for m in messages), text=True, capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
        self.assertIn("synthetic MCP answer", replies[2]["result"]["content"][0]["text"])
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertEqual(replies[4]["result"], {})


if __name__ == "__main__":
    unittest.main()
