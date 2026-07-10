"""Parse a claude.ai account data export into normalized message records.

Claude Desktop (and claude.ai on the web) keep their conversations in the cloud,
not as local transcript files, so there is nothing to tail. The ground truth is
the official account export (claude.ai Settings > Privacy > Export data), which
emails a link to a zip. Depending on vintage the zip holds either
conversations.json (a JSON array of conversation objects) or conversations.jsonl
(one conversation object per line). Each conversation carries a uuid, a
name/title, timestamps, and a list of messages; each message has a sender role,
text content (a plain string or a list of content blocks), a uuid, and a
timestamp.

HONESTY GUARD: this parser was written against synthetic fixtures matching the
structure above, before a real export from the account had been opened. It is
deliberately defensive, and the CLI `ingest-claudia --inspect` mode prints a
file's structure (keys and shapes, never content) so the assumptions here can be
checked against a real export and corrected in a fast follow-up.

Everything is tolerant: a malformed conversation is skipped and counted in a
warnings summary; the batch never aborts. Missing or empty message content
(a known quirk of very long conversations) is treated as empty text.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

SOURCE = "claudia"
PROJECT_KEY = "claude_desktop"

# How a claude.ai message's content blocks map to searchable text, verified
# against a real export on 2026-07-10. A single message (one chat_message) bundles
# a whole turn, so all of its blocks are concatenated into one record, the same
# way the Claude Code and Codex parsers index these kinds:
#   text        -> the block's "text"
#   thinking    -> the block's "thinking" (Claude's reasoning; often long)
#   tool_use    -> name + a rendering of the input
#   tool_result -> a rendering of the result content
# Tool renderings are stored in full (not capped): unlike the tailing corpora,
# the export is fully parsed once at ingest and there is no raw-line recovery
# path afterward, so a capped tool payload could never be re-expanded.
# Skipped (no searchable conversational text): flag (moderation markers),
# and message-level attachments/files (binary or file references). Verified that
# text-only extraction would drop about 64 percent of messages (the thinking and
# tool turns), so all four kinds above are indexed for full recall.


def load_export_text(path) -> str:
    """Return the raw conversations text from an export zip, a directory
    containing the export, or a conversations.json / conversations.jsonl file."""
    p = Path(path)
    if zipfile.is_zipfile(str(p)):
        with zipfile.ZipFile(str(p)) as zf:
            target = None
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if base in ("conversations.json", "conversations.jsonl"):
                    target = name
                    break
            if target is None:
                raise ValueError(
                    "no conversations.json or conversations.jsonl found in the zip"
                )
            with zf.open(target) as fh:
                return fh.read().decode("utf-8", errors="replace")
    if p.is_dir():
        for name in ("conversations.json", "conversations.jsonl"):
            candidate = p / name
            if candidate.exists():
                return candidate.read_text(encoding="utf-8", errors="replace")
        raise ValueError(
            "no conversations.json or conversations.jsonl found in the directory"
        )
    return p.read_text(encoding="utf-8", errors="replace")


def iter_conversations(raw_text, warnings):
    """Yield conversation objects from the export text, sniffing JSON array vs
    JSONL by content (not by extension). warnings is a mutable counter dict."""
    stripped = raw_text.lstrip()
    if not stripped:
        return
    if stripped[0] == "[":
        try:
            data = json.loads(raw_text)
        except Exception:
            warnings["file_parse_errors"] = warnings.get("file_parse_errors", 0) + 1
            data = []
        if isinstance(data, list):
            for conv in data:
                yield conv
        return

    # JSONL: one conversation object per line (the file starts with '{').
    any_parsed = False
    line_errors = 0
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            conv = json.loads(line)
        except Exception:
            line_errors += 1
            continue
        any_parsed = True
        yield conv
    if any_parsed:
        # Only genuine JSONL counts its bad lines. A pretty-printed single object
        # or wrapper fails every physical line here; that tally is discarded when
        # we fall through to the whole-text parse below.
        if line_errors:
            warnings["malformed_lines"] = warnings.get("malformed_lines", 0) + line_errors
        return

    # Fallback: not JSONL after all (for example a single pretty-printed object
    # or a wrapper object). Parse the whole text once.
    try:
        data = json.loads(raw_text)
    except Exception:
        warnings["file_parse_errors"] = warnings.get("file_parse_errors", 0) + 1
        return
    if isinstance(data, list):
        for conv in data:
            yield conv
    elif isinstance(data, dict):
        inner = data.get("conversations")
        if isinstance(inner, list):
            for conv in inner:
                yield conv
        else:
            yield data


def _flatten_tool_result(content) -> str:
    """Flatten a tool_result block's content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, default=str, ensure_ascii=False)


def _render_message(msg) -> str:
    """Return the searchable text for one message, concatenating its content.

    content may be a plain string or a list of blocks. See the module header for
    the block-type mapping. The message-level "text" field (present on older or
    simple messages) is used as the base when it is non-empty; otherwise the base
    comes from the text blocks (or a bare-string content). Thinking and tool
    renderings are always appended. Nothing is capped.
    """
    content = msg.get("content")
    text_blocks = []
    thinking = []
    tools = []

    if isinstance(content, str):
        if content:
            text_blocks.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text_blocks.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                value = block.get("text")
                if isinstance(value, str) and value:
                    text_blocks.append(value)
            elif btype == "thinking":
                value = block.get("thinking") or block.get("text")
                if isinstance(value, str) and value:
                    thinking.append(value)
            elif btype == "tool_use":
                name = block.get("name") or "tool_use"
                raw_input = block.get("input")
                if isinstance(raw_input, str):
                    rendered = raw_input
                elif raw_input is not None:
                    rendered = json.dumps(raw_input, default=str, ensure_ascii=False)
                else:
                    rendered = ""
                tools.append(f"{name} {rendered}".strip())
            elif btype == "tool_result":
                rendered = _flatten_tool_result(block.get("content"))
                if rendered:
                    tools.append(rendered)
            # flag blocks and any other type carry no conversational text.

    field = msg.get("text")
    if isinstance(field, str) and field:
        base = field
    else:
        base = "\n".join(t for t in text_blocks if t)
    parts = [base] + thinking + tools
    return "\n".join(p for p in parts if p)


def _message_uuid(conv_uuid, msg, index) -> str:
    raw = msg.get("uuid") or msg.get("id")
    base = str(raw) if raw else f"{conv_uuid}:{index}"
    return base if base.startswith("claudia:") else "claudia:" + base


def parse_conversation(conv, warnings):
    """Parse one conversation object into (session_id, meta, [records]).

    Returns None for a malformed conversation (not a dict, or no usable uuid),
    which the caller counts and skips.
    """
    if not isinstance(conv, dict):
        return None
    sid = conv.get("uuid") or conv.get("conversation_id") or conv.get("id")
    if not sid:
        return None
    sid = str(sid)
    title = conv.get("name") or conv.get("title") or None

    raw_msgs = conv.get("chat_messages")
    if not isinstance(raw_msgs, list):
        raw_msgs = conv.get("messages")
    if not isinstance(raw_msgs, list):
        raw_msgs = []

    records = []
    timestamps = []
    first_user_prompt = None
    for i, msg in enumerate(raw_msgs):
        if not isinstance(msg, dict):
            warnings["malformed_messages"] = warnings.get("malformed_messages", 0) + 1
            continue
        sender = (msg.get("sender") or msg.get("role") or "").lower()
        if sender in ("assistant", "ai"):
            role = entry_type = "assistant"
        else:
            role = entry_type = "user"
        text = _render_message(msg)
        ts = msg.get("created_at") or msg.get("timestamp") or msg.get("updated_at")
        # Only string timestamps feed min/max, so a stray non-string value cannot
        # raise a str-vs-int comparison. The raw ts is still stored on the record.
        if isinstance(ts, str) and ts:
            timestamps.append(ts)
        if first_user_prompt is None and role == "user" and text:
            first_user_prompt = text
        records.append({
            "uuid": _message_uuid(sid, msg, i),
            "session_id": sid,
            "parent_uuid": msg.get("parent_message_uuid"),
            "role": role,
            "entry_type": entry_type,
            "source": SOURCE,
            "ts": ts,
            "tool_name": None,
            "cwd": None,
            "char_len": len(text),
            "is_truncated": 0,
            "text": text,
            "line_no": i,
        })

    started = min(timestamps) if timestamps else conv.get("created_at")
    last = max(timestamps) if timestamps else (conv.get("updated_at") or started)
    meta = {
        "project_key": PROJECT_KEY,
        "source": SOURCE,
        "cwd": None,
        "first_user_prompt": first_user_prompt,
        "summary": title,
        "started_ts": started,
        "last_ts": last,
    }
    return sid, meta, records


def inspect_export(path) -> dict:
    """Return the structural shape of an export (top-level type, the keys present
    on the first conversation and first message, block types, counts). Never
    includes any content values; this is the safe probe to run against a real
    export before trusting the parser's assumptions."""
    raw = load_export_text(path)
    warnings = {}
    stripped = raw.lstrip()
    top_level = "json-array" if stripped[:1] == "[" else "jsonl-or-object"
    result = {"top_level": top_level}

    convs = iter_conversations(raw, warnings)
    first = next(convs, None)
    total = 1 if first is not None else 0
    for _ in convs:
        total += 1
    result["total_conversations"] = total

    if isinstance(first, dict):
        result["conversation_keys"] = sorted(first.keys())
        msgs = first.get("chat_messages")
        msg_key = "chat_messages"
        if not isinstance(msgs, list):
            msgs = first.get("messages")
            msg_key = "messages" if isinstance(msgs, list) else None
        result["message_array_key"] = msg_key
        result["message_count_first_conv"] = len(msgs) if isinstance(msgs, list) else 0
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            result["message_keys"] = sorted(msgs[0].keys())
            content = msgs[0].get("content")
            result["message_content_type"] = type(content).__name__
            if isinstance(content, list) and content and isinstance(content[0], dict):
                result["content_block_keys"] = sorted(content[0].keys())
                result["content_block_types"] = sorted(
                    {b.get("type") for b in content if isinstance(b, dict) and b.get("type")}
                )
    if warnings:
        result["warnings"] = warnings
    return result
