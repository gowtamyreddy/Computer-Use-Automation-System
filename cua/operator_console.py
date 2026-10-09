"""Minimal operator console (deliberately bare; see REPORT.md section 5 for the production design).

Serves on http://127.0.0.1:<port>:
    GET  /                 HTML page: pending intervention, masked screenshot, live human-action feed
    GET  /api/state        JSON lease + intervention state
    POST /api/claim        {"operator": "alice"}                         -> lease to human
    POST /api/release      {"operator": "alice", "resolution": "...", "note": "..."} -> lease to automation
    GET  /screens/<file>   masked screenshots from the run directory

The human operates the *same* live browser the automation was driving: the headed Chromium window
(--headed), or remotely via the Chrome DevTools endpoint (--debug-port) for a headless run.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .control import ControlChannel, ControlError

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Operator Console</title>
<style>body{font:14px system-ui;margin:20px;max-width:1100px}code{background:#eee;padding:1px 4px}
.card{border:1px solid #ccc;border-radius:6px;padding:12px;margin:10px 0}.k{color:#666}
button{margin:4px;padding:6px 12px}img{max-width:100%;border:1px solid #999}
.owner{font-weight:bold;padding:2px 8px;border-radius:4px}.automation{background:#def}.human{background:#fd9}</style></head>
<body><h2>Operator Console</h2><div id="root">loading...</div>
<script>
let me = localStorage.getItem('op') || 'operator1';
async function post(path, body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json(); if(!r.ok) alert(j.error); refresh();}
function claim(){post('/api/claim',{operator:me});}
function rel(res){post('/api/release',{operator:me,resolution:res,note:document.getElementById('note').value});}
async function refresh(){
 const s=await (await fetch('/api/state')).json(); const iv=s.intervention; let h=`<p>Control: <span class="owner ${s.owner}">${s.owner}</span> ${HINT}</p>`;
 if(!iv){h+='<p>No open intervention.</p>';}
 else{
  h+=`<div class="card"><b>${iv.kind.toUpperCase()}</b> &middot; <code>${iv.code}</code> &middot; ${iv.status}<br>
  <span class="k">subject</span> ${iv.subject}<br><span class="k">step</span> ${iv.step_id||'-'} ${iv.step_intent||''}<br>
  <span class="k">reason</span> ${iv.reason}<br><span class="k">frames</span> <code>${JSON.stringify(iv.frames)}</code></div>`;
  if(iv.status==='pending'){h+=`<button onclick="claim()">Take control of live session</button>`;
    for(const r of iv.allowed_resolutions.filter(r=>['approve','deny','abort'].includes(r))) h+=`<button onclick="rel('${r}')">${r}</button>`;}
  if(iv.status==='claimed'){h+=`<p>You hold the session. Operate the browser window, then hand back:</p>`;
    for(const r of iv.allowed_resolutions) h+=`<button onclick="rel('${r}')">${r}</button>`;}
  h+=`<br><input id="note" size="80" placeholder="note for the record">`;
  if(iv.human_actions.length){h+='<div class="card"><b>Captured human actions</b><ol>'+iv.human_actions.map(a=>`<li>${a.type} ${a.element.role} "${a.element.name||a.element.label||a.element.text||''}" ${a.element.value!==undefined?'value='+a.element.value:''} <span class="k">${a.url}</span></li>`).join('')+'</ol></div>';}
  if(iv.screenshot) h+=`<p class="k">Screenshot at escalation (sensitive values masked):</p><img src="/${iv.screenshot}">`;
 }
 const note=document.getElementById('note'); const keep=note?note.value:'';
 document.getElementById('root').innerHTML=h; const n2=document.getElementById('note'); if(n2) n2.value=keep;
}
refresh(); setInterval(()=>{ if(document.activeElement.id!=='note') refresh(); },1500);
</script></body></html>"""


class OperatorConsole:
    def __init__(self, channel: ControlChannel, port: int = 8780, debug_port: int | None = None):
        self.channel = channel
        self.port = port
        hint = (
            f"&middot; remote takeover: open <code>http://127.0.0.1:{debug_port}</code> (Chrome DevTools)"
            if debug_port
            else "&middot; operate the headed Chromium window"
        )
        page = PAGE.replace("${HINT}", hint)
        console = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                data = json.dumps(obj, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/":
                    data = page.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/state":
                    self._json(console.channel.snapshot())
                elif self.path.startswith("/screens/") and ".." not in self.path:
                    p = console.channel.run.dir / self.path.lstrip("/")
                    if p.exists():
                        data = p.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self._json({"error": "not found"}, 404)
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    if self.path == "/api/claim":
                        iv = console.channel.claim(body.get("operator", "operator"))
                    elif self.path == "/api/release":
                        iv = console.channel.release(body["resolution"], body.get("operator", "operator"), body.get("note", ""))
                    else:
                        return self._json({"error": "not found"}, 404)
                    self._json(iv.model_dump(mode="json"))
                except (ControlError, KeyError, ValueError) as e:
                    self._json({"error": str(e)}, 409)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "OperatorConsole":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"
