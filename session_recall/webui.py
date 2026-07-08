"""Local web UI for Session_Recall (phase 0.5).

A small single-page browser front end over the same index the CLI and MCP server
use. It serves a search box, a session browser, and a message reader, so the
archive is usable without a terminal.

Design constraints:
- Standard library only, no new dependencies.
- Bound to 127.0.0.1 exclusively. Transcripts can contain secrets that passed
  through tools, so the index is never exposed on the network.
- For a human reading in a browser (not a model's context window) the hard caps
  that protect the MCP tools do not apply: a message is shown in full.
- Search, session listing, and outline run the freshness guard first, so the
  page reflects a current index just like the MCP read tools do.

Run with:  python -m session_recall.webui   (or the Session_Recall Web UI.bat launcher)
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import DEFAULT_PER_SESSION_CAP
from .indexer import maybe_refresh
from .parser import read_full_text
from .store import Store

HOST = "127.0.0.1"
PORT = int(os.environ.get("SESSION_RECALL_WEB_PORT", "8765"))
DB_PATH = os.environ.get(
    "SESSION_RECALL_DB",
    str(Path(__file__).resolve().parent.parent / "data" / "recall.db"),
)
READ_MAX_CHARS = 200000


# -- data functions (pure of HTTP; unit tested directly) -----------------

def index_status(db_path=None):
    store = Store(db_path or DB_PATH)
    try:
        return store.status()
    finally:
        store.close()


def search_index(q, project=None, role=None, limit=25, cap=DEFAULT_PER_SESSION_CAP,
                 source=None, db_path=None):
    store = Store(db_path or DB_PATH)
    try:
        rows = store.search(q, project=project or None, role=role or None,
                            limit=limit, per_session_cap=cap, source=source or None)
        return [{
            "snippet": r["snippet"],
            "session_id": r["session_id"],
            "uuid": r["uuid"],
            "ts": r["ts"],
            "project": r["cwd"] or r["project_key"],
            "source": r["source"],
            "role": r["role"],
            "kind": r["entry_type"],
            "tool_name": r["tool_name"],
        } for r in rows]
    finally:
        store.close()


def sessions_list(project=None, limit=100, db_path=None):
    store = Store(db_path or DB_PATH)
    try:
        rows = store.list_sessions(project=project or None, limit=limit)
        return [{
            "session_id": r["session_id"],
            "project": r["cwd"] or r["project_key"],
            "started": r["started_ts"],
            "last": r["last_ts"],
            "message_count": r["message_count"],
            "first_user_prompt": r["first_user_prompt"],
        } for r in rows]
    finally:
        store.close()


def session_outline(session_id, max_items=300, db_path=None):
    store = Store(db_path or DB_PATH)
    try:
        rows = store.get_session_outline(session_id, max_items=max_items)
        return [{
            "uuid": r["uuid"],
            "role": r["role"],
            "kind": r["entry_type"],
            "ts": r["ts"],
            "tool_name": r["tool_name"],
            "head": r["head"],
        } for r in rows]
    finally:
        store.close()


def read_message(uuid, db_path=None, max_chars=READ_MAX_CHARS):
    store = Store(db_path or DB_PATH)
    try:
        targets = store.resolve_read_targets(None, uuids=[uuid], count=1)
        if not targets:
            return None
        t = targets[0]
        text = store.stored_text(t["id"])
        if t["is_truncated"] and t["path"]:
            full = read_full_text(t["path"], t["line_no"])
            if full:
                text = full
    finally:
        store.close()
    truncated = len(text) > max_chars
    return {
        "uuid": t["uuid"],
        "role": t["role"],
        "kind": t["entry_type"],
        "ts": t["ts"],
        "tool_name": t["tool_name"],
        "text": text[:max_chars],
        "truncated": truncated,
    }


def _refresh():
    """Best-effort freshness guard; never let a refresh failure break a request."""
    try:
        maybe_refresh(DB_PATH, verbose=False)
    except Exception:
        pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session_Recall</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
         background: #14161a; color: #d9dde3; }
  header { padding: 12px 18px; background: #1b1e24; border-bottom: 1px solid #2a2f38;
           display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .ver { color: #7f8794; font-size: 12px; }
  header .status { margin-left: auto; color: #7f8794; font-size: 12px; }
  .controls { display: flex; gap: 8px; padding: 12px 18px; flex-wrap: wrap;
              background: #171a1f; border-bottom: 1px solid #2a2f38; align-items: center; }
  input, select, button { font: inherit; background: #23272f; color: #d9dde3;
              border: 1px solid #363c47; border-radius: 6px; padding: 7px 10px; }
  input:focus, select:focus { outline: none; border-color: #4c8bf5; }
  input#q { flex: 1 1 320px; min-width: 200px; }
  input#project { flex: 0 1 200px; }
  button { cursor: pointer; background: #2d5bd0; border-color: #2d5bd0; color: #fff; }
  button.ghost { background: #23272f; border-color: #363c47; color: #b9c0cc; }
  button:hover { filter: brightness(1.1); }
  main { display: flex; height: calc(100vh - 108px); }
  #list { width: 42%; min-width: 300px; overflow-y: auto; border-right: 1px solid #2a2f38; }
  #detail { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .item { padding: 10px 16px; border-bottom: 1px solid #21252c; cursor: pointer; }
  .item:hover { background: #1c2129; }
  .item.sel { background: #202838; }
  .item .meta { color: #8c94a1; font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
  .item .snip { margin-top: 3px; color: #cdd3db; }
  .item .snip mark { background: #3a4d2a; color: #e6f2d8; padding: 0 1px; border-radius: 2px; }
  .badge { display: inline-block; padding: 0 6px; border-radius: 10px; font-size: 11px;
           background: #2a3542; color: #9fb4cc; }
  .badge.src-claude { background: #2d3b52; color: #8fb0e0; }
  .badge.src-codex { background: #143a30; color: #6fd0a8; }
  .hint { color: #7f8794; padding: 16px 20px; font-size: 13px; }
  #detail .dmeta { color: #8c94a1; font-size: 12px; margin-bottom: 10px; display: flex;
           gap: 10px; flex-wrap: wrap; align-items: center; }
  #detail pre { white-space: pre-wrap; word-break: break-word; background: #1a1d23;
           border: 1px solid #2a2f38; border-radius: 8px; padding: 14px; margin: 0; }
  a.link { color: #6fa8ff; cursor: pointer; text-decoration: none; }
  a.link:hover { text-decoration: underline; }
  .empty { color: #7f8794; padding: 24px; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>Session_Recall</h1><span class="ver" id="ver"></span>
  <span class="status" id="status">loading...</span>
</header>
<div class="controls">
  <input id="q" placeholder="search: 2 to 4 distinctive keywords, or a &quot;quoted phrase&quot;" autofocus>
  <input id="project" placeholder="project filter (optional)">
  <select id="role">
    <option value="">any role</option>
    <option value="user">user</option>
    <option value="assistant">assistant</option>
    <option value="system">system</option>
  </select>
  <select id="source">
    <option value="">both tools</option>
    <option value="claude">claude</option>
    <option value="codex">codex</option>
  </select>
  <button id="go">Search</button>
  <button id="browse" class="ghost">Browse sessions</button>
</div>
<main>
  <div id="list"><div class="hint">Search the archive, or browse sessions. Scope common terms with a project filter; quote exact phrases.</div></div>
  <div id="detail"><div class="empty">Select a result to read its full text.</div></div>
</main>
<script>
const $ = s => document.querySelector(s);
const listEl = $('#list'), detailEl = $('#detail');
let selected = null;

function esc(t){ const d=document.createElement('div'); d.textContent=t==null?'':t;
  return d.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function snippetHtml(s){ return esc(s).replace(/\[(.*?)\]/g, '<mark>$1</mark>'); }

async function api(path){ const r = await fetch(path); if(!r.ok) throw new Error(await r.text()); return r.json(); }

async function loadStatus(){
  try {
    const s = await api('/api/status');
    $('#ver').textContent = 'v' + s.version_running;
    $('#status').textContent = s.messages.toLocaleString() + ' messages / ' + s.sessions +
      ' sessions / last index ' + (s.last_index_time || 'never').replace('T',' ').slice(0,19);
  } catch(e){ $('#status').textContent = 'status unavailable'; }
}

function setList(html){ listEl.innerHTML = html; }
function clearDetail(){ detailEl.innerHTML = '<div class="empty">Select a result to read its full text.</div>'; }

async function doSearch(){
  const q = $('#q').value.trim();
  if(!q){ setList('<div class="hint">Type a query above, then press Enter or Search.</div>'); return; }
  setList('<div class="hint">searching...</div>');
  const params = new URLSearchParams({ q, project: $('#project').value.trim(), role: $('#role').value, source: $('#source').value });
  try {
    const hits = await api('/api/search?' + params);
    if(!hits.length){ setList('<div class="empty">No hits. Try fewer or more distinctive keywords.</div>'); return; }
    setList(hits.map((h,i)=>`
      <div class="item" data-uuid="${esc(h.uuid)}" data-session="${esc(h.session_id)}" data-i="${i}">
        <div class="meta"><span class="badge src-${esc(h.source||'')}">${esc(h.source||'')}</span><span class="badge">${esc(h.role)}/${esc(h.kind)}</span>
          <span>${esc((h.ts||'').replace('T',' ').slice(0,19))}</span>
          <span>${esc(h.project||'')}</span></div>
        <div class="snip">${snippetHtml(h.snippet)}</div>
      </div>`).join(''));
    bindItems();
  } catch(e){ setList('<div class="empty">Search error: '+esc(e.message)+'</div>'); }
}

async function browseSessions(){
  setList('<div class="hint">loading sessions...</div>');
  const params = new URLSearchParams({ project: $('#project').value.trim() });
  try {
    const rows = await api('/api/sessions?' + params);
    if(!rows.length){ setList('<div class="empty">No sessions.</div>'); return; }
    setList(rows.map(s=>`
      <div class="item sess" data-session="${esc(s.session_id)}">
        <div class="meta"><span class="badge">${s.message_count} msgs</span>
          <span>${esc((s.last||'').replace('T',' ').slice(0,19))}</span>
          <span>${esc(s.project||'')}</span></div>
        <div class="snip">${esc(s.first_user_prompt||'(no first prompt)')}</div>
      </div>`).join(''));
    listEl.querySelectorAll('.sess').forEach(el =>
      el.onclick = () => openOutline(el.dataset.session));
  } catch(e){ setList('<div class="empty">Error: '+esc(e.message)+'</div>'); }
}

async function openOutline(sessionId){
  setList('<div class="hint">loading outline...</div>');
  try {
    const items = await api('/api/outline?session_id=' + encodeURIComponent(sessionId));
    const head = `<div class="hint">Outline of ${esc(sessionId).slice(0,8)} &middot;
      <a class="link" id="back">back to sessions</a></div>`;
    setList(head + items.map(it=>`
      <div class="item" data-uuid="${esc(it.uuid)}" data-session="${esc(sessionId)}">
        <div class="meta"><span class="badge">${esc(it.role)}/${esc(it.kind)}</span>
          <span>${esc((it.ts||'').replace('T',' ').slice(11,19))}</span>
          ${it.tool_name?'<span>'+esc(it.tool_name)+'</span>':''}</div>
        <div class="snip">${esc(it.head||'')}</div>
      </div>`).join(''));
    $('#back').onclick = browseSessions;
    bindItems();
  } catch(e){ setList('<div class="empty">Error: '+esc(e.message)+'</div>'); }
}

function bindItems(){
  listEl.querySelectorAll('.item[data-uuid]').forEach(el =>
    el.onclick = () => { selectItem(el); readMessage(el.dataset.uuid, el.dataset.session); });
}
function selectItem(el){
  if(selected) selected.classList.remove('sel');
  selected = el; el.classList.add('sel');
}

async function readMessage(uuid, sessionId){
  detailEl.innerHTML = '<div class="empty">loading...</div>';
  try {
    const m = await api('/api/read?uuid=' + encodeURIComponent(uuid));
    if(!m){ detailEl.innerHTML = '<div class="empty">Message not found.</div>'; return; }
    detailEl.innerHTML = `
      <div class="dmeta"><span class="badge">${esc(m.role)}/${esc(m.kind)}</span>
        <span>${esc((m.ts||'').replace('T',' ').slice(0,19))}</span>
        ${m.tool_name?'<span>tool: '+esc(m.tool_name)+'</span>':''}
        <a class="link" id="outline">session outline</a>
        ${m.truncated?'<span>(truncated for display)</span>':''}</div>
      <pre id="body"></pre>`;
    $('#body').textContent = m.text;
    $('#outline').onclick = () => openOutline(sessionId);
  } catch(e){ detailEl.innerHTML = '<div class="empty">Error: '+esc(e.message)+'</div>'; }
}

$('#go').onclick = doSearch;
$('#browse').onclick = browseSessions;
$('#q').addEventListener('keydown', e => { if(e.key === 'Enter') doSearch(); });
$('#project').addEventListener('keydown', e => { if(e.key === 'Enter') doSearch(); });
loadStatus();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console quiet; errors still surface

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def one(name, default=""):
            return qs.get(name, [default])[0]

        try:
            if path == "/" or path == "/index.html":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                s = index_status()
                s["version_running"] = __version__
                self._json(s)
            elif path == "/api/search":
                _refresh()
                self._json(search_index(
                    one("q"), project=one("project"), role=one("role"),
                    source=one("source"), limit=int(one("limit", "25") or 25)))
            elif path == "/api/sessions":
                _refresh()
                self._json(sessions_list(project=one("project")))
            elif path == "/api/outline":
                _refresh()
                self._json(session_outline(one("session_id"),
                                           max_items=int(one("max_items", "300") or 300)))
            elif path == "/api/read":
                self._json(read_message(one("uuid")) or {})
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as exc:  # never crash the server on a bad request
            self._json({"error": str(exc)}, code=500)


class _Server(ThreadingHTTPServer):
    # Disable SO_REUSEADDR. On Windows it would let a second launch bind the same
    # already-listening port instead of failing, so the "already running, just
    # reopen the tab" guard in main() would never fire and every extra
    # double-click would leak another competing server on the same port.
    allow_reuse_address = False


def main():
    url = f"http://{HOST}:{PORT}/"
    try:
        server = _Server((HOST, PORT), Handler)
    except OSError:
        # Port already in use: assume a server is already running and just open it.
        print(f"Session_Recall Web UI already running at {url}")
        webbrowser.open(url)
        return
    # Prime the index once so the first search is instant.
    _refresh()
    print(f"Session_Recall Web UI {__version__} serving at {url}")
    print("Leave this window open. Close it or press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Session_Recall Web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
