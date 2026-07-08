"""Incremental indexing: append tailing, replace detection, unchanged skip,
absolute line-number continuity, and per-file transactional atomicity.
"""

import unittest

from tests.helpers import (append_jsonl, assistant_text_line, make_env,
                           user_line, write_jsonl)
from session_recall.indexer import run_index
from session_recall.store import Store


class Incremental(unittest.TestCase):
    def test_append_replace_unchanged_and_line_continuity(self):
        root, db = make_env()
        jsonl = root / "c--projA" / "s1.jsonl"
        write_jsonl(jsonl, [user_line("u1", "first"), assistant_text_line("a1", "reply one")])

        s = run_index(db, root=root, full=True, verbose=False)
        self.assertEqual(s.get("files_new", 0), 1)
        self.assertEqual(s.get("stored", 0), 2)

        append_jsonl(jsonl, [user_line("u2", "second about ECONNRESET")])
        s = run_index(db, root=root, full=False, verbose=False)
        self.assertEqual(s.get("files_appended", 0), 1)
        self.assertEqual(s.get("stored", 0), 1)

        s = run_index(db, root=root, full=False, verbose=False)
        self.assertEqual(s.get("files_unchanged", 0), 1)
        self.assertEqual(s.get("stored", 0), 0)

        st = Store(db)
        rows = {r["uuid"]: r["line_no"] for r in st.conn.execute(
            "SELECT uuid, line_no FROM messages ORDER BY line_no")}
        st.close()
        self.assertEqual(rows["u1"], 0)
        self.assertEqual(rows["a1"], 1)
        self.assertEqual(rows["u2"], 2)  # absolute line number continued across append

    def test_replace_resets_file_rows(self):
        root, db = make_env()
        jsonl = root / "c--projA" / "s1.jsonl"
        write_jsonl(jsonl, [user_line("u1", "one"), user_line("u2", "two"), user_line("u3", "three")])
        run_index(db, root=root, full=True, verbose=False)
        # Rewrite shorter (a truncation/replace): size shrinks -> reparse from 0.
        write_jsonl(jsonl, [user_line("u9", "brand new shorter file")])
        s = run_index(db, root=root, full=False, verbose=False)
        self.assertEqual(s.get("files_replaced", 0), 1)
        st = Store(db)
        uuids = sorted(r[0] for r in st.conn.execute("SELECT uuid FROM messages"))
        st.close()
        self.assertEqual(uuids, ["u9"])

    def test_failed_file_is_atomic(self):
        root, db = make_env()
        jsonl = root / "c--projA" / "s1.jsonl"
        write_jsonl(jsonl, [user_line("u1", "ok"), user_line("BOOM", "explodes"), user_line("u3", "ok")])
        orig = Store.insert_message

        def exploding(self, record, file_id):
            if record.get("uuid") == "BOOM":
                raise RuntimeError("simulated failure")
            return orig(self, record, file_id)

        Store.insert_message = exploding
        try:
            s = run_index(db, root=root, full=True, verbose=False)
        finally:
            Store.insert_message = orig
        self.assertEqual(s.get("file_errors", 0), 1)
        st = Store(db)
        # Nothing from the failed file was committed (u1 rolled back with BOOM).
        self.assertEqual(st.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(st.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0], 0)
        st.close()
        # Recovery: without the fault, a rerun indexes the whole file.
        s = run_index(db, root=root, full=False, verbose=False)
        st = Store(db)
        uuids = sorted(r[0] for r in st.conn.execute("SELECT uuid FROM messages"))
        st.close()
        self.assertEqual(uuids, ["BOOM", "u1", "u3"])


if __name__ == "__main__":
    unittest.main()
