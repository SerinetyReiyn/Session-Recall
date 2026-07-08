"""Shared helpers for the test suite: build throwaway transcript corpora.

Tests run against temp directories, never the real archive, so they are safe and
hermetic. Run them from the project root:

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

# Ensure the project root is importable when tests are run directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def make_env():
    """Return (root_dir, db_path) both under fresh temp directories."""
    root = Path(tempfile.mkdtemp(prefix="sr_test_root_"))
    db = str(Path(tempfile.mkdtemp(prefix="sr_test_db_")) / "recall.db")
    return root, db


def user_line(uuid, text, session="s1", ts="2026-06-01T00:00:00Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": session, "timestamp": ts,
        "cwd": "C:/work", "message": {"role": "user", "content": text},
    })


def assistant_text_line(uuid, text, session="s1", ts="2026-06-01T00:00:01Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session, "timestamp": ts,
        "cwd": "C:/work",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def tool_use_line(uuid, name, inp, session="s1", ts="2026-06-01T00:00:02Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session, "timestamp": ts,
        "cwd": "C:/work",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "tu_" + uuid,
                                 "name": name, "input": inp}]},
    })


def tool_result_line(uuid, tool_use_id, content, session="s1", ts="2026-06-01T00:00:03Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": session, "timestamp": ts,
        "cwd": "C:/work",
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                 "content": content}]},
    })


def write_jsonl(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def append_jsonl(path: Path, lines):
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_sidecar(session_dir: Path, name: str, content: str = "offloaded output"):
    tr = session_dir / "tool-results"
    tr.mkdir(parents=True, exist_ok=True)
    with io.open(tr / name, "w", encoding="utf-8") as fh:
        fh.write(content)
