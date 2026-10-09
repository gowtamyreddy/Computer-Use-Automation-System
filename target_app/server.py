"""CoreLink Teller 4.2 -- a deliberately *hostile* stand-in for a legacy core-banking UI.

Everything about it is chosen to resemble the long tail of bank back-office apps:

* server-rendered HTML 4 with a <frameset> (header / nav / work frames)
* table-based layouts, labels in neighbouring <td>s (no <label for>), no ARIA, no test IDs
* opaque field names (F0231, B01), session id carried in the URL query string (?sid=...)
* business errors rendered as red text with vendor codes (E201 NO RECORD FOUND ...)
* native JS confirm() dialogs, interstitial notices, session expiry, transient "busy" pages

Runtime faults can be injected through an out-of-band admin endpoint (POST /__faults) that is
*not* linked from the UI and *not* on the agent's allowlist -- only the test/demo harness uses it.

Two "tenants" of the same vendor product can be served via --variant (base | lakeside): same
internals and field names, different branding and labels -- a stand-in for multi-tenant config drift.

All data is fictional. Run:  python -m target_app.server --port 8765 [--variant lakeside]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# --------------------------------------------------------------------------------------------
# Fictional data
# --------------------------------------------------------------------------------------------

MEMBERS: dict[str, dict] = {
    "12345": {
        "name": "SAMPLE, JANE Q",
        "ssn": "123-45-6789",
        "dob": "03/12/1984",
        "phone": "(555) 010-2233",
        "accounts": [("0000", "Share Savings", "4,210.55"), ("0010", "Share Draft Checking", "1,032.10")],
    },
    "23456": {
        "name": "TESTCASE, ROBERT",
        "ssn": "987-65-4321",
        "dob": "11/02/1971",
        "phone": "(555) 010-9876",
        "accounts": [
            ("0000", "Share Savings", "15.00"),
            ("0010", "Share Draft Checking", "220.00"),
            ("0020", "Money Market", "10,000.00"),
        ],
    },
    "55555": {"restricted": True},
    "77777": {
        "name": "HOLDER, PAT",
        "ssn": "555-12-3434",
        "dob": "07/04/1990",
        "phone": "(555) 010-4455",
        "hold": True,
        "accounts": [("0000", "Share Savings", "300.00")],
    },
}

USERS = {"teller1": os.environ.get("CORELINK_DEMO_PASSWORD", "demo-only-not-a-secret")}

VARIANTS = {
    "base": {
        "brand": "CoreLink Teller 4.2 &mdash; Demo Federal Credit Union",
        "color": "#003366",
        "mbr_label": "Member #",
        "go_label": "Go",
        "inq_link": "Member Inquiry",
        "inq_title": "MEMBER INQUIRY",
    },
    # Same vendor product, another tenant: re-branded, relabelled, same field names / flow.
    "lakeside": {
        "brand": "CoreLink Teller 4.3 &mdash; Lakeside Community CU",
        "color": "#1d5c2e",
        "mbr_label": "Member No.",
        "go_label": "Search",
        "inq_link": "Member Lookup",
        "inq_title": "MEMBER LOOKUP",
    },
}


@dataclass
class Faults:
    notice_once: bool = False  # interstitial "SYSTEM NOTICE" before the next work-frame page
    busy_once: bool = False  # next inquiry submit returns "E503 SYSTEM BUSY"
    error_once: bool = False  # next inquiry submit returns an HTTP 500 vendor crash page
    slow_ms: int = 0  # latency added to every work-frame response
    expire_after: int = 0  # expire the session after N more authenticated requests (0 = off)


@dataclass
class State:
    variant: str = "base"
    faults: Faults = field(default_factory=Faults)
    sessions: dict[str, dict] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_suffix: int = 30


STATE = State()

# --------------------------------------------------------------------------------------------
# HTML helpers (intentionally 2003-era markup)
# --------------------------------------------------------------------------------------------

CSS = """
body{font-family:Verdana,Arial,sans-serif;font-size:11px;margin:4px;background:#f4f4ee}
td{font-size:11px} .lbl{font-weight:bold;text-align:right;padding-right:6px;color:#333}
.hdrcell{background:%s;color:#fff;font-weight:bold;padding:3px}
.err{color:#c00;font-weight:bold} .grid td{border-bottom:1px solid #ccc;padding:2px 8px}
.btn{font-size:11px;border:2px outset #ccc;background:#ddd}
"""


def v() -> dict:
    return VARIANTS[STATE.variant]


def page(body: str, title: str = "CoreLink", onload: str = "") -> str:
    onload_attr = f' onload="{html.escape(onload)}"' if onload else ""
    return (
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">\n'
        f"<html><head><title>{title}</title><style>{CSS % v()['color']}</style></head>"
        f"<body{onload_attr}>{body}</body></html>"
    )


def boxed(title: str, inner: str) -> str:
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td>'
        f'<table width="100%" cellpadding="2" cellspacing="0"><tr><td class="hdrcell">{title}</td></tr>'
        f'<tr><td><table cellpadding="3" cellspacing="0" border="0">{inner}</table></td></tr></table>'
        "</td></tr></table>"
    )


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# --------------------------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "CoreLinkHTTPD/4.2"

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("CORELINK_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- plumbing -------------------------------------------------------------------------
    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=iso-8859-1"):
        data = body.encode("latin-1", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _params(self) -> dict[str, str]:
        q = {k: vals[0] for k, vals in parse_qs(urlparse(self.path).query).items()}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("latin-1") if n else ""
            if self.headers.get("Content-Type", "").startswith("application/json"):
                q.update(json.loads(raw or "{}"))
            else:
                q.update({k: vals[0] for k, vals in parse_qs(raw, keep_blank_values=True).items()})
        return q

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        path = urlparse(self.path).path
        p = self._params()
        if path == "/__faults":
            return self._faults(p)
        if path in ("/", ""):
            return self._redirect("/login")
        if path == "/login":
            return self._login(p)
        public = {"/login"}
        if path not in public:
            sess = self._session(p.get("sid", ""))
            if sess is None:
                return self._send(self._expired_page(), status=200)
        routes = {
            "/main": self._main,
            "/hdr": self._hdr,
            "/nav": self._nav,
            "/welcome": self._welcome,
            "/notice": self._notice,
            "/mbrinq": self._mbrinq,
            "/mbrdtl": self._mbrdtl,
            "/subacct": self._subacct,
            "/subacct/review": self._subacct_review,
            "/subacct/commit": self._subacct_commit,
            "/signoff": self._signoff,
        }
        fn = routes.get(path)
        if fn is None:
            return self._send(page("<h3>404 - FUNCTION NOT AVAILABLE</h3>"), status=404)
        if path not in ("/main", "/hdr", "/nav") and STATE.faults.slow_ms:
            time.sleep(STATE.faults.slow_ms / 1000)
        fn(p)

    # ---- session ------------------------------------------------------------------------
    def _session(self, sid: str) -> dict | None:
        with STATE.lock:
            sess = STATE.sessions.get(sid)
            if sess is None:
                return None
            if STATE.faults.expire_after:
                STATE.faults.expire_after -= 1
                if STATE.faults.expire_after == 0:
                    STATE.sessions.pop(sid, None)
                    return None
            return sess

    def _expired_page(self) -> str:
        return page(
            '<br><table align="center"><tr><td class="err">E401 SESSION EXPIRED - PLEASE SIGN ON AGAIN</td></tr>'
            '<tr><td align="center"><a href="/login" target="_top">Return to Sign On</a></td></tr></table>',
            title="Session Expired",
        )

    # ---- out-of-band fault injection (demo harness only) ------------------------------
    def _faults(self, p: dict):
        if self.command == "POST":
            with STATE.lock:
                if p.get("reset"):
                    STATE.faults = Faults()
                for k in ("notice_once", "busy_once", "error_once"):
                    if k in p:
                        setattr(STATE.faults, k, bool(p[k]))
                for k in ("slow_ms", "expire_after"):
                    if k in p:
                        setattr(STATE.faults, k, int(p[k]))
        self._send(json.dumps(STATE.faults.__dict__), ctype="application/json")

    # ---- pages ----------------------------------------------------------------------------
    def _login(self, p: dict):
        err = ""
        if self.command == "POST":
            user, pwd = p.get("usr", ""), p.get("pwd", "")
            if USERS.get(user) == pwd:
                sid = secrets.token_hex(12)
                with STATE.lock:
                    STATE.sessions[sid] = {"user": user}
                return self._redirect(f"/main?sid={sid}")
            err = '<tr><td colspan="2" class="err">E100 INVALID USER ID OR PASSWORD</td></tr>'
        body = (
            f'<br><br><center><b style="color:{v()["color"]}">{v()["brand"]}</b><br><br>'
            '<form method="post" action="/login">'
            + boxed(
                "SIGN ON",
                err
                + '<tr><td class="lbl">User ID</td><td><input type="text" name="usr" size="12"></td></tr>'
                '<tr><td class="lbl">Password</td><td><input type="password" name="pwd" size="12"></td></tr>'
                '<tr><td></td><td><input type="submit" class="btn" name="B00" value="Sign On"></td></tr>',
            )
            + "</form></center>"
        )
        self._send(page(body, title="CoreLink Sign On"))

    def _main(self, p: dict):
        sid = esc(p["sid"])
        first = f"/notice?sid={sid}&next=welcome" if STATE.faults.notice_once else f"/welcome?sid={sid}"
        self._send(
            '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN">'
            "<html><head><title>CoreLink Teller</title></head>"
            '<frameset rows="38,*" border="1">'
            f'<frame name="hdr" src="/hdr?sid={sid}" scrolling="no">'
            '<frameset cols="170,*">'
            f'<frame name="nav" src="/nav?sid={sid}">'
            f'<frame name="work" src="{first}">'
            "</frameset></frameset></html>"
        )

    def _hdr(self, p: dict):
        sid = esc(p["sid"])
        self._send(
            page(
                f'<table width="100%"><tr><td class="hdrcell">{v()["brand"]}</td>'
                f'<td class="hdrcell" align="right"><a style="color:#fff" href="/signoff?sid={sid}" target="_top">'
                "Sign Off</a></td></tr></table>"
            )
        )

    def _nav(self, p: dict):
        sid = esc(p["sid"])
        links = [
            (f"/mbrinq?sid={sid}", v()["inq_link"]),
            (f"/welcome?sid={sid}&fn=drawer", "Teller Drawer"),
            (f"/welcome?sid={sid}&fn=gl", "GL Inquiry"),
            (f"/admin/users?sid={sid}", "System Admin"),
        ]
        rows = "".join(
            f'<tr><td>&raquo; <a href="{href}" target="work">{text}</a></td></tr>' for href, text in links
        )
        self._send(page(f'<table cellpadding="4">{rows}</table>'))

    def _welcome(self, p: dict):
        self._send(page(boxed("WELCOME", "<tr><td>Select a function from the menu.</td></tr>")))

    def _notice(self, p: dict):
        with STATE.lock:
            STATE.faults.notice_once = False
        sid = esc(p["sid"])
        nxt = p.get("next", "welcome")
        self._send(
            page(
                boxed(
                    "SYSTEM NOTICE",
                    "<tr><td>Scheduled maintenance tonight 02:00-04:00 ET. Postings will be delayed.</td></tr>"
                    f'<tr><td><form method="get" action="/{esc(nxt)}"><input type="hidden" name="sid" value="{sid}">'
                    '<input type="submit" class="btn" name="B99" value="Acknowledge"></form></td></tr>',
                )
            )
        )

    def _inq_form(self, sid: str, msg: str = "", value: str = "") -> str:
        err = f'<tr><td colspan="2" class="err">{msg}</td></tr>' if msg else ""
        return page(
            '<form method="post" action="/mbrinq">'
            f'<input type="hidden" name="sid" value="{esc(sid)}">'
            + boxed(
                v()["inq_title"],
                err
                + f'<tr><td class="lbl">{v()["mbr_label"]}</td>'
                f'<td><input type="text" name="F0231" size="8" maxlength="10" value="{esc(value)}"></td>'
                '<td class="lbl">Suffix</td><td><input type="text" name="F0232" size="4" value="0000"></td></tr>'
                f'<tr><td></td><td colspan="3"><input type="submit" class="btn" name="B01" value="{v()["go_label"]}">'
                '&nbsp;<input type="reset" class="btn" value="Clear"></td></tr>',
            )
            + "</form>"
        )

    def _mbrinq(self, p: dict):
        sid = p["sid"]
        if self.command == "GET":
            if STATE.faults.notice_once:
                return self._redirect(f"/notice?sid={sid}&next=mbrinq")
            return self._send(self._inq_form(sid))
        mbr = p.get("F0231", "").strip()
        with STATE.lock:
            busy, err = STATE.faults.busy_once, STATE.faults.error_once
            STATE.faults.busy_once = False
            STATE.faults.error_once = False
        if err:
            return self._send(
                page("<h2>CL-500 UNHANDLED EXCEPTION</h2><pre>MBRINQ.CBL line 4410: SQLCODE -911</pre>"), status=500
            )
        if busy:
            return self._send(page(boxed("ERROR", '<tr><td class="err">E503 SYSTEM BUSY - PLEASE RETRY</td></tr>')))
        if not (mbr.isdigit() and len(mbr) == 5):
            return self._send(self._inq_form(sid, "E102 INVALID MEMBER NUMBER FORMAT", mbr))
        m = MEMBERS.get(mbr)
        if m is None:
            return self._send(self._inq_form(sid, f"E201 NO RECORD FOUND FOR MEMBER {esc(mbr)}", mbr))
        if m.get("restricted"):
            return self._send(
                self._inq_form(sid, "E403 ACCESS DENIED - RESTRICTED ACCOUNT. SUPERVISOR OVERRIDE REQUIRED", mbr)
            )
        return self._redirect(f"/mbrdtl?sid={sid}&mbr={mbr}")

    def _mbrdtl(self, p: dict):
        sid, mbr = p["sid"], p.get("mbr", "")
        m = MEMBERS.get(mbr)
        if not m or m.get("restricted"):
            return self._send(self._inq_form(sid, f"E201 NO RECORD FOUND FOR MEMBER {esc(mbr)}"))
        onload = ""
        if m.get("hold"):
            onload = f"if(!confirm('Member has an active HOLD. Continue?')){{location='/mbrinq?sid={sid}'}}"
        info = "".join(
            f'<tr><td class="lbl">{k}</td><td>{esc(val)}</td></tr>'
            for k, val in (
                ("Name", m["name"]),
                ("Member #", mbr),
                ("SSN", m["ssn"]),
                ("Date of Birth", m["dob"]),
                ("Phone", m["phone"]),
            )
        )
        grid = (
            '<table class="grid" cellspacing="0"><tr><th>Suffix</th><th>Type</th><th>Balance</th><th>Status</th></tr>'
            + "".join(
                f"<tr><td>{s}</td><td>{t}</td><td align='right'>${b}</td><td>OPEN</td></tr>"
                for s, t, b in m["accounts"]
            )
            + "</table>"
        )
        actions = (
            f'<form method="get" action="/subacct"><input type="hidden" name="sid" value="{esc(sid)}">'
            f'<input type="hidden" name="mbr" value="{esc(mbr)}">'
            '<input type="submit" class="btn" name="B20" value="Open Sub-Account"></form>'
        )
        body = (
            boxed("MEMBER DETAIL", info)
            + "<br>"
            + boxed("SHARES / ACCOUNTS", f"<tr><td>{grid}</td></tr>")
            + f"<br>{actions}"
        )
        self._send(page(body, onload=onload))

    def _subacct(self, p: dict):
        sid, mbr = p["sid"], p.get("mbr", "")
        opts = "".join(f"<option>{t}</option>" for t in ("Share Savings", "Money Market", "Share Certificate"))
        body = (
            '<form method="post" action="/subacct/review">'
            f'<input type="hidden" name="sid" value="{esc(sid)}"><input type="hidden" name="mbr" value="{esc(mbr)}">'
            + boxed(
                f"OPEN SUB-ACCOUNT &mdash; MEMBER {esc(mbr)}",
                f'<tr><td class="lbl">Share Type</td><td><select name="F0410">{opts}</select></td></tr>'
                '<tr><td class="lbl">Nickname</td><td><input type="text" name="F0411" size="20"></td></tr>'
                '<tr><td class="lbl">Initial Deposit</td><td><input type="text" name="F0412" size="10"></td></tr>'
                '<tr><td></td><td><input type="submit" class="btn" name="B21" value="Review"></td></tr>',
            )
            + "</form>"
        )
        self._send(page(body))

    def _subacct_review(self, p: dict):
        sid, mbr = p["sid"], p.get("mbr", "")
        dep = p.get("F0412", "").replace(",", "").replace("$", "")
        try:
            ok = float(dep) >= 0
        except ValueError:
            ok = False
        if not ok:
            return self._send(page(boxed("ERROR", '<tr><td class="err">E310 INITIAL DEPOSIT INVALID</td></tr>')))
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{esc(p.get(k, ""))}">'
            for k in ("sid", "mbr", "F0410", "F0411", "F0412")
        )
        rows = "".join(
            f'<tr><td class="lbl">{k}</td><td>{esc(val)}</td></tr>'
            for k, val in (
                ("Member #", mbr),
                ("Share Type", p.get("F0410", "")),
                ("Nickname", p.get("F0411", "")),
                ("Initial Deposit", f"${float(dep):,.2f}"),
            )
        )
        body = (
            f'<form method="post" action="/subacct/commit">{hidden}'
            + boxed(
                "CONFIRM NEW SUB-ACCOUNT",
                rows
                + '<tr><td></td><td><input type="submit" class="btn" name="B30" value="Confirm">&nbsp;'
                f'<a href="/mbrdtl?sid={esc(sid)}&mbr={esc(mbr)}">Cancel</a></td></tr>',
            )
            + "</form>"
        )
        self._send(page(body))

    def _subacct_commit(self, p: dict):
        with STATE.lock:
            suffix = f"{STATE.next_suffix:04d}"
            STATE.next_suffix += 10
        self._send(page(boxed("COMPLETE", f"<tr><td>Sub-account {suffix} opened for member {esc(p.get('mbr',''))}.</td></tr>")))

    def _signoff(self, p: dict):
        with STATE.lock:
            STATE.sessions.pop(p.get("sid", ""), None)
        self._redirect("/login")


def make_server(port: int = 8765, variant: str = "base", host: str = "127.0.0.1") -> ThreadingHTTPServer:
    STATE.variant = variant
    STATE.faults = Faults()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="base")
    args = ap.parse_args()
    srv = make_server(args.port, args.variant)
    print(f"CoreLink Teller ({args.variant}) on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
