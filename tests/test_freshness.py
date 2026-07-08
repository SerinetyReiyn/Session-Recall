"""Tests for the freshness guard (phase 0.4).

is_stale is the pure decision function the guard is built on; it is tested
directly. maybe_refresh is exercised end to end over a temp corpus to confirm it
runs an incremental pass only when the index is stale and picks up new files.
"""

import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import make_env, user_line, write_jsonl
from session_recall.indexer import is_stale, maybe_refresh, run_index
from session_recall.store import Store


class TestIsStale(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(is_stale(None, self.now, 900))
        self.assertTrue(is_stale("", self.now, 900))

    def test_unparseable_timestamp_is_stale(self):
        self.assertTrue(is_stale("not-a-date", self.now, 900))

    def test_recent_is_fresh(self):
        last = (self.now - timedelta(seconds=60)).isoformat()
        self.assertFalse(is_stale(last, self.now, 900))

    def test_old_is_stale(self):
        last = (self.now - timedelta(seconds=1000)).isoformat()
        self.assertTrue(is_stale(last, self.now, 900))

    def test_boundary_is_not_stale(self):
        # Exactly at the threshold is not stale (strictly greater than).
        last = (self.now - timedelta(seconds=900)).isoformat()
        self.assertFalse(is_stale(last, self.now, 900))

    def test_naive_timestamp_assumed_utc(self):
        last = (self.now - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
        self.assertFalse(is_stale(last, self.now, 900))

    def test_threshold_zero_refreshes_any_positive_age(self):
        last = (self.now - timedelta(seconds=1)).isoformat()
        self.assertTrue(is_stale(last, self.now, 0))
        self.assertFalse(is_stale(self.now.isoformat(), self.now, 0))


class TestMaybeRefresh(unittest.TestCase):
    def test_refresh_runs_only_when_stale_and_indexes_new_files(self):
        root, db = make_env()
        write_jsonl(root / "c--projA" / "s1.jsonl",
                    [user_line("u1", "alpha keyword", session="sA")])
        run_index(db, root=root, full=True, verbose=False)

        # A large threshold means the fresh index is not stale: no refresh.
        self.assertFalse(maybe_refresh(db, threshold_seconds=10_000, root=root))

        # A new file appears. With now well past the last index, the guard is
        # stale and refreshes, picking up the new file.
        write_jsonl(root / "c--projB" / "s2.jsonl",
                    [user_line("u2", "beta keyword", session="sB")])
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertTrue(maybe_refresh(db, threshold_seconds=0, now=future, root=root))

        store = Store(db)
        try:
            hits = store.search("beta")
        finally:
            store.close()
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
