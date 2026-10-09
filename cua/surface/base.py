"""The seam between "how we perceive/act on a surface" and "the recorded flow".

Discovery and replay only talk to a `Surface`. A surface:
  * perceives: `observe()` -> frames/windows of described elements (role, name, label, grid coords)
  * resolves:  a surface-neutral `Target` -> a handle (or a precise "why not")
  * acts:      click / fill / select / press / read on a handle
  * evidences: masked screenshot, redacted DOM/UI-tree snapshot
  * hands over: pump the event loop while a human drives, and report what they did

`WebSurface` (Playwright, frames, DOM) is the one implemented. A `DesktopSurface` would implement
the same protocol over UIA (Windows) / AX (macOS): frames -> window/pane path, role/name/label from
the accessibility tree, `css` locators unsupported (see REPORT.md section 4).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..schema import Target


@dataclass
class FrameView:
    key: str  # short id used in refs shown to the model, e.g. "f2"
    path: list[str]  # frame-name chain from the top document
    url: str
    title: str
    items: list[dict]


@dataclass
class Observation:
    frames: list[FrameView]

    def item(self, ref: str) -> tuple[FrameView, dict]:
        fkey, _, local = ref.partition(":")
        for f in self.frames:
            if f.key == fkey:
                for it in f.items:
                    if it["ref"] == local:
                        return f, it
        raise KeyError(f"no element with ref {ref!r} in the current observation")

    def digest(self) -> str:
        """Fingerprint of the visible state, used for 'no progress' (stuck) detection."""
        payload = [(f.path, f.url.split("?")[0], [(i.get("role"), i.get("name"), i.get("text"), i.get("value")) for i in f.items]) for f in self.frames]
        return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()[:16]


@dataclass
class FrameState:
    path: list[str]
    url: str
    text: str


@dataclass
class Resolution:
    handle: Any | None
    strategy_index: int | None
    attempts: list[dict] = field(default_factory=list)  # [{kind, matches, accepted}]
    frame_found: bool = True
    hit_ref: str | None = None


class Surface(Protocol):
    def observe(self, max_items: int = 400) -> Observation: ...
    def frame_states(self) -> list[FrameState]: ...
    def resolve(self, target: Target) -> Resolution: ...
    def handle_for_ref(self, obs: Observation, ref: str) -> Any: ...
    def click(self, handle: Any, timeout_ms: int | None = None) -> None: ...
    def fill(self, handle: Any, value: str) -> None: ...
    def select(self, handle: Any, option: str) -> None: ...
    def press(self, handle: Any, key: str) -> None: ...
    def read_text(self, handle: Any) -> str: ...
    def read_value(self, handle: Any) -> str: ...
    def goto(self, url: str) -> None: ...
    def top_url(self) -> str: ...
    def screenshot(self, path: str, extra_patterns: list[str] = ()) -> None: ...
    def ui_snapshot(self, extra_patterns: list[str] = ()) -> str: ...
    def pump(self, ms: int) -> None: ...
    def on_dialog(self, fn: Callable[[str, str], bool]) -> None: ...
    def on_human_event(self, fn: Callable[[dict], None]) -> None: ...
    def on_blocked_request(self, fn: Callable[[str, str], None]) -> None: ...
