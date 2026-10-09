"""Goal-driven discovery: observe -> decide (LLM) -> act, until done or a stopping condition.

Design choices (see REPORT.md section 1):
* Observation = accessibility-style text rendering of every frame (role, name, visual label, grid
  coordinates), not raw HTML. It is the same representation replay resolves against, it works on
  table-layout/frameset apps, and it maps 1:1 to a desktop accessibility tree. Screenshots are
  optional (--vision) and masked.
* The model never sees credentials (the app profile signs on) nor input values (it references
  parameters by name), and PII is masked by label and pattern before anything leaves the process.
* Each turn is rebuilt from scratch (goal + compact action history + current screen) instead of
  an ever-growing chat, which bounds cost and keeps every decision auditable on its own.
* Every action passes the PolicyGuard; irreversible ones require an operator's approval.
* Known host states (interstitials, session expiry) are handled by the app profile, not the model,
  so the recorded flow contains only the intended path.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PWError

from .config import AppProfile, Policy, PolicyGuard, PolicyViolation, SecretProvider, Tenant
from .control import ControlChannel, InterventionKind, Resolution
from .evidence import RunLog
from .llm import DecisionModel, ToolCall
from .recorder import CapabilityMeta, TraceEntry, build_capability, build_target, url_expectations
from .redact import Redactor, fingerprint
from .render import render_observation
from .replay import Executor, StopBusiness, StopFailure, StopRestart, login, parse_output
from .schema import (
    Capability,
    InputParam,
    LiteralValue,
    OutputField,
    OutputType,
    ParamValue,
    Risk,
    Target,
)
from .surface.base import Observation

SYSTEM_PROMPT = """You are the discovery agent of a computer-use system that operates legacy back-office banking
applications for credit-union staff. You see the application as frames containing elements; each element has a
ref like [f3:12]. You act by calling exactly ONE tool per turn.

Your successful run is converted into a deterministic automation that will be replayed many times, so:
- Take the most direct, conventional path a trained operator would take. Do not explore or click around.
- Values that come from the task parameters MUST be entered with fill/select using `param` (you are not shown
  parameter values). Use `text`/`option` only for fixed values that are the same on every run.
- Read data with `extract`, pointing at the element that contains the value. Values may appear masked
  (e.g. $[AMOUNT], [REDACTED-PII]) -- that is intentional; locate the right element by its row/column/label.
  Give each output a snake_case name and a type.
- You are already signed on. Never sign off and never open administration functions.
- Controls that commit changes of record (Confirm, Post, Submit payment, ...) require human approval. Only use
  them if the goal explicitly requires committing. If the goal is to reach a review/confirmation screen, stop there.
- If the host shows an error that is a legitimate answer (e.g. no record found), call finish with success=false
  and explain. If you are blocked or unsure how to proceed safely, call request_human.
- When the goal is achieved (including all extracts), call finish with success=true, naming 1-3 checkpoint_refs:
  stable elements (e.g. a screen title or field label, not a data value) that prove you are on the final screen."""


def _ref(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS = [
    {"type": "function", "function": {"name": "click", "description": "Click a link/button.",
     "parameters": {"type": "object", "properties": {"ref": _ref("element ref, e.g. f3:12"), "reason": _ref("why, in operator language")},
                    "required": ["ref", "reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "fill", "description": "Type into a text field. Provide exactly one of param or text.",
     "parameters": {"type": "object", "properties": {"ref": _ref("element ref"), "param": _ref("task parameter name"),
                    "text": _ref("fixed literal (same on every run)"), "reason": _ref("why")},
                    "required": ["ref", "reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "select", "description": "Choose an option in a dropdown. Provide exactly one of param or option.",
     "parameters": {"type": "object", "properties": {"ref": _ref("element ref"), "param": _ref("task parameter name"),
                    "option": _ref("visible option text"), "reason": _ref("why")},
                    "required": ["ref", "reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "press", "description": "Press a key (e.g. Enter, Tab) in an element.",
     "parameters": {"type": "object", "properties": {"ref": _ref("element ref"), "key": _ref("key name"), "reason": _ref("why")},
                    "required": ["ref", "key", "reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "extract", "description": "Declare an output: read the value of an element.",
     "parameters": {"type": "object", "properties": {
         "ref": _ref("element containing the value"), "output": _ref("snake_case output name"),
         "type": {"type": "string", "enum": [t.value for t in OutputType]}, "description": _ref("what the value means"),
         "reason": _ref("why this element")}, "required": ["ref", "output", "type", "description", "reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "request_human", "description": "Ask a human operator to take over the live session.",
     "parameters": {"type": "object", "properties": {"reason": _ref("what is blocking you")}, "required": ["reason"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "finish", "description": "End the run.",
     "parameters": {"type": "object", "properties": {
         "success": {"type": "boolean"}, "summary": _ref("what was achieved / why not"),
         "checkpoint_refs": {"type": "array", "items": {"type": "string"}, "description": "1-3 refs proving the final screen"},
         "capability_title": _ref("short title for the reusable capability"),
         "capability_description": _ref("one-sentence description for a calling agent")},
         "required": ["success", "summary"], "additionalProperties": False}}},
]


@dataclass
class ParamSpec:
    name: str
    value: str
    description: str = ""


@dataclass
class DiscoveryResult:
    success: bool
    summary: str
    run_id: str
    capability: Capability | None = None
    steps_taken: int = 0
    stop_code: str | None = None
    usage: dict = field(default_factory=dict)


class DiscoveryAgent:
    def __init__(self, *, surface, model: DecisionModel, run: RunLog, guard: PolicyGuard, policy: Policy,
                 profile: AppProfile, tenant: Tenant, secrets: SecretProvider, channel: ControlChannel | None,
                 goal: str, params: list[ParamSpec], capability_id: str, vision: bool = False, console_url: str = ""):
        self.surface, self.model, self.run, self.guard, self.policy = surface, model, run, guard, policy
        self.profile, self.tenant, self.secrets, self.channel = profile, tenant, secrets, channel
        self.params = {p.name: p.value for p in params}
        self.param_specs = params
        self.capability_id = capability_id
        self.vision = vision
        self.console_url = console_url
        self.redactor: Redactor = run.redactor
        for p in params:
            self.redactor.register(p.value, "{" + p.name + "}")
        self.goal = self.redactor.text(goal)  # the model sees {member_id}, never 12345
        self.ex = Executor(surface, run, guard, profile.state_rules, self.params, secrets, channel)
        self.trace: list[TraceEntry] = []
        self.outputs: list[OutputField] = []
        self.history: list[str] = []
        self.notes: list[str] = []
        self.interventions = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self._transcript = open(run.dir / "llm_transcript.jsonl", "a", encoding="utf-8")

    # ---- helpers ----------------------------------------------------------------------------
    def _log_turn(self, turn: int, prompt: str, call: ToolCall) -> None:
        rec = {"turn": turn, "model": self.model.model, "prompt": self.redactor.text(prompt),
               "tool_call": {"name": call.name, "args": self.redactor.obj(call.args)}, "usage": call.usage}
        self._transcript.write(json.dumps(rec) + "\n")
        self._transcript.flush()

    def _user_turn(self, n: int, screen: str, host_note: str | None) -> str:
        params = "\n".join(f"  - {p.name}: {p.description or '(no description)'}" for p in self.param_specs) or "  (none)"
        hist = "\n".join(self.history[-20:]) or "  (nothing yet)"
        host = f"\nHOST STATE: {host_note}\n" if host_note else ""
        outs = ", ".join(o.name for o in self.outputs) or "(none)"
        return (f"GOAL: {self.goal}\nPARAMETERS (values supplied at runtime; reference by name):\n{params}\n"
                f"OUTPUTS DECLARED SO FAR: {outs}\nTURN {n} of at most {self.policy.max_discovery_steps}\n"
                f"HISTORY:\n{hist}\n{host}\nCURRENT SCREEN:\n{screen}\n\nChoose the next action.")

    def _host_state(self) -> str | None:
        """Let the app profile handle known states; describe anything the model must reason about."""
        for _ in range(3):
            try:
                if self.ex.apply_rules(self.surface.frame_states(), None):
                    self.history.append(f"  (app profile auto-handled: {self.ex.recoveries[-1]['code']})")
                    continue
                return None
            except StopBusiness as b:
                return f"{b.rule.code} -- {b.rule.description}"
            except StopFailure as f:
                return f"{f.detail.code} -- {f.detail.message}"
            except StopRestart as r:
                self.run.event("discovery_restart", reason=r.rule.code)
                login(self.ex, self.profile, self.tenant)
                self.trace.clear()
                self.outputs.clear()
                self.history.append(f"  ({r.rule.code}: signed on again; recording restarted from the home screen)")
        return None

    def _escalate(self, kind: InterventionKind, code: str, reason: str) -> Resolution | None:
        if self.channel is None:
            self.run.event("escalation_unavailable", code=code, reason=reason)
            return None
        self.interventions += 1
        shot = self.run.screenshot(self.surface, f"escalation-{code.lower()}")
        frames = [{"frame": "/".join(s.path) or "top", "url": s.url} for s in self.surface.frame_states()]
        self.channel.raise_intervention(kind=kind, subject=f"discovery: {self.goal}", code=code, reason=self.redactor.text(reason),
                                        screenshot=shot, frames=self.redactor.obj(frames))
        if self.console_url:
            print(f"\n>>> Human intervention required ({kind.value}: {code}). Operator console: {self.console_url}\n", file=sys.stderr, flush=True)
        iv = self.channel.wait_for_resolution(self.surface, self.policy.human_timeout_s)
        self.surface.settle()
        if iv.resolution in (Resolution.resume, Resolution.completed):
            self._fold_human_actions(iv.human_actions)
            did = "; ".join(f"{a['type']} {a['element'].get('role')} '{a['element'].get('name') or a['element'].get('label') or ''}'"
                            for a in iv.human_actions) or "no recorded actions"
            self.history.append(f"  OPERATOR took control ({iv.resolution.value}): {did}. Note: {iv.note or '-'}")
            if iv.resolution == Resolution.completed:
                self.history.append("  OPERATOR says the goal is now complete: declare any outputs, then call finish.")
        return iv.resolution

    def _fold_human_actions(self, actions: list[dict]) -> None:
        """Human actions become recorded steps (flagged for review), so the artifact stays complete."""
        for a in actions:
            el = a["element"]
            role = el.get("role")
            try:
                target, notes = build_target(self.surface, a.get("frame", []), el, set(self.params.values()), validate=False)
            except ValueError as e:
                self.notes.append(f"human action not recordable: {e}")
                continue
            if a["type"] == "click" and role in ("link", "button"):
                self.trace.append(TraceEntry("click", target, "(operator) click " + target.description, source="human"))
            elif a["type"] == "change" and role in ("textbox", "combobox"):
                v = str(el.get("value", ""))
                if v.startswith("{") and v.endswith("}") and v[1:-1] in self.params:
                    value = ParamValue(param=v[1:-1])
                elif v and self.redactor.is_clean(v):
                    value = LiteralValue(value=v)
                else:
                    self.notes.append(f"operator entered a value in {target.description} that is neither a declared input nor safe to persist")
                    continue
                self.trace.append(TraceEntry("select" if role == "combobox" else "fill", target, "(operator) enter " + target.description,
                                             value=value, source="human"))
            self.notes.extend(notes)

    # ---- main loop ----------------------------------------------------------------------------
    def run_discovery(self) -> DiscoveryResult:
        t_end = time.monotonic() + self.policy.discovery_timeout_s
        self.run.event("discovery_start", goal=self.goal, params=sorted(self.params), model=self.model.model, tenant=self.tenant.id)
        self.run.write_json("system_prompt.json", {"system": SYSTEM_PROMPT, "tools": TOOLS})
        try:
            login(self.ex, self.profile, self.tenant)
        except StopFailure as f:
            return self._done(False, f"sign-on failed: {f.detail.message}", 0, "LOGIN_FAILED")
        no_change = errors = 0
        n = 0
        while True:
            n += 1
            if n > self.policy.max_discovery_steps or time.monotonic() > t_end:
                code = "MAX_STEPS" if n > self.policy.max_discovery_steps else "TIMEOUT"
                if self._escalate(InterventionKind.stuck, code, f"no completion after {n - 1} turns") in (Resolution.resume, Resolution.completed):
                    n, t_end = 1, time.monotonic() + self.policy.discovery_timeout_s
                    continue
                return self._done(False, f"stopped: {code}", n - 1, code)
            if no_change >= self.policy.stuck_after_no_change or errors >= 3:
                code = "NO_PROGRESS" if no_change >= self.policy.stuck_after_no_change else "REPEATED_ERRORS"
                no_change = errors = 0
                res = self._escalate(InterventionKind.stuck, code, f"{code}: the agent's last actions did not change the screen or failed")
                if res not in (Resolution.resume, Resolution.completed):
                    return self._done(False, f"stuck: {code}", n, code)

            host_note = self._host_state()
            self.surface.settle()
            obs: Observation = self.surface.observe()
            screen = render_observation(obs, self.redactor)
            digest = obs.digest()
            prompt = self._user_turn(n, screen, host_note)
            png = None
            if self.vision:
                rel = self.run.screenshot(self.surface, f"turn{n:02d}")
                png = (self.run.dir / rel).read_bytes() if rel else None
            call = self.model.decide(SYSTEM_PROMPT, prompt, TOOLS, png)
            for k in ("prompt_tokens", "completion_tokens"):
                self.usage[k] += int(call.usage.get(k) or 0)
            self.usage["calls"] += 1
            self._log_turn(n, prompt, call)
            reason = str(call.args.get("reason", ""))
            self.run.event("decision", turn=n, tool=call.name, args={k: v for k, v in call.args.items() if k != "reason"}, reason=reason)

            if call.name == "finish":
                return self._finish(call.args, obs, n)
            if call.name == "request_human":
                res = self._escalate(InterventionKind.stuck, "AGENT_REQUESTED_HELP", call.args.get("reason", ""))
                if res not in (Resolution.resume, Resolution.completed):
                    return self._done(False, f"agent asked for help; operator resolution={res}", n, "AGENT_REQUESTED_HELP")
                continue
            try:
                self._act(call, obs, reason, n)
                errors = 0
            except (KeyError, ValueError, PolicyViolation, PWError, StopFailure) as e:
                errors += 1
                msg = e.detail.message if isinstance(e, StopFailure) else str(e).splitlines()[0]
                code = getattr(e, "code", type(e).__name__)
                self.history.append(f"{n}. {call.name} {call.args.get('ref', '')} -> REJECTED ({code}: {msg})")
                self.run.event("action_rejected", turn=n, tool=call.name, code=code, message=msg)
                continue
            new_digest = self.surface.observe().digest()
            no_change = no_change + 1 if (new_digest == digest and call.name in ("click", "press")) else 0

    def _act(self, call: ToolCall, obs: Observation, reason: str, n: int) -> None:
        a = call.args
        frame, item = obs.item(str(a.get("ref", "")))
        self.guard.check_action(call.name)
        if call.name == "click":
            self.guard.check_link_target(item, frame.url)
        risk, why = self.guard.classify(item) if call.name in ("click", "press") else (Risk.safe, "")
        if call.name == "extract":
            risk = Risk.read
        param_values = set(self.params.values())
        target, notes = build_target(self.surface, frame.path, item, param_values, for_extract=call.name == "extract")
        self.notes.extend(notes)
        if risk == Risk.irreversible:
            if self.policy.irreversible == "block":
                raise PolicyViolation("IRREVERSIBLE_BLOCKED", f"{target.description} is irreversible ({why}); policy blocks it")
            res = self._escalate(InterventionKind.approval, "APPROVAL_REQUIRED", f"agent wants to {call.name} {target.description}: {reason} [{why}]")
            if res == Resolution.skip_step:
                self.history.append(f"{n}. operator performed {target.description} manually")
                self.trace.append(TraceEntry(call.name, target, reason, risk=risk, source="human"))
                return
            if res != Resolution.approve:
                raise PolicyViolation("APPROVAL_DENIED", f"operator did not approve {target.description}")
        pre = self.surface.frame_states()
        handle = self.surface.handle_for_ref(obs, a["ref"])
        entry = TraceEntry(call.name, target, reason, risk=risk)
        if call.name == "click":
            self.surface.click(handle)
        elif call.name in ("fill", "select"):
            if bool(a.get("param")) == bool(a.get("text") or a.get("option")):
                raise ValueError("provide exactly one of param or text/option")
            if a.get("param"):
                if a["param"] not in self.params:
                    raise ValueError(f"unknown parameter {a['param']!r}; known: {sorted(self.params)}")
                value, entry.value = self.params[a["param"]], ParamValue(param=a["param"])
            else:
                value = str(a.get("text") or a.get("option"))
                if value in param_values:
                    entry.value = ParamValue(param=next(k for k, v in self.params.items() if v == value))
                elif not self.redactor.is_clean(value):
                    raise ValueError("that literal looks sensitive and would be persisted; use a parameter")
                else:
                    entry.value = LiteralValue(value=value)
            (self.surface.fill if call.name == "fill" else self.surface.select)(handle, value)
        elif call.name == "press":
            entry.key = str(a["key"])
            self.surface.press(handle, entry.key)
        elif call.name == "extract":
            out_type = OutputType(a.get("type", "string"))
            raw = self.surface.read_text(handle)
            parsed = parse_output(raw, out_type)  # ValueError -> rejected, model must pick another element
            name = str(a["output"])
            self.outputs = [o for o in self.outputs if o.name != name] + [OutputField(name=name, type=out_type, description=str(a.get("description", "")))]
            self.trace = [t for t in self.trace if not (t.action == "extract" and t.output == name)]
            entry.output = name
            self.trace.append(entry)
            self.run.event("extracted", turn=n, output=name, output_type=out_type.value, value_fingerprint=fingerprint(parsed))
            self.history.append(f"{n}. extract {name} from {target.description} -> ok (value hidden)")
            return
        self.surface.settle()
        self.surface.pump(400)
        self.surface.settle()
        post = self.surface.frame_states()
        entry.expect = url_expectations(pre, post, self.params) if call.name in ("click", "press") else []
        self.trace.append(entry)
        moved = ", ".join(f"{'/'.join(c.frame.path) or 'top'} -> {c.path}" for c in entry.expect) or "no navigation"
        self.history.append(f"{n}. {call.name} {target.description}" + (f" <- {entry.value.kind}:{getattr(entry.value, 'param', '')}" if entry.value else "") + f" -> ok ({moved})")
        self.run.event("action", turn=n, tool=call.name, target=target.description, strategies=[s.kind for s in target.strategies],
                       risk=risk.value, expect=[c.model_dump(mode="json") for c in entry.expect])

    def _finish(self, args: dict, obs: Observation, n: int) -> DiscoveryResult:
        if not args.get("success"):
            return self._done(False, str(args.get("summary", "")), n, "AGENT_GAVE_UP")
        if not self.trace:
            return self._done(False, "finish(success) with no recorded actions", n, "EMPTY_TRACE")
        checkpoints: list[Target] = []
        for ref in args.get("checkpoint_refs") or []:
            try:
                frame, item = obs.item(ref)
                t, notes = build_target(self.surface, frame.path, item, set(self.params.values()))
                checkpoints.append(t)
                self.notes.extend(notes)
            except (KeyError, ValueError) as e:
                self.notes.append(f"checkpoint {ref} skipped: {e}")
        from . import store

        meta = CapabilityMeta(
            id=self.capability_id, title=str(args.get("capability_title") or self.capability_id),
            description=str(args.get("capability_description") or args.get("summary", "")), goal=self.goal,
            model=self.model.model, run_id=self.run.run_id, tenant=self.tenant.id, profile_id=self.profile.id,
            product=self.profile.product, versions=self.profile.versions, surface=self.profile.surface,
        )
        inputs = [InputParam(name=p.name, description=p.description, sensitivity="pii") for p in self.param_specs]
        cap = build_capability(meta, inputs, self.outputs, self.trace, self.surface.frame_states(), checkpoints, self.params,
                               store.next_version(self.capability_id), self.notes, self.redactor, self.interventions)
        path = store.save(cap, self.redactor)
        (self.run.dir / "artifact.yaml").write_text(store.dump(cap))
        self.run.event("artifact_saved", path=f"{cap.id}/{cap.version}.yaml", steps=len(cap.steps), outputs=[o.name for o in cap.outputs])
        self.run.screenshot(self.surface, "final")
        return self._done(True, str(args.get("summary", "")), n, None, cap)

    def _done(self, ok: bool, summary: str, n: int, code: str | None, cap: Capability | None = None) -> DiscoveryResult:
        self._transcript.close()
        r = DiscoveryResult(ok, self.redactor.text(summary), self.run.run_id, cap, n, code, self.usage)
        self.run.write_json("result.json", {"success": ok, "summary": r.summary, "steps_taken": n, "stop_code": code,
                                            "capability": f"{cap.id}@{cap.version}" if cap else None, "usage": self.usage})
        self.run.event("discovery_finished", success=ok, stop_code=code, turns=n, usage=self.usage)
        return r
