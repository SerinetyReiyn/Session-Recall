"""Web UI data functions, exercised against a temp index (phase 0.5).

The HTTP layer is a thin wrapper over these pure functions, so testing them
directly covers the behavior without binding a socket.
"""

import unittest

from tests.helpers import (assistant_text_line, make_env, user_line, write_jsonl)
from session_recall.indexer import run_index
from session_recall import webui


class WebUIData(unittest.TestCase):
    def setUp(self):
        self.root, self.db = make_env()
        write_jsonl(self.root / "c--projX" / "s1.jsonl", [
            user_line("u1", "how do we fix the widget alignment bug", session="sX"),
            assistant_text_line("a1", "the widget alignment fix is to reset the flex basis", session="sX"),
        ])
        run_index(self.db, root=self.root, full=True, verbose=False)

    def test_status(self):
        s = webui.index_status(db_path=self.db)
        self.assertGreaterEqual(s["messages"], 2)
        self.assertEqual(s["sessions"], 1)

    def test_search_returns_hits(self):
        hits = webui.search_index("widget alignment", db_path=self.db)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(all("uuid" in h and "snippet" in h for h in hits))

    def test_sessions_and_outline(self):
        sessions = webui.sessions_list(db_path=self.db)
        self.assertEqual(len(sessions), 1)
        sid = sessions[0]["session_id"]
        outline = webui.session_outline(sid, db_path=self.db)
        self.assertGreaterEqual(len(outline), 2)

    def test_read_message_returns_full_text(self):
        hits = webui.search_index("flex basis", db_path=self.db)
        self.assertGreaterEqual(len(hits), 1)
        m = webui.read_message(hits[0]["uuid"], db_path=self.db)
        self.assertIsNotNone(m)
        self.assertIn("flex basis", m["text"])

    def test_read_missing_uuid_returns_none(self):
        self.assertIsNone(webui.read_message("does-not-exist", db_path=self.db))

    def test_second_bind_raises_so_reopen_guard_fires(self):
        # main()'s "already running, just reopen" path depends on a second bind
        # failing. On Windows the default SO_REUSEADDR would let it succeed and
        # leak a competing server, so _Server disables reuse.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = webui._Server(("127.0.0.1", port), webui.Handler)
        try:
            with self.assertRaises(OSError):
                webui._Server(("127.0.0.1", port), webui.Handler)
        finally:
            srv.server_close()

    def test_page_escapes_quotes_for_attribute_safety(self):
        # esc() must neutralize quotes so uuid/session_id cannot break out of a
        # double-quoted data-* attribute (attribute-context XSS).
        self.assertIn("&quot;", webui.PAGE)


if __name__ == "__main__":
    unittest.main()
