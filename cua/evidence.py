"""Run evidence: an append-only, redacted, structured event log plus richer artifacts on demand.

runs/<run_id>/
    events.jsonl        every decision/action/recovery/handoff, redacted at write time
    screens/*.png       full-page screenshots with sensitive cells blacked out in-page
    dom/*.html          per-frame HTML snapshots, label- and pattern-redacted
    interventions/*.json
    result.json | artifact.yaml | llm_transcript.jsonl
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT
from .redact import Redactor


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(self, kind: str, redactor: Redactor, root: Path | None = None, echo: bool = True):
        self.run_id = f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        self.dir = (root or ROOT / "runs") / self.run_id
        (self.dir / "screens").mkdir(parents=True, exist_ok=True)
        (self.dir / "dom").mkdir(exist_ok=True)
        self.redactor = redactor
        self.echo = echo
        self._seq = 0
        self._t0 = time.monotonic()
        self._fh = open(self.dir / "events.jsonl", "a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> dict:
        self._seq += 1
        rec = {"seq": self._seq, "ts": now_iso(), "t_ms": int((time.monotonic() - self._t0) * 1000), "type": type_}
        rec.update(self.redactor.obj(data))
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        if self.echo:
            brief = {k: v for k, v in rec.items() if k not in ("seq", "ts", "t_ms", "type")}
            line = json.dumps(brief, default=str)
            print(f"  [{rec['t_ms']:>6}ms] {type_:<22} {line[:180]}", file=sys.stderr)
        return rec

    def screenshot(self, surface, name: str) -> str | None:
        rel = f"screens/{self._seq:03d}-{name}.png"
        try:
            surface.screenshot(str(self.dir / rel), self.redactor.literal_patterns())
            return rel
        except Exception as e:  # evidence must never crash the run
            self.event("evidence_error", what="screenshot", error=str(e))
            return None

    def ui_snapshot(self, surface, name: str) -> str | None:
        rel = f"dom/{self._seq:03d}-{name}.html"
        try:
            (self.dir / rel).write_text(self.redactor.text(surface.ui_snapshot(self.redactor.literal_patterns())), encoding="utf-8")
            return rel
        except Exception as e:
            self.event("evidence_error", what="ui_snapshot", error=str(e))
            return None

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str), encoding="utf-8")
        return p

    def close(self) -> None:
        self._fh.close()
