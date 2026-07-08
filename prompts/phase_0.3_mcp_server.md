# Session_Recall Phase 0.3.0: MCP server

Precondition: Phase 0.2.1 accepted. Read the build doc (`prompts\phase_0.1_build_doc.md`) section 3.2 and this prompt in full before starting. Section 0 conventions in force; your completion report must contain zero em-dashes; no time estimates; `__version__` becomes `"0.3.0"`.

Before writing any server code (build doc section 0, item 4): consult the current official Python MCP SDK documentation for the stdio server API surface, and verify the exact current user-scope registration syntax (`claude mcp add --scope user ...` or whatever the current equivalent is). Do not code either from memory.

## Scope

1. **Six tools per build doc section 3.2, with the hard caps as specified:** `search_history`, `list_sessions`, `get_session_outline`, `read_messages`, `recall_status`, `reindex`. The caps are load-bearing: this tool exists to protect the context window and must never flood it. Oversized requests return the first slice plus a continuation hint, never an error and never the full payload.
2. **`search_history` refinements:**
   - Per-session diversity cap, default 3 hits per session, configurable. Today a plain `ECONNRESET` query returns 10 hits from one session; one noisy session must not fill the result list.
   - Support FTS5 quoted-phrase queries so callers can search exact phrases.
   - Pass the project filter through to the store.
3. **Registration:** user scope, so every project on the machine sees the server. Record the final registered server name and wire it into the indexer's self-echo exclusion prefix (config, not hardcode), so the exclusion tested in 0.2.1 matches reality.
4. **Small polish, low priority:** CLI snippet output on the Windows console renders non-ASCII characters as replacement glyphs. Set UTF-8 output handling in the CLI, or document the `chcp 65001` / `PYTHONIOENCODING` workaround in the README. Cosmetic only; MCP JSON responses are unaffected.

## Acceptance

- From a fresh Claude Code session opened in a different project, each of the six tools is called successfully at least once.
- Via MCP, all three canonical queries pass within caps:
  - `ECONNRESET` surfaces PatchManager session `edb4bc71`
  - `"comprehensive review"` surfaces MAS_Trader session `abc5642d`
  - `Wintermaul` surfaces Serinety_TD session `36916d19`
- `read_messages` retrieves exact stored text; an intentionally oversized request demonstrably truncates with a continuation hint.
- End-to-end echo check: after exercising the tools in a live session, run an incremental index pass and confirm zero newly indexed rows carry a tool name matching the registered prefix.
- `recall_status` answers over MCP while an index pass is running (WAL working in practice).

## Deliverables

`__version__ = "0.3.0"`, README updated with registration steps and a tool reference, and an em-dash-free completion report including the registered server name and the exact registration command used.

## Still deferred

Everything on the build doc's deferred list. The finalized CLAUDE.md adoption snippet and full concurrency polish remain Phase 0.4.
