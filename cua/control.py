"""Control transfer for a live session: who is driving, intervention requests, and handback.

Model: one *lease* per live session, held by exactly one controller -- `automation` or `human`.

    AUTOMATION --raise(intervention)--> AWAITING_HUMAN --claim--> HUMAN --release(resolution)--> AUTOMATION
                                              \\--resolve without claiming (approve/deny/abort)--/

* Every automated action calls `require_automation()` first, so automation can never act while
  a human holds the lease (the seam is enforced, not conventional).
* While a human holds the lease, their clicks/changes on the live page are captured by the
  surface (same perception rules as discovery) and attached to the intervention, redacted.
* Release carries a typed resolution that tells the engine *how* to resume (retry the step,
  skip it because the human did it, verify-and-finish, resume the agent, approve, deny, abort).

Routing here is a JSON file per intervention in the run directory plus an in-process operator
console (operator_console.py). In production the same Intervention record would go to a queue /
case system and the console would be a co-browsing UI; the lease semantics would not change.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .evidence import RunLog, now_iso


class Owner(str, Enum):
    automation = "automation"
    human = "human"


class InterventionKind(str, Enum):
    stuck = "stuck"  # discovery cannot make progress
    approval = "approval"  # an irreversible step needs a human decision
    failure = "failure"  # replay hit an unrecoverable condition


class Resolution(str, Enum):
    resume = "resume"  # discovery: agent continues from the current (human-modified) state
    retry_step = "retry_step"  # replay: re-run the failed step
    skip_step = "skip_step"  # replay: human performed this step; continue with the next one
    completed = "completed"  # human finished the task; automation verifies checkpoint and extracts
    approve = "approve"  # approval: automation may perform the irreversible step
    deny = "deny"  # approval: do not perform it
    abort = "abort"


ALLOWED = {
    InterventionKind.stuck: [Resolution.resume, Resolution.completed, Resolution.abort],
    InterventionKind.approval: [Resolution.approve, Resolution.deny, Resolution.skip_step, Resolution.abort],
    InterventionKind.failure: [Resolution.retry_step, Resolution.skip_step, Resolution.completed, Resolution.abort],
}


class Intervention(BaseModel):
    id: str
    run_id: str
    kind: InterventionKind
    subject: str  # capability id@version, or the discovery goal (redacted)
    step_id: str | None = None
    step_intent: str | None = None
    code: str
    reason: str
    screenshot: str | None = None
    frames: list[dict] = Field(default_factory=list)
    allowed_resolutions: list[Resolution] = Field(default_factory=list)
    status: str = "pending"  # pending | claimed | resolved
    created_at: str = Field(default_factory=now_iso)
    operator: str | None = None
    claimed_at: str | None = None
    resolved_at: str | None = None
    resolution: Resolution | None = None
    note: str | None = None
    human_actions: list[dict] = Field(default_factory=list)


class ControlError(Exception):
    pass


class ControlChannel:
    def __init__(self, run: RunLog):
        self.run = run
        self._lock = threading.Lock()
        self.owner = Owner.automation
        self.current: Intervention | None = None
        self.history: list[Intervention] = []
        self._n = 0
        (run.dir / "interventions").mkdir(exist_ok=True)

    # ---- automation side --------------------------------------------------------------------
    def require_automation(self) -> None:
        if self.owner != Owner.automation:
            raise ControlError("automation does not hold the control lease")

    def raise_intervention(self, **fields: Any) -> Intervention:
        with self._lock:
            self._n += 1
            kind = InterventionKind(fields["kind"])
            iv = Intervention(id=f"iv{self._n}", run_id=self.run.run_id, allowed_resolutions=ALLOWED[kind], **fields)
            self.current = iv
            self._persist(iv)
        self.run.event("intervention_raised", id=iv.id, kind=iv.kind.value, code=iv.code, reason=iv.reason, step_id=iv.step_id)
        return iv

    def wait_for_resolution(self, surface, timeout_s: int) -> Intervention:
        """Block automation (while keeping the browser's event loop alive for the human)."""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                iv = self.current
                if iv and iv.status == "resolved":
                    self.current = None
                    self.history.append(iv)
                    return iv
            if time.monotonic() > deadline:
                self._resolve(Resolution.abort, "system", "no operator response before timeout")
                continue
            surface.pump(300)

    # ---- human side (called from the operator console thread) ---------------------------------
    def claim(self, operator: str) -> Intervention:
        with self._lock:
            iv = self._need("pending")
            iv.status, iv.operator, iv.claimed_at = "claimed", operator, now_iso()
            self.owner = Owner.human
            self._persist(iv)
        self.run.event("control_transferred", to="human", operator=operator, intervention=iv.id)
        return iv

    def release(self, resolution: str, operator: str, note: str = "") -> Intervention:
        return self._resolve(Resolution(resolution), operator, note)

    def _resolve(self, resolution: Resolution, operator: str, note: str) -> Intervention:
        with self._lock:
            iv = self.current
            if iv is None or iv.status == "resolved":
                raise ControlError("no open intervention")
            if resolution not in iv.allowed_resolutions:
                raise ControlError(f"resolution {resolution.value} not allowed for {iv.kind.value}")
            if resolution in (Resolution.skip_step, Resolution.completed, Resolution.resume) and iv.status != "claimed":
                raise ControlError(f"'{resolution.value}' asserts the human acted; take control first")
            iv.status, iv.resolution, iv.note, iv.resolved_at = "resolved", resolution, note, now_iso()
            iv.operator = iv.operator or operator
            self.owner = Owner.automation
            self._persist(iv)
        self.run.event(
            "control_transferred", to="automation", operator=operator, intervention=iv.id, resolution=resolution.value,
            note=note, human_actions=len(iv.human_actions),
        )
        return iv

    def record_human_event(self, ev: dict) -> None:
        ev = self.run.redactor.obj(ev)
        with self._lock:
            if self.owner != Owner.human or self.current is None:
                return
            self.current.human_actions.append(ev)
            self._persist(self.current)
        self.run.event("human_action", **ev)

    def snapshot(self) -> dict:
        with self._lock:
            return {"owner": self.owner.value, "intervention": self.current.model_dump(mode="json") if self.current else None,
                    "history": [h.model_dump(mode="json") for h in self.history]}

    def _need(self, status: str) -> Intervention:
        if self.current is None or self.current.status != status:
            raise ControlError(f"no {status} intervention")
        return self.current

    def _persist(self, iv: Intervention) -> None:
        self.run.write_json(f"interventions/{iv.id}.json", iv.model_dump(mode="json"))
