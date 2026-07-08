"""Turn Codex rollout JSONL lines into the same normalized message records.

Codex (the OpenAI Codex CLI) stores each session as an append-only JSONL rollout
file under ~/.codex/sessions/<date>/rollout-*.jsonl (and archived_sessions/).
The schema is Codex's own (the OpenAI Responses format), different from Claude
Code's, so it gets its own parser that emits the same record dicts the Claude
parser does, tagged source='codex', so both corpora share one index.

Format, verified against live data on 2026-07-06:
- Every line is {"type", "timestamp", "payload"}.
- Top-level types: session_meta (carries session_id + cwd), turn_context
  (carries cwd), event_msg (UI telemetry that DUPLICATES the response_item
  stream, so it is skipped), response_item (the real conversation), compacted
  (a compaction boundary).
- response_item payload.type: message (role user/assistant/developer, content is
  a list of {type: input_text|output_text, text} blocks), function_call and
  custom_tool_call / tool_search_call / web_search_call (tool calls), the paired
  *_output items (tool results), and reasoning (encrypted, like Claude's redacted
  thinking, so only its plaintext summary is indexable and that is usually empty).

Defensive throughout: a bad or unknown line is counted and skipped, never raised.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from .config import self_echo_prefixes_codex
from .parser import TOOL_RESULT_CAP, TOOL_USE_INPUT_CAP, _truncate

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# response_item payload types that are tool invocations and tool results.
_TOOL_CALL_TYPES = frozenset({
    "function_call", "custom_tool_call", "tool_search_call",
    "web_search_call", "local_shell_call",
})
_TOOL_OUTPUT_TYPES = frozenset({
    "function_call_output", "custom_tool_call_output", "tool_search_output",
    "web_search_output", "local_shell_call_output",
})


def _session_id_from_filename(path) -> str:
    m = _UUID_RE.search(Path(path).name)
    return m.group(0) if m else Path(path).stem


def read_codex_session_meta(path):
    """Return (session_id, cwd) from a rollout's first session_meta line.

    Falls back to the uuid embedded in the filename when the meta line is absent
    (for example when tailing from a byte offset past it).
    """
    session_id = None
    cwd = None
    try:
        with io.open(path, "rb") as fh:
            first = fh.readline().decode("utf-8", errors="replace").strip()
        if first:
            obj = json.loads(first)
            if isinstance(obj, dict) and obj.get("type") == "session_meta":
                payload = obj.get("payload") or {}
                session_id = payload.get("session_id") or payload.get("id")
                cwd = payload.get("cwd")
    except Exception:
        pass
    if not session_id:
        session_id = _session_id_from_filename(path)
    return session_id, cwd


def codex_project_key(cwd):
    """A short project key for a Codex session, derived from its cwd basename."""
    if not cwd:
        return "codex-unknown"
    base = cwd.replace("/", "\\").rstrip("\\").split("\\")[-1]
    return base or "codex-unknown"


def _text_from_content(content) -> str:
    """Flatten a content or summary list into text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
            elif block.get("type") in ("input_image", "image"):
                parts.append("[image]")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _render_output(out) -> str:
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        for key in ("output", "text", "content", "result"):
            value = out.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list):
                flat = _text_from_content(value)
                if flat:
                    return flat
        return json.dumps(out, default=str, ensure_ascii=False)
    if isinstance(out, list):
        flat = _text_from_content(out)
        return flat if flat else json.dumps(out, default=str, ensure_ascii=False)
    return str(out)


def _mk(uid, session_id, role, entry_type, ts, tool_name, cwd, text, line_no, truncated):
    text = text or ""
    return {
        "uuid": "codex:" + str(uid),
        "session_id": session_id,
        "parent_uuid": None,
        "role": role,
        "entry_type": entry_type,
        "source": "codex",
        "ts": ts,
        "tool_name": tool_name,
        "cwd": cwd,
        "char_len": len(text),
        "is_truncated": 1 if truncated else 0,
        "text": text,
        "line_no": line_no,
    }


def _response_item(payload, session_id, cwd, ts, line_no, echo_names, echo_call_ids):
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")

    if ptype == "message":
        role = payload.get("role")
        text = _text_from_content(payload.get("content"))
        if role == "assistant":
            entry_type, role_out = "assistant", "assistant"
        elif role == "developer":
            entry_type, role_out = "system", "system"
        else:
            entry_type, role_out = "user", "user"
        uid = payload.get("id") or f"{session_id}:{line_no}"
        return _mk(uid, session_id, role_out, entry_type, ts, None, cwd, text, line_no, False)

    if ptype in _TOOL_CALL_TYPES:
        name = payload.get("name") or ptype
        call_id = payload.get("call_id") or payload.get("id")
        if name in echo_names:
            # This server's own tool call. Remember its call_id so the paired
            # output (the search result) is suppressed too, then drop it.
            if call_id:
                echo_call_ids.add(call_id)
            return None
        args = payload.get("arguments")
        if args is None:
            args = payload.get("input")
        if isinstance(args, str):
            rendered = args
        elif args is not None:
            rendered = json.dumps(args, default=str, ensure_ascii=False)
        else:
            rendered = ""
        if not rendered:
            # web_search_call and similar carry no arguments/input; the query
            # lives under payload.action ({type, query, queries}).
            action = payload.get("action")
            if isinstance(action, dict):
                parts = []
                if isinstance(action.get("query"), str):
                    parts.append(action["query"])
                for item in action.get("queries") or []:
                    if isinstance(item, str):
                        parts.append(item)
                rendered = " ".join(parts)
        rendered, truncated = _truncate(rendered, TOOL_USE_INPUT_CAP)
        text = f"{name} {rendered}".strip()
        uid = f"call:{call_id}" if call_id else f"{session_id}:{line_no}"
        return _mk(uid, session_id, "assistant", "tool_use", ts, name, cwd, text, line_no, truncated)

    if ptype in _TOOL_OUTPUT_TYPES:
        call_id = payload.get("call_id")
        if call_id and call_id in echo_call_ids:
            return None
        rendered = _render_output(
            payload.get("output") if "output" in payload else
            payload.get("tools") or payload.get("result")
        )
        rendered, truncated = _truncate(rendered, TOOL_RESULT_CAP)
        if not rendered:
            return None
        uid = f"out:{call_id}:{line_no}" if call_id else f"{session_id}:{line_no}"
        return _mk(uid, session_id, "user", "tool_result", ts, None, cwd, rendered, line_no, truncated)

    if ptype == "reasoning":
        # encrypted_content is opaque; only the summary (usually empty) is plain.
        text = _text_from_content(payload.get("summary"))
        if not text:
            return None
        uid = payload.get("id") or f"{session_id}:{line_no}"
        return _mk(uid, session_id, "assistant", "thinking", ts, None, cwd, text, line_no, False)

    return None


def _compacted(payload, session_id, cwd, ts, line_no):
    text = ""
    if isinstance(payload, dict):
        for key in ("message", "summary", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                text = value
                break
            if isinstance(value, list):
                flat = _text_from_content(value)
                if flat:
                    text = flat
                    break
    if not text:
        return None
    text, truncated = _truncate(text, TOOL_RESULT_CAP)
    return _mk(f"{session_id}:compacted:{line_no}", session_id, "system", "summary",
               ts, None, cwd, text, line_no, truncated)


def _line_full_text(obj):
    """Uncapped text for one Codex rollout line (used to expand a truncated row)."""
    if not isinstance(obj, dict):
        return ""
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return ""
    ptype = payload.get("type")
    if ptype == "message":
        return _text_from_content(payload.get("content"))
    if ptype in _TOOL_CALL_TYPES:
        name = payload.get("name") or ptype
        args = payload.get("arguments")
        if args is None:
            args = payload.get("input")
        if isinstance(args, str):
            rendered = args
        elif args is not None:
            rendered = json.dumps(args, default=str, ensure_ascii=False)
        else:
            rendered = ""
            action = payload.get("action")
            if isinstance(action, dict):
                parts = []
                if isinstance(action.get("query"), str):
                    parts.append(action["query"])
                for item in action.get("queries") or []:
                    if isinstance(item, str):
                        parts.append(item)
                rendered = " ".join(parts)
        return f"{name} {rendered}".strip()
    if ptype in _TOOL_OUTPUT_TYPES:
        return _render_output(
            payload.get("output") if "output" in payload else
            payload.get("tools") or payload.get("result")
        )
    if ptype == "reasoning":
        return _text_from_content(payload.get("summary"))
    return ""


def read_full_text_codex(path, line_no):
    """Read one line (0-based) from a Codex rollout and return its uncapped text.

    Returns None if the file or line cannot be read or parsed. Mirrors
    parser.read_full_text so read_messages can expand a truncated Codex row.
    """
    try:
        with io.open(path, "rb") as handle:
            for i, raw in enumerate(handle):
                if i == line_no:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                    return _line_full_text(obj)
    except Exception:
        return None
    return None


def parse_codex_file(path, stats, start_offset=0, start_line=0, progress=None,
                     session_id=None, cwd=None, echo_names=None, echo_call_ids=None):
    """Yield normalized records for one Codex rollout file.

    Mirrors parser.parse_file: reads from byte start_offset with absolute line
    numbering, leaves a partial final line for the next pass, and reports the
    resume point in progress. session_id and cwd are threaded in (read from the
    file's session_meta by the caller) and stamped on every record, since the
    per-item lines do not repeat them.
    """
    if progress is None:
        progress = {}
    echo_names = set(echo_names if echo_names is not None else self_echo_prefixes_codex())
    # call_ids of suppressed self-echo tool calls, seeded by the caller from the
    # file's persisted set so a paired output arriving in a later incremental
    # pass is still recognized and dropped.
    if echo_call_ids is None:
        echo_call_ids = set()
    cur_session = session_id
    cur_cwd = cwd

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
            payload = obj.get("payload")
            ts = obj.get("timestamp")

            if etype == "session_meta":
                if isinstance(payload, dict):
                    cur_session = cur_session or payload.get("session_id") or payload.get("id")
                    if payload.get("cwd"):
                        cur_cwd = payload.get("cwd")
                stats["skipped_type"] += 1
                continue
            if etype == "turn_context":
                if isinstance(payload, dict) and payload.get("cwd"):
                    cur_cwd = payload.get("cwd")
                stats["skipped_type"] += 1
                continue
            if etype == "event_msg":
                # Duplicates the response_item stream; skip to avoid double indexing.
                stats["skipped_type"] += 1
                continue

            try:
                if etype == "response_item":
                    record = _response_item(payload, cur_session, cur_cwd, ts,
                                            current_line, echo_names, echo_call_ids)
                elif etype == "compacted":
                    record = _compacted(payload, cur_session, cur_cwd, ts, current_line)
                else:
                    stats["skipped_unknown"] += 1
                    continue
            except Exception:
                stats["parse_errors"] += 1
                continue

            if record is None:
                stats["skipped_empty"] += 1
                continue
            stats["indexed"] += 1
            yield record

    progress["end_offset"] = committed_offset
    progress["end_line"] = committed_line
