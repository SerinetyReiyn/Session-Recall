"""Session metadata: first_user_prompt stability across incremental appends,
and sidecar pointer rows excluded from session aggregates.
"""

import unittest

from tests.helpers import (append_jsonl, assistant_text_line, make_env,
                           user_line, write_jsonl, write_sidecar)
from session_recall.indexer import run_index
from session_recall.store import Store


def _first_prompt(db, session="s1"):
    st = Store(db)
    try:
        row = st.conn.execute(
            "SELECT first_user_prompt FROM sessions WHERE session_id=?", (session,)).fetchone()
        return row[0] if row else None
    finally:
        st.close()


class SessionMetadata(unittest.TestCase):
    def test_first_prompt_survives_append(self):
        root, db = make_env()
        jsonl = root / "c--projA" / "s1.jsonl"
        write_jsonl(jsonl, [user_line("u1", "FIRST REAL PROMPT")])
        run_index(db, root=root, full=True, verbose=False)
        self.assertEqual(_first_prompt(db), "FIRST REAL PROMPT")
        append_jsonl(jsonl, [assistant_text_line("a1", "reply"), user_line("u2", "LATER followup")])
        run_index(db, root=root, full=False, verbose=False)
        self.assertEqual(_first_prompt(db), "FIRST REAL PROMPT")

    def test_first_prompt_captured_from_later_append_when_none_yet(self):
        root, db = make_env()
        jsonl = root / "c--projA" / "s1.jsonl"
        write_jsonl(jsonl, [assistant_text_line("a0", "assistant opens, no user yet")])
        run_index(db, root=root, full=True, verbose=False)
        self.assertIsNone(_first_prompt(db))
        append_jsonl(jsonl, [user_line("u1", "the real first user turn")])
        run_index(db, root=root, full=False, verbose=False)
        self.assertEqual(_first_prompt(db), "the real first user turn")

    def test_sidecars_excluded_from_session_counts_and_bounds(self):
        root, db = make_env()
        proj = root / "c--projA"
        write_jsonl(proj / "s1.jsonl", [user_line("u1", "hi"), assistant_text_line("a1", "yo")])
        for name in ("aa.txt", "bb.txt", "cc.txt"):
            write_sidecar(proj / "s1", name)
        run_index(db, root=root, full=True, verbose=False)
        st = Store(db)
        row = st.conn.execute(
            "SELECT message_count, last_ts FROM sessions WHERE session_id='s1'").fetchone()
        sidecars = st.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE entry_type='sidecar' AND session_id='s1'").fetchone()[0]
        st.close()
        self.assertEqual(sidecars, 3)
        self.assertEqual(row["message_count"], 2)          # sidecars not counted
        self.assertEqual(row["last_ts"], "2026-06-01T00:00:01Z")  # sidecar mtime did not skew bound


if __name__ == "__main__":
    unittest.main()
