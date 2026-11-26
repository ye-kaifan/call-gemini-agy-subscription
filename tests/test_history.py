"""History behavior against a synthetic app-server protocol; no user data."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from app_server import RpcError
from history import History, cursor_decode, scope_key


def user(text, ident=None):
    return {"type": "userMessage", "id": ident or str(uuid4()),
            "content": [{"type": "text", "text": text}]}


def assistant(text, phase="final_answer"):
    return {"type": "agentMessage", "id": str(uuid4()), "text": text, "phase": phase}


class FakeServer:
    def __init__(self):
        self.rows, self.items, self.requests = [], {}, []
        self.errors, self.unsupported_items = {}, set()
        self.closed = False

    def add(self, items, *, archived=False, source="cli", mode="paginated", title="Synthetic chat"):
        ident, turn = str(uuid4()), str(uuid4())
        row = {"id": ident, "name": title, "preview": "first message", "cwd": "/synthetic/project",
               "source": source, "historyMode": mode, "updatedAt": 123, "archived": archived}
        self.rows.append(row)
        self.items[ident] = [{"turnId": turn, "item": item} for item in items]
        return ident

    @staticmethod
    def page(rows, cursor, size):
        index = int(cursor or 0)
        data = rows[index:index + size]
        return {"data": copy.deepcopy(data), "nextCursor": str(index + size) if index + size < len(rows) else None}

    def request(self, method, args):
        self.requests.append((method, copy.deepcopy(args)))
        ident = args.get("threadId")
        if (method, ident) in self.errors:
            raise self.errors[(method, ident)]
        if method == "thread/list":
            rows = [r for r in self.rows if r["archived"] == args["archived"] and r["source"] in args["sourceKinds"]]
            return self.page(rows, args.get("cursor"), 2)
        row = next(r for r in self.rows if r["id"] == ident)
        if method == "thread/read":
            return {"thread": copy.deepcopy(row)}
        if method == "thread/items/list":
            if row["historyMode"] == "legacy" or ident in self.unsupported_items:
                raise RpcError(method, {"code": -32601, "message": "not supported yet"})
            return self.page(self.items[ident], args.get("cursor"), 2)
        if method == "thread/turns/list":
            turns = []
            for entry in self.items[ident]:
                if not turns or turns[-1]["id"] != entry["turnId"]:
                    turns.append({"id": entry["turnId"], "items": [], "itemsView": "full"})
                turns[-1]["items"].append(copy.deepcopy(entry["item"]))
            return self.page(turns, args.get("cursor"), 1)
        raise AssertionError("unexpected method: " + method)

    def close(self):
        self.closed = True


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.reader = History(Path("/synthetic/home"), client=self.server)

    def pages(self, op, args, key):
        values, pages = [], []
        for _ in range(100):
            result = self.reader.call(op, args)
            pages.append(result)
            values.extend(result[key])
            if result["next_cursor"] is None:
                return values, pages
            args = dict(args, cursor=result["next_cursor"])
        self.fail("pagination did not terminate")

    def test_default_interactive_active_and_optional_archives(self):
        wanted = {self.server.add([user("main")], source=s) for s in ("cli", "vscode", "appServer")}
        for source in ("exec", "subAgent", "unknown"):
            self.server.add([user("background")], source=source)
        archived = self.server.add([user("archived")], archived=True)
        rows, _ = self.pages("list", {"limit": 1}, "threads")
        self.assertEqual({r["id"] for r in rows}, wanted)
        rows, _ = self.pages("list", {"archive": "all", "limit": 1}, "threads")
        self.assertEqual({r["id"] for r in rows}, wanted | {archived})
        self.assertTrue(all(a["modelProviders"] == [] for m, a in self.server.requests if m == "thread/list"))

    def test_substring_filters_preserve_empty_page_continuation(self):
        for title in ("skip", "skip", "skip", "Found TARGET"):
            self.server.add([user("x")], title=title)
        with patch("history.PAGE_SECONDS", -1):
            rows, pages = self.pages("list", {"title": "target", "project": "PROJECT"}, "threads")
        self.assertEqual(len(rows), 1)
        self.assertTrue(any(not p["threads"] and p["next_cursor"] for p in pages))

    def test_only_user_and_final_assistant_text(self):
        tid = self.server.add([user("question"), assistant("progress", "commentary"),
                               {"type": "reasoning", "id": "secret", "summary": ["private"]},
                               {"type": "commandExecution", "id": "tool", "aggregatedOutput": "logs"},
                               assistant("final"), assistant("legacy final", None)])
        rows, _ = self.pages("read", {"thread_id": tid, "limit": 1}, "records")
        self.assertEqual([r["text"] for r in rows], ["question", "final", "legacy final"])
        for query in ("progress", "private", "logs"):
            self.assertEqual(self.reader.call("search", {"query": query})["matches"], [])

    def test_long_message_pages_are_lossless_across_upstream_pages(self):
        texts = ["first", "中文 exact\n" * 120, "third", "final text" * 90]
        tid = self.server.add([user(texts[0]), assistant(texts[1]), user(texts[2]), assistant(texts[3])])
        rows, _ = self.pages("read", {"thread_id": tid, "limit": 2, "max_chars": 256}, "records")
        self.assertEqual("".join(r["text"] for r in rows), "".join(texts))
        self.assertTrue(any(not r["record_complete"] for r in rows))
        self.assertTrue(any(a.get("cursor") for m, a in self.server.requests if m == "thread/items/list"))

    def test_legacy_and_unsupported_items_use_full_turns(self):
        for mode in ("legacy", "paginated"):
            with self.subTest(mode=mode):
                tid = self.server.add([user("old question"), assistant("old answer")], mode=mode)
                self.server.unsupported_items.add(tid)
                # A second turn forces actual turn pagination.
                self.server.items[tid].append({"turnId": str(uuid4()), "item": assistant("last")})
                rows, _ = self.pages("read", {"thread_id": tid, "limit": 1}, "records")
                self.assertEqual([r["text"] for r in rows], ["old question", "old answer", "last"])
        self.assertTrue(all(a["itemsView"] == "full" for m, a in self.server.requests if m == "thread/turns/list"))
        self.assertFalse(any(m == "thread/read" and a.get("includeTurns") for m, a in self.server.requests))

    def test_search_is_body_search_and_continues_within_and_between_chats(self):
        wanted = set()
        for archived, mode in [(False, "legacy"), (False, "paginated"), (True, "paginated")]:
            tid = self.server.add([user("unique first"), assistant("prefix " * 300 + "UNIQUE second"),
                                   user("no match"), assistant("unique last")], archived=archived, mode=mode)
            wanted.update((tid, item["item"]["id"]) for item in self.server.items[tid]
                          if "unique" in json.dumps(item).lower())
        rows, pages = self.pages("search", {"query": "unique", "archive": "all", "limit": 1}, "matches")
        self.assertEqual({(r["thread"]["id"], r["record_id"]) for r in rows}, wanted)
        self.assertEqual(len(rows), len(wanted))
        self.assertTrue(pages[-1]["complete"])
        self.assertEqual(self.reader.call("list", {"title": "unique"})["threads"], [])

    def test_search_time_budget_can_return_empty_pages_without_losing_progress(self):
        tid = self.server.add([user("first"), assistant("no match"), user("needle final")])
        with patch("history.PAGE_SECONDS", -1):
            rows, pages = self.pages("search", {"query": "needle"}, "matches")
        self.assertEqual([r["thread"]["id"] for r in rows], [tid])
        self.assertTrue(any(not p["matches"] and p["next_cursor"] for p in pages))

    def test_rpc_failure_is_not_silently_replaced_or_reported_complete(self):
        tid = self.server.add([user("needle")])
        self.server.errors[("thread/items/list", tid)] = RpcError("thread/items/list", {"code": -32602, "message": "broken data"})
        result = self.reader.call("search", {"query": "needle"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["thread_id"], tid)
        self.assertFalse(any(m == "thread/turns/list" for m, _ in self.server.requests))
        with self.assertRaises(RpcError):
            self.reader.call("read", {"thread_id": tid})

    def test_cursor_is_bound_to_query_and_home(self):
        self.server.add([user("a"), assistant("a")])
        cursor = self.reader.call("search", {"query": "a", "limit": 1})["next_cursor"]
        with self.assertRaisesRegex(ValueError, "游标"):
            self.reader.call("search", {"query": "b", "limit": 1, "cursor": cursor})
        another = History(Path("/another/home"), client=self.server)
        with self.assertRaisesRegex(ValueError, "游标"):
            another.call("search", {"query": "a", "limit": 1, "cursor": cursor})
        for invalid in ("!bad", "e30=", "W10=", "bnVsbA=="):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cursor_decode(invalid, scope_key("list", {}, self.reader.home), None)

    def test_changed_page_anchor_is_reported(self):
        tid = self.server.add([user("a"), assistant("b")])
        args = {"thread_id": tid, "limit": 1}
        cursor = self.reader.call("read", args)["next_cursor"]
        self.server.items[tid][0]["item"]["id"] = "replaced"
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.reader.call("read", dict(args, cursor=cursor))

    def test_no_tool_log_option_and_invalid_inputs(self):
        for op, args in [("read", {"thread_id": "not-an-id"}), ("list", {"limit": True}),
                         ("list", {"archive": "other"}), ("search", {"query": " "}),
                         ("read", {"thread_id": str(uuid4()), "include_tools": True})]:
            with self.subTest(op=op, args=args), self.assertRaises(ValueError):
                self.reader.call(op, args)

    def test_rejects_summary_instead_of_full_turn_history(self):
        tid = self.server.add([user("full text")], mode="legacy")
        original = self.server.request
        def request(method, args):
            result = original(method, args)
            if method == "thread/turns/list":
                result["data"][0]["itemsView"] = "summary"
            return result
        self.server.request = request
        with self.assertRaisesRegex(ValueError, "摘要"):
            self.reader.call("read", {"thread_id": tid})


if __name__ == "__main__":
    unittest.main()
