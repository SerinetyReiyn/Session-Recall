# Session_Recall: Build Document

Provisional project name: Session_Recall (Serinety may rename)
First deliverable version: `__version__ = "0.1.0"`
Authored: 2026-07-01 by Claudia (Claude Desktop), for execution by Claude Code (Opus)
Owner: Serinety

---

## 0. Read this first (non-negotiable conventions)

1. **Em-dashes are forbidden.** Not in code comments, not in docs, not in commit messages, not in chat output. Use commas, colons, parentheses, or periods. This applies to every file you write in this project.
2. **No time estimates.** Describe work, deliverables, and order only. Never "this will take X hours/days".
3. **`__version__` in source is the phase number.** One source of truth. Bump it as each phase ships. Start at "0.1.0".
4. **Do not code external APIs from memory.** Before writing the MCP server, consult the current official Python MCP SDK documentation and verify the API surface. Same for any library call you are not certain about: verify, then code.
5. **`C:\Users\Serin\.claude\` is strictly READ ONLY for this project.** Never write, rename, touch, or delete anything under it. Do not modify file mtimes: Claude Code's retention sweep is mtime-keyed and there are open upstream bugs where touched files get silently deleted. All of this project's writes go in the project folder.
6. **Verify before building the parser.** The transcript format is internal to Claude Code and changes between versions. Before coding, sample real data: read the first and last ~20 lines of 3 recent `.jsonl` session files across at least 2 different project folders and confirm the entry shapes described in section 2. Build defensively against drift.
7. Save each phase prompt you receive into `prompts\` in this project. Phase prompt files are the project's paper trail.

---

## 1. Problem statement

Long Claude Code sessions auto-compact. Compaction replaces the in-context history with a summary, and the post-compact model routinely re-derives solutions, diagnoses, and design decisions it already produced earlier in the same session or in prior sessions.

The data is not lost. Claude Code writes a complete append-only JSONL transcript of every session to disk: user messages, assistant text, extended thinking blocks verbatim, tool calls with full inputs, tool results, compaction boundaries, summaries, git snapshots, and per-turn token usage. What is missing is a retrieval path: a compacted session has no way to reach back into that archive.

**Session_Recall is a local MCP server, backed by a SQLite FTS5 index over the transcript archive, that gives any Claude Code session cheap search and surgical read access to everything that ever happened on this machine.**

The cardinal design rule: this tool exists to protect the context window, so it must never flood it. Every tool response is hard-capped. Outline first, then targeted reads. Never return a whole transcript.

---

## 2. Verified environment facts (checked 2026-07-01 on this machine)

- Transcript root: `C:\Users\Serin\.claude\projects\`
- Layout: `<encoded-project-path>\<session-id>.jsonl`. Encoded folder names replace non-alphanumeric characters with hyphens (both `C--` and `c--` prefixes observed). The encoding is lossy: treat the folder name as an opaque project key. The true working directory is available as a `cwd` field inside transcript entries; prefer that as ground truth for project identity.
- Scale at time of writing: 1,652 jsonl files, ~1.1 GB, 36 project folders. Oldest surviving data: 2026-05-23 (everything earlier was deleted by the default 30-day retention sweep before we fixed it).
- Files are append-only, one JSON object per line, never rewritten.
- Entry shapes (documented and observed; verify against live samples per section 0.6):
  - `user` entries: `message.content` is either a plain string or an array of content blocks. `tool_result` blocks appear here, referencing `tool_use_id`.
  - `assistant` entries: `message.content` is an array mixing `text`, `thinking`, and `tool_use` blocks. `message.usage` carries token counts. Thinking blocks are stored verbatim.
  - Additional entry types: summary/compaction boundary entries, file-history snapshots, hook output, metadata. Unknown types will appear over time; skip them gracefully.
  - Entries carry `uuid`, `parentUuid`, `sessionId`, `timestamp`, `cwd`.
- Subagent transcripts exist in subdirectories named `subagents`. Glob recursively; index them like any session. Parent linkage (via agent/parent metadata fields) is best-effort, not required for v0.
- Sidecar payloads: some sessions store large tool results as separate files, observed as `<encoded-project>\<session-id>\tool-results\webfetch-*.bin`. Never ingest binary sidecars. Index a pointer only (path, tool name, timestamp).
- Duplicate message uuids can appear across multiple files (branching and resume can write the same message to more than one transcript). Dedupe by uuid for search results and counts.
- A global prompt log also exists at `C:\Users\Serin\.claude\history.jsonl` (one line per user prompt: text, timestamp, project path, session id). Useful as a cross-check; not the primary corpus.
- Retention status: `settings.json` now carries `"cleanupPeriodDays": 99999` (set 2026-07-01). **Never set this to 0**: a confirmed upstream bug makes 0 disable transcript writing entirely instead of disabling cleanup. Upstream bugs also exist where deletion happens despite high values, which is why the read-only and no-mtime-touching rules above are load-bearing, and why a separate backup task is planned outside this project.

---

## 3. Architecture

Language: Python 3.11+. Storage: sqlite3 with FTS5 (standard library; verify FTS5 is available at startup and fail loudly with guidance if not). Dependencies: the official Python MCP SDK, nothing else unless genuinely necessary. No network egress, no telemetry. The transcripts are plaintext and can contain secrets that passed through tools; the index inherits that sensitivity, so everything stays local.

Project root: `C:\Users\Serin\Desktop\ClaudeCode\projects\Session_Recall\`

Suggested layout (flat, in the style of Terminal_Share):

```
Session_Recall\
  session_recall\
    __init__.py        (__version__ lives here)
    parser.py          (jsonl line -> normalized records)
    store.py           (schema, upserts, FTS queries)
    indexer.py         (discovery, incremental offsets, orchestration)
    server.py          (MCP stdio server)
    cli.py             (index/search/status from the command line)
  data\
    recall.db          (gitignored)
  prompts\
  README.md
```

### 3.1 Indexer

- Discovery: recursive glob of `C:\Users\Serin\.claude\projects\**\*.jsonl`.
- Incremental: a `files` table tracks path, size, mtime, and `last_offset` (byte offset). Because transcripts are append-only, resume parsing from `last_offset`. If current size is smaller than `last_offset` (file replaced), reindex that file from zero.
- Defensive parsing: `json.loads` per line inside try/except. Skip malformed lines and unknown entry types, increment a skip counter, and never let a bad line or bad file abort the run.
- What gets full-text indexed:
  - user text
  - assistant text
  - thinking text
  - `tool_use`: tool name plus a compacted rendering of the input, capped around 1 KB
  - `tool_result`: text preview capped around 2 KB, with an `is_truncated` flag and a raw locator (file path plus line number) so full content can be fetched later on demand
- What gets excluded from the index:
  - binary and sidecar payloads (pointer records only)
  - base64 and image blocks
  - **entries produced by Session_Recall's own MCP tools.** Detect by tool name prefix (for example `mcp__session-recall`, match against whatever name the server registers under). Without this exclusion, every search result gets re-logged into new transcripts and re-indexed, and past search output starts dominating future searches. This echo suppression is a hard requirement.
- Schema sketch (adjust as implementation demands, keep the shape):
  - `files(id, path, project_key, size, mtime, last_offset, last_indexed_ts)`
  - `sessions(session_id PK, project_key, cwd, started_ts, last_ts, message_count, first_user_prompt, summary)`
  - `messages(uuid PK, session_id, parent_uuid, role, entry_type, ts, tool_name, file_id, line_no, char_len, is_truncated)`
  - `messages_fts` (FTS5 over the extracted text, rowid-mapped to `messages`)
  - Dedupe: uuid primary key, keep first occurrence, optionally record alternate locations.
- Use SQLite WAL mode so queries and indexing can overlap safely.
- CLI smoke interface (no MCP required): `python -m session_recall index [--full]`, `python -m session_recall search "query" [--project X]`, `python -m session_recall status`.

### 3.2 MCP server (stdio)

Hard output caps on every tool are the point of the design, not an optimization. Suggested tools:

1. `search_history(query, project=None, role=None, limit=10)` returns ranked hits: snippet (<= 300 chars), session_id, uuid, timestamp, project, tool_name when relevant. Total response capped around 4 KB.
2. `list_sessions(project=None, limit=15)` returns session_id, project, started/last timestamps, message_count, first_user_prompt (<= 150 chars), recency sorted.
3. `get_session_outline(session_id, max_items=100)` returns an ordered skim: uuid, role, timestamp, kind, head (<= 120 chars), tool_name. Response capped around 8 KB. This is the "look before you read" tool.
4. `read_messages(session_id, uuids=None, start_uuid=None, count=10, max_chars=6000)` returns exact stored text for specific messages. If the indexed copy was truncated, fetch full text from the raw jsonl via the stored locator, still bounded by max_chars. If the request exceeds caps, return the first slice plus a continuation hint.
5. `recall_status()` returns file count, message count, skip counter, last index time.
6. `reindex(full=False)` triggers an index pass.

Registration: user scope, so every project on the machine sees it. Verify the exact current registration syntax in the Claude Code docs (`claude mcp add --scope user ...` or the current equivalent) rather than assuming.

### 3.3 Adoption plumbing

The server is useless if post-compact sessions never think to call it. Deliverables:

- README.md: install, registration, CLI usage, tool reference.
- A ready-to-paste CLAUDE.md snippet, final wording in Phase 0.4, along these lines:

> Session_Recall MCP is available. After any context compaction, and before re-deriving a solution, design decision, or diagnosis, call `search_history` with 2 to 4 distinctive keywords. Use `get_session_outline` and then `read_messages` for surgical retrieval. Never pull a whole session into context.

---

## 4. Phases

No time estimates. Each phase ships with its acceptance criteria met and `__version__` bumped.

### Phase 0.1.0: skeleton, schema, single-project indexer, CLI search
Build the package skeleton, schema, defensive parser, and a one-shot (non-incremental) indexer pointed at exactly two project folders: the PatchManager folder(s) and the MAS-Trader folder. Wire the CLI.
**Acceptance:** `python -m session_recall search "ECONNRESET"` surfaces the PatchManager sessions containing the F5 WAF TLS-inspection diagnosis. Report what MAS_Trader material survives (its April-era sessions likely predate the 2026-05-23 retention horizon; whatever you find, say so plainly).

### Phase 0.2.0: full corpus, incremental, hardening
Extend to the full corpus. Incremental offset tailing, size/mtime change detection, uuid dedupe, sidecar pointer records, tool_result caps, self-echo exclusion.
**Acceptance:** a full build over all 1,652+ files completes with zero raised exceptions and a reported skip count. An immediate rerun is incremental and near-instant. Malformed lines are counted, not fatal.

### Phase 0.3.0: MCP server
Implement the six tools with hard caps, WAL-safe concurrent access, and user-scope registration.
**Acceptance:** from a fresh Code session opened in an unrelated project (for example Serinety_TD), each tool is called once successfully; `search_history("ECONNRESET")` returns hits from PatchManager history and `read_messages` retrieves exact text within caps.

### Phase 0.4.0: docs, adoption, polish
README, final CLAUDE.md snippet, `recall_status` polish, and verification that two concurrent Code sessions can query while an index pass runs.
**Acceptance:** docs complete; concurrency check passes; Serinety signs off on the snippet wording.

### Deferred (do not build now)
File-watcher daemon (manual/on-demand `reindex` is enough for v0), semantic embeddings, SessionStart auto-context hook, PreCompact digest hook, the backup scheduled task (owned separately by Claudia and Serinety), cross-machine sync, any web UI.

---

## 5. Open items (Serinety's, not blocking the build)

1. Confirm or rename the project (Session_Recall is provisional).
2. Choose a backup destination for the `~/.claude/projects` mirror (separate task).

---

## 6. Canonical test material

The PatchManager ECONNRESET saga is the reference retrieval case: a V12 PowerShell project whose Code sessions hit ECONNRESET failures, diagnosed as a corporate F5 WAF doing TLS inspection on large POST bodies, with a phone-hotspot-vs-work-network test identified as the decisive experiment. A post-compact session asking "what did we conclude about the ECONNRESET failures" must be able to recover that diagnosis through this tool in under three tool calls. That is the bar.
