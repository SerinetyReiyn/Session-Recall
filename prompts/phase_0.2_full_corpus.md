# Session_Recall Phase 0.2.0: full corpus, incremental, hardening

Read `prompts\phase_0.1_build_doc.md` first. Phase 0.1.0 was independently verified and accepted 2026-07-01 (clean em-dash scan of all deliverables, `__version__` confirmed, status and the ECONNRESET search reproduced from the CLI). This prompt carries the Phase 0.2.0 scope from build doc section 4 plus four ratified amendments from your 0.1 findings. Where this prompt and the build doc differ, this prompt wins.

All section 0 conventions remain in force. One addition, A4 below.

## Ratified amendments

**A1. `system` entry indexing is approved.** Your decision to index capped `system` entries (api_error and similar) despite their omission from section 3.1 is ratified. Keep the code comment and README note.

**A2. Read-only rule scope clarified.** The prohibition on writing under `C:\Users\Serin\.claude\` binds Session_Recall code and you acting as this project's builder. Claude Code's own native background persistence (transcripts, todos, its memory feature) is out of scope; do not fight the application. For this project's session-to-session persistence, use project-local files: `NOTES.md` in the project root, or a `notes\` folder if it grows. Never `~/.claude`.

**A3. Canonical test amended.** Your finding is correct and the reason is now understood: the F5 WAF / TLS-inspection / hotspot diagnosis was worked out in Claude Desktop conversations, which are a separate corpus that never lands in `~/.claude/projects`. It cannot be recovered from Code transcripts because it was never in them. Rulings:
- The mechanical `ECONNRESET` search stays as a permanent smoke test.
- New living-data bar, verified against the index in this phase and exercised through MCP in 0.3: a search along the lines of "comprehensive review" scoped to MAS_Trader must surface session `abc5642d` (2026-06-11), and after full-corpus indexing, a Serinety_TD query (for example "element core" or "dual core") must surface the recent Serinety_TD sessions.
- Indexing Desktop-app conversation history as a second corpus goes on the deferred list. Do not build it.

**A4. The em-dash prohibition covers chat output and completion reports, not only files.** Your 0.1 files were clean; your 0.1 completion report was not. Fix the habit.

## Scope

1. **Full corpus.** Extend discovery to all of `C:\Users\Serin\.claude\projects\**\*.jsonl` (1,652+ files at last count).
2. **Incremental indexing.** `files` table tracks size, mtime, `last_offset`; append-only tailing from `last_offset`; if current size is smaller than `last_offset`, reindex that file from zero.
3. **Sidecar pointer records.** For `<session-id>\tool-results\*` and similar binary sidecars: pointer records only (path, tool name if derivable, timestamp). Never ingest content.
4. **Self-echo exclusion.** Skip indexing `tool_use` and `tool_result` entries whose tool name matches this project's MCP server (prefix `mcp__session-recall` or whatever name gets registered in 0.3; make the prefix configurable). Cover with a unit test.
5. **Caps.** Enforce the section 3.1 caps on `tool_use` (~1 KB) and `tool_result` (~2 KB preview, `is_truncated`, raw locator).
6. **WAL mode.** Enable if 0.1 did not already.
7. **Skip visibility.** 0.1 status shows `skipped_type: 1135` and `skipped_unknown: 189` as opaque totals. Break skip counters down by entry type in `status` so we can see what is being discarded and catch format drift early.
8. **Sessions/files reconciliation.** 0.1 status reports 102 files but only 2 sessions. Investigate and explain: presumably subagent files share the parent `sessionId` and collapse intentionally, but confirm, and add per-project `sessions` and `files` counts to `status` so the mapping is visible.

## Acceptance

- Full build over the entire corpus completes with zero raised exceptions; per-type skip counts reported.
- Immediate rerun is incremental and near-instant when nothing changed.
- Malformed lines are counted, never fatal.
- `search "ECONNRESET"` still passes (smoke).
- The two A3 living-data queries return the expected sessions.
- At least one sidecar pointer record exists in the DB (find any `tool-results` directory in the corpus; if genuinely none survive, say so plainly).
- Self-echo exclusion has a passing unit test.
- `status` shows per-type skips and per-project session/file counts.

## Deliverables

`__version__ = "0.2.0"`. Updated README (incremental behavior, exclusions, status fields). A completion report with final counts, written without em-dashes.

## Noted for later, do not build now

Search-result diversity (0.1's ECONNRESET query returns ten hits from one session; MCP-facing search in 0.3 should consider per-session grouping or diversity limits). Desktop-corpus indexing. Watcher daemon. Everything else on the build doc's deferred list.
