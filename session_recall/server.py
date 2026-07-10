"""Session_Recall MCP server (stdio).

Exposes six tools over the Model Context Protocol so any Claude Code session can
search and surgically read the transcript archive without pulling whole
transcripts into context. Hard output caps on every tool are the point of the
design: this server exists to protect the context window and must never flood
it. Oversized requests return the first slice plus a continuation hint, never an
error and never the full payload.

Runs over stdio. Nothing here writes to stdout except the MCP protocol itself;
all logging goes to stderr. Registered at user scope as "session-recall", so its
tools appear as mcp__session-recall__<tool> (which the indexer's self-echo
exclusion is configured to drop).

API verified against the official Python MCP SDK (mcp) v1.28.1.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import DEFAULT_PER_SESSION_CAP
from .indexer import maybe_refresh, run_index
from .parser import read_full_text
from .parser_codex import read_full_text_codex
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] session-recall: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("session_recall.server")

SERVER_NAME = "session-recall"

DB_PATH = os.environ.get(
    "SESSION_RECALL_DB",
    str(Path(__file__).resolve().parent.parent / "data" / "recall.db"),
)

# Hard caps. These protect the caller's context window.
SEARCH_SNIPPET_CHARS = 300
SEARCH_RESPONSE_CHARS = 4000
SEARCH_MAX_LIMIT = 25
LIST_PROMPT_CHARS = 150
LIST_MAX_LIMIT = 50
OUTLINE_HEAD_CHARS = 120
OUTLINE_RESPONSE_CHARS = 8000
OUTLINE_MAX_ITEMS = 300
READ_DEFAULT_MAX_CHARS = 6000
READ_HARD_MAX_CHARS = 20000
READ_MAX_COUNT = 50

mcp = FastMCP(SERVER_NAME)


def _clamp(value, low, high):
    return max(low, min(value, high))


def _oneline(text, cap):
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed[:cap]


def _ensure_fresh(reason):
    """Freshness guard: run an incremental pass first if the index is stale.

    Best effort: a refresh failure must never break a read. When a refresh runs,
    the last index time advances, which is observable via recall_status.
    """
    try:
        if maybe_refresh(DB_PATH, verbose=False):
            logger.info("index was stale; ran incremental refresh before %s", reason)
    except Exception as exc:
        logger.warning("freshness refresh before %s failed: %s", reason, exc)


@mcp.tool()
def search_history(query: str, project: str = None, role: str = None,
                   limit: int = 10, per_session_cap: int = DEFAULT_PER_SESSION_CAP,
                   source: str = None) -> dict:
    """Search the transcript archive with ranked full-text search.

    Use this first, before re-deriving a solution, design decision, or diagnosis.
    Two to four distinctive keywords work best; wrap an exact phrase in double
    quotes (for example '"comprehensive review"'). Results are diversified so no
    single session dominates. The archive spans Claude Code, Codex, and Claudia
    (claude.ai / Claude Desktop) sessions, so history from any of them is
    searchable; a search with no source filter includes all three.

    Args:
        query: Search terms. Quoted substrings are matched as exact phrases.
        project: Optional project key or working-directory substring to filter by.
        role: Optional role filter: user, assistant, system, or summary.
        limit: Max hits to return (capped).
        per_session_cap: Max hits from any one session (diversity).
        source: Optional corpus filter: 'claude', 'codex', or 'claudia'. None
            searches all of them.

    Returns:
        A dict with the ranked hits (snippet, session_id, uuid, timestamp,
        project, source, role, tool_name), capped to protect the context window.
    """
    _ensure_fresh("search_history")
    limit = _clamp(limit, 1, SEARCH_MAX_LIMIT)
    per_session_cap = _clamp(per_session_cap, 1, limit)
    store = Store(DB_PATH)
    try:
        rows = store.search(query, project=project, role=role, limit=limit,
                            per_session_cap=per_session_cap, source=source)
    finally:
        store.close()

    hits = []
    used = 0
    for row in rows:
        snippet = _oneline(row["snippet"], SEARCH_SNIPPET_CHARS)
        hit = {
            "snippet": snippet,
            "session_id": row["session_id"],
            "uuid": row["uuid"],
            "timestamp": row["ts"],
            "project": row["cwd"] or row["project_key"],
            "source": row["source"],
            "role": row["role"],
            "entry_type": row["entry_type"],
            "tool_name": row["tool_name"],
        }
        used += len(snippet) + 160  # rough per-hit metadata budget
        if hits and used > SEARCH_RESPONSE_CHARS:
            break
        hits.append(hit)

    result = {"query": query, "returned": len(hits), "hits": hits}
    if len(hits) < len(rows):
        result["note"] = ("Response capped. Refine the query or use "
                          "get_session_outline plus read_messages on a session.")
    return result


@mcp.tool()
def list_sessions(project: str = None, limit: int = 15) -> dict:
    """List indexed sessions, most recent first.

    Args:
        project: Optional project key or working-directory substring to filter by.
        limit: Max sessions to return (capped).

    Returns:
        A dict of sessions with id, project, timestamps, message count, and the
        first user prompt (trimmed).
    """
    _ensure_fresh("list_sessions")
    limit = _clamp(limit, 1, LIST_MAX_LIMIT)
    store = Store(DB_PATH)
    try:
        rows = store.list_sessions(project=project, limit=limit)
    finally:
        store.close()
    sessions = [{
        "session_id": r["session_id"],
        "project": r["cwd"] or r["project_key"],
        "started": r["started_ts"],
        "last": r["last_ts"],
        "message_count": r["message_count"],
        "first_user_prompt": _oneline(r["first_user_prompt"], LIST_PROMPT_CHARS),
    } for r in rows]
    return {"returned": len(sessions), "sessions": sessions}


@mcp.tool()
def get_session_outline(session_id: str, max_items: int = 100) -> dict:
    """Return an ordered skim of a session: look before you read.

    Use this to locate the messages worth reading, then fetch them with
    read_messages. Never pull a whole session into context.

    Args:
        session_id: The session to outline.
        max_items: Max entries to list (capped).

    Returns:
        A dict of ordered items (uuid, role, kind, timestamp, tool_name, head),
        capped to protect the context window.
    """
    _ensure_fresh("get_session_outline")
    max_items = _clamp(max_items, 1, OUTLINE_MAX_ITEMS)
    store = Store(DB_PATH)
    try:
        rows = store.get_session_outline(session_id, max_items=max_items)
    finally:
        store.close()

    items = []
    used = 0
    for row in rows:
        head = _oneline(row["head"], OUTLINE_HEAD_CHARS)
        used += len(head) + 80
        if items and used > OUTLINE_RESPONSE_CHARS:
            break
        items.append({
            "uuid": row["uuid"],
            "role": row["role"],
            "kind": row["entry_type"],
            "timestamp": row["ts"],
            "tool_name": row["tool_name"],
            "head": head,
        })
    result = {"session_id": session_id, "returned": len(items), "items": items}
    if len(items) < len(rows):
        result["note"] = ("Outline capped. Call again with a larger max_items or "
                          "read_messages starting from the last uuid shown.")
    return result


@mcp.tool()
def read_messages(session_id: str = None, uuids: list = None, start_uuid: str = None,
                  count: int = 10, max_chars: int = READ_DEFAULT_MAX_CHARS,
                  start_char: int = 0) -> dict:
    """Read exact stored text for specific messages.

    Provide either a list of uuids, or a start_uuid (plus count) to read forward
    from a point in a session, or just a session_id (plus count) to read from its
    start. If the indexed copy was truncated, the full text is fetched from the
    raw transcript. Bounded by max_chars: if the request exceeds it, the first
    slice is returned with a continuation hint that is always followable.

    The continuation hint depends on how you called this tool: after a uuids
    request it carries next_uuids (call again with uuids=next_uuids); otherwise it
    carries next_start_uuid (call again with start_uuid=next_start_uuid). If a
    single message is longer than max_chars, the hint also carries next_start_char
    so the message can be paged through in bounded slices until fully read.

    Args:
        session_id: Session to read from (required unless uuids or start_uuid is given).
        uuids: Specific message uuids to read.
        start_uuid: Read forward starting at this uuid.
        count: How many messages to read when not passing uuids (capped).
        max_chars: Total character budget for returned text (capped).
        start_char: Character offset into the first message (for paging a long one).

    Returns:
        A dict of messages with their exact text, plus a continuation hint if the
        budget was reached before all requested messages were returned.
    """
    count = _clamp(count, 1, READ_MAX_COUNT)
    max_chars = _clamp(max_chars, 200, READ_HARD_MAX_CHARS)
    start_char = max(0, start_char)
    uuid_mode = bool(uuids)
    if uuids:
        uuids = list(uuids)[:READ_MAX_COUNT]

    store = Store(DB_PATH)
    try:
        targets = store.resolve_read_targets(session_id, uuids=uuids,
                                             start_uuid=start_uuid, count=count)
        prepared = []
        for t in targets:
            text = store.stored_text(t["id"])
            if t["is_truncated"] and t["path"]:
                if t["source"] == "codex":
                    full = read_full_text_codex(t["path"], t["line_no"])
                else:
                    full = read_full_text(t["path"], t["line_no"])
                if full:
                    text = full
            prepared.append((t, text))
    finally:
        store.close()

    def _defer(idx, t):
        # Emit a hint the caller can actually follow, matching how they called in.
        if uuid_mode:
            return {"next_uuids": [p[0]["uuid"] for p in prepared[idx:]],
                    "note": "Budget reached. Call again with uuids set to next_uuids."}
        return {"next_start_uuid": t["uuid"],
                "note": "Budget reached. Call again with start_uuid set to next_start_uuid."}

    messages = []
    used = 0
    continuation = None
    for idx, (t, full_text) in enumerate(prepared):
        # start_char only offsets the first message (for paging a long one).
        text = full_text[start_char:] if idx == 0 and start_char else full_text
        remaining = max_chars - used
        if remaining <= 0:
            continuation = _defer(idx, t)
            break
        if len(text) > remaining:
            if messages:
                continuation = _defer(idx, t)
                break
            # First (or resumed) message is itself larger than the budget: return
            # a bounded slice and a hint that advances a char offset so the rest
            # is reachable and the loop terminates.
            slice_text = text[:remaining]
            entry = _entry(t, slice_text, truncated=True)
            if idx == 0 and start_char:
                entry["char_offset"] = start_char
            messages.append(entry)
            abs_next = (start_char if idx == 0 else 0) + len(slice_text)
            if abs_next < len(full_text):
                if uuid_mode:
                    continuation = {"next_uuids": [t["uuid"]], "next_start_char": abs_next,
                                    "note": "Message longer than max_chars; call again with the same uuids and start_char=next_start_char."}
                else:
                    continuation = {"next_start_uuid": t["uuid"], "next_start_char": abs_next,
                                    "note": "Message longer than max_chars; call again with the same start_uuid and start_char=next_start_char."}
            break
        entry = _entry(t, text)
        if idx == 0 and start_char:
            entry["char_offset"] = start_char
        messages.append(entry)
        # Charge a fixed metadata budget per message so many tiny or empty-text
        # entries still trip the cap rather than returning unbounded rows.
        used += len(text) + 120

    result = {"returned": len(messages), "messages": messages}
    if continuation:
        result["continuation"] = continuation
    return result


def _entry(t, text, truncated=False):
    entry = {
        "uuid": t["uuid"],
        "role": t["role"],
        "kind": t["entry_type"],
        "timestamp": t["ts"],
        "tool_name": t["tool_name"],
        "text": text,
    }
    if truncated:
        entry["truncated"] = True
    return entry


@mcp.tool()
def recall_status() -> dict:
    """Report index health: file, message, and session counts, journal mode, and
    the last index time. Answers even while an index pass is running (WAL)."""
    store = Store(DB_PATH)
    try:
        s = store.status()
    finally:
        store.close()
    return {
        "version": __version__,
        "transcript_files": s["transcript_files"],
        "sidecars": s["sidecars"],
        "messages": s["messages"],
        "sessions": s["sessions"],
        "journal_mode": s["journal_mode"],
        "last_index_time": s["last_index_time"],
        "indexed_by_type": s["indexed_by_type"],
        "skipped_by_type": s["skipped_by_type"],
    }


@mcp.tool()
def reindex(full: bool = False) -> dict:
    """Trigger an index pass. Incremental by default; full rebuilds from scratch.

    Args:
        full: If true, clear and rebuild the whole index (slow). Otherwise tail
            only what changed (fast).

    Returns:
        A summary of the pass (files new/appended/unchanged, messages stored).
    """
    logger.info("reindex requested (full=%s)", full)
    stats = run_index(DB_PATH, full=full, verbose=False)
    return {
        "full": full,
        "files_discovered": stats.get("files_discovered", 0),
        "files_new": stats.get("files_new", 0),
        "files_appended": stats.get("files_appended", 0),
        "files_unchanged": stats.get("files_unchanged", 0),
        "files_replaced": stats.get("files_replaced", 0),
        "messages_stored": stats.get("stored", 0),
        "sidecars_indexed": stats.get("sidecars_indexed", 0),
        "parse_errors": stats.get("parse_errors", 0),
        "file_errors": stats.get("file_errors", 0),
    }


def main():
    logger.info("Starting Session_Recall MCP server %s (db=%s)", __version__, DB_PATH)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
