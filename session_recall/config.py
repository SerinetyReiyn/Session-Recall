"""Small configuration surface for Session_Recall.

Kept deliberately tiny. It holds the few values worth configuring in one place,
each overridable by environment:

- the self-echo tool-name prefixes excluded from the index, which must match the
  name the MCP server registers under (env SESSION_RECALL_ECHO_PREFIXES);
- the default per-session diversity cap shared by the MCP server and the CLI, so
  their search semantics match;
- the freshness threshold that decides when a read tool refreshes the index
  first (env SESSION_RECALL_FRESHNESS_SECONDS).
"""

from __future__ import annotations

import os

# Default prefixes for the project's own MCP tool traffic. The server registers
# as "session-recall" in phase 0.3, so its tools appear as
# mcp__session-recall__<tool>. The underscore spelling is kept as a defensive
# alias in case a future registration uses it.
DEFAULT_SELF_ECHO_PREFIXES = ("mcp__session-recall", "mcp__session_recall")

# Environment override: a comma-separated list of prefixes.
ECHO_PREFIXES_ENV = "SESSION_RECALL_ECHO_PREFIXES"

# Default max hits from any one session in a ranked search (diversity cap), so
# one noisy session cannot fill the result list. Shared by the MCP server and
# the CLI so their search semantics match.
DEFAULT_PER_SESSION_CAP = 3

# Freshness guard: before answering a read tool, if the last index pass is older
# than this many seconds, run an incremental pass first. There is no watcher
# daemon (deferred), so this keeps the index self-maintaining. Env-overridable.
FRESHNESS_ENV = "SESSION_RECALL_FRESHNESS_SECONDS"
DEFAULT_FRESHNESS_SECONDS = 900  # 15 minutes


def self_echo_prefixes():
    """Return the configured tuple of self-echo tool-name prefixes."""
    raw = os.environ.get(ECHO_PREFIXES_ENV)
    if raw:
        prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
        if prefixes:
            return prefixes
    return DEFAULT_SELF_ECHO_PREFIXES


def self_echo_prefixes_codex():
    """Return the set of bare tool names that are this server's own tools as seen
    in Codex rollouts.

    Codex flattens MCP tool names to the bare tool name (no server prefix), so a
    Codex call to this server appears as e.g. "search_history", not
    "mcp__session-recall__search_history". Suppressing that known set keeps
    Codex's own session-recall traffic (the search output) out of the index.
    Env override SESSION_RECALL_CODEX_ECHO_TOOLS is a comma-separated list.
    """
    raw = os.environ.get("SESSION_RECALL_CODEX_ECHO_TOOLS")
    if raw:
        names = tuple(n.strip() for n in raw.split(",") if n.strip())
        if names:
            return names
    return ("search_history", "list_sessions", "get_session_outline",
            "read_messages", "recall_status", "reindex")


def freshness_threshold_seconds():
    """Return the configured freshness threshold in seconds (non-negative)."""
    raw = os.environ.get(FRESHNESS_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return DEFAULT_FRESHNESS_SECONDS
