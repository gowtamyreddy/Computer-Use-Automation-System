"""Playwright implementation of the Surface protocol for (legacy) web apps.

Perception and locator resolution run *inside* each frame via perceive.js, so frames, framesets
and table layouts are handled by the same code path. Actions are then performed through real
Playwright input events on the element the resolver stamped, so the app sees genuine user input.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Page, sync_playwright

from ..config import PolicyGuard
from ..redact import SCREENSHOT_MASK_PATTERNS, SENSITIVE_LABELS
from ..schema import Target
from .base import FrameState, FrameView, Observation, Resolution

PERCEIVE_JS = (Path(__file__).parent / "perceive.js").read_text()


def frame_path(frame: Frame) -> list[str]:
    names: list[str] = []
    f = frame
    while f.parent_frame is not None:
        names.append(f.name or "@" + f.url.split("?")[0].rsplit("/", 1)[-1])
        f = f.parent_frame
    return list(reversed(names))


class WebSurface:
    def __init__(
        self,
        guard: PolicyGuard,
        headless: bool = True,
        debug_port: int | None = None,
        viewport: tuple[int, int] = (1280, 800),
        slow_mo_ms: int = 0,
    ):
        self.guard = guard
        self.headless = headless
        self.debug_port = debug_port
        self.viewport = viewport
        self.slow_mo_ms = slow_mo_ms
        self._dialog_fn: Callable[[str, str], bool] = lambda msg, kind: False
        self._human_fn: Callable[[dict], None] = lambda ev: None
        self._blocked_fn: Callable[[str, str], None] = lambda url, why: None
        self._frames_by_key: dict[str, Frame] = {}

    # ---- lifecycle ----------------------------------------------------------------------
    def __enter__(self) -> "WebSurface":
        self._pw = sync_playwright().start()
        args = [f"--remote-debugging-port={self.debug_port}"] if self.debug_port else []
        self.browser = self._pw.chromium.launch(headless=self.headless, args=args, slow_mo=self.slow_mo_ms)
        self.context = self.browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})
        self.context.add_init_script(PERCEIVE_JS)
        self.context.route("**/*", self._route)
        self.context.expose_binding("__cuaHumanEvent", self._human_binding)
        self.page: Page = self.context.new_page()
        self.page.on("dialog", self._dialog)
        self.page.set_default_timeout(5000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()

    # ---- guards & callbacks ---------------------------------------------------------------
    def _route(self, route, request) -> None:
        ok, why = self.guard.url_allowed(request.url)
        if ok:
            route.continue_()
        else:
            self._blocked_fn(request.url, why)
            route.abort("blockedbyclient")

    def _dialog(self, dialog) -> None:
        try:
            accept = self._dialog_fn(dialog.message, dialog.type)
            dialog.accept() if accept else dialog.dismiss()
        except PWError:
            pass

    def _human_binding(self, source, payload) -> None:
        try:
            payload["frame"] = frame_path(source["frame"])
        except Exception:
            payload["frame"] = []
        self._human_fn(payload)

    def on_dialog(self, fn) -> None:
        self._dialog_fn = fn

    def on_human_event(self, fn) -> None:
        self._human_fn = fn

    def on_blocked_request(self, fn) -> None:
        self._blocked_fn = fn

    # ---- perception -----------------------------------------------------------------------
    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def _eval(self, frame: Frame, expr: str, arg: Any = None, default: Any = None) -> Any:
        for _ in range(3):
            try:
                return frame.evaluate(expr, arg)
            except PWError as e:  # frame navigating / context destroyed: settle and retry
                if "detached" in str(e).lower():
                    return default
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=2000)
                except PWError:
                    pass
        return default

    def find_frame(self, path: list[str]) -> Frame | None:
        for f in self._frames():
            if frame_path(f) == list(path):
                return f
        return None

    def observe(self, max_items: int = 400) -> Observation:
        frames: list[FrameView] = []
        self._frames_by_key = {}
        for i, f in enumerate(self._frames()):
            snap = self._eval(f, "n => window.__cua ? window.__cua.snapshot(n) : null", max_items)
            if snap is None:
                continue
            key = f"f{i}"
            self._frames_by_key[key] = f
            frames.append(FrameView(key, frame_path(f), snap["url"], snap["title"], snap["items"]))
        return Observation(frames)

    def frame_states(self) -> list[FrameState]:
        out = []
        for f in self._frames():
            text = self._eval(f, "() => window.__cua ? window.__cua.pageText() : document.body ? document.body.innerText : ''", default="")
            out.append(FrameState(frame_path(f), f.url, text or ""))
        return out

    def resolve(self, target: Target) -> Resolution:
        frame = self.find_frame(target.frame.path)
        if frame is None:
            return Resolution(None, None, frame_found=False)
        attempts = []
        fp = target.fingerprint
        for idx, loc in enumerate(target.strategies):
            token = uuid.uuid4().hex[:10]
            hits = self._eval(frame, "([l, t]) => window.__cua ? window.__cua.resolve(l, t) : []", [loc.model_dump(), token], []) or []
            ok_idx = [
                i
                for i, h in enumerate(hits)
                if (not fp.tag or h["tag"] == fp.tag) and (not fp.input_type or h["input_type"] == fp.input_type)
            ]
            accepted = len(ok_idx) == 1
            attempts.append({"kind": loc.kind, "matches": len(hits), "fingerprint_ok": len(ok_idx), "accepted": accepted})
            if accepted:
                handle = frame.locator(f'[data-cua-hit="{token}"]').nth(ok_idx[0])
                return Resolution(handle, idx, attempts, hit_ref=hits[ok_idx[0]].get("ref"))
        return Resolution(None, None, attempts)

    def handle_for_ref(self, obs: Observation, ref: str) -> Any:
        fkey, _, local = ref.partition(":")
        frame = self._frames_by_key.get(fkey)
        if frame is None or frame.is_detached():
            raise KeyError(f"frame for ref {ref!r} is gone; re-observe")
        loc = frame.locator(f'[data-cua-ref="{local}"]')
        if loc.count() != 1:
            raise KeyError(f"element {ref!r} is no longer on the page; re-observe")
        return loc

    # ---- actions --------------------------------------------------------------------------
    def click(self, handle, timeout_ms: int | None = None) -> None:
        handle.click(timeout=timeout_ms)

    def fill(self, handle, value: str) -> None:
        handle.fill(value)

    def select(self, handle, option: str) -> None:
        handle.select_option(label=option)

    def press(self, handle, key: str) -> None:
        handle.press(key)

    def read_text(self, handle) -> str:
        return " ".join(handle.inner_text().split())

    def read_value(self, handle) -> str:
        return handle.input_value()

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="load")

    def top_url(self) -> str:
        return self.page.url

    def settle(self, timeout_ms: int = 3000) -> None:
        for f in self._frames():
            try:
                f.wait_for_load_state("load", timeout=timeout_ms)
            except PWError:
                pass

    def pump(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # ---- evidence -------------------------------------------------------------------------
    def screenshot(self, path: str, extra_patterns: list[str] = ()) -> None:
        """Full-page screenshot with PII/financial values (and registered input values) blacked out in every frame."""
        frames = self._frames()
        patterns = [*SCREENSHOT_MASK_PATTERNS, *extra_patterns]
        for f in frames:
            self._eval(f, "([p, l]) => window.__cua && window.__cua.markSensitive(p, l, true)", [patterns, SENSITIVE_LABELS])
        try:
            self.page.screenshot(path=path, full_page=True)
        finally:
            for f in frames:
                self._eval(f, "() => window.__cua && window.__cua.markSensitive([], [], false)")

    def ui_snapshot(self, extra_patterns: list[str] = ()) -> str:
        """Per-frame HTML with sensitive cells blanked in-page (caller applies pattern redaction too)."""
        parts = []
        for f in self._frames():
            html = self._eval(
                f,
                "([p, l]) => window.__cua ? window.__cua.redactedHTML(p, l) : document.documentElement.outerHTML",
                [[*SCREENSHOT_MASK_PATTERNS, *extra_patterns], SENSITIVE_LABELS],
                "",
            )
            parts.append(f"<!-- frame path={frame_path(f)} url={f.url} -->\n{html}")
        return "\n\n".join(parts)
