"""Tests for the claudia (claude.ai export) ingest, phase 0.7.

All fixtures are fully synthetic. No real export content is used.
"""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.helpers import make_env
from session_recall.indexer import ingest_claudia
from session_recall.parser_claude_export import inspect_export
from session_recall.store import Store


# -- fixture builders (synthetic) ------------------------------------------

def tb(text):
    return {"type": "text", "text": text}


def think(text):
    return {"type": "thinking", "thinking": text}


def tool_use(name, inp):
    return {"type": "tool_use", "name": name, "input": inp}


def tool_result(text):
    return {"type": "tool_result", "content": [{"type": "text", "text": text}]}


def message(sender, content, ts, uuid):
    """content is a list of blocks (or a plain string)."""
    return {"uuid": uuid, "sender": sender, "created_at": ts, "content": content}


def conversation(uuid, name, messages, created="2026-01-01T00:00:00Z", updated=None):
    return {
        "uuid": uuid,
        "name": name,
        "created_at": created,
        "updated_at": updated or created,
        "chat_messages": messages,
    }


def write_json(path, convs):
    Path(path).write_text(json.dumps(convs), encoding="utf-8")


def write_jsonl(path, convs):
    with io.open(path, "w", encoding="utf-8") as fh:
        for conv in convs:
            fh.write(json.dumps(conv) + "\n")


def write_zip(path, convs, inner="conversations.json"):
    with zipfile.ZipFile(path, "w") as zf:
        if inner.endswith(".jsonl"):
            zf.writestr(inner, "\n".join(json.dumps(c) for c in convs))
        else:
            zf.writestr(inner, json.dumps(convs))


def sample_convs():
    return [
        conversation("conv-a", "Widget debugging", [
            message("human", [tb("why does the widget crash on load")],
                    "2026-01-01T00:00:01Z", "a1"),
            message("assistant", [
                think("the stack points at a quarkline null deref"),
                tb("it is a null deref in render"),
                tool_use("web_search", {"query": "render null deref"}),
                tool_result("stackoverflow says guard the ref"),
            ], "2026-01-01T00:00:02Z", "a2"),
        ]),
        conversation("conv-b", "Trip planning", [
            message("human", [tb("plan a trip to Kyoto in spring")],
                    "2026-01-02T00:00:01Z", "b1"),
        ]),
    ]


def tmpfile(name):
    return Path(tempfile.mkdtemp()) / name


class TestClaudiaIngest(unittest.TestCase):
    def test_json_array_ingest_counts_fields_and_search(self):
        _, db = make_env()
        path = tmpfile("conversations.json")
        write_json(path, sample_convs())
        stats, warnings = ingest_claudia(db, str(path))

        self.assertEqual(stats["conversations_seen"], 2)
        self.assertEqual(stats["conversations_new"], 2)
        self.assertEqual(stats["messages_inserted"], 3)
        self.assertFalse(warnings)

        store = Store(db)
        try:
            self.assertEqual(store.status()["by_source"]["claudia"]["sessions"], 2)
            # field mapping
            row = store.get_session("conv-a")
            self.assertEqual(row["source"], "claudia")
            summary = store.conn.execute(
                "SELECT summary, first_user_prompt FROM sessions WHERE session_id='conv-a'"
            ).fetchone()
            self.assertEqual(summary["summary"], "Widget debugging")
            self.assertIn("widget crash", summary["first_user_prompt"])
            # searchable, tagged claudia
            hits = store.search("widget crash")
            self.assertTrue(hits and all(h["source"] == "claudia" for h in hits))
            # thinking and tool content are indexed too
            self.assertTrue(store.search("quarkline"))       # from a thinking block
            self.assertTrue(store.search("stackoverflow"))   # from a tool_result block
            self.assertTrue(store.search("web_search"))      # from a tool_use block
        finally:
            store.close()

    def test_jsonl_vintage_matches_json(self):
        _, db = make_env()
        path = tmpfile("conversations.jsonl")
        write_jsonl(path, sample_convs())
        stats, _ = ingest_claudia(db, str(path))
        self.assertEqual(stats["conversations_new"], 2)
        self.assertEqual(stats["messages_inserted"], 3)
        store = Store(db)
        try:
            self.assertTrue(store.search("Kyoto", source="claudia"))
        finally:
            store.close()

    def test_zip_ingest(self):
        _, db = make_env()
        path = tmpfile("export.zip")
        write_zip(path, sample_convs(), inner="conversations.json")
        stats, _ = ingest_claudia(db, str(path))
        self.assertEqual(stats["messages_inserted"], 3)

    def test_idempotent_reingest_is_a_noop(self):
        _, db = make_env()
        path = tmpfile("conversations.json")
        write_json(path, sample_convs())
        ingest_claudia(db, str(path))
        store = Store(db)
        before = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        store.close()

        stats2, _ = ingest_claudia(db, str(path))
        self.assertEqual(stats2["messages_inserted"], 0)
        self.assertEqual(stats2["skipped_unchanged"], 2)
        store = Store(db)
        try:
            after = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(before, after)
        finally:
            store.close()

    def test_growth_lands_only_the_delta(self):
        _, db = make_env()
        p1 = tmpfile("v1.json")
        write_json(p1, sample_convs())
        ingest_claudia(db, str(p1))

        # conv-a gains a third message with a newer timestamp.
        grown = sample_convs()
        grown[0]["chat_messages"].append(
            message("human", [tb("any follow up on the fix")],
                    "2026-01-03T00:00:00Z", "a3"))
        grown[0]["updated_at"] = "2026-01-03T00:00:00Z"
        p2 = tmpfile("v2.json")
        write_json(p2, grown)
        stats, _ = ingest_claudia(db, str(p2))

        self.assertEqual(stats["messages_inserted"], 1)   # only the new message
        self.assertEqual(stats["conversations_updated"], 1)
        self.assertEqual(stats["skipped_unchanged"], 1)   # conv-b unchanged
        store = Store(db)
        try:
            row = store.get_session("conv-a")
            self.assertEqual(row["message_count"], 3)
            self.assertEqual(row["last_ts"], "2026-01-03T00:00:00Z")
        finally:
            store.close()

    def test_malformed_conversation_is_skipped_not_fatal(self):
        _, db = make_env()
        convs = [
            "this is not a conversation object",         # not a dict
            {"name": "no uuid here", "chat_messages": []},  # missing uuid
            conversation("conv-good", "Good one", [
                message("human", [tb("a real question about pistachios")],
                        "2026-01-01T00:00:01Z", "g1")]),
        ]
        path = tmpfile("conversations.json")
        write_json(path, convs)
        stats, _ = ingest_claudia(db, str(path))
        self.assertEqual(stats["malformed"], 2)
        self.assertEqual(stats["conversations_new"], 1)
        store = Store(db)
        try:
            self.assertTrue(store.search("pistachios"))
        finally:
            store.close()

    def test_empty_content_message_makes_an_inert_row(self):
        _, db = make_env()
        convs = [conversation("conv-e", "Has an empty turn", [
            message("human", [tb("real text here about umbrellas")],
                    "2026-01-01T00:00:01Z", "e1"),
            message("assistant", [], "2026-01-01T00:00:02Z", "e2"),  # empty shell
        ])]
        path = tmpfile("conversations.json")
        write_json(path, convs)
        ingest_claudia(db, str(path))
        store = Store(db)
        try:
            # both message rows exist, only one is searchable
            n = store.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id='conv-e'").fetchone()[0]
            self.assertEqual(n, 2)
            searchable = store.conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN messages_fts f ON f.rowid=m.id "
                "WHERE m.session_id='conv-e'").fetchone()[0]
            self.assertEqual(searchable, 1)
        finally:
            store.close()

    def test_full_rebuild_preserves_the_claudia_archive(self):
        # A full rebuild of the tailing corpora (clear_all) must not wipe claudia,
        # which cannot be rebuilt from a live local source.
        from session_recall.indexer import run_index
        root, db = make_env()
        path = tmpfile("conversations.json")
        write_json(path, sample_convs())
        ingest_claudia(db, str(path))
        run_index(db, root=root, full=True, verbose=False)  # empty claude corpus
        store = Store(db)
        try:
            n = store.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE source='claudia'").fetchone()[0]
            self.assertEqual(n, 3)
            self.assertTrue(store.search("widget crash", source="claudia"))
        finally:
            store.close()

    def test_inspect_reports_structure_without_content(self):
        path = tmpfile("conversations.json")
        write_json(path, sample_convs())
        shape = inspect_export(str(path))
        self.assertEqual(shape["top_level"], "json-array")
        self.assertEqual(shape["total_conversations"], 2)
        self.assertIn("uuid", shape["conversation_keys"])
        self.assertEqual(shape["message_array_key"], "chat_messages")
        # the structural report must not leak message content
        blob = json.dumps(shape)
        self.assertNotIn("widget", blob)
        self.assertNotIn("Kyoto", blob)


if __name__ == "__main__":
    unittest.main()
