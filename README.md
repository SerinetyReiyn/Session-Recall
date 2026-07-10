# Session Recall

**Searchable long-term memory for [Claude Code](https://claude.com/claude-code) and [Codex](https://openai.com/codex/).**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-working-brightgreen)

When a long agent session auto-compacts, the in-context history is replaced by a
summary and the model starts re-deriving solutions, diagnoses, and decisions it
already worked out earlier. The data is not actually lost: Claude Code and Codex
each write a complete transcript of every session to disk. What is missing is a
way back in.

Session Recall builds a local full-text index over those transcripts and exposes
it as tools, so any session can search everything you have ever done and pull
back the exact piece it needs, instead of starting over.

```text
you (after a compaction):  "what did we conclude about the ECONNRESET failures?"
the agent:                  search_history("ECONNRESET", project="patch-manager")
                            -> finds the session, reads the two messages that mattered,
                               answers in one turn instead of re-investigating
```

## Highlights

- **One index over two tools.** Claude Code transcripts (`~/.claude/projects`) and
  Codex rollouts (`~/.codex/sessions`) are indexed into a single database, so each
  assistant can recall the other's history and its own. Every hit is tagged with
  its `source` (`claude` or `codex`).
- **Built to protect the context window, not flood it.** Every tool response is
  hard-capped. The intended flow is search, then outline, then read only the
  specific messages you need. It never dumps a whole session back into context.
- **Six MCP tools** over stdio: `search_history`, `list_sessions`,
  `get_session_outline`, `read_messages`, `recall_status`, `reindex`.
- **A CLI** for humans and a **local web UI** (double-click to start) over the same
  index.
- **Incremental and self-maintaining.** Files are tailed from a saved byte offset,
  so reruns are near-instant. A freshness guard refreshes the index on demand
  before a search, so there is no daemon and no scheduled task.
- **Local only.** No network egress, no telemetry. See [Privacy](#privacy).

Reading and contributing are separate. Any MCP host can be pointed at the index
to *search* it without adding to it: Claude Desktop, for example, registers the
server in its own `claude_desktop_config.json` and can then recall the shared
Claude Code and Codex history. The contributing corpora (the transcripts that get
indexed) are Claude Code and Codex, since both write clean local transcript files;
Claude Desktop's conversations live in the cloud and are not part of the corpus.

Backend: Python standard library `sqlite3` with FTS5. The only third-party
dependency is the official MCP SDK, and only the MCP server needs it.

## Quick start

Requires Python 3.11+ with FTS5 (standard on official CPython builds).

```powershell
git clone https://github.com/SerinetyReiyn/Session-Recall.git
cd Session-Recall
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# Build the index from your local transcript archives:
session-recall index

# See what it found:
session-recall status
```

`session-recall index` reads `~/.claude/projects` and `~/.codex/sessions` and
writes an index to `data/recall.db` inside the repo. That database stays local
and is git-ignored.

## Using it

### From an agent (MCP)

Register the server once, at user scope, so every project can use it.

Claude Code:

```powershell
claude mcp add -s user session-recall -- <repo>\.venv\Scripts\session-recall-mcp.exe
```

Codex (add to `~/.codex/config.toml`):

```toml
[mcp_servers.session-recall]
command = '<repo>\.venv\Scripts\session-recall-mcp.exe'
```

The agent then calls the tools itself. To make it reach for them automatically
after a compaction, paste [`SNIPPET.md`](SNIPPET.md) into a `CLAUDE.md` (or
`AGENTS.md` for Codex).

### From the terminal (CLI)

```text
session-recall search "2 to 4 distinctive keywords" [--project X] [--source claude|codex]
session-recall index [--full]
session-recall status
```

Two to four distinctive keywords work best. Wrap an exact phrase in double
quotes. Scope common terms with `--project`. `--source` limits results to one
tool.

### From a browser (web UI)

Double-click **`Session_Recall Web UI.bat`** to start a local page (127.0.0.1
only) with search, session browsing, outlines, and a full-text reader. Double-click
**`Stop Session_Recall Web UI.bat`** to stop it. From an activated venv you can
also run `session-recall-web`.

## How it works

```text
transcripts on disk  ->  parser  ->  store (SQLite + FTS5)  ->  server / cli / web ui
  ~/.claude, ~/.codex     normalize    incremental index,        capped tools and
  (read only)             each format   uuid-deduped, WAL         a browser view
```

Two parsers normalize each tool's on-disk format into one record shape; the store
holds them in an FTS5 index keyed for surgical retrieval; the indexer tails files
incrementally and dedupes by uuid; and the server, CLI, and web UI are three views
over the same store.

## Configuration

All optional. Set as environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SESSION_RECALL_ROOT` | `~/.claude/projects` | Claude Code transcript root |
| `SESSION_RECALL_CODEX_HOME` | `~/.codex` | Codex home (indexes `sessions/` and `archived_sessions/`) |
| `SESSION_RECALL_DB` | `data/recall.db` in the repo | Index database path |
| `SESSION_RECALL_FRESHNESS_SECONDS` | `900` | Refresh the index before a search if it is older than this |
| `SESSION_RECALL_WEB_PORT` | `8765` | Web UI port |
| `SESSION_RECALL_ECHO_PREFIXES` | `mcp__session-recall` | Tool-name prefixes to exclude (self-echo) |
| `SESSION_RECALL_CODEX_ECHO_TOOLS` | the tool names | Bare tool names to exclude in Codex rollouts |

The last two matter because the index must not re-index its own search output:
otherwise past results would dominate future searches. Claude Code namespaces the
tools (`mcp__session-recall__*`) while Codex flattens them to bare names, so each
corpus is filtered accordingly.

## Privacy

The index is built and stored entirely on your machine. Transcripts can contain
anything that ever passed through a tool, including secrets, so the index inherits
that sensitivity. `data/recall.db` is git-ignored and never leaves your computer.
The tool makes no network requests and sends no telemetry. It only reads the
transcript archives; it never writes to, renames, or touches them.

## Platform

Built and tested on Windows 11 with Python 3.12. The archive roots and database
path are all configurable, so it should adapt to other layouts, but the web UI
launchers are Windows `.bat` files and it has not been exercised on macOS or Linux.

## Development

Tests use the standard-library `unittest` and build throwaway temp corpora; they
never touch your real archive.

```text
python -m unittest discover -s tests -t . -v
```

## Notes

This started as a personal tool and is shared in case it is useful to someone
else. It is not affiliated with Anthropic or OpenAI. Licensed under the MIT
License; see [LICENSE](LICENSE).
