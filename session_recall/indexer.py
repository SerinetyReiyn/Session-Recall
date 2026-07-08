"""Discovery and orchestration for the index pass.

Phase 0.2 covers the full transcript corpus with incremental indexing. Each
file's byte offset and line count are persisted, so a rerun tails only the
bytes appended since the last pass (append-only transcripts), detects replaced
or truncated files by a shrunk size and reparses them from zero, and skips
unchanged files after a single stat. Large tool results stored as separate
sidecar files are recorded as pointers only; their content is never ingested.

This module only ever reads the transcript archive. C:\\Users\\Serin\\.claude
is strictly read only for this project: nothing here writes, renames, or
touches anything under it, and no file mtime is modified.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import (freshness_threshold_seconds, self_echo_prefixes,
                     self_echo_prefixes_codex)
from .parser import parse_file
from .parser_codex import (codex_project_key, parse_codex_file,
                           read_codex_session_meta)
from .store import Store

# The transcript archive root. Overridable via env var for testing, but the
# value below is the verified location on this machine.
DEFAULT_ROOT = Path(
    os.environ.get("SESSION_RECALL_ROOT", r"C:\Users\Serin\.claude\projects")
)

# Codex keeps its session rollouts under ~/.codex. This is the second corpus, so
# both Claude Code and Codex share one index and can see each other's history.
DEFAULT_CODEX_HOME = Path(
    os.environ.get("SESSION_RECALL_CODEX_HOME", r"C:\Users\Serin\.codex")
)


def default_root() -> Path:
    return DEFAULT_ROOT


def default_codex_home() -> Path:
    return DEFAULT_CODEX_HOME


def discover_codex(codex_home: Path):
    """Return a list of Codex rollout file paths (sessions + archived_sessions)."""
    codex_home = Path(codex_home)
    found = []
    for sub in ("sessions", "archived_sessions"):
        base = codex_home / sub
        if base.exists():
            found.extend(p for p in base.rglob("rollout-*.jsonl") if p.is_file())
    return sorted(found)


def is_stale(last_index_iso, now, threshold_seconds) -> bool:
    """Return True if an index built at last_index_iso is older than the
    threshold relative to now, or if the timestamp is missing or unparseable.

    Pure and side-effect free, so the freshness decision is unit testable
    without touching the corpus.
    """
    if not last_index_iso:
        return True
    try:
        last = datetime.fromisoformat(last_index_iso)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() > threshold_seconds


def maybe_refresh(db_path, threshold_seconds=None, now=None, verbose=False,
                  root=None) -> bool:
    """Freshness guard: if the last index pass is older than the threshold, run
    an incremental pass first. Returns True if a refresh ran.

    This is what keeps the index current with no watcher daemon: an incremental
    pass over the full corpus costs well under a second, so a read tool can
    cheaply ensure freshness before answering.
    """
    if threshold_seconds is None:
        threshold_seconds = freshness_threshold_seconds()
    if now is None:
        now = datetime.now(timezone.utc)
    store = Store(db_path)
    try:
        last = store.get_meta("last_index_time")
    finally:
        store.close()
    if not is_stale(last, now, threshold_seconds):
        return False
    run_index(db_path, root=root, full=False, verbose=verbose)
    return True


def _project_key(root: Path, path: Path):
    try:
        return path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return None


def discover(root: Path, filters=None):
    """Return a list of (jsonl_path, project_key) for the corpus.

    project_key is the immediate encoded folder name under root. The glob is
    recursive so subagent and workflow transcripts are included. If filters is
    given (a tuple of case-insensitive folder-name substrings) discovery is
    limited to matching project folders; otherwise the full corpus is returned.
    """
    root = Path(root)
    if not root.exists():
        return []
    lowered = tuple(f.lower() for f in filters) if filters else None
    found = []
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        pk = _project_key(root, path)
        if lowered and not (pk and any(f in pk.lower() for f in lowered)):
            continue
        found.append((path, pk))
    return found


def discover_sidecars(root: Path):
    """Yield (sidecar_path, project_key, session_id) for tool-result sidecars.

    Any file under a tool-results directory is a sidecar: a large tool result
    offloaded to its own file. We index a pointer only, never the content.
    """
    root = Path(root)
    if not root.exists():
        return
    for tr_dir in sorted(root.rglob("tool-results")):
        if not tr_dir.is_dir():
            continue
        session_id = tr_dir.parent.name
        project_key = _project_key(root, tr_dir)
        for path in sorted(tr_dir.rglob("*")):
            if path.is_file():
                yield path, project_key, session_id


def _sidecar_record(path: Path, session_id, mtime: float) -> dict:
    name = path.name
    text = f"sidecar {name}"
    return {
        "uuid": "sidecar:" + str(path),
        "session_id": session_id,
        "parent_uuid": None,
        "role": "sidecar",
        "entry_type": "sidecar",
        "ts": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        "tool_name": None,
        "cwd": None,
        "char_len": len(text),
        "is_truncated": 0,
        "text": text,
        "line_no": 0,
    }


def _seed_echo_ids(existing, start_offset):
    """Seed the self-echo id set from a file's persisted set when appending, so a
    self-tool output whose call was suppressed in an earlier pass is still
    dropped when it arrives in a later incremental pass."""
    if existing is not None and start_offset > 0 and existing["echo_ids"]:
        try:
            return set(json.loads(existing["echo_ids"]))
        except (TypeError, ValueError):
            return set()
    return set()


def _capture_session_meta(session_meta: dict, record: dict, project_key, source="claude"):
    sid = record.get("session_id")
    if not sid:
        return
    meta = session_meta.setdefault(
        sid, {"project_key": project_key, "source": source, "cwd": None,
              "first_user_prompt": None}
    )
    if meta["cwd"] is None and record.get("cwd"):
        meta["cwd"] = record["cwd"]
    # Gate on entry_type, not role: tool_result entries also carry role
    # "user" but hold tool output, not a prompt. entry_type == "user" is set
    # only for genuine user text (see parser._parse_message_entry).
    if meta["first_user_prompt"] is None and record.get("entry_type") == "user":
        text = record.get("text")
        if text:
            meta["first_user_prompt"] = text


def run_index(db_path, root=None, filters=None, full=False, verbose=True,
              progress_every=250, codex_home=None):
    """Run an index pass. Incremental by default; full=True rebuilds from zero.

    Indexes two corpora into one database: Claude Code transcripts under root and
    Codex rollouts under codex_home. When codex_home is None, the Codex corpus is
    indexed only for a real default-root pass (root is None); a caller that passes
    a custom root (the tests) gets Codex only if it passes codex_home explicitly.

    Returns a stats dict. A single bad file is counted (file_errors) and skipped,
    never allowed to abort the run.
    """
    real_default = root is None
    root = Path(root) if root else default_root()
    store = Store(db_path)
    try:
        if full:
            store.clear_all()

        targets = discover(root, filters)
        stats = Counter()
        stats["files_discovered"] = len(targets)
        session_meta = {}
        type_skips = Counter()
        echo_prefixes = self_echo_prefixes()

        for i, (path, project_key) in enumerate(targets, 1):
            try:
                st = path.stat()
            except OSError:
                stats["file_errors"] += 1
                continue

            existing = None if full else store.get_file(str(path))
            if existing is not None:
                stored_offset = existing["last_offset"] or 0
                if st.st_size == stored_offset:
                    stats["files_unchanged"] += 1
                    _maybe_progress(verbose, i, len(targets), stats, progress_every)
                    continue
                if st.st_size < stored_offset:
                    # File shrank: it was replaced or truncated. Drop its old
                    # rows and reparse from the beginning.
                    store.delete_messages_for_file(existing["id"])
                    start_offset, start_line = 0, 0
                    stats["files_replaced"] += 1
                else:
                    start_offset = stored_offset
                    start_line = existing["last_line"] or 0
                    stats["files_appended"] += 1
            else:
                start_offset, start_line = 0, 0
                stats["files_new"] += 1

            try:
                file_id = store.get_or_create_file(str(path), project_key, source="claude")
                echo_ids = _seed_echo_ids(existing, start_offset)
                progress = {}
                for record in parse_file(str(path), stats, start_offset, start_line,
                                         progress, echo_prefixes, type_skips, echo_ids):
                    if store.insert_message(record, file_id):
                        stats["stored"] += 1
                    _capture_session_meta(session_meta, record, project_key, source="claude")
                end_offset = progress.get("end_offset", start_offset)
                end_line = progress.get("end_line", start_line)
                store.update_file_progress(file_id, st.st_size, st.st_mtime, end_offset, end_line, echo_ids)
                store.commit()
                stats["files_indexed"] += 1
            except Exception as exc:
                # Keep each file atomic: discard this file's partial, uncommitted
                # rows so they cannot be committed by a later file's commit.
                store.rollback()
                stats["file_errors"] += 1
                if verbose:
                    print(f"  ERROR on {path}: {exc}")
            _maybe_progress(verbose, i, len(targets), stats, progress_every)

        _index_sidecars(store, root, stats, full, verbose)

        # Second corpus: Codex rollouts. Indexed into the same database so both
        # tools share one memory.
        codex_target = None
        if codex_home is not None:
            codex_target = Path(codex_home)
        elif real_default:
            codex_target = default_codex_home()
        if codex_target is not None:
            _index_codex(store, codex_target, stats, session_meta, full, verbose,
                         progress_every)

        store.rebuild_sessions(session_meta)

        skips = {
            "json_errors": stats.get("json_errors", 0),
            "parse_errors": stats.get("parse_errors", 0),
            "skipped_type": stats.get("skipped_type", 0),
            "skipped_unknown": stats.get("skipped_unknown", 0),
            "skipped_empty": stats.get("skipped_empty", 0),
            "blank": stats.get("blank", 0),
            "file_errors": stats.get("file_errors", 0),
        }
        store.set_meta("last_skips", skips)
        store.set_meta("last_skips_by_type", dict(type_skips))
        store.set_meta("last_index_time", datetime.now(timezone.utc).isoformat())
        store.set_meta("last_index_projects", sorted({pk for _, pk in targets if pk}))
        return dict(stats)
    finally:
        store.close()


def _index_sidecars(store, root, stats, full, verbose):
    for path, project_key, session_id in discover_sidecars(root):
        try:
            st = path.stat()
        except OSError:
            stats["file_errors"] += 1
            continue
        existing = None if full else store.get_file(str(path))
        if existing is not None and st.st_size == (existing["last_offset"] or 0):
            stats["sidecars_unchanged"] += 1
            continue
        try:
            file_id = store.get_or_create_file(str(path), project_key)
            record = _sidecar_record(path, session_id, st.st_mtime)
            if store.insert_message(record, file_id):
                stats["sidecars_indexed"] += 1
            # Sidecar content is never read, so last_offset = size marks it done.
            store.update_file_progress(file_id, st.st_size, st.st_mtime, st.st_size, 0)
            store.commit()
        except Exception as exc:
            store.rollback()
            stats["file_errors"] += 1
            if verbose:
                print(f"  ERROR on sidecar {path}: {exc}")


def _index_codex(store, codex_home, stats, session_meta, full, verbose, progress_every):
    """Index the Codex rollout corpus into the shared database (source='codex').

    Mirrors the Claude file loop: per-file change detection by byte offset, atomic
    per-file commits, and defensive skipping. session_id and cwd come from each
    file's session_meta line and are threaded into the parser.
    """
    targets = discover_codex(codex_home)
    stats["codex_files_discovered"] = len(targets)
    echo_names = self_echo_prefixes_codex()

    for i, path in enumerate(targets, 1):
        try:
            st = path.stat()
        except OSError:
            stats["file_errors"] += 1
            continue

        existing = None if full else store.get_file(str(path))
        if existing is not None:
            stored_offset = existing["last_offset"] or 0
            if st.st_size == stored_offset:
                stats["codex_files_unchanged"] += 1
                _maybe_progress(verbose, i, len(targets), stats, progress_every)
                continue
            if st.st_size < stored_offset:
                store.delete_messages_for_file(existing["id"])
                start_offset, start_line = 0, 0
                stats["codex_files_replaced"] += 1
            else:
                start_offset = stored_offset
                start_line = existing["last_line"] or 0
                stats["codex_files_appended"] += 1
        else:
            start_offset, start_line = 0, 0
            stats["codex_files_new"] += 1

        try:
            session_id, cwd = read_codex_session_meta(str(path))
            project_key = codex_project_key(cwd)
            file_id = store.get_or_create_file(str(path), project_key, source="codex")
            echo_call_ids = _seed_echo_ids(existing, start_offset)
            progress = {}
            for record in parse_codex_file(str(path), stats, start_offset, start_line,
                                           progress, session_id=session_id, cwd=cwd,
                                           echo_names=echo_names, echo_call_ids=echo_call_ids):
                if store.insert_message(record, file_id):
                    stats["stored"] += 1
                _capture_session_meta(session_meta, record, project_key, source="codex")
            end_offset = progress.get("end_offset", start_offset)
            end_line = progress.get("end_line", start_line)
            store.update_file_progress(file_id, st.st_size, st.st_mtime, end_offset, end_line,
                                       echo_call_ids)
            store.commit()
            stats["codex_files_indexed"] += 1
        except Exception as exc:
            store.rollback()
            stats["file_errors"] += 1
            if verbose:
                print(f"  ERROR on codex {path}: {exc}")
        _maybe_progress(verbose, i, len(targets), stats, progress_every)


def _maybe_progress(verbose, i, total, stats, every):
    if verbose and every and (i % every == 0 or i == total):
        print(
            f"  [{i}/{total}] new={stats.get('files_new', 0)} "
            f"appended={stats.get('files_appended', 0)} "
            f"unchanged={stats.get('files_unchanged', 0)} "
            f"stored={stats.get('stored', 0)}"
        )
