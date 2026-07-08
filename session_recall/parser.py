"""Turn raw JSONL transcript lines into normalized message records.

Every function here is pure and does no I/O beyond reading the file passed in.
The parser is deliberately defensive: the transcript format is internal to
Claude Code and drifts between versions, so a single bad line or an unknown
entry type must never abort a run. Malformed lines and unknown types are
counted, not raised.

Entry shapes were verified against live data on 2026-07-01 (Claude Code
v2.1.x). See the phase 0.1 build document in prompts/ for the survey.
"""

from __future__ import annotations

import io
import json
from collections import Counter

from .config import self_echo_prefixes as _default_self_echo_prefixes

# Caps. Plain conversational text (user, assistant, thinking) is stored in
# full so search snippets are accurate. Only tool payloads are capped here,
# because they can be arbitrarily large and full content is recoverable later
# from the raw jsonl via the stored file_id + line_no locator.
TOOL_USE_INPUT_CAP = 1000
TOOL_RESULT_CAP = 2000

# Entry types that carry no searchable conversational text. Skipped on purpose.
SKIP_TYPES = frozenset({
    "queue-operation",
    "attachment",
    "file-history-snapshot",
    "ai-title",
    "last-prompt",
    "custom-title",
    "mode",
})


def _truncate(text: str, cap: int):
    """Return (possibly truncated text, was_truncated)."""
    if len(text) > cap:
        return text[:cap], True
    return text, False


def _render_tool_result(content) -> str:
    """Flatten a tool_result content value (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                parts.append("[image]")
            else:
                parts.append(f"[{btype}]")
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, default=str, ensure_ascii=False)


def _parse_message_entry(obj: dict, entry_type_hint: str, echo_prefixes, echo_ids):
    """Parse a user or assistant entry into a normalized record dict, or None.

    One transcript line maps to at most one record (keyed by uuid). When a line
    holds several content blocks their text is concatenated into one document.
    echo_prefixes is the tuple of tool-name prefixes to exclude (self-echo).
    echo_ids is a mutable set carrying the tool_use ids of suppressed self-echo
    calls, so the matching tool_result entry (which has no tool name of its own,
    only a tool_use_id) is suppressed too. This is the load-bearing half: the
    tool_result carries the search OUTPUT, and indexing it is exactly what would
    let past results dominate future searches.
    """
    echo_prefix_tuple = tuple(echo_prefixes) if echo_prefixes else ()
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role") or entry_type_hint
    content = msg.get("content")

    texts = []
    tool_name = None
    truncated = False
    echo_hit = False
    has_tool_use = has_tool_result = has_text = has_thinking = False

    if isinstance(content, str):
        if content:
            texts.append(content)
        has_text = True
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                has_text = True
                texts.append(block.get("text", ""))
            elif btype == "thinking":
                has_thinking = True
                thinking = block.get("thinking", "")
                if thinking:
                    texts.append(thinking)
            elif btype == "tool_use":
                has_tool_use = True
                name = block.get("name")
                tool_name = tool_name or name
                if name and echo_prefix_tuple and name.startswith(echo_prefix_tuple):
                    echo_hit = True
                    if echo_ids is not None and block.get("id"):
                        echo_ids.add(block.get("id"))
                raw_input = block.get("input")
                rendered = ""
                if raw_input is not None:
                    rendered = json.dumps(raw_input, default=str, ensure_ascii=False)
                    rendered, was_cut = _truncate(rendered, TOOL_USE_INPUT_CAP)
                    truncated = truncated or was_cut
                texts.append(f"{name or ''} {rendered}".strip())
            elif btype == "tool_result":
                has_tool_result = True
                if echo_ids is not None and block.get("tool_use_id") in echo_ids:
                    echo_hit = True
                rendered = _render_tool_result(block.get("content"))
                rendered, was_cut = _truncate(rendered, TOOL_RESULT_CAP)
                truncated = truncated or was_cut
                if rendered:
                    texts.append(rendered)
            # image and unknown blocks are excluded from the index.
    else:
        return None

    # Self-echo suppression: drop Session_Recall's own tool traffic, both the
    # tool_use (the query) and the matching tool_result (the search output).
    if echo_hit:
        return None

    if has_tool_use:
        entry_type = "tool_use"
    elif has_tool_result:
        entry_type = "tool_result"
    elif has_text:
        entry_type = "assistant" if role == "assistant" else "user"
    elif has_thinking:
        entry_type = "thinking"
    else:
        entry_type = role or "unknown"

    text = "\n".join(t for t in texts if t).strip()
    return {
        "uuid": obj.get("uuid"),
        "session_id": obj.get("sessionId"),
        "parent_uuid": obj.get("parentUuid"),
        "role": role,
        "entry_type": entry_type,
        "ts": obj.get("timestamp"),
        "tool_name": tool_name,
        "cwd": obj.get("cwd"),
        "char_len": len(text),
        "is_truncated": 1 if truncated else 0,
        "text": text,
    }


def _parse_system_entry(obj: dict):
    """Parse a system entry (for example an api_error) into a record, or None.

    The build document's index list does not name system entries, but the
    phase 0.1 acceptance test searches for "ECONNRESET", and on this machine
    that string lives only inside system api_error entries (error.formatted
    and error.connection). Indexing them, capped, is what lets a post-compact
    session find its way back to the session where the failure happened.
    """
    subtype = obj.get("subtype")
    error = obj.get("error")
    parts = []
    if subtype:
        parts.append(str(subtype))
    if isinstance(error, dict):
        for key in ("formatted", "message"):
            value = error.get(key)
            if isinstance(value, str):
                parts.append(value)
        connection = error.get("connection")
        if connection is not None:
            parts.append(json.dumps(connection, default=str, ensure_ascii=False))
    elif isinstance(error, str):
        parts.append(error)

    text = " ".join(p for p in parts if p).strip()
    if not text:
        return None
    text, truncated = _truncate(text, TOOL_RESULT_CAP)
    return {
        "uuid": obj.get("uuid"),
        "session_id": obj.get("sessionId"),
        "parent_uuid": obj.get("parentUuid"),
        "role": "system",
        "entry_type": "system",
        "ts": obj.get("timestamp"),
        "tool_name": subtype,
        "cwd": obj.get("cwd"),
        "char_len": len(text),
        "is_truncated": 1 if truncated else 0,
        "text": text,
    }


def _parse_summary_entry(obj: dict):
    """Parse a compaction summary entry, or None if it carries no usable text."""
    summary = obj.get("summary")
    uuid = obj.get("uuid") or obj.get("leafUuid")
    if not isinstance(summary, str) or not summary.strip() or not uuid:
        return None
    text, truncated = _truncate(summary, TOOL_RESULT_CAP)
    return {
        "uuid": uuid,
        "session_id": obj.get("sessionId"),
        "parent_uuid": obj.get("parentUuid"),
        "role": "summary",
        "entry_type": "summary",
        "ts": obj.get("timestamp"),
        "tool_name": None,
        "cwd": obj.get("cwd"),
        "char_len": len(text),
        "is_truncated": 1 if truncated else 0,
        "text": text,
    }


def parse_file(path: str, stats: Counter, start_offset: int = 0,
               start_line: int = 0, progress=None, echo_prefixes=None,
               type_skips=None, echo_ids=None):
    """Yield normalized record dicts for one transcript file.

    Reads from byte start_offset (a line boundary from a prior pass) and numbers
    lines from start_line, so incremental tailing of an append-only file keeps
    absolute line numbers stable for the stored locator. The file is opened in
    binary mode so byte offsets are exact regardless of newline translation.

    A final line with no trailing newline is treated as still being written: its
    record is still yielded, but the committed offset stops before it, so the
    next pass re-reads it once it is complete (uuid dedupe absorbs the re-read).

    stats is updated in place with counters: lines, indexed, blank, json_errors,
    parse_errors, skipped_type, skipped_unknown, skipped_empty. If type_skips (a
    Counter) is given, each skipped entry's raw type is tallied there so status
    can show what is being discarded per type. echo_prefixes overrides the
    configured self-echo prefixes. On return, progress (if given) carries
    end_offset and end_line, the resume point for the next incremental pass.
    Never raises on a bad line; a bad or malformed line is counted and skipped,
    so one bad line can never discard the rest of the file.
    """
    if progress is None:
        progress = {}
    if echo_prefixes is None:
        echo_prefixes = _default_self_echo_prefixes()
    # Tool_use ids of suppressed self-echo calls, so the matching tool_result
    # (which carries only a tool_use_id) is suppressed too. Seeded by the caller
    # from the file's persisted set so a result whose call was suppressed in an
    # earlier incremental pass is still recognized when it arrives later.
    if echo_ids is None:
        echo_ids = set()
    offset = start_offset
    line_no = start_line
    committed_offset = start_offset
    committed_line = start_line
    with io.open(path, "rb") as handle:
        handle.seek(start_offset)
        while True:
            raw = handle.readline()
            if not raw:
                break
            ends_newline = raw.endswith(b"\n")
            offset += len(raw)
            current_line = line_no
            line_no += 1
            if ends_newline:
                committed_offset = offset
                committed_line = line_no
            stats["lines"] += 1

            text_line = raw.decode("utf-8", errors="replace").strip()
            if not text_line:
                stats["blank"] += 1
                continue
            try:
                obj = json.loads(text_line)
            except Exception:
                stats["json_errors"] += 1
                continue
            if not isinstance(obj, dict):
                stats["skipped_unknown"] += 1
                continue

            etype = obj.get("type")
            if etype in SKIP_TYPES:
                stats["skipped_type"] += 1
                if type_skips is not None:
                    type_skips[etype] += 1
                continue

            try:
                if etype in ("user", "assistant"):
                    record = _parse_message_entry(obj, etype, echo_prefixes, echo_ids)
                elif etype == "system":
                    record = _parse_system_entry(obj)
                elif etype == "summary":
                    record = _parse_summary_entry(obj)
                else:
                    stats["skipped_unknown"] += 1
                    if type_skips is not None:
                        type_skips[etype] += 1
                    continue
            except Exception:
                # A malformed entry must not abort the file. Count and skip it.
                stats["parse_errors"] += 1
                continue

            if record is None:
                stats["skipped_empty"] += 1
                continue

            record["line_no"] = current_line
            stats["indexed"] += 1
            yield record

    progress["end_offset"] = committed_offset
    progress["end_line"] = committed_line


def full_text_from_obj(obj: dict) -> str:
    """Extract the full, uncapped text of one transcript entry.

    Used by read_messages to fetch complete content when the indexed copy was
    truncated. Mirrors the extraction in the parse helpers but applies no caps.
    """
    if not isinstance(obj, dict):
        return ""
    etype = obj.get("type")
    if etype in ("user", "assistant"):
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "thinking":
                    parts.append(block.get("thinking", ""))
                elif btype == "tool_use":
                    name = block.get("name") or ""
                    raw_input = block.get("input")
                    rendered = ""
                    if raw_input is not None:
                        rendered = json.dumps(raw_input, default=str, ensure_ascii=False)
                    parts.append(f"{name} {rendered}".strip())
                elif btype == "tool_result":
                    parts.append(_render_tool_result(block.get("content")))
        return "\n".join(p for p in parts if p).strip()
    if etype == "system":
        error = obj.get("error")
        parts = []
        subtype = obj.get("subtype")
        if subtype:
            parts.append(str(subtype))
        if isinstance(error, dict):
            for key in ("formatted", "message"):
                value = error.get(key)
                if isinstance(value, str):
                    parts.append(value)
            connection = error.get("connection")
            if connection is not None:
                parts.append(json.dumps(connection, default=str, ensure_ascii=False))
        elif isinstance(error, str):
            parts.append(error)
        return " ".join(p for p in parts if p).strip()
    if etype == "summary":
        summary = obj.get("summary")
        return summary if isinstance(summary, str) else ""
    return ""


def read_full_text(path: str, line_no: int):
    """Read one line (0-based) from a raw jsonl file and return its full text.

    Returns None if the file or line cannot be read or parsed. Reads
    sequentially to the target line, which is cheap for on-demand single reads.
    """
    try:
        with io.open(path, "rb") as handle:
            for i, raw in enumerate(handle):
                if i == line_no:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                    return full_text_from_obj(obj)
    except Exception:
        return None
    return None
