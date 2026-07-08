"""Tests for the Codex rollout parser and the unified two-corpus index (phase 0.6)."""

import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tests.helpers import make_env
from session_recall.parser_codex import parse_codex_file, read_codex_session_meta
from session_recall.indexer import run_index
from session_recall.store import Store


def write_codex_rollout(codex_home, session_id, cwd, entries, rel=None):
    """Write a synthetic Codex rollout file. entries are {type, payload} dicts;
    a session_meta line and timestamps are added automatically."""
    rel = rel or f"sessions/2026/07/01/rollout-2026-07-01T00-00-00-{session_id}.jsonl"
    path = Path(codex_home) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "timestamp": "2026-07-01T00:00:00Z",
              "payload": {"session_id": session_id, "id": session_id, "cwd": cwd}}]
    for i, e in enumerate(entries):
        e = dict(e)
        e.setdefault("timestamp", f"2026-07-01T00:00:{i + 1:02d}Z")
        lines.append(e)
    with io.open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def msg(role, text, mid=None):
    block = "output_text" if role == "assistant" else "input_text"
    p = {"type": "message", "role": role, "content": [{"type": block, "text": text}]}
    if mid:
        p["id"] = mid
    return {"type": "response_item", "payload": p}


def SAMPLE_ENTRIES():
    return [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "dup"}},  # skipped
        msg("user", "how do I fix the widget crash"),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [],
                                              "encrypted_content": "opaque"}},  # skipped (empty)
        msg("assistant", "the widget crash is a null deref in render", "msg_1"),
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell_command",
                                              "arguments": "{\"cmd\":\"ls\"}", "call_id": "c1"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
                                              "call_id": "c1", "output": "render.py widget.py"}},
    ]


class TestCodexParser(unittest.TestCase):
    def test_parses_messages_tools_and_skips_events(self):
        home = Path(tempfile.mkdtemp())
        path = write_codex_rollout(home, "cx1", "C:/work/Crucible", SAMPLE_ENTRIES())
        sid, cwd = read_codex_session_meta(str(path))
        self.assertEqual(sid, "cx1")
        self.assertEqual(cwd, "C:/work/Crucible")

        stats = Counter()
        recs = list(parse_codex_file(str(path), stats, session_id=sid, cwd=cwd))
        kinds = Counter(r["entry_type"] for r in recs)
        self.assertEqual(kinds["user"], 1)
        self.assertEqual(kinds["assistant"], 1)
        self.assertEqual(kinds["tool_use"], 1)
        self.assertEqual(kinds["tool_result"], 1)
        self.assertEqual(kinds.get("thinking", 0), 0)  # empty reasoning dropped
        self.assertTrue(all(r["source"] == "codex" for r in recs))
        self.assertTrue(all(r["session_id"] == "cx1" for r in recs))
        # event_msg was skipped, not indexed
        texts = " ".join(r["text"] for r in recs)
        self.assertNotIn("dup", texts)
        self.assertIn("null deref", texts)

    def test_web_search_call_indexes_the_query(self):
        home = Path(tempfile.mkdtemp())
        entries = [{"type": "response_item", "payload": {
            "type": "web_search_call", "status": "completed",
            "action": {"type": "search", "query": "TradingAgents arXiv multi-agent",
                       "queries": ["FinRL framework"]}}}]
        path = write_codex_rollout(home, "cx3", "C:/work/Research", entries)
        sid, cwd = read_codex_session_meta(str(path))
        recs = list(parse_codex_file(str(path), Counter(), session_id=sid, cwd=cwd))
        tu = [r for r in recs if r["entry_type"] == "tool_use"]
        self.assertEqual(len(tu), 1)
        self.assertIn("TradingAgents", tu[0]["text"])
        self.assertIn("FinRL", tu[0]["text"])

    def test_read_full_text_codex_expands_truncated_output(self):
        from session_recall.parser_codex import read_full_text_codex
        home = Path(tempfile.mkdtemp())
        big = "X" * 5000  # exceeds the tool-result cap, so the stored row is truncated
        path = write_codex_rollout(home, "cxT", "C:/work/App", [
            msg("user", "run it"),
            {"type": "response_item", "payload": {"type": "function_call",
                "name": "shell", "arguments": "{}", "call_id": "T1"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                "call_id": "T1", "output": big}},
        ])
        recs = list(parse_codex_file(str(path), Counter(), session_id="cxT", cwd="C:/work/App"))
        tr = [r for r in recs if r["entry_type"] == "tool_result"][0]
        self.assertEqual(tr["is_truncated"], 1)
        self.assertLess(len(tr["text"]), 5000)
        full = read_full_text_codex(str(path), tr["line_no"])
        self.assertEqual(len(full), 5000)

    def test_self_echo_suppresses_own_tool_call_and_output(self):
        home = Path(tempfile.mkdtemp())
        entries = [
            msg("user", "look it up"),
            {"type": "response_item", "payload": {"type": "function_call",
                "name": "search_history", "arguments": "{\"query\":\"x\"}", "call_id": "e1"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                "call_id": "e1", "output": "SECRET past search output blob"}},
        ]
        path = write_codex_rollout(home, "cx2", "C:/work/App", entries)
        sid, cwd = read_codex_session_meta(str(path))
        recs = list(parse_codex_file(str(path), Counter(), session_id=sid, cwd=cwd))
        blob = " ".join(r["text"] for r in recs)
        self.assertNotIn("SECRET past search output", blob)
        self.assertFalse(any(r["tool_name"] == "search_history" for r in recs))


class TestUnifiedIndex(unittest.TestCase):
    def test_both_corpora_indexed_and_cross_searchable(self):
        root, db = make_env()  # empty Claude root
        home = Path(tempfile.mkdtemp())
        write_codex_rollout(home, "cx1", "C:/work/Crucible", SAMPLE_ENTRIES())

        run_index(db, root=root, codex_home=home, full=True, verbose=False)
        store = Store(db)
        try:
            hits = store.search("widget crash")
            self.assertTrue(hits)
            self.assertTrue(all(h["source"] == "codex" for h in hits))
            # source filter
            self.assertEqual(len(store.search("widget", source="claude")), 0)
            self.assertTrue(store.search("widget", source="codex"))
            st = store.status()
            self.assertIn("codex", st["by_source"])
            self.assertEqual(st["by_source"]["codex"]["sessions"], 1)
            # project attribution from cwd
            self.assertTrue(store.search("widget", project="Crucible"))
        finally:
            store.close()

    def test_self_echo_holds_across_incremental_pass_boundary(self):
        # A self-tool call in one pass and its output in a later pass: the output
        # must still be suppressed (the persisted echo id set makes this hold).
        root, db = make_env()
        home = Path(tempfile.mkdtemp())
        path = write_codex_rollout(home, "cxE", "C:/work/App", [
            msg("user", "look it up"),
            {"type": "response_item", "payload": {"type": "function_call",
                "name": "search_history", "arguments": "{\"query\":\"x\"}", "call_id": "E1"}},
        ])
        run_index(db, root=root, codex_home=home, full=True, verbose=False)
        # The paired output is written after the first pass committed past the call.
        with io.open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "response_item", "timestamp": "2026-07-01T00:02:00Z",
                "payload": {"type": "function_call_output", "call_id": "E1",
                            "output": "SECRETSEARCHRESULT leaked blob"}}) + "\n")
        run_index(db, root=root, codex_home=home, full=False, verbose=False)
        store = Store(db)
        try:
            self.assertEqual(len(store.search("SECRETSEARCHRESULT")), 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
