# Session_Recall project notes

Session-to-session working notes for this project (per phase 0.2 amendment A2:
project persistence lives in project-local files, never under C:\Users\Serin\.claude).
Authoritative phase specs are the `prompts\phase_*.md` files authored by Claudia.

## Status

- 0.1.0 accepted: skeleton, schema, defensive parser, one-shot indexer (two
  target folders), CLI (index / search / status).
- 0.2.0 accepted: full corpus, incremental byte-offset tailing, change
  detection, sidecar pointer records, uuid dedupe.
- 0.2.1 (this closeout, from `prompts\phase_0.2.1_closeout.md`): configurable
  self-echo prefix, tests directory, per-type skip breakdown and per-project
  counts and journal_mode in status, WAL confirmed, NOTES.md. Also folded in
  three post-0.2.0 hardening fixes found by adversarial review:
  per-file transactional atomicity; first_user_prompt no longer clobbered on
  incremental append; sidecar rows excluded from session aggregates.
- 0.3.0 (from `prompts\phase_0.3_mcp_server.md`): MCP stdio server with six
  capped tools (search_history with per-session diversity cap and quoted-phrase
  support, list_sessions, get_session_outline, read_messages with raw-transcript
  full fetch and continuation hints, recall_status, reindex). Packaged with
  pyproject.toml for an editable install and two console scripts. Registered at
  user scope as "session-recall" (command:
  .venv\Scripts\session-recall-mcp.exe). CLI output reconfigured to UTF-8.
  Registered tool prefix mcp__session-recall matches the self-echo exclusion.
  Post-review hardening applied during 0.3 verification: (a) self-echo now also
  drops the matching tool_result (the search output), not just the tool_use
  query, via a suppressed-tool_use-id set in the parser (this was the
  load-bearing half of the hard requirement); (b) read_messages continuation is
  always followable (next_uuids for the uuids path, next_start_uuid resolves its
  own session for the session path) and a single oversized message pages through
  in bounded slices via start_char and terminates; (c) the uuids path is capped
  and charges a per-message metadata budget; (d) search tokenization normalizes
  backslashes so Windows-path queries do not collapse under shlex.
- 0.4.0 (from `prompts\phase_0.4_adoption.md`, final planned phase): freshness
  guard (search_history / list_sessions / get_session_outline run an incremental
  pass first when the last index is older than a threshold, default 15 min, env
  SESSION_RECALL_FRESHNESS_SECONDS; is_stale is a pure, unit-tested function); no
  watcher daemon needed. CLI `search` brought to parity with the MCP tool via a
  shared per-session diversity cap default (config.DEFAULT_PER_SESSION_CAP,
  `--cap`, `--cap 0` disables). busy_timeout added so concurrent refreshes wait
  rather than error. SNIPPET.md delivered (project-scoped canonical forms).
  README finalized with Freshness, Concurrency, and how-to sections. Canonical
  queries are now project-scoped in their durable forms per Claudia's ruling:
  Wintermaul's top unscoped hit is now a Session_Recall dev session that typed
  the token, which is ordinary indexing, not a self-echo failure.

- 0.5.0 (from `prompts\phase_0.5.0_web_ui.md`, requested directly by Serinety):
  local web UI (`session_recall/webui.py`, stdlib http.server, 127.0.0.1 only)
  with search, session browsing, outline, and full message reader. Double-click
  launchers in the project root: `Session_Recall Web UI.bat` (start plus browser)
  and `Stop Session_Recall Web UI.bat`. Reuses the store and freshness guard;
  the reader shows full text since context caps are for models, not a human.
  Console script `session-recall-web`, port env `SESSION_RECALL_WEB_PORT`
  (default 8765). While packaging this, fixed a pre-existing broken editable
  install: an earlier reinstall had rolled back because the running MCP server
  held `session-recall-mcp.exe` locked, leaving the package uninstalled and the
  console scripts resolvable only from the project-root cwd. Stopped the leaked
  server processes (about seven stale 0.3.0 instances), removed the invalid
  `~ession_recall` leftover, reinstalled; the MCP server now spawns cleanly and
  reports 0.5.0. Lesson: stop the MCP server before `pip install -e .` on
  Windows, since the exe is locked while it runs.

- 0.6.0 (from `prompts\phase_0.6.0_codex_corpus.md`, requested directly by
  Serinety): unified cross-tool memory. Codex rollouts (`~/.codex/sessions` and
  `archived_sessions`, JSONL in the OpenAI Responses format) are indexed into the
  same `recall.db` as Claude Code, so both tools share one memory. New
  `parser_codex.py` (skips the duplicate `event_msg` stream, threads
  session_id/cwd from `session_meta`, drops encrypted reasoning). New `source`
  column ('claude' or 'codex') on messages/sessions/files, exposed as an optional
  filter: MCP `search_history(source=...)`, CLI `search --source`, and a
  per-source `status` breakdown. Codex flattens MCP tool names to bare names, so
  self-echo suppresses the known set (`search_history`, `recall_status`, ...) via
  `self_echo_prefixes_codex`, env `SESSION_RECALL_CODEX_ECHO_TOOLS`. The Codex
  corpus indexes only on a real default-root pass, so tests with a temp root do
  not touch it (they opt in via `codex_home`). Verified: full rebuild 40 Claude +
  72 Codex sessions, 0 parse/file errors; Codex self-echo held (0 own-tool rows);
  incremental rerun tails both corpora in under a second. Also registered the
  server in `~/.codex/config.toml` (Serinety authorized the write). Known
  characteristic: Codex `first_user_prompt` may show injected context (AGENTS.md,
  environment) since Codex delivers those as user-role messages.
  Post-review fixes (adversarial review, all confirmed minor): (a) `web_search_call`
  now indexes its query from `payload.action.query`/`queries` (it carries no
  `arguments`), so 84 real search queries became searchable; (b) self-echo now
  holds across an incremental pass boundary: suppressed call-ids (Codex) and
  tool_use-ids (Claude) are persisted on the `files.echo_ids` column and reseeded
  each pass, so a self-tool output arriving in a later pass than its call is still
  dropped (this closed a pre-existing gap in the Claude parser too); (c)
  `read_messages` can now expand a truncated Codex tool row to full text via
  `read_full_text_codex`, dispatched on the row's `source`.

## Note on canonical queries and this build session

Testing this project from within a live Code session pollutes the index with the
very terms used to test (ECONNRESET, Wintermaul, session ids), because the
session's own transcript is part of the corpus and CLI or Bash testing is not
self-echo traffic. The per-session diversity cap in search_history is what keeps
one noisy session from filling the result list: for example Wintermaul had 19 of
its top 20 hits from this build session, yet the diversified top 10 still
surfaces Serinety_TD session 36916d19. Bare ECONNRESET remains non-distinctive
over the full corpus; scope it by project.

## Ratified decisions (from the phase prompts)

- A1: indexing capped `system` entries (api_error and similar) is approved,
  even though build doc section 3.1 omits them. This is what makes the
  ECONNRESET smoke test work; the string lives only in system error entries.
- A2: the read-only rule on C:\Users\Serin\.claude binds this project's code and
  builder. Claude Code's own memory / transcripts / todos are out of scope; do
  not fight the app. Project persistence uses NOTES.md here.
- A3: the F5 WAF / TLS-inspection / hotspot diagnosis was worked out in Claude
  Desktop conversations, a separate corpus that never lands in ~/.claude/projects,
  so it is not recoverable from Code transcripts. The ECONNRESET search stays as
  a mechanical smoke test. Desktop-corpus indexing is deferred, do not build it.
- A4: the em-dash prohibition covers chat output and completion reports, not
  only files.

## Canonical queries (smoke and living-data bars)

- `ECONNRESET` -> PatchManager session `edb4bc71` (mechanical smoke test).
- `"comprehensive review"` -> MAS_Trader session `abc5642d` (2026-06-11).
- `Wintermaul` -> Serinety_TD session `36916d19`.

## Files-to-sessions reconciliation (0.2 item 8)

Transcript files greatly outnumber sessions because subagent and workflow
transcripts share the parent session's `sessionId`. Every `agent-*.jsonl` and
`journal.jsonl` under a session's `subagents\...` tree carries the same
`sessionId` as the top-level `<session-id>.jsonl`, so they collapse into one
session row. Examples from the live index: MAS-Trader has 16 transcript files
but 1 session; Ascension has 350 files across 3 sessions. The full corpus is
~1700 transcript files collapsing to 34 sessions, which is correct, not a
grouping bug. As of 0.2.1 every session has a first_user_prompt (0 without).

## Notes on status skip counts

The per-type skip breakdown in `status` reflects the last index pass only (it is
stored in `meta` each run). After an incremental no-op rerun the numbers are
near zero; run `python -m session_recall index --full` to see corpus-wide skip
counts. This explains the earlier observation that `skipped_type` appeared to
fall from 1135 to 4 between passes: those were different passes, not a
regression.

## Interpreter note

Bare `python` on this machine is the Windows Store stub. The project venv
(`.venv`, created from `py -3.12`) provides a real interpreter; activate it, or
call `.\.venv\Scripts\python.exe` directly.
