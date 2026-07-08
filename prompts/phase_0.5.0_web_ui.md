# Session_Recall Phase 0.5.0: local web UI

Requested directly by Serinety (not via a Claudia-authored prompt): build a web
UI so the tool is usable without a terminal, with a launcher kept in the project
folder that starts and stops by double-click. This file is the paper-trail
record of that request, per convention 0.7.

The web UI was on the deferred list at the end of phase 0.4; scheduling it makes
it a new minor version (0.5.0) with its own record, as the build doc says.
`__version__` becomes "0.5.0". Section 0 conventions in force: no em-dashes, no
time estimates.

## Scope

1. A single-page local web UI over the existing index: search box, project
   filter, session browser, and a full message reader (outline then read).
   Standard library only (`http.server`), no new dependencies.
2. Bound to `127.0.0.1` exclusively. Transcripts can contain secrets, so the
   index is never exposed on the network.
3. Reuse the store and the freshness guard. Search, session list, and outline
   run an incremental refresh first, so the page is current. Unlike the MCP
   tools, the reader shows a message in full (context-window caps are for a
   model, not a human at a browser).
4. Double-click launchers in the project root: one to start (opens the browser),
   one to stop. Starting again while already running just reopens the tab.
5. Unit test the data functions; smoke test the HTTP server end to end.
6. README section; version bump; console script `session-recall-web`.

## Acceptance

- Double-clicking the launcher serves the page at `http://127.0.0.1:8765/` and
  opens a browser; the stop launcher (or closing the window) stops it.
- The page searches, lists and browses sessions, shows an outline, and reads a
  message in full over HTTP.
- All tests pass; `__version__` reads 0.5.0; completion report em-dash free.

## Note recorded during this phase

Fixing the packaging surfaced a pre-existing breakage: a prior editable reinstall
had failed and rolled back because the running MCP server process held
`session-recall-mcp.exe` open (Windows file lock), leaving the package
uninstalled. Console scripts then resolved only when the current directory was
the project root, and a fresh MCP spawn would have failed. Resolved by stopping
the leaked server processes (about seven had accumulated, all the stale 0.3.0
build), removing the invalid-distribution leftover, and reinstalling. The MCP
server now spawns cleanly and reports 0.5.0.

## Still deferred

Watcher daemon, semantic embeddings, Desktop-corpus indexing, SessionStart and
PreCompact hooks, backup task, cross-machine sync. Each becomes its own version
with its own prompt if scheduled.
