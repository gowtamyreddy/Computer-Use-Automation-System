"""Replay result contract returned to the calling agent.

Exactly one of four statuses -- the caller branches on `status`, then on a stable `code`:

    succeeded         outputs populated; checkpoint verified
    business_outcome  a legitimate answer from the host (RECORD_NOT_FOUND, ACCESS_DENIED, ...)
                      -> the agent tells the member / picks another path; NOT an incident
    failed            hard failure; `error` says which step, what was expected, what was observed,
                      where the evidence is, and whether a retry could help
    rejected          refused before touching the UI (bad inputs, irreversible step not permitted,
                      deprecated capability, policy) -> a caller/configuration bug

Recoverable conditions never surface as a status: they are handled and listed in `recoveries`.
Locator fallbacks (possible UI drift) are listed in `warnings` even when the run succeeds.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Status(str, Enum):
    succeeded = "succeeded"
    business_outcome = "business_outcome"
    failed = "failed"
    rejected = "rejected"


class Outcome(BaseModel):
    code: str
    message: str
    rule_id: str | None = None
    step_id: str | None = None


class ErrorDetail(BaseModel):
    code: str  # TARGET_NOT_FOUND | AMBIGUOUS_TARGET | FRAME_NOT_FOUND | POSTCONDITION_TIMEOUT | CHECKPOINT_FAILED |
    #            APP_ERROR | UNEXPECTED_DIALOG | INPUT_NOT_ACCEPTED | EXTRACTION_PARSE_ERROR | ACTION_FAILED |
    #            LOGIN_FAILED | UNSAFE_TO_RESTART | RECOVERY_EXHAUSTED | POLICY_VIOLATION | HUMAN_ABORTED | ...
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: list[str] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    retryable: bool = False


class StepRecord(BaseModel):
    step_id: str
    action: str
    status: str  # ok | failed | skipped_by_human | performed_by_human
    strategy: str | None = None
    duration_ms: int = 0


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    version: str
    tenant: str
    status: Status
    outputs: dict[str, str] = Field(default_factory=dict)
    outcome: Outcome | None = None
    error: ErrorDetail | None = None
    recoveries: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    interventions: list[dict] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    duration_ms: int = 0
