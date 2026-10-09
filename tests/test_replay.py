"""Replay against the live CoreLink stand-in: the result contract for every runtime condition."""

from __future__ import annotations

import pytest

from cua.result import Status
from cua.schema import Risk
from target_app import server as app


def test_happy_path_returns_typed_output_and_verifies_checkpoint(env, balance_cap):
    r = env().replay(balance_cap, {"member_id": "12345"})
    assert r.status == Status.succeeded
    assert r.outputs == {"savings_balance": "4210.55"}
    assert [s.step_id for s in r.steps] == ["s1", "s2", "s3", "s4"]
    assert not r.warnings and not r.recoveries


def test_same_capability_different_input(env, balance_cap):
    r = env().replay(balance_cap, {"member_id": "23456"})
    assert r.status == Status.succeeded and r.outputs["savings_balance"] == "15.00"


@pytest.mark.parametrize(
    "member, code",
    [("99999", "RECORD_NOT_FOUND"), ("55555", "ACCESS_DENIED"), ("1234", "VALIDATION_REJECTED")],
)
def test_business_outcomes_are_answers_not_crashes(env, balance_cap, member, code):
    r = env().replay(balance_cap, {"member_id": member})
    assert r.status == Status.business_outcome
    assert r.outcome.code == code and r.outcome.step_id == "s3"
    assert r.error is None and r.outputs == {}


@pytest.mark.parametrize(
    "faults, member, code",
    [
        ({"notice_once": True}, "12345", "INTERSTITIAL_NOTICE"),
        ({"busy_once": True}, "12345", "TRANSIENT_BUSY"),
        ({}, "77777", "MEMBER_HOLD_NOTICE"),
        ({"expire_after": 6}, "12345", "SESSION_EXPIRED"),
    ],
)
def test_recoverable_conditions_are_handled_and_reported(env, balance_cap, faults, member, code):
    app.STATE.faults.__dict__.update(faults)
    r = env().replay(balance_cap, {"member_id": member})
    assert r.status == Status.succeeded, r.error
    assert code in [x["code"] for x in r.recoveries]


def test_slow_host_is_waited_for_not_slept_for(env, balance_cap):
    app.STATE.faults.slow_ms = 1500
    r = env().replay(balance_cap, {"member_id": "12345"})
    assert r.status == Status.succeeded


def test_host_crash_is_a_hard_failure_with_evidence(env, balance_cap):
    app.STATE.faults.error_once = True
    e = env()
    r = e.replay(balance_cap, {"member_id": "12345"})
    assert r.status == Status.failed
    assert r.error.code == "APP_ERROR" and r.error.step_id == "s3"
    assert r.error.evidence and all((e.run.dir / p).exists() for p in r.error.evidence)
    assert "CL-500" in r.error.observed["screen"]


def test_target_missing_reports_expected_vs_observed(env, balance_cap):
    cap = balance_cap.model_copy(deep=True)
    cap.steps[2].target.strategies = [s for s in cap.steps[2].target.strategies if s.kind != "css"]
    cap.steps[2].target.strategies[0].name = "Lookup"  # a control that does not exist
    cap.steps[2].timeout_ms = 1500
    r = env().replay(cap, {"member_id": "12345"})
    assert r.status == Status.failed and r.error.code == "TARGET_NOT_FOUND" and r.error.step_id == "s3"
    assert r.error.observed["strategy_attempts"] == [{"kind": "role_name", "matches": 0, "fingerprint_ok": 0, "accepted": False}]
    assert r.error.expected


def test_fallback_locator_is_used_and_flagged_as_drift(env, balance_cap):
    cap = balance_cap.model_copy(deep=True)
    cap.steps[1].target.strategies[0].label = "Member Number"  # label drifted; css fallback still unique
    r = env().replay(cap, {"member_id": "12345"})
    assert r.status == Status.succeeded
    assert r.warnings[0]["code"] == "LOCATOR_FALLBACK" and r.warnings[0]["used"] == "css"


@pytest.mark.parametrize("params", [{}, {"member_id": "12345", "extra": "x"}])
def test_bad_inputs_rejected_before_touching_ui(env, balance_cap, params):
    r = env().replay(balance_cap, params)
    assert r.status == Status.rejected and r.error.code == "INVALID_INPUT" and r.steps == []


def test_irreversible_steps_need_approval(env, balance_cap):
    cap = balance_cap.model_copy(deep=True)
    cap.steps[2].risk = Risk.irreversible
    r = env().replay(cap, {"member_id": "12345"}, allow_irreversible=True)  # draft -> still refused
    assert r.status == Status.rejected and r.error.code == "IRREVERSIBLE_NOT_PERMITTED"


def test_tenant_variant_degrades_without_overlay_and_is_clean_with_it(env, balance_cap):
    app.STATE.variant = "lakeside"
    without = env().replay(balance_cap, {"member_id": "12345"})
    # Labels differ but the vendor field names don't: css fallbacks carry it, flagged as drift.
    assert without.status == Status.succeeded
    assert {w["step_id"] for w in without.warnings} == {"s1", "s2", "s3"}
    with_overlay = env().replay(balance_cap, {"member_id": "12345"}, overlay_rel="overlays/lakeside.yaml")
    assert with_overlay.status == Status.succeeded and with_overlay.warnings == []


def test_evidence_never_contains_pii_secrets_or_session_ids(env, balance_cap):
    app.STATE.faults.error_once = False
    e = env()
    r = e.replay(balance_cap, {"member_id": "12345"})
    assert r.status == Status.succeeded
    # force a failure on the detail page so a DOM snapshot of PII-bearing content is captured
    cap = balance_cap.model_copy(deep=True)
    cap.success[0].path = "/nowhere"
    e2 = env()
    r2 = e2.replay(cap, {"member_id": "12345"})
    assert r2.status == Status.failed and r2.error.code == "CHECKPOINT_FAILED"
    blob = "\n".join(p.read_text(errors="ignore") for d in (e.run.dir, e2.run.dir) for p in d.rglob("*") if p.suffix in (".json", ".jsonl", ".html"))
    for needle in ("123-45-6789", "SAMPLE, JANE", "(555) 010-2233", "03/12/1984", "4,210.55", "4210.55", "demo-only-not-a-secret", "mbr=12345"):
        assert needle not in blob, needle
    assert "sid=[REDACTED]" in blob
