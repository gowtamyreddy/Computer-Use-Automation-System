"""Simulated human operator for demos/tests -- does exactly what a remote human would:

  1. watches the operator console for an intervention   (GET  /api/state)
  2. takes control of the live session                  (POST /api/claim)
  3. drives the SAME browser the automation was using, attached over Chrome DevTools (CDP)
  4. hands control back with a resolution and a note    (POST /api/release)

Usage (while a run is waiting with --escalate/--operator and --debug-port 9222):
    python scripts/operator_bot.py --console http://127.0.0.1:8780 --cdp 9222 --lookup 12345 --resolution skip_step
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright


def _call(console: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(console.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for_intervention(console: str, timeout_s: float = 60) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            iv = _call(console, "/api/state").get("intervention")
            if iv and iv["status"] == "pending":
                return iv
        except OSError:
            pass
        time.sleep(0.3)
    raise TimeoutError("no intervention raised")


def claim(console: str, operator: str = "op.alice") -> dict:
    return _call(console, "/api/claim", {"operator": operator})


def release(console: str, resolution: str, note: str = "", operator: str = "op.alice") -> dict:
    return _call(console, "/api/release", {"operator": operator, "resolution": resolution, "note": note})


def on_live_page(cdp_port: int, fn) -> None:
    """Attach to the running browser over CDP and run fn(page) on the automation's page."""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        page = next(pg for ctx in browser.contexts for pg in ctx.pages if "/main" in pg.url or "/login" in pg.url)
        fn(page)
        # never browser.close(): the browser belongs to the automation; leaving the block only disconnects


def lookup_member(member: str):
    """What the human does by hand: open Member Inquiry, type the member number, press the search button."""

    def act(page):
        page.frame(name="nav").get_by_role("link", name="Member Inquiry").or_(
            page.frame(name="nav").get_by_role("link", name="Member Lookup")).click()
        work = page.frame(name="work")
        work.wait_for_url("**/mbrinq**")
        field = work.locator('input[name="F0231"]')
        field.fill(member)
        field.dispatch_event("change")  # a human tabbing out fires change
        work.locator('input[name="B01"]').click()
        work.wait_for_url("**/mbrdtl**")
        page.wait_for_timeout(300)  # let the capture binding deliver the last events

    return act


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", default="http://127.0.0.1:8780")
    ap.add_argument("--cdp", type=int, default=9222)
    ap.add_argument("--lookup", help="member number to look up by hand while in control")
    ap.add_argument("--resolution", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--operator", default="op.alice")
    a = ap.parse_args()
    iv = wait_for_intervention(a.console, 300)
    print(f"intervention {iv['id']}: {iv['kind']} {iv['code']} -- {iv['reason']}")
    if a.lookup or a.resolution in ("skip_step", "completed", "resume"):
        claim(a.console, a.operator)
        if a.lookup:
            on_live_page(a.cdp, lookup_member(a.lookup))
    print(json.dumps(release(a.console, a.resolution, a.note, a.operator), indent=2))


if __name__ == "__main__":
    main()
