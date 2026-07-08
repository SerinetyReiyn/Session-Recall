"""Defensive parsing: malformed lines are counted, never fatal, and never drop
the rest of a file; tool_result previews are capped with is_truncated set.
"""

import unittest
from collections import Counter

from tests.helpers import make_env, tool_result_line, user_line, write_jsonl
from session_recall.parser import parse_file, TOOL_RESULT_CAP


class DefensiveParsing(unittest.TestCase):
    def test_malformed_block_counted_and_tail_preserved(self):
        root, _ = make_env()
        jsonl = root / "s1.jsonl"
        # Middle line has a non-string text leaf, which would raise inside the
        # per-entry parse; it must be counted and skipped without losing line 3.
        bad = '{"type":"assistant","uuid":"bad","message":{"role":"assistant",' \
              '"content":[{"type":"text","text":12345}]}}'
        write_jsonl(jsonl, [user_line("u1", "good one"), bad, user_line("u3", "tail with ECONNRESET")])
        stats = Counter()
        records = list(parse_file(str(jsonl), stats))
        texts = [r["text"] for r in records]
        self.assertEqual(stats["parse_errors"], 1)
        self.assertEqual(stats["lines"], 3)
        self.assertTrue(any("ECONNRESET" in t for t in texts), "tail after bad line was dropped")

    def test_invalid_json_line_counted(self):
        root, _ = make_env()
        jsonl = root / "s1.jsonl"
        write_jsonl(jsonl, [user_line("u1", "ok"), "{not valid json", user_line("u2", "ok2")])
        stats = Counter()
        records = list(parse_file(str(jsonl), stats))
        self.assertEqual(stats["json_errors"], 1)
        self.assertEqual(len(records), 2)

    def test_tool_result_cap_and_truncation_flag(self):
        root, _ = make_env()
        jsonl = root / "s1.jsonl"
        big = "X" * (TOOL_RESULT_CAP + 500)
        write_jsonl(jsonl, [tool_result_line("r1", "tu_x", big)])
        stats = Counter()
        records = list(parse_file(str(jsonl), stats))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["entry_type"], "tool_result")
        self.assertEqual(rec["is_truncated"], 1)
        self.assertLessEqual(rec["char_len"], TOOL_RESULT_CAP)


if __name__ == "__main__":
    unittest.main()
