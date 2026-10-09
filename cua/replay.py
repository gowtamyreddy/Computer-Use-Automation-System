"""Deterministic replay: execute a capability artifact with no model in the loop.

Determinism comes from four rules, applied identically on every run:
  1. Resolve, don't search: each target is resolved by its ordered strategies using the *same*
     perception code that recorded it; a strategy is accepted only if it yields exactly one element
     that also matches the target's fingerprint. Ambiguity is never "pick the first".
  2. Wait on conditions, not time: after every action the engine polls until the step's
     post-conditions hold, while classifying the screen against state rules on every poll.
  3. Classify every non-happy state: a matching state rule is a business outcome (stop, return it),
     recoverable (run its bounded handler, continue) or a failure (stop with evidence). Anything
     unclassified is a timeout with a full expected-vs-observed report.
  4. Verify the checkpoint before returning outputs.
"""

from __future__ import annotations

import re
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PWError

from .config import AppProfile, PolicyGuard, PolicyViolation, SecretProvider, Tenant
from .control import ControlChannel, InterventionKind, Resolution
from .evidence import RunLog
from .redact import Redactor, fingerprint
from .render import render_observation
from .result import ErrorDetail, Outcome, ReplayResult, Status, StepRecord
from .schema import (
    AcceptDialog,
    Capability,
    Classification,
    Condition,
    Dismiss,
    ElementPresent,
    Extract,
    Fill,
    LiteralValue,
    OutputType,
    ParamType,
    ParamValue,
    PressKey,
    Reauthenticate,
    RetryFlow,
    Risk,
    SecretValue,
    SelectOption,
    Sensitivity,
    StateRule,
    Status as CapStatus,
    Step,
    Target,
    TextPresent,
    UrlMatches,
)
from .surface.base import FrameState

# --------------------------------------------------------------------------------------------
# Internal control flow
# --------------------------------------------------------------------------------------------


class StopBusiness(Exception):
    def __init__(self, rule: StateRule, step_id: str | None):
        super().__init__(rule.code)
        self.rule, self.step_id = rule, step_id


class StopFailure(Exception):
    def __init__(self, detail: ErrorDetail):
        super().__init__(detail.code)
        self.detail = detail


class StopRestart(Exception):
    def __init__(self, rule: StateRule):
        super().__init__(rule.code)
        self.rule = rule


# --------------------------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------------------------


def _subst(s: str, params: dict[str, str]) -> str:
    return re.sub(r"\{([a-z][a-z0-9_]*)\}", lambda m: params.get(m.group(1), m.group(0)), s)


def _glob_to_re(pattern: str) -> re.Pattern:
    return re.compile("^" + re.escape(pattern).replace(r"\*", "[^/]*") + "$")


def url_matches(cond: UrlMatches, states: list[FrameState], params: dict[str, str]) -> bool:
    for st in states:
        if st.path != cond.frame.path:
            continue
        u = urlparse(st.url)
        if not _glob_to_re(_subst(cond.path, params)).match(u.path):
            continue
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if all(k in q and _glob_to_re(_subst(v, params)).match(q[k]) for k, v in cond.query.items()):
            return True
    return False


def describe_condition(c: Condition) -> str:
    if isinstance(c, UrlMatches):
        q = "".join(f" {k}={v}" for k, v in c.query.items())
        return f"frame {'/'.join(c.frame.path) or 'top'} at {c.path}{q}"
    if isinstance(c, TextPresent):
        return f"text /{c.pattern}/ visible"
    if isinstance(c, ElementPresent):
        return f"element present: {c.target.description}"
    return f"dialog /{c.pattern}/"


def parse_output(raw: str, t: OutputType) -> str:
    s = raw.strip()
    if t in (OutputType.money, OutputType.decimal):
        neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
        cleaned = re.sub(r"[^\d.]", "", s)
        try:
            d = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"not a {t.value}: {raw!r}") from None
        return str(-d if neg else d)
    if t == OutputType.integer:
        cleaned = re.sub(r"[,\s]", "", s)
        if not re.fullmatch(r"-?\d+", cleaned):
            raise ValueError(f"not an integer: {raw!r}")
        return cleaned
    return s


# --------------------------------------------------------------------------------------------
# Step executor (shared by profile login, capability replay and discovery's host-state handling)
# --------------------------------------------------------------------------------------------


class Executor:
    def __init__(
        self,
        surface,
        run: RunLog,
        guard: PolicyGuard,
        rules: list[StateRule],
        params: dict[str, str],
        secrets: SecretProvider | None = None,
        channel: ControlChannel | None = None,
        output_types: dict[str, OutputType] | None = None,
    ):
        self.surface, self.run, self.guard = surface, run, guard
        # Higher priority first; stable sort keeps capability rules ahead of profile rules on ties.
        self.rules = sorted(rules, key=lambda r: -r.priority)
        self.params = params
        self.secrets = secrets
        self.channel = channel
        self.output_types = output_types or {}
        self.recoveries: list[dict] = []
        self.warnings: list[dict] = []
        self.outputs: dict[str, str] = {}
        self.dismiss_budget = 3
        self._pending: StateRule | ErrorDetail | None = None
        self._current_step: Step | None = None
        surface.on_dialog(self._on_dialog)
        surface.on_blocked_request(lambda url, why: self.run.event("request_blocked", url=url, why=why))

    # ---- state classification ---------------------------------------------------------------
    def _on_dialog(self, message: str, kind: str) -> bool:
        sid = self._current_step.id if self._current_step else None
        for r in self.rules:
            if r.when.kind == "dialog" and re.search(r.when.pattern, message, re.I):
                self.run.event("dialog", message=message, rule=r.id, classification=r.classification.value, step_id=sid)
                if r.classification == Classification.recoverable and isinstance(r.handler, AcceptDialog):
                    self.recoveries.append({"code": r.code, "rule": r.id, "action": "accept_dialog" if r.handler.accept else "dismiss_dialog", "step_id": sid})
                    return r.handler.accept
                self._pending = r
                return False
        self.run.event("dialog_unexpected", message=message, kind=kind, step_id=sid)
        self._pending = ErrorDetail(
            code="UNEXPECTED_DIALOG", message=f"unclassified {kind} dialog was dismissed", step_id=sid,
            observed={"dialog": message}, expected=["no modal dialog, or one matching a state rule"],
        )
        return False

    def holds(self, cond: Condition, states: list[FrameState]) -> bool:
        if isinstance(cond, UrlMatches):
            return url_matches(cond, states, self.params)
        if isinstance(cond, TextPresent):
            rx = re.compile(_subst(cond.pattern, self.params), re.I)
            return any((cond.frame is None or st.path == cond.frame.path) and rx.search(st.text) for st in states)
        if isinstance(cond, ElementPresent):
            return self.surface.resolve(cond.target).handle is not None
        return False  # dialogs are events, handled in _on_dialog

    def classify(self, states: list[FrameState]) -> StateRule | None:
        for r in self.rules:
            if r.when.kind != "dialog" and self.holds(r.when, states):
                return r
        return None

    def apply_rules(self, states: list[FrameState], step: Step | None) -> bool:
        """Raise for business/failure/restart; return True if a recoverable state was handled."""
        if self._pending is not None:
            p, self._pending = self._pending, None
            if isinstance(p, ErrorDetail):
                p.evidence = self.capture("unexpected-dialog")
                raise StopFailure(p)
            if p.classification == Classification.business_outcome:
                raise StopBusiness(p, step.id if step else None)
            raise StopFailure(self.error(p.code, p.description, step))
        rule = self.classify(states)
        if rule is None:
            return False
        sid = step.id if step else None
        self.run.event("state_detected", rule=rule.id, code=rule.code, classification=rule.classification.value, step_id=sid)
        if rule.classification == Classification.business_outcome:
            raise StopBusiness(rule, sid)
        if rule.classification == Classification.failure:
            raise StopFailure(self.error(rule.code, rule.description, step, states=states))
        h = rule.handler
        if isinstance(h, Dismiss):
            if self.dismiss_budget <= 0:
                raise StopFailure(self.error("RECOVERY_EXHAUSTED", f"'{rule.id}' kept reappearing", step))
            deadline = time.monotonic() + 3
            res = self.surface.resolve(h.target)
            while res.handle is None:
                # The text may have been read from a document that was already navigating away
                # (e.g. right after a previous dismiss). Re-check before calling it a failure.
                self.surface.pump(200)
                if not self.holds(rule.when, self.surface.frame_states()):
                    return True
                if time.monotonic() > deadline:
                    raise StopFailure(self.error("RECOVERY_FAILED", f"could not find '{h.target.description}' to dismiss {rule.id}", step))
                res = self.surface.resolve(h.target)
            self.dismiss_budget -= 1
            try:
                self.surface.click(res.handle, timeout_ms=2000)
            except PWError:  # document replaced under us; re-poll (bounded by dismiss_budget)
                self.run.event("dismiss_retry", rule=rule.id, step_id=sid)
                return True
            self.surface.settle()
            self.recoveries.append({"code": rule.code, "rule": rule.id, "action": "dismiss", "step_id": sid})
            self.run.event("recovered", rule=rule.id, action="dismiss", step_id=sid)
            return True
        if isinstance(h, (RetryFlow, Reauthenticate)):
            raise StopRestart(rule)
        return False

    # ---- waiting ------------------------------------------------------------------------------
    def wait_until(self, conds: list[Condition], step: Step | None, timeout_ms: int, code: str = "POSTCONDITION_TIMEOUT") -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            states = self.surface.frame_states()
            if self.apply_rules(states, step):
                continue
            failing = [c for c in conds if not self.holds(c, states)]
            if not failing:
                return
            if time.monotonic() > deadline:
                raise StopFailure(
                    self.error(code, f"{len(failing)} condition(s) not met within {timeout_ms} ms", step,
                               expected=[describe_condition(c) for c in failing], states=states, retryable=True)
                )
            self.surface.pump(150)

    def resolve(self, target: Target, step: Step | None, timeout_ms: int):
        deadline = time.monotonic() + timeout_ms / 1000
        fallback_after = float("inf")
        while True:
            states = self.surface.frame_states()
            if self.apply_rules(states, step):
                continue
            res = self.surface.resolve(target)
            if res.handle is not None and res.strategy_index and time.monotonic() < fallback_after:
                # A lower-ranked locator matched first. Before accepting it (and reporting drift), give
                # the page a moment to finish rendering so the preferred locator gets a fair chance.
                if fallback_after == float("inf"):
                    fallback_after = time.monotonic() + 0.6
                self.surface.settle()
                self.surface.pump(150)
                continue
            if res.handle is not None:
                return res
            if time.monotonic() > deadline:
                if not res.frame_found:
                    code = "FRAME_NOT_FOUND"
                elif any(a["fingerprint_ok"] > 1 for a in res.attempts):
                    code = "AMBIGUOUS_TARGET"
                else:
                    code = "TARGET_NOT_FOUND"
                raise StopFailure(
                    self.error(code, f"could not uniquely resolve '{target.description}'", step,
                               expected=[f"exactly one element: {target.description} in frame {'/'.join(target.frame.path) or 'top'}"],
                               extra={"strategy_attempts": res.attempts}, states=states)
                )
            self.surface.pump(200)

    # ---- execution ----------------------------------------------------------------------------
    def value_of(self, v) -> str:
        if isinstance(v, ParamValue):
            return self.params[v.param]
        if isinstance(v, LiteralValue):
            return v.value
        if isinstance(v, SecretValue):
            if self.secrets is None:
                raise StopFailure(ErrorDetail(code="SECRET_UNAVAILABLE", message=f"no secret provider for {v.ref}"))
            return self.secrets.get(v.ref)
        raise TypeError(v)

    def execute(self, step: Step) -> StepRecord:
        self._current_step = step
        if self.channel:
            self.channel.require_automation()
        try:
            self.guard.check_action(step.action)
        except PolicyViolation as e:
            raise StopFailure(self.error("POLICY_VIOLATION", str(e), step)) from None
        t0 = time.monotonic()
        res = self.resolve(step.target, step, step.timeout_ms)
        strategy = step.target.strategies[res.strategy_index]
        if res.strategy_index > 0:
            w = {"code": "LOCATOR_FALLBACK", "step_id": step.id, "used": strategy.kind, "attempts": res.attempts,
                 "note": "preferred locator(s) no longer unique -- possible UI drift or tenant variation"}
            self.warnings.append(w)
            self.run.event("locator_fallback", **w)
        try:
            if isinstance(step, Fill):
                value = self.value_of(step.value)
                self.surface.fill(res.handle, value)
                if self.surface.read_value(res.handle) != value:
                    raise StopFailure(self.error("INPUT_NOT_ACCEPTED", "field did not retain the entered value", step))
            elif isinstance(step, SelectOption):
                self.surface.select(res.handle, self.value_of(step.value))
            elif isinstance(step, PressKey):
                self.surface.press(res.handle, step.key)
            elif isinstance(step, Extract):
                raw = self.surface.read_text(res.handle)
                try:
                    self.outputs[step.output] = parse_output(raw, self.output_types.get(step.output, OutputType.string))
                except ValueError as e:
                    raise StopFailure(self.error("EXTRACTION_PARSE_ERROR", str(e), step)) from None
                self.run.event("extracted", step_id=step.id, output=step.output, value_fingerprint=fingerprint(self.outputs[step.output]))
            else:
                self.surface.click(res.handle)
        except PWError as e:
            raise StopFailure(self.error("ACTION_FAILED", str(e).splitlines()[0], step, retryable=True)) from None
        self.run.event("step_ok", step_id=step.id, action=step.action, strategy=strategy.kind, intent=step.intent)
        if step.expect:
            self.surface.settle()
            self.wait_until(step.expect, step, step.timeout_ms)
        return StepRecord(step_id=step.id, action=step.action, status="ok", strategy=strategy.kind,
                          duration_ms=int((time.monotonic() - t0) * 1000))

    # ---- errors & evidence --------------------------------------------------------------------
    def capture(self, name: str) -> list[str]:
        out = [self.run.screenshot(self.surface, name), self.run.ui_snapshot(self.surface, name)]
        return [p for p in out if p]

    def error(self, code, message, step: Step | None, expected=None, states=None, extra=None, retryable=False) -> ErrorDetail:
        observed: dict = dict(extra or {})
        if states is not None:
            observed["frames"] = [{"frame": "/".join(s.path) or "top", "url": s.url} for s in states]
        try:
            observed["screen"] = render_observation(self.surface.observe(), self.run.redactor, max_items_per_frame=40)
        except Exception:
            pass
        detail = ErrorDetail(
            code=code, message=message, step_id=step.id if step else None, step_intent=step.intent if step else None,
            expected=expected or [], observed=self.run.redactor.obj(observed), evidence=self.capture(code.lower()),
            retryable=retryable,
        )
        self.run.event("error", code=code, message=message, step_id=detail.step_id, expected=detail.expected, evidence=detail.evidence)
        return detail


# --------------------------------------------------------------------------------------------
# Session establishment (app profile login; credentials never leave the secret provider)
# --------------------------------------------------------------------------------------------


def login(ex: Executor, profile: AppProfile, tenant: Tenant) -> str:
    ex.run.event("login_start", profile=profile.id, tenant=tenant.id)
    ex.surface.goto(tenant.base_url.rstrip("/") + profile.login.path)
    try:
        for step in profile.login.steps:
            ex.execute(step)
        ex.wait_until(profile.login.success, None, 8000, code="LOGIN_FAILED")
    except StopFailure as f:
        f.detail.code = "LOGIN_FAILED"
        raise
    ex.surface.settle()
    ex.run.event("login_ok", home=ex.surface.top_url())
    return ex.surface.top_url()


# --------------------------------------------------------------------------------------------
# Capability replay
# --------------------------------------------------------------------------------------------


class ReplayOptions:
    def __init__(self, allow_irreversible: bool = False, escalate: bool = False, human_timeout_s: int = 900, console_url: str = ""):
        self.allow_irreversible = allow_irreversible
        self.escalate = escalate
        self.human_timeout_s = human_timeout_s
        self.console_url = console_url


def validate_inputs(cap: Capability, params: dict[str, str]) -> list[str]:
    errs = []
    declared = {p.name: p for p in cap.inputs}
    for name in params:
        if name not in declared:
            errs.append(f"unknown input '{name}'")
    for p in cap.inputs:
        v = params.get(p.name)
        if v is None:
            if p.required:
                errs.append(f"missing required input '{p.name}'")
            continue
        if p.type == ParamType.integer and not re.fullmatch(r"-?\d+", v):
            errs.append(f"'{p.name}' must be an integer")
        if p.type == ParamType.decimal:
            try:
                Decimal(v)
            except InvalidOperation:
                errs.append(f"'{p.name}' must be a decimal")
        if p.enum and v not in p.enum:
            errs.append(f"'{p.name}' must be one of {p.enum}")
        if p.pattern and not re.fullmatch(p.pattern, v):
            errs.append(f"'{p.name}' does not match pattern {p.pattern}")
    return errs


class ReplayEngine:
    def __init__(self, surface, cap: Capability, profile: AppProfile, tenant: Tenant, guard: PolicyGuard,
                 run: RunLog, secrets: SecretProvider, channel: ControlChannel | None, options: ReplayOptions):
        self.surface, self.cap, self.profile, self.tenant = surface, cap, profile, tenant
        self.guard, self.run, self.secrets, self.channel, self.opt = guard, run, secrets, channel, options
        self.interventions: list[dict] = []
        self.steps: list[StepRecord] = []

    def _result(self, status: Status, ex: Executor | None = None, **kw) -> ReplayResult:
        r = ReplayResult(
            run_id=self.run.run_id, capability=self.cap.id, version=self.cap.version, tenant=self.tenant.id, status=status,
            recoveries=ex.recoveries if ex else [], warnings=ex.warnings if ex else [], interventions=self.interventions,
            steps=self.steps, duration_ms=int((time.monotonic() - self._t0) * 1000), **kw,
        )
        # The caller receives raw outputs; persisted evidence keeps only fingerprints.
        persisted = r.model_dump(mode="json")
        persisted["outputs"] = {k: fingerprint(v) for k, v in r.outputs.items()}
        self.run.write_json("result.json", persisted)
        self.run.event("replay_finished", status=status.value,
                       code=(r.outcome.code if r.outcome else r.error.code if r.error else None))
        return r

    def run_replay(self, params: dict[str, str]) -> ReplayResult:
        self._t0 = time.monotonic()
        cap = self.cap
        self.run.event("replay_start", capability=cap.id, version=cap.version, status=cap.status.value, tenant=self.tenant.id,
                       inputs=sorted(params))

        # ---- pre-flight: refuse before touching the UI ------------------------------------
        errs = validate_inputs(cap, params)
        if cap.status == CapStatus.deprecated:
            errs.append("capability is deprecated")
        for s in cap.steps:
            try:
                self.guard.check_action(s.action)
            except PolicyViolation as e:
                errs.append(str(e))
        if cap.has_irreversible and not (cap.status == CapStatus.approved and self.opt.allow_irreversible) and not self.opt.escalate:
            return self._result(Status.rejected, error=ErrorDetail(
                code="IRREVERSIBLE_NOT_PERMITTED",
                message="capability contains irreversible steps: requires status=approved and --allow-irreversible, or --escalate for per-run human approval"))
        if errs:
            return self._result(Status.rejected, error=ErrorDetail(code="INVALID_INPUT", message="; ".join(errs)))

        for p in cap.inputs:
            if p.sensitivity != Sensitivity.public and p.name in params:
                self.run.redactor.register(params[p.name], "{" + p.name + "}")

        rules = [*cap.outcomes, *self.profile.state_rules]
        ex = Executor(self.surface, self.run, self.guard, rules, params, self.secrets, self.channel,
                      {o.name: o.type for o in cap.outputs})
        restarts: dict[str, int] = {}
        irreversible_done = False
        try:
            login(ex, self.profile, self.tenant)
            i = 0
            while i < len(cap.steps):
                step = cap.steps[i]
                try:
                    if step.risk == Risk.irreversible and not (cap.status == CapStatus.approved and self.opt.allow_irreversible):
                        decision = self._escalate(InterventionKind.approval, "APPROVAL_REQUIRED", f"irreversible step: {step.intent}", step, ex)
                        if decision == Resolution.deny:
                            return self._result(Status.rejected, ex, error=ErrorDetail(code="IRREVERSIBLE_DENIED", message="operator denied the irreversible step", step_id=step.id))
                        if decision == Resolution.abort:
                            raise StopFailure(ErrorDetail(code="HUMAN_ABORTED", message="operator aborted at approval", step_id=step.id))
                        if decision == Resolution.skip_step:
                            self.steps.append(StepRecord(step_id=step.id, action=step.action, status="performed_by_human"))
                            irreversible_done = True
                            i += 1
                            continue
                    self.steps.append(ex.execute(step))
                    if step.risk == Risk.irreversible:
                        irreversible_done = True
                    i += 1
                except StopRestart as r:
                    n = restarts.get(r.rule.id, 0) + 1
                    restarts[r.rule.id] = n
                    h = r.rule.handler
                    if irreversible_done:
                        raise StopFailure(ex.error("UNSAFE_TO_RESTART", f"{r.rule.code} after an irreversible step; refusing to re-run", step)) from None
                    if n > h.max_attempts:
                        raise StopFailure(ex.error("RECOVERY_EXHAUSTED", f"{r.rule.code} persisted after {h.max_attempts} restart(s)", step, retryable=True)) from None
                    ex.recoveries.append({"code": r.rule.code, "rule": r.rule.id, "action": h.kind, "step_id": step.id, "attempt": n})
                    self.run.event("recovering", rule=r.rule.id, action=h.kind, attempt=n, from_step=step.id)
                    if isinstance(h, RetryFlow):
                        self.surface.pump(h.delay_ms)
                    login(ex, self.profile, self.tenant)
                    ex.outputs.clear()
                    i = 0
                except StopFailure as f:
                    if not self.opt.escalate or f.detail.code in ("POLICY_VIOLATION", "HUMAN_ABORTED"):
                        raise
                    decision = self._escalate(InterventionKind.failure, f.detail.code, f.detail.message, step, ex)
                    if decision == Resolution.retry_step:
                        continue
                    if decision == Resolution.skip_step:
                        self.steps.append(StepRecord(step_id=step.id, action=step.action, status="performed_by_human"))
                        i += 1
                        continue
                    if decision == Resolution.completed:
                        self.steps.append(StepRecord(step_id=step.id, action=step.action, status="performed_by_human"))
                        for later in cap.steps[i + 1:]:
                            if isinstance(later, Extract):
                                self.steps.append(ex.execute(later))
                        break
                    f.detail.code, f.detail.message = "HUMAN_ABORTED", f"operator aborted after {f.detail.code}: {f.detail.message}"
                    raise
            # ---- checkpoint ----------------------------------------------------------------
            ex.wait_until(cap.success, None, 5000, code="CHECKPOINT_FAILED")
            self.run.event("checkpoint_ok", conditions=[describe_condition(c) for c in cap.success])
            self.run.screenshot(self.surface, "success")
            return self._result(Status.succeeded, ex, outputs=dict(ex.outputs))
        except StopBusiness as b:
            ev = ex.capture(b.rule.code.lower())
            self.run.event("business_outcome", code=b.rule.code, step_id=b.step_id, evidence=ev)
            return self._result(Status.business_outcome, ex, outcome=Outcome(code=b.rule.code, message=b.rule.description, rule_id=b.rule.id, step_id=b.step_id))
        except StopFailure as f:
            return self._result(Status.failed, ex, error=f.detail)
        except StopRestart as r:  # restart condition hit while (re)establishing the session
            return self._result(Status.failed, ex, error=ex.error("RECOVERY_EXHAUSTED", f"{r.rule.code} during sign-on", None))

    # ---- human escalation ---------------------------------------------------------------------
    def _escalate(self, kind: InterventionKind, code: str, reason: str, step: Step, ex: Executor) -> Resolution:
        if self.channel is None:
            raise StopFailure(ErrorDetail(code=code, message=reason, step_id=step.id))
        shot = self.run.screenshot(self.surface, f"escalation-{code.lower()}")
        frames = [{"frame": "/".join(s.path) or "top", "url": s.url} for s in self.surface.frame_states()]
        iv = self.channel.raise_intervention(
            kind=kind, subject=f"{self.cap.id}@{self.cap.version}", step_id=step.id, step_intent=step.intent,
            code=code, reason=reason, screenshot=shot, frames=self.run.redactor.obj(frames),
        )
        if self.opt.console_url:
            print(f"\n>>> Human intervention required ({kind.value}: {code}). Operator console: {self.opt.console_url}\n", file=sys.stderr, flush=True)
        done = self.channel.wait_for_resolution(self.surface, self.opt.human_timeout_s)
        self.interventions.append(done.model_dump(mode="json", exclude={"frames"}))
        self.surface.settle()
        return done.resolution
