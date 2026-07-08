"""Self-echo exclusion (phase 0.2 item 4 / 0.2.1 closeout item 3).

Entries whose tool name matches the configured self-echo prefix must never be
indexed, so the server's own search traffic cannot re-log itself and dominate
future searches.
"""

import os
import unittest

from tests.helpers import (make_env, tool_result_line, tool_use_line, user_line,
                           write_jsonl)
from session_recall.indexer import run_index
from session_recall.store import Store


def _indexed_tool_names(db):
    st = Store(db)
    try:
        return [r[0] for r in st.conn.execute(
            "SELECT tool_name FROM messages WHERE tool_name IS NOT NULL")]
    finally:
        st.close()


class SelfEchoExclusion(unittest.TestCase):
    def test_default_prefix_excluded(self):
        root, db = make_env()
        write_jsonl(root / "c--projA" / "s1.jsonl", [
            user_line("u1", "a normal prompt"),
            tool_use_line("t1", "mcp__session-recall__search_history", {"query": "x"}),
            tool_use_line("t2", "Bash", {"command": "ls"}),
        ])
        run_index(db, root=root, full=True, verbose=False)
        names = _indexed_tool_names(db)
        self.assertIn("Bash", names)
        self.assertNotIn("mcp__session-recall__search_history", names)

    def test_tool_result_of_self_echo_also_excluded(self):
        # The tool_result carries the search OUTPUT and has no tool name of its
        # own; it must be suppressed via its tool_use_id so past results cannot
        # be re-indexed and dominate future searches.
        root, db = make_env()
        write_jsonl(root / "c--projC" / "s1.jsonl", [
            tool_use_line("t1", "mcp__session-recall__search_history", {"query": "x"}, session="sE"),
            tool_result_line("r1", "tu_t1", "OFFENDING search output with ECONNRESET and edb4bc71", session="sE"),
            user_line("u1", "a normal prompt", session="sE"),
        ])
        run_index(db, root=root, full=True, verbose=False)
        st = Store(db)
        try:
            hits = st.search("OFFENDING")
        finally:
            st.close()
        self.assertEqual(len(hits), 0, "self-echo tool_result output was indexed")

    def test_env_override_changes_which_prefix_is_excluded(self):
        os.environ["SESSION_RECALL_ECHO_PREFIXES"] = "mcp__custom-thing"
        try:
            root, db = make_env()
            write_jsonl(root / "c--projB" / "s1.jsonl", [
                tool_use_line("t1", "mcp__custom-thing__do", {"a": 1}),
                tool_use_line("t2", "mcp__session-recall__search_history", {"q": "x"}),
            ])
            run_index(db, root=root, full=True, verbose=False)
            names = _indexed_tool_names(db)
            # The env-configured prefix is excluded; the former default is now kept.
            self.assertNotIn("mcp__custom-thing__do", names)
            self.assertIn("mcp__session-recall__search_history", names)
        finally:
            del os.environ["SESSION_RECALL_ECHO_PREFIXES"]


if __name__ == "__main__":
    unittest.main()
