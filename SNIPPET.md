# Adoption snippet

Paste the block below into a `CLAUDE.md` (global or per project) so sessions know
the tool exists and how to use it. It is intentionally short: it costs almost
nothing in every context window. Everything under the horizontal rule is the
snippet.

---

## Session_Recall (transcript memory)

The `session-recall` MCP is available in every project. It searches the full
archive of past Claude Code sessions on this machine. After any context
compaction, and before re-deriving a solution, diagnosis, or design decision you
may have reached earlier, search the archive first rather than starting over.

- `search_history` with 2 to 4 distinctive keywords to find where it happened.
  Wrap an exact phrase in double quotes (for example `"comprehensive review"`).
  For a generic term such as a common error name, add a `project` filter (a
  project name or working-directory substring), otherwise unrelated sessions
  crowd out the one you want.
- `get_session_outline` on a promising session to skim it, then `read_messages`
  to pull only the specific messages you need.
- Outline first, then read surgically. Never pull a whole session into context;
  responses are already capped to protect the context window.

Examples of well-formed queries: `ECONNRESET` with `project="PatchManager"`;
`Wintermaul` with `project="Serinety-TD"`; the phrase `"comprehensive review"`
with `project="MAS"`.
