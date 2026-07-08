"""MCP server tool behavior, exercised in-process against a temp index.

Skipped if the mcp SDK is not installed. The tool functions are called directly
(FastMCP leaves them callable); this covers the logic without a live transport.
"""

import unittest

try:
    from session_recall import server as srv
    _HAVE_MCP = True
except Exception:  # pragma: no cover - only when mcp is absent
    _HAVE_MCP = False

from tests.helpers import (assistant_text_line, make_env, tool_use_line,
                           user_line, write_jsonl)
from session_recall.indexer import run_index


@unittest.skipUnless(_HAVE_MCP, "mcp SDK not installed")
class ServerTools(unittest.TestCase):
    def setUp(self):
        self.root, self.db = make_env()
        write_jsonl(self.root / "c--projX" / "s1.jsonl", [
            user_line("u1", "please investigate the Wintermaul tower defense bug", session="sX"),
            assistant_text_line("a1", "looking into Wintermaul now", session="sX"),
            tool_use_line("t1", "mcp__session-recall__search_history", {"query": "x"}, session="sX"),
            user_line("u2", "X" * 5000, session="sX"),
        ])
        run_index(self.db, root=self.root, full=True, verbose=False)
        self._old_db = srv.DB_PATH
        srv.DB_PATH = self.db

    def tearDown(self):
        srv.DB_PATH = self._old_db

    def test_search_returns_hits(self):
        r = srv.search_history("Wintermaul")
        self.assertGreaterEqual(r["returned"], 1)
        self.assertTrue(any("Wintermaul" in h["snippet"] for h in r["hits"]))

    def test_self_echo_traffic_not_indexed(self):
        # The mcp__session-recall tool_use entry must not have been indexed.
        r = srv.search_history("search_history")
        self.assertFalse(
            any(h["tool_name"] == "mcp__session-recall__search_history" for h in r["hits"]))

    def test_quoted_phrase(self):
        r = srv.search_history('"tower defense"')
        self.assertGreaterEqual(r["returned"], 1)

    def test_negative_cap_is_treated_as_no_cap(self):
        # The CLI passes --cap unclamped; a negative cap must not silently
        # return zero hits. It is treated as no diversity cap.
        from session_recall.store import Store
        store = Store(self.db)
        try:
            hits = store.search("Wintermaul", per_session_cap=-1)
        finally:
            store.close()
        self.assertGreaterEqual(len(hits), 1)

    def test_read_messages_defers_when_budget_reached(self):
        r = srv.read_messages(session_id="sX", count=10, max_chars=500)
        self.assertIn("continuation", r)

    def test_read_messages_slices_single_oversized_message(self):
        r = srv.read_messages(uuids=["u2"], max_chars=500)
        self.assertEqual(r["returned"], 1)
        self.assertTrue(r["messages"][0].get("truncated"))
        self.assertLessEqual(len(r["messages"][0]["text"]), 500)
        self.assertIn("continuation", r)

    def test_oversized_message_pages_to_completion(self):
        # u2 is 5000 chars. Page it via uuids following the continuation; it must
        # terminate and reconstruct the whole message exactly.
        collected = ""
        call = {"uuids": ["u2"], "max_chars": 800, "start_char": 0}
        for _ in range(30):
            r = srv.read_messages(**call)
            self.assertEqual(r["returned"], 1)
            collected += r["messages"][0]["text"]
            cont = r.get("continuation")
            if not cont:
                break
            call = {"uuids": cont["next_uuids"], "max_chars": 800,
                    "start_char": cont["next_start_char"]}
        else:
            self.fail("paging did not terminate")
        self.assertEqual(collected, "X" * 5000)

    def test_start_uuid_continuation_followable_without_session_id(self):
        # Following a next_start_uuid hint (no session_id) must resolve.
        first = srv.read_messages(session_id="sX", count=1)
        uid = first["messages"][0]["uuid"]
        r = srv.read_messages(start_uuid=uid, count=1)
        self.assertGreaterEqual(r["returned"], 1)
        self.assertEqual(r["messages"][0]["uuid"], uid)

    def test_outline_and_status(self):
        out = srv.get_session_outline("sX", max_items=10)
        self.assertGreaterEqual(out["returned"], 1)
        st = srv.recall_status()
        self.assertEqual(st["journal_mode"].lower(), "wal")

    def test_list_sessions(self):
        r = srv.list_sessions(limit=5)
        self.assertGreaterEqual(r["returned"], 1)


if __name__ == "__main__":
    unittest.main()
