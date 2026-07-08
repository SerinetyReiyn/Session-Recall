"""Command line interface: index, search, and status.

Usage (from the project root):
    python -m session_recall index [--full]
    python -m session_recall search "query" [--project X] [--role user] [--limit N] [--cap K]
    python -m session_recall status

No MCP required. This is the direct command-line interface; the MCP server
(phase 0.3) lives in server.py and reuses the same store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_PER_SESSION_CAP
from .indexer import default_root, run_index
from .store import Store


def _configure_utf8_output():
    """Make CLI output UTF-8 safe.

    The default Windows console codepage (cp1252) cannot encode characters that
    routinely appear in transcript snippets (arrows, box drawing, non-Latin
    text), and printing one raises UnicodeEncodeError, aborting the command.
    Reconfigure the streams to UTF-8 and replace anything unencodable. MCP JSON
    responses go over a different channel and are unaffected.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

# The index lives inside the project, never in the read-only .claude tree.
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "recall.db"


def _cmd_index(args):
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    mode = "full rebuild" if args.full else "incremental"
    print(f"Session_Recall {__version__}: indexing ({mode}) from {default_root()}")
    stats = run_index(str(db), full=args.full)
    print("\nDone.")
    print(f"  files discovered : {stats.get('files_discovered', 0)}")
    print(f"    new            : {stats.get('files_new', 0)}")
    print(f"    appended       : {stats.get('files_appended', 0)}")
    print(f"    replaced       : {stats.get('files_replaced', 0)}")
    print(f"    unchanged      : {stats.get('files_unchanged', 0)}")
    print(f"  lines read       : {stats.get('lines', 0)}")
    print(f"  records parsed   : {stats.get('indexed', 0)}")
    print(f"  messages stored  : {stats.get('stored', 0)} (after uuid dedupe)")
    print(f"  sidecars indexed : {stats.get('sidecars_indexed', 0)}")
    if stats.get("codex_files_discovered"):
        print(f"  codex files      : {stats.get('codex_files_discovered', 0)} "
              f"(new {stats.get('codex_files_new', 0)}, appended {stats.get('codex_files_appended', 0)}, "
              f"unchanged {stats.get('codex_files_unchanged', 0)})")
    print(f"  json errors      : {stats.get('json_errors', 0)}")
    print(f"  parse errors     : {stats.get('parse_errors', 0)}")
    print(f"  skipped (type)   : {stats.get('skipped_type', 0)}")
    print(f"  skipped (unknown): {stats.get('skipped_unknown', 0)}")
    print(f"  skipped (empty)  : {stats.get('skipped_empty', 0)}")
    print(f"  file errors      : {stats.get('file_errors', 0)}")


def _cmd_search(args):
    db = Path(args.db)
    if not db.exists():
        print(f"No index found at {db}. Run 'python -m session_recall index' first.")
        return
    store = Store(str(db))
    try:
        rows = store.search(args.query, project=args.project, role=args.role,
                            limit=args.limit, per_session_cap=args.cap, source=args.source)
    finally:
        store.close()
    if not rows:
        print(f"No hits for {args.query!r}.")
        return
    print(f"{len(rows)} hit(s) for {args.query!r}:\n")
    for i, row in enumerate(rows, 1):
        snippet = " ".join((row["snippet"] or "").split())
        loc = row["cwd"] or row["project_key"] or "?"
        tool = f" tool={row['tool_name']}" if row["tool_name"] else ""
        print(f"[{i}] {row['ts']}  [{row['source']}] {row['role']}/{row['entry_type']}{tool}")
        print(f"    project: {loc}")
        print(f"    session: {row['session_id']}")
        print(f"    uuid   : {row['uuid']}")
        print(f"    {snippet}\n")


def _cmd_status(args):
    db = Path(args.db)
    if not db.exists():
        print(f"No index found at {db}. Run 'python -m session_recall index' first.")
        return
    store = Store(str(db))
    try:
        s = store.status()
    finally:
        store.close()
    print(f"Session_Recall {__version__} status ({db}):")
    print(f"  transcript files : {s['transcript_files']}")
    print(f"  sidecar pointers : {s['sidecars']}")
    print(f"  messages         : {s['messages']}")
    print(f"  sessions         : {s['sessions']} ({s['sessions_without_prompt']} without a first prompt)")
    by_source = s.get("by_source") or {}
    if by_source:
        for src in sorted(by_source):
            d = by_source[src]
            print(f"    {src:8s}: {d.get('messages', 0)} messages, {d.get('sessions', 0)} sessions")
    print(f"  journal_mode     : {s['journal_mode']}")
    print(f"  last index       : {s['last_index_time']}")

    indexed = s.get("indexed_by_type") or {}
    if indexed:
        print("  indexed by entry type:")
        for k, v in indexed.items():
            print(f"    {k:12s}: {v}")

    by_type = s.get("skipped_by_type") or {}
    if by_type:
        print("  skipped by entry type (last index pass):")
        for k, v in sorted(by_type.items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {k:22s}: {v}")

    skips = s.get("skips") or {}
    if skips:
        print("  skip categories (last index pass):")
        for k, v in skips.items():
            print(f"    {k}: {v}")

    per_project = s.get("per_project") or []
    if per_project:
        print(f"  per project ({len(per_project)} folders, files = transcripts):")
        for row in per_project:
            print(f"    {row['sessions']:3d} sessions  {row['files']:5d} files  {row['project']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session_recall", description="Search the Claude Code transcript archive.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to the index database")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build or update the index (incremental by default)")
    p_index.add_argument("--full", action="store_true", help="clear and rebuild the whole index from scratch")
    p_index.set_defaults(func=_cmd_index)

    p_search = sub.add_parser("search", help="full-text search the index")
    p_search.add_argument("query", help="search terms (2 to 4 distinctive keywords work best)")
    p_search.add_argument("--project", default=None, help="filter by project key or cwd substring")
    p_search.add_argument("--role", default=None, choices=["user", "assistant", "system", "summary"], help="filter by role")
    p_search.add_argument("--limit", type=int, default=10, help="max hits to return")
    p_search.add_argument("--cap", type=int, default=DEFAULT_PER_SESSION_CAP,
                          help="max hits from any one session (diversity); 0 disables the cap")
    p_search.add_argument("--source", default=None, choices=["claude", "codex"],
                          help="filter by corpus: claude or codex (default: both)")
    p_search.set_defaults(func=_cmd_search)

    p_status = sub.add_parser("status", help="show index statistics")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv=None):
    _configure_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
