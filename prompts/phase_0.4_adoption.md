# Session_Recall Phase 0.4.0: adoption, freshness, final polish

Precondition: Phases 0.2.1 and 0.3.0 were independently verified and accepted 2026-07-01 (21 tests passing, status complete with per-type and per-project breakdowns, WAL confirmed live, registration Connected at user scope, zero em-dashes in files or report). Read the build doc section 3.3 and this prompt in full. Section 0 conventions in force; `__version__` becomes `"0.4.0"`; completion report em-dash free; no time estimates.

This is the final planned phase. Everything not listed here stays on the deferred list.

## A finding you should know about, and a ruling

The unscoped `Wintermaul` query now returns, as its top hit, your own 0.3 development session: an Edit tool_use writing test code that contained the string "Wintermaul". This is not a self-echo failure (the exclusion correctly targets only `mcp__session-recall` entries); it is ordinary indexing of a session that happens to manipulate the canonical strings. Sessions about Session_Recall will accumulate every canonical token over time.

Ruling: canonical queries are now project-scoped in their durable forms, and the adoption snippet must coach project scoping:
- `ECONNRESET` scoped to PatchManager reaches `edb4bc71`
- `"comprehensive review"` scoped to MAS reaches `abc5642d`
- `Wintermaul` scoped to Serinety_TD reaches `36916d19`

## Scope

1. **Freshness guard.** There is no watcher daemon (deferred), so define when the index refreshes: before answering `search_history`, `list_sessions`, or `get_session_outline`, if the last index pass is older than a configurable threshold (default 15 minutes, env-overridable), run an incremental pass first. Observed incremental cost over the full corpus is well under a second, so the latency trade is acceptable; document it. Unit test the threshold logic. This makes the tool self-maintaining with no daemon and no scheduled task.
2. **Final CLAUDE.md adoption snippet.** Start from the build doc 3.3 draft and finalize. It must coach: search after any compaction and before re-deriving prior work; 2 to 4 distinctive keywords; quoted phrases for exact matches; the project filter for anything generic; outline first, then `read_messages`; never pull whole sessions. Keep it short enough that it costs almost nothing in every context window. Deliver the final text in your report and as a `SNIPPET.md` in the project root; Serinety decides where to paste it.
3. **CLI parity.** The MCP `search_history` applies the per-session diversity cap, phrase support, and project filter; the CLI `search` does not apply the cap (a PatchManager-scoped ECONNRESET query returns 10 hits from one session at the CLI). Bring the CLI search to parity with the MCP semantics.
4. **Live end-to-end echo check, carried from 0.3.** Your 0.3 session predated registration and could not run it. This phase's acceptance session doubles as the vehicle: after exercising the tools live, run an incremental pass and confirm zero newly indexed rows carry the registered prefix.
5. **README finalization.** Install, registration, tool reference, freshness behavior, encoding notes, test instructions, and a short "how retrieval is meant to be used" section mirroring the snippet.
6. **Concurrency sign-off.** The 4210-reads-during-write demonstration stands; state the supported concurrency model in the README in one paragraph (WAL, readers never block on the indexer, single-writer assumption).

## Acceptance

- From a fresh Claude Code session in a different project: the three project-scoped canonical queries pass via MCP; an outline-then-read flow retrieves exact text; the live echo check then shows zero prefix rows after an incremental pass.
- Freshness guard: with the threshold set artificially low, a search visibly triggers an incremental pass first (log line or status delta), covered by a unit test for the threshold logic.
- CLI ECONNRESET scoped to PatchManager now respects the diversity cap.
- `SNIPPET.md` delivered; README complete; all tests pass; `__version__` reads 0.4.0.
- Completion report em-dash free.

## After this phase

Remaining deferred items stay deferred: watcher daemon, semantic embeddings, Desktop-corpus indexing, SessionStart/PreCompact hooks, backup task (owned by Claudia and Serinety), cross-machine sync, web UI. Any of these becomes a new minor version with its own prompt if ever scheduled.
