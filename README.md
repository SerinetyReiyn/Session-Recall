# Session_Recall

Local search and surgical read over the Claude Code transcript archive.

Long Claude Code sessions auto-compact, and the post-compact model routinely
re-derives solutions, diagnoses, and design decisions it already produced. The
data is not lost: Claude Code writes a complete append-only JSONL transcript of
every session to disk. Session_Recall builds a local SQLite FTS5 index over that
archive so any session can search and retrieve what already happened, without
flooding the context window.

Version: 0.6.0. The version string in `session_recall/__init__.py` is the single
source of truth for the project phase.

## Status: phase 0.6.0

The index covers two corpora in one database: Claude Code transcripts under
`~/.claude/projects` and Codex rollouts under `~/.codex/sessions`. Both MCP
servers (the one Claude Code launches and the one Codex launches) read the same
`data/recall.db`, so the two tools share one memory: each can search the other's
history and its own. Every row is tagged with its `source` (`claude` or `codex`)
so results can be blended by default or filtered to one tool. See "Two corpora"
below.

Indexing is incremental for both corpora: each file's byte offset and line count
are persisted, so a rerun tails only the bytes appended since the last pass,
detects replaced or truncated files by a shrunk size and reparses them from zero,
and skips unchanged files after a single stat. Large tool results stored as
separate sidecar files (Claude Code) are recorded as pointers only; their content
is never ingested. Records are deduped by uuid across branched and resumed
transcripts.

An MCP server exposes six capped tools over stdio so any session can search and
surgically read the archive. See "MCP server" below. There is no watcher daemon:
instead a freshness guard keeps the index current on demand (see "Freshness"), so
the tool is self-maintaining with no scheduled task.

The transcript archives (`~/.claude` and `~/.codex`) are treated as strictly read
only. The indexer only reads transcripts; it never writes, renames, or touches
anything in those trees, and it does not modify file mtimes. The index database
lives in this project's `data/` folder.

## Setup

Requires Python 3.11 or newer with FTS5 compiled into sqlite3 (standard on
official CPython builds). The MCP server depends on the official `mcp` SDK; the
CLI and indexer are otherwise pure standard library.

```text
cd C:\Users\Serin\Desktop\ClaudeCode\projects\Session_Recall
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

The editable install puts three console scripts on the path, `session-recall`
(the CLI), `session-recall-mcp` (the MCP server entry point), and
`session-recall-web` (the web UI server), and makes the `session_recall` package
importable from any working directory.

## Command line usage

```text
python -m session_recall index [--full]
python -m session_recall search "query" [--project X] [--role ROLE] [--limit N] [--cap K] [--source claude|codex]
python -m session_recall status
```

- `index` builds or updates the index over the full corpus. It is incremental by
  default: unchanged files are skipped, appended files are tailed from their last
  byte offset, and replaced files are reparsed from zero. `--full` clears the
  index and rebuilds from scratch.
- `search` runs a full-text query. Two to four distinctive keywords work best.
  Terms are ANDed together, and an exact phrase can be wrapped in double quotes.
  `--project` filters by project key or working directory substring; `--role`
  filters by `user`, `assistant`, `system`, or `summary`; `--cap` sets the
  per-session diversity cap (default 3, matching the MCP tool; `--cap 0` disables
  it) so one noisy session cannot fill the results; `--source` filters by corpus
  (`claude` or `codex`), defaulting to both.
- `status` reports file, sidecar, message, and session counts (with a per-source
  breakdown), the skip breakdown, and the last index time.

The index database defaults to `data/recall.db` and can be pointed elsewhere
with the global `--db PATH` option. CLI output is reconfigured to UTF-8 so
snippets containing non-ASCII characters render rather than crashing the Windows
console.

## MCP server

The server exposes six tools over stdio. Every response is hard-capped: the tool
exists to protect the context window, so oversized requests return a first slice
plus a continuation hint, never the full payload. Register it once at user scope
so every project on the machine can use it:

```text
claude mcp add -s user session-recall -- C:/Users/Serin/Desktop/ClaudeCode/projects/Session_Recall/.venv/Scripts/session-recall-mcp.exe
```

Verify with `claude mcp list` (expect `session-recall ... Connected`); remove with
`claude mcp remove session-recall`. The server logs only to stderr. Its tools are
exposed as `mcp__session-recall__<tool>`, and the indexer excludes that prefix
(self-echo suppression, configurable via `SESSION_RECALL_ECHO_PREFIXES`) so search
output never re-indexes itself.

Tools:

- `search_history(query, project=None, role=None, limit=10, per_session_cap=3, source=None)`:
  ranked full-text search across both corpora. Wrap a substring in double quotes
  for an exact phrase. Results are diversified so one noisy session cannot fill the
  list. `source` filters to `claude` or `codex`; each hit carries its `source`.
- `list_sessions(project=None, limit=15)`: recent sessions with their first prompt.
- `get_session_outline(session_id, max_items=100)`: an ordered skim, to look
  before you read.
- `read_messages(session_id=None, uuids=None, start_uuid=None, count=10, max_chars=6000, start_char=0)`:
  exact stored text for specific messages, fetching full content from the raw
  transcript when the indexed copy was truncated, bounded by `max_chars`. When a
  request exceeds the budget it returns a first slice plus a followable
  continuation hint (`next_uuids` after a uuids call, otherwise `next_start_uuid`,
  with `next_start_char` for paging a single long message), never the full payload.
- `recall_status()`: index health (counts, journal mode, last index time).
- `reindex(full=False)`: trigger an index pass (incremental by default).

`search_history`, `list_sessions`, and `get_session_outline` run the freshness
guard before answering (see "Freshness"); `recall_status` and `reindex` do not,
so status always reflects the index as it stands.

A note on scoping queries. A term that recurs across projects (a common error
name, or any string that sessions about Session_Recall itself accumulate) is not
distinctive over the full corpus, so scope it with a `project` filter. The
canonical reference queries in their durable, project-scoped forms are:

- `ECONNRESET` scoped to `PatchManager` reaches session `edb4bc71`.
- `"comprehensive review"` scoped to `MAS` reaches session `abc5642d`.
- `Wintermaul` scoped to `Serinety-TD` reaches session `36916d19`.

The ready-to-paste CLAUDE.md adoption snippet lives in `SNIPPET.md`; see also
"How retrieval is meant to be used" below.

## Web UI

A local browser front end over the same index, for when you would rather click
than type commands. It serves a search box, a session browser, and a full
message reader.

Start it by double-clicking `Session_Recall Web UI.bat` in the project folder. A
browser tab opens at `http://127.0.0.1:8765/`. Leave the small console window
open while you use it; close that window, or double-click
`Stop Session_Recall Web UI.bat`, to stop the server. Double-clicking the
launcher again while it is already running just reopens the tab.

It binds to `127.0.0.1` only and is never exposed on the network, because
transcripts can contain secrets. Search, session listing, and outline run the
freshness guard first, so results are current. Unlike the MCP tools, the reader
shows a message in full: the hard caps exist to protect a model's context
window, which does not apply to you reading in a browser.

The port is overridable with `SESSION_RECALL_WEB_PORT`; from an activated venv
the server is also available as the `session-recall-web` command.

## Freshness

There is no watcher daemon (deferred). Instead, before `search_history`,
`list_sessions`, or `get_session_outline` answers, it checks how long ago the
last index pass ran; if that is older than a threshold (default 15 minutes,
override with `SESSION_RECALL_FRESHNESS_SECONDS`), it runs an incremental pass
first. An incremental pass over the full corpus costs well under a second because
unchanged files are skipped after a single stat, so the latency trade is small
and the caller always sees a current index. A refresh advances the last index
time, which is visible in `recall_status`, and is logged to stderr. The guard is
best effort: if a refresh fails, the read still answers from the existing index.
Set the threshold to 0 to refresh on every read, or very high to disable the
guard and refresh only via `reindex`.

## How retrieval is meant to be used

The tool exists to protect the context window, not to fill it. The intended flow,
which the `SNIPPET.md` adoption text coaches, is:

- After any compaction, and before re-deriving a solution, diagnosis, or design
  decision, `search_history` with 2 to 4 distinctive keywords. Quote an exact
  phrase; add a `project` filter for anything generic.
- `get_session_outline` on a promising session to look before you read.
- `read_messages` for only the specific messages you need.
- Never pull a whole session into context. Every response is already capped.

## Two corpora: Claude Code and Codex

The index ingests both Claude Code transcripts (`~/.claude/projects/**/*.jsonl`)
and Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl` plus
`archived_sessions/`) into one database. Because both MCP servers read that one
database, Claude Code and Codex share a single memory: each can recall the other's
work and its own. Register the same server in Codex by adding it to
`~/.codex/config.toml`:

```toml
[mcp_servers.session-recall]
command = 'C:\Users\Serin\Desktop\ClaudeCode\projects\Session_Recall\.venv\Scripts\session-recall-mcp.exe'
```

Codex stores each session as an append-only JSONL rollout whose schema is its own
(the OpenAI Responses format), so it has a dedicated parser. Notes on the Codex
side:

- Codex `event_msg` lines duplicate the real `response_item` stream and are
  skipped, so content is not indexed twice.
- Codex `reasoning` is encrypted, so it contributes no searchable text, the same
  as Claude Code's redacted thinking.
- Codex flattens MCP tool names to the bare tool name, so this server's own calls
  appear as `search_history`, `recall_status`, and so on. Those are suppressed
  (self-echo), configurable via `SESSION_RECALL_CODEX_ECHO_TOOLS`.
- Codex injects large context blocks (AGENTS.md, environment, tool definitions)
  as `user`-role messages, so a Codex session's `first_user_prompt` may show
  injected context rather than the literal first human prompt. This is indexed
  faithfully rather than filtered by a fragile heuristic.

Every row carries a `source` of `claude` or `codex`. Search blends both by
default; pass `source` (MCP) or `--source` (CLI) to isolate one, and `status`
shows a per-source count. The Codex corpus root is overridable with
`SESSION_RECALL_CODEX_HOME`.

## What gets indexed

Indexed entry types (as of 0.2.1), stored in `messages` under a normalized
`entry_type`:

- `user`: user prompt text.
- `assistant`: assistant reply text.
- `thinking`: extended-thinking text when present (see note 1 below).
- `tool_use`: tool name plus a capped (about 1 KB) rendering of the input.
- `tool_result`: a capped (about 2 KB) text preview, with `is_truncated` set and
  a file-plus-line locator so the full result can be fetched later.
- `system`: system error entries such as `api_error` (see note 2 below).
- `summary`: compaction summary text, if any such entries ever appear (none
  exist in the corpus at present).
- `sidecar`: pointer records for large tool results offloaded under a
  `tool-results` directory. Filename, session, and timestamp only; the content
  is never ingested.

Skipped entry types (metadata and orchestration, no conversational text):
`queue-operation`, `attachment`, `file-history-snapshot`, `ai-title`,
`last-prompt`, `custom-title`, `mode`, `permission-mode`, `started`, `result`,
`bridge-session`, `agent-name`, and any unknown future type. Also excluded from
full text: sidecar content, image blocks, and base64 payloads.

`status` shows the live indexed-by-type counts and the per-type skip counts from
the last index pass, so format drift (a new or unexpected type) is visible.

Two points worth calling out:

1. Recent Claude Code versions (v2.1.x) store thinking blocks with an empty text
   body and a signature only, so thinking is effectively redacted on disk. Those
   entries are recorded for later navigation but carry no searchable text.
2. The build document's index list does not name system entries, but on this
   machine the string `ECONNRESET` (the phase 0.1 acceptance target) appears only
   inside system `api_error` entries. Session_Recall indexes those entries,
   capped, so a search can find its way back to the session where a failure
   happened. This is a deliberate, acceptance-driven choice.

## Storage

- `files`: one row per transcript file and per sidecar (path, project key, size,
  mtime, and the byte offset plus line count consumed so far, which drive
  incremental tailing).
- `sessions`: per-session summary (project, cwd, time bounds, message count,
  first user prompt).
- `messages`: one row per indexed entry, deduped by uuid, with a locator
  (file plus line number) for later on-demand full reads.
- `messages_fts`: an FTS5 table over the extracted text, mapped by rowid to
  `messages`. Entries with no searchable text are recorded in `messages` but
  omitted here.

WAL mode is enabled so a query session and an index pass can overlap safely.
`status` reports the live `journal_mode` so this can be confirmed.

## Concurrency

The supported model is many concurrent readers and a single writer. Under WAL,
readers never block on the indexer and the indexer never blocks readers: a query
sees a consistent snapshot while an index pass writes. This was demonstrated with
4210 status reads issued during a full index write, with zero errors. Writes
(the incremental refresh triggered by the freshness guard, or an explicit
`reindex`) are serialized by SQLite; a connection that finds the write lock held
waits up to a short busy timeout rather than erroring, so two sessions that
happen to refresh at once degrade to a brief wait rather than a failure. Running
two full rebuilds at the same time is out of scope and not supported.

## Sessions and files

Transcript files greatly outnumber sessions because subagent and workflow
transcripts share the parent session's `sessionId`. Every `agent-*.jsonl` and
`journal.jsonl` under a session's `subagents\...` tree carries the same
`sessionId` as the top-level `<session-id>.jsonl`, so they collapse into one
session row. The full corpus of roughly 1700 transcript files collapses to a few
dozen sessions, which is expected, not a grouping bug. `status` shows per-project
transcript-file and session counts so this mapping is visible.

## Tests

The suite uses the standard library `unittest` and builds throwaway temp corpora,
never touching the real archive. Run from the project root:

```text
python -m unittest discover -s tests -t . -v
```

(`python -m pytest tests` also works if pytest is installed.)

## Layout

```text
Session_Recall\
  pyproject.toml     (editable install; console scripts; version from __init__)
  session_recall\
    __init__.py      (__version__ lives here)
    config.py        (self-echo prefixes, freshness, env-overridable)
    parser.py        (Claude Code jsonl line to normalized records)
    parser_codex.py  (Codex rollout line to normalized records)
    store.py         (schema, upserts, FTS queries)
    indexer.py       (discovery and orchestration)
    server.py        (MCP stdio server, six capped tools)
    webui.py         (local web UI server, stdlib http.server)
    cli.py           (index / search / status)
    __main__.py      (python -m session_recall)
  Session_Recall Web UI.bat        (double-click to start the web UI)
  Stop Session_Recall Web UI.bat   (double-click to stop it)
  tests\             (unittest suite over temp corpora)
  data\
    recall.db        (gitignored)
  prompts\           (phase prompts, the project's paper trail)
  NOTES.md           (session-to-session project notes)
  SNIPPET.md         (ready-to-paste CLAUDE.md adoption snippet)
  README.md
```
