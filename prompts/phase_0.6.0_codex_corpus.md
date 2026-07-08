# Session_Recall Phase 0.6.0: Codex corpus (unified cross-tool memory)

Directed by Serinety on 2026-07-06, during the session that shipped 0.5.0. Not
one of the originally planned phases; a scheduled deferred item ("cross-tool"
memory) turned into its own minor version, per the 0.4 closeout rule. Section 0
conventions in force; `__version__` becomes "0.6.0"; no em-dashes; no time
estimates.

## Goal

Both Claude Code and Codex should see each other's history, and Codex should see
its own. The elegant realization: this is one shared index, not a second
instance. Both MCP servers already launch the same exe and read the same
`data/recall.db`, so the only work is teaching the indexer to also ingest Codex's
transcripts into that one database.

## Verified format (checked 2026-07-06)

Codex stores each session as an append-only JSONL rollout under
`~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl` (and `~/.codex/archived_sessions/`).
72 rollout files at time of writing. Each line is `{type, timestamp, payload}`:

- `session_meta`: first line; `payload.session_id` and `payload.cwd`.
- `turn_context`: carries `cwd`.
- `event_msg`: UI telemetry (`user_message`, `agent_message`, ...) that
  DUPLICATES the response_item stream. Skipped, to avoid double indexing.
- `response_item`: the real conversation. `payload.type` is one of:
  - `message`: `role` user/assistant/developer, `content` a list of
    `{type: input_text|output_text, text}` blocks.
  - `function_call` / `custom_tool_call` / `tool_search_call` / `web_search_call`:
    tool calls (`name`, `arguments`, `call_id`).
  - the paired `*_output` items: tool results (`call_id`, `output`).
  - `reasoning`: encrypted (like Claude's redacted thinking); only the usually
    empty plaintext `summary` is indexable.
- `compacted`: a compaction boundary; indexed as a summary when it carries text.

Codex flattens MCP tool names to the bare tool name, so this server's own calls
appear as `search_history`, `recall_status`, etc., not `mcp__session-recall__*`.

## Scope

1. New `parser_codex.py`: normalize rollout entries into the same record dicts the
   Claude parser emits, tagged `source='codex'`. Byte-offset tailing and defensive
   skipping identical in spirit to `parser.py`. Skip `event_msg` (dedup). Thread
   `session_id`/`cwd` (from `session_meta`) into every record.
2. `source` column on `messages`, `sessions`, `files` (migration for existing
   databases; existing rows default to `claude`).
3. Indexer: `discover_codex` + `_index_codex`, wired into `run_index` as a second
   corpus. The Codex corpus is indexed on a real default-root pass; a custom root
   (tests) opts in via `codex_home`.
4. Self-echo for Codex: suppress this server's own tool calls (bare names) and
   their paired outputs by `call_id`. Configurable via
   `SESSION_RECALL_CODEX_ECHO_TOOLS`.
5. Expose `source` as an optional filter: MCP `search_history(source=...)`, CLI
   `search --source`, and `status` per-source counts. Default blends both.
6. Tests, docs, version bump, adversarial review.

## Acceptance

- A full rebuild ingests both corpora with zero raised exceptions; `status` shows
  a `by_source` split (claude and codex).
- A Codex session's work (for example the Crucible project) is searchable, tagged
  `codex`, and reachable from a Claude Code session (one shared index).
- The `source` filter isolates each corpus; project scoping by `cwd` works for
  Codex sessions.
- An incremental rerun tails both corpora and is near-instant.
- Codex's own session-recall calls are not indexed (self-echo holds).

## Known characteristics (documented, not bugs)

- Codex `reasoning` is encrypted, so it contributes no searchable text (parity
  with Claude's redacted thinking).
- Codex injects large context blocks (AGENTS.md, environment, tool defs) as
  `user`-role messages, so a Codex session's `first_user_prompt` may show injected
  context rather than the literal first human prompt. Indexed faithfully rather
  than filtered by a fragile prefix heuristic.
