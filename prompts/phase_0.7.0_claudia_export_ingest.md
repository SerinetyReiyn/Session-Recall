# Phase 0.7.0: Claudia export ingest (third corpus)

You are building phase 0.7.0 of Session_Recall. Bump
session_recall/__init__.py __version__ to "0.7.0". Version and phase are
one source of truth.

## Hard rules (Serinety's standing constraints)

- NO em-dashes anywhere: code, comments, docs, tests, commit messages.
- No time estimates in anything you write.
- recall.db contents and any real conversation data never leave this
  machine. Test fixtures must be fully synthetic. Never copy real
  export content into the repo.
- The export zip is read-only input. Never modify, move, or delete it.
- Inspect the existing code (store.py, indexer.py, parser.py,
  parser_codex.py, server.py, cli.py) and follow its established
  patterns before inventing new ones.

## Context

Session_Recall v0.6.0 indexes two corpora into data/recall.db with a
source column: "claude" (Claude Code JSONL under ~/.claude/projects)
and "codex" (Codex rollouts). Both are tailed from local disk.

The third corpus is Claude Desktop / claude.ai conversations, called
source "claudia". There is NO local transcript store for the desktop
app (verified empirically: the local IndexedDB cache is about 2.3 MB
and contains no conversation text). Ground truth is the official
account data export: claude.ai Settings > Privacy > Export data, which
emails a link to a zip. Depending on vintage the zip contains
conversations.json (array of conversation objects) or
conversations.jsonl (one conversation object per line). Each
conversation object carries a uuid, a name/title, timestamps, and an
array of messages with sender roles, text content, and per-message
uuids and timestamps.

IMPORTANT HONESTY GUARD: we have not yet opened a real export from
Serinety's account. Build against synthetic fixtures matching the
structure described above, keep the parser defensive, and provide the
--inspect mode described below so assumptions can be corrected against
the real file in a fast follow-up.

## Scope

1. New module session_recall/parser_claude_export.py
   - Accepts a path to an export zip OR an already-extracted
     conversations.json / conversations.jsonl.
   - Auto-detects json vs jsonl by sniffing, not by extension alone.
   - Tolerant: a malformed conversation is skipped and counted in a
     warnings summary; the batch never aborts. Known upstream quirk:
     very long conversations occasionally serialize with incomplete
     assistant messages. Treat missing or empty content as empty text.
   - Message text may be a plain string or a list of content blocks;
     concatenate text-bearing blocks. Document in a comment what block
     types are skipped.

2. Schema mapping (follow store.py patterns exactly)
   - source = "claudia" on every row this ingest creates.
   - sessions: session_id = conversation uuid, project_key =
     "claude_desktop", cwd = None, started_ts / last_ts from the
     conversation's message timestamps, first_user_prompt from the
     first human message, summary = the conversation name/title,
     message_count maintained the same way the other corpora do it.
   - messages: uuid = the message uuid from the export; if a message
     lacks one, synthesize deterministically as
     "claudia:<conversation_uuid>:<index>" so re-ingest stays stable.
     Map sender human -> role user, assistant -> role assistant.
     entry_type mirrors the conventions the other parsers use for
     plain conversational turns. ts from the message timestamp.
     char_len and FTS text from the concatenated message text.
   - files: messages reference a file_id in this schema, so register
     the export file itself as a files row (source "claudia") and hang
     the messages off it, consistent with how store.py expects
     origins. If a second export is ingested later, its conversations
     upsert against the same sessions regardless of which export file
     they arrived in.

3. Idempotent upsert (this is the core requirement)
   - Repeated ingestion of full-account dumps is the NORMAL mode.
   - Re-ingesting the same export must change nothing: identical
     counts before and after.
   - Ingesting a newer export where a conversation gained messages
     must append only the new messages and refresh that session's
     last_ts, message_count, and summary.
   - Skip-fast path: if a conversation's newest message ts is not
     newer than the stored session last_ts, skip it without touching
     rows.
   - Deleted conversations simply stop appearing in exports; do NOT
     delete their indexed rows. The index is an archive.

4. CLI (wire into cli.py / __main__.py following existing style)
   - ingest-claudia <path>: run the ingest, print a summary
     (conversations seen / new / updated / skipped / malformed,
     messages inserted).
   - ingest-claudia <path> --dry-run: parse and report, write nothing.
   - ingest-claudia <path> --inspect: print the structural shape of
     the file (top-level type, keys present on the first conversation
     and first message, counts) WITHOUT printing any content values.
     This is the safe probe Serinety will run against her real export
     first.

5. MCP server surface
   - search_history and list_sessions already carry source filtering
     for "claude" and "codex". Extend accepted values to include
     "claudia" wherever sources are validated or documented, including
     tool descriptions. Searching with no source filter must include
     claudia rows automatically.

6. Tests (follow tests/ conventions, synthetic fixtures only)
   - Fixture builders for BOTH vintages: a conversations.json array
     and a conversations.jsonl, plus a zip wrapping one of them.
   - Ingest correctness: expected session and message counts, field
     mapping spot checks, FTS searchability of an inserted phrase.
   - Idempotency: ingest the same fixture twice, counts identical.
   - Growth: second fixture where one conversation gained messages;
     only the delta lands.
   - Tolerance: one malformed conversation in the batch is skipped,
     counted, and everything else still lands.

7. Docs and closeout
   - README: short "Claudia corpus" section covering how to request
     the export (Settings > Privacy > Export data, link arrives by
     email, 24 hour expiry) and the ingest commands.
   - NOTES.md: phase entry with what was built and any format
     assumptions that still need verification against a real export.
   - Full pytest suite green, including all pre-existing tests.

## Acceptance

- __version__ == "0.7.0" and pytest fully green.
- Synthetic fixture ingest produces exact expected counts; double
  ingest is a no-op; growth ingest lands only the delta.
- search_history returns claudia rows tagged source "claudia".
- Report back with: test counts, ingest summary output from the
  fixtures, and a short list of every assumption about the real
  export format that --inspect should be used to verify.
