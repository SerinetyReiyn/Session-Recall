"""SQLite storage: schema, FTS5 index, message upserts, and search.

The schema follows the shape sketched in the phase 0.1 build document. The one
adaptation: messages use a surrogate integer id as the FTS rowid and enforce
uuid uniqueness with a UNIQUE constraint, rather than making the TEXT uuid the
literal primary key. This gives FTS5 a stable integer rowid to map to while
keeping uuid as the dedupe key, exactly as the sketch intends.

WAL mode is on so a query session and an index pass can overlap safely.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE,
    project_key TEXT,
    source TEXT,
    size INTEGER,
    mtime REAL,
    last_offset INTEGER,
    last_line INTEGER,
    echo_ids TEXT,
    last_indexed_ts TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_key TEXT,
    source TEXT,
    cwd TEXT,
    started_ts TEXT,
    last_ts TEXT,
    message_count INTEGER,
    first_user_prompt TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    uuid TEXT UNIQUE,
    session_id TEXT,
    parent_uuid TEXT,
    role TEXT,
    entry_type TEXT,
    source TEXT,
    ts TEXT,
    tool_name TEXT,
    file_id INTEGER,
    line_no INTEGER,
    char_len INTEGER,
    is_truncated INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(entry_type);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
USING fts5(text, tokenize='unicode61');
"""

FIRST_PROMPT_CAP = 300


class Store:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Wait rather than error if another connection briefly holds the write
        # lock (for example a freshness refresh in another session). Readers do
        # not block under WAL; this only smooths transient writer contention.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def _create_schema(self):
        self.conn.executescript(SCHEMA)
        try:
            self.conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "SQLite FTS5 is not available in this Python build. "
                "Session_Recall requires FTS5. Install a Python whose sqlite3 "
                "has FTS5 compiled in (most official CPython builds do). "
                f"Underlying error: {exc}"
            ) from exc
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after the initial schema, if missing.

        The index database is regenerable, so this only smooths over an older
        database left from a prior phase rather than being a real migration path.
        """
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(files)")}
        if "last_line" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN last_line INTEGER DEFAULT 0")
        if "echo_ids" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN echo_ids TEXT")
        # source column (phase 0.6, the Codex corpus). Default existing rows to
        # 'claude' since that was the only corpus before.
        for table in ("files", "sessions", "messages"):
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if "source" not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN source TEXT DEFAULT 'claude'"
                )

    # -- indexing writes -------------------------------------------------

    def get_file(self, path: str):
        """Return the files row for path, or None if not yet indexed."""
        return self.conn.execute(
            "SELECT id, size, mtime, last_offset, last_line, echo_ids FROM files WHERE path = ?",
            (path,),
        ).fetchone()

    def get_or_create_file(self, path: str, project_key: str, source: str = "claude") -> int:
        """Return the file id for path, creating a zero-progress row if new."""
        row = self.conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        if row is not None:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO files (path, project_key, source, size, mtime, last_offset, last_line, last_indexed_ts) "
            "VALUES (?, ?, ?, 0, 0, 0, 0, NULL)",
            (path, project_key, source),
        )
        return cur.lastrowid

    def update_file_progress(self, file_id: int, size: int, mtime: float,
                             last_offset: int, last_line: int, echo_ids=None):
        now = datetime.now(timezone.utc).isoformat()
        echo_json = json.dumps(sorted(echo_ids)) if echo_ids else None
        self.conn.execute(
            "UPDATE files SET size=?, mtime=?, last_offset=?, last_line=?, echo_ids=?, "
            "last_indexed_ts=? WHERE id=?",
            (size, mtime, last_offset, last_line, echo_json, now, file_id),
        )

    def delete_messages_for_file(self, file_id: int):
        """Drop all indexed rows for a file (used when a file was replaced)."""
        self.conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE file_id=?)",
            (file_id,),
        )
        self.conn.execute("DELETE FROM messages WHERE file_id=?", (file_id,))

    def insert_message(self, record: dict, file_id: int) -> bool:
        """Insert one message. Dedupe by uuid. Return True if newly inserted."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(uuid, session_id, parent_uuid, role, entry_type, source, ts, tool_name, "
            " file_id, line_no, char_len, is_truncated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["uuid"], record["session_id"], record["parent_uuid"],
                record["role"], record["entry_type"], record.get("source") or "claude",
                record["ts"], record["tool_name"], file_id, record["line_no"],
                record["char_len"], record["is_truncated"],
            ),
        )
        if cur.rowcount != 1:
            return False
        text = record.get("text") or ""
        if text:
            self.conn.execute(
                "INSERT INTO messages_fts (rowid, text) VALUES (?, ?)",
                (cur.lastrowid, text),
            )
        return True

    def rebuild_sessions(self, session_meta: dict):
        """Populate the sessions table from indexed messages plus captured meta.

        Counts and timestamp bounds come from the deduped messages table so they
        are correct even if the same uuid appeared in more than one file.
        session_meta carries project_key, cwd, and first_user_prompt captured
        during parsing.
        """
        # Exclude sidecar pointer rows: they carry a real session_id but are
        # not conversational messages, so counting them would inflate
        # message_count and their file-mtime timestamps could skew the time
        # bounds. IS NOT keeps any (currently nonexistent) NULL entry_type rows.
        rows = self.conn.execute(
            "SELECT session_id, MIN(ts) AS started, MAX(ts) AS last, COUNT(*) AS n "
            "FROM messages WHERE session_id IS NOT NULL AND entry_type IS NOT 'sidecar' "
            "GROUP BY session_id"
        ).fetchall()
        for row in rows:
            sid = row["session_id"]
            meta = session_meta.get(sid, {})
            prompt = meta.get("first_user_prompt")
            if prompt:
                prompt = prompt[:FIRST_PROMPT_CAP]
            # Counts and time bounds always come from the (deduped) messages
            # table. The captured metadata (project_key, cwd, first_user_prompt)
            # is only present for sessions touched this pass. For first_user_prompt
            # the stored value wins on conflict: a prior pass that parsed from
            # offset 0 captured the true first prompt, whereas an incremental
            # append only sees a mid-session prompt, so the existing value must
            # not be overwritten. The fallback still populates it from the tail
            # when no value was stored yet (no user turn seen before).
            self.conn.execute(
                "INSERT INTO sessions "
                "(session_id, project_key, source, cwd, started_ts, last_ts, message_count, "
                " first_user_prompt, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "project_key=COALESCE(excluded.project_key, sessions.project_key), "
                "source=COALESCE(excluded.source, sessions.source), "
                "cwd=COALESCE(excluded.cwd, sessions.cwd), "
                "started_ts=excluded.started_ts, last_ts=excluded.last_ts, "
                "message_count=excluded.message_count, "
                "first_user_prompt=COALESCE(sessions.first_user_prompt, excluded.first_user_prompt)",
                (sid, meta.get("project_key"), meta.get("source"), meta.get("cwd"),
                 row["started"], row["last"], row["n"], prompt, None),
            )
        self.conn.commit()

    def set_meta(self, key: str, value):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]

    def clear_all(self):
        """Drop indexed content for a full rebuild. Does not touch the schema."""
        self.conn.executescript(
            "DELETE FROM messages_fts; DELETE FROM messages; "
            "DELETE FROM sessions; DELETE FROM files; DELETE FROM meta;"
        )
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    # -- reads -----------------------------------------------------------

    @staticmethod
    def _build_match(query: str) -> str:
        """Build a safe FTS5 MATCH expression from a free-text query.

        The query is tokenized with shlex so a caller can pass an exact phrase in
        double quotes (for example '"comprehensive review"') and have it treated
        as an FTS5 phrase; bare words become single-term phrases. Every token is
        wrapped in FTS5 double quotes so operators in user input are literals, and
        tokens are ANDed together.
        """
        # Normalize backslashes to spaces first: shlex in posix mode treats a
        # backslash as an escape and would eat it, so a Windows-path query like
        # C:\Users\Serin would otherwise collapse to a single mangled token.
        normalized = query.replace("\\", " ")
        try:
            tokens = shlex.split(normalized)
        except ValueError:
            tokens = normalized.split()
        if not tokens:
            return '""'
        return " AND ".join('"' + tok.replace('"', '""') + '"' for tok in tokens)

    def search(self, query: str, project=None, role=None, limit: int = 10,
               per_session_cap=None, source=None):
        """Ranked full-text search. If per_session_cap is set, no single session
        contributes more than that many hits (diversity), preventing one noisy
        session from filling the result list. source filters by corpus
        ('claude' or 'codex'); None searches both.
        """
        match = self._build_match(query)
        sql = (
            "SELECT m.uuid, m.session_id, m.ts, m.role, m.entry_type, m.tool_name, "
            "       m.source, f.project_key, s.cwd, "
            "       snippet(messages_fts, 0, '[', ']', ' ... ', 12) AS snippet "
            "FROM messages_fts "
            "JOIN messages m ON m.id = messages_fts.rowid "
            "LEFT JOIN files f ON f.id = m.file_id "
            "LEFT JOIN sessions s ON s.session_id = m.session_id "
            "WHERE messages_fts MATCH ? "
        )
        params = [match]
        if project:
            sql += "AND (f.project_key LIKE ? OR s.cwd LIKE ?) "
            like = f"%{project}%"
            params.extend([like, like])
        if role:
            sql += "AND m.role = ? "
            params.append(role)
        if source:
            sql += "AND m.source = ? "
            params.append(source)
        sql += "ORDER BY bm25(messages_fts) "

        # A cap of 0, None, or any value below 1 means no diversity cap. This
        # guards direct callers (the CLI passes --cap unclamped): a negative cap
        # would otherwise make the per-session test true for every row and
        # silently return nothing.
        if not per_session_cap or per_session_cap < 1:
            sql += "LIMIT ?"
            params.append(limit)
            return self.conn.execute(sql, params).fetchall()

        # Over-fetch ranked candidates, then keep at most per_session_cap per
        # session in bm25 order until limit is reached.
        candidate_limit = max(limit * 5, limit + per_session_cap * 10, 100)
        sql += "LIMIT ?"
        params.append(candidate_limit)
        out = []
        per = {}
        for row in self.conn.execute(sql, params):
            sid = row["session_id"]
            if per.get(sid, 0) >= per_session_cap:
                continue
            out.append(row)
            per[sid] = per.get(sid, 0) + 1
            if len(out) >= limit:
                break
        return out

    def list_sessions(self, project=None, limit: int = 15):
        sql = ("SELECT session_id, project_key, cwd, started_ts, last_ts, "
               "message_count, first_user_prompt FROM sessions ")
        params = []
        if project:
            sql += "WHERE project_key LIKE ? OR cwd LIKE ? "
            like = f"%{project}%"
            params.extend([like, like])
        sql += "ORDER BY last_ts DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def get_session_outline(self, session_id: str, max_items: int = 100):
        # Ordered by timestamp then line number so subagent files (which share
        # the parent session_id but have independent per-file line numbers)
        # interleave sensibly rather than by raw line number.
        sql = ("SELECT m.uuid, m.role, m.entry_type, m.ts, m.tool_name, "
               "       substr(fts.text, 1, 120) AS head "
               "FROM messages m LEFT JOIN messages_fts fts ON fts.rowid = m.id "
               "WHERE m.session_id = ? ORDER BY m.ts, m.line_no LIMIT ?")
        return self.conn.execute(sql, (session_id, max_items)).fetchall()

    def resolve_read_targets(self, session_id, uuids=None, start_uuid=None, count: int = 10):
        """Return message rows to read, ordered by (ts, line_no), with the raw
        file path for on-demand full-text fetch.
        """
        base = ("SELECT m.id, m.uuid, m.role, m.entry_type, m.ts, m.tool_name, "
                "       m.source, m.is_truncated, m.line_no, f.path "
                "FROM messages m LEFT JOIN files f ON f.id = m.file_id ")
        if uuids:
            placeholders = ",".join("?" * len(uuids))
            return self.conn.execute(
                base + f"WHERE m.uuid IN ({placeholders}) ORDER BY m.ts, m.line_no",
                list(uuids),
            ).fetchall()
        if start_uuid:
            start = self.conn.execute(
                "SELECT session_id, ts, line_no FROM messages WHERE uuid = ?", (start_uuid,)
            ).fetchone()
            if start is None:
                return []
            # Fall back to the start message's own session, so a continuation
            # hint of just start_uuid is followable without passing session_id.
            sid = session_id or start["session_id"]
            return self.conn.execute(
                base + "WHERE m.session_id = ? AND (m.ts, m.line_no) >= (?, ?) "
                "ORDER BY m.ts, m.line_no LIMIT ?",
                (sid, start["ts"], start["line_no"], count),
            ).fetchall()
        return self.conn.execute(
            base + "WHERE m.session_id = ? ORDER BY m.ts, m.line_no LIMIT ?",
            (session_id, count),
        ).fetchall()

    def stored_text(self, message_id: int) -> str:
        row = self.conn.execute(
            "SELECT text FROM messages_fts WHERE rowid = ?", (message_id,)
        ).fetchone()
        return row[0] if row and row[0] else ""

    def status(self) -> dict:
        c = self.conn
        file_count = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        message_count = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        session_count = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        sidecar_count = c.execute(
            "SELECT COUNT(*) FROM messages WHERE entry_type='sidecar'"
        ).fetchone()[0]
        transcript_files = c.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE '%.jsonl'"
        ).fetchone()[0]
        journal_mode = c.execute("PRAGMA journal_mode").fetchone()[0]

        indexed_by_type = {
            row[0]: row[1]
            for row in c.execute(
                "SELECT entry_type, COUNT(*) FROM messages GROUP BY entry_type ORDER BY 2 DESC"
            )
        }

        # Per-source (corpus) message and session counts: claude vs codex.
        by_source = {
            row[0]: {"messages": row[1]}
            for row in c.execute(
                "SELECT source, COUNT(*) FROM messages GROUP BY source"
            )
        }
        for row in c.execute("SELECT source, COUNT(*) FROM sessions GROUP BY source"):
            by_source.setdefault(row[0], {"messages": 0})["sessions"] = row[1]

        # Per-project transcript-file and session counts (the 0.2 reconciliation).
        files_by_project = {
            row[0]: row[1]
            for row in c.execute(
                "SELECT project_key, COUNT(*) FROM files WHERE path LIKE '%.jsonl' GROUP BY project_key"
            )
        }
        sessions_by_project = {
            row[0]: row[1]
            for row in c.execute(
                "SELECT project_key, COUNT(*) FROM sessions GROUP BY project_key"
            )
        }
        per_project = [
            {"project": pk,
             "files": files_by_project.get(pk, 0),
             "sessions": sessions_by_project.get(pk, 0)}
            for pk in sorted(set(files_by_project) | set(sessions_by_project))
        ]
        sessions_without_prompt = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE first_user_prompt IS NULL"
        ).fetchone()[0]

        return {
            "files": file_count,
            "transcript_files": transcript_files,
            "sidecars": sidecar_count,
            "messages": message_count,
            "sessions": session_count,
            "sessions_without_prompt": sessions_without_prompt,
            "journal_mode": journal_mode,
            "indexed_by_type": indexed_by_type,
            "by_source": by_source,
            "skipped_by_type": self.get_meta("last_skips_by_type", {}),
            "skips": self.get_meta("last_skips", {}),
            "per_project": per_project,
            "last_index_time": self.get_meta("last_index_time"),
            "last_index_projects": self.get_meta("last_index_projects", []),
        }
