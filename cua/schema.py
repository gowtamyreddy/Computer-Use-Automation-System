"""Capability artifact schema (v1).

An artifact is a *capability contract* first and a step list second:

    header      who/what/which app + version range, lifecycle status, provenance
    inputs      typed parameters the calling agent supplies
    outputs     typed values the caller gets back (and their sensitivity)
    outcomes    business outcomes the caller must handle (MEMBER_NOT_FOUND is an answer, not a crash)
    steps       ordered actions; each with a multi-strategy Target, a risk class and post-conditions
    success     checkpoint that must hold before outputs are returned

Surface-neutral by construction: Targets are described in terms of what an operator perceives
(role, accessible name, visual label, grid coordinates, visible text). Only the `css` locator kind
is web-specific and is always ranked last. A desktop surface implements the same locator kinds
against UIA / AX trees (see REPORT.md section 4).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------------
# Targeting
# --------------------------------------------------------------------------------------------


class RoleName(_M):
    """Role + accessible name. Portable to any accessibility tree (web, UIA, AX, AT-SPI)."""

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str


class VisualLabel(_M):
    """Control identified by the label an operator sees beside/above it (legacy table layouts)."""

    kind: Literal["label"] = "label"
    label: str
    role: str | None = None


class TableCell(_M):
    """Data cell identified by grid coordinates: column header + row key."""

    kind: Literal["table_cell"] = "table_cell"
    column: str
    row_key: str


class VisibleText(_M):
    kind: Literal["text"] = "text"
    text: str
    role: str | None = None


class Css(_M):
    """Surface-specific escape hatch (web only). Always ranked last; never the sole strategy."""

    kind: Literal["css"] = "css"
    selector: str


Locator = Annotated[Union[RoleName, VisualLabel, TableCell, VisibleText, Css], Field(discriminator="kind")]


class FrameRef(_M):
    """Which document/window the target lives in, as a chain of frame names from the top.

    On desktop this is the window/pane path; [] means the top-level document/window."""

    path: list[str] = Field(default_factory=list)


class Fingerprint(_M):
    """Structural facts used to sanity-check a match (guards against a fallback locator
    silently resolving to a *different kind* of control)."""

    tag: str | None = None
    input_type: str | None = None
    role: str | None = None


class Target(_M):
    description: str = Field(description="Human-readable: what this control is, for reviewers and error messages")
    frame: FrameRef = Field(default_factory=FrameRef)
    strategies: list[Locator] = Field(min_length=1, description="Ordered most- to least-preferred")
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)

    @field_validator("strategies")
    @classmethod
    def _css_not_alone(cls, v: list) -> list:
        if all(s.kind == "css" for s in v):
            raise ValueError("a target needs at least one surface-neutral strategy, not only css")
        return v


# --------------------------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------------------------


class ParamValue(_M):
    kind: Literal["param"] = "param"
    param: str


class LiteralValue(_M):
    kind: Literal["literal"] = "literal"
    value: str


class SecretValue(_M):
    """Reference to a secret resolved at runtime from the secret provider. Never a value."""

    kind: Literal["secret"] = "secret"
    ref: str = Field(pattern=r"^[a-z0-9_.\-/]+$")


ValueSource = Annotated[Union[ParamValue, LiteralValue, SecretValue], Field(discriminator="kind")]


# --------------------------------------------------------------------------------------------
# Conditions (post-conditions, checkpoints, state-detection rules)
# --------------------------------------------------------------------------------------------


class UrlMatches(_M):
    """Frame's location path matches a pattern. `{param}` placeholders are substituted;
    `*` matches one path segment / value. Query keys listed must match; others are ignored."""

    kind: Literal["url_matches"] = "url_matches"
    frame: FrameRef = Field(default_factory=FrameRef)
    path: str
    query: dict[str, str] = Field(default_factory=dict)


class ElementPresent(_M):
    kind: Literal["element_present"] = "element_present"
    target: Target


class TextPresent(_M):
    kind: Literal["text_present"] = "text_present"
    pattern: str = Field(description="Regex, case-insensitive, searched in the frame's visible text")
    frame: FrameRef | None = Field(default=None, description="None = any frame")

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v


class DialogPresent(_M):
    """A native modal (JS alert/confirm, OS message box) whose message matches."""

    kind: Literal["dialog"] = "dialog"
    pattern: str


Condition = Annotated[Union[UrlMatches, ElementPresent, TextPresent, DialogPresent], Field(discriminator="kind")]


# --------------------------------------------------------------------------------------------
# State rules: how replay classifies what it sees (shared by app profiles and artifacts)
# --------------------------------------------------------------------------------------------


class Classification(str, Enum):
    business_outcome = "business_outcome"  # legitimate answer for the caller; stop, return it
    recoverable = "recoverable"  # known interstitial/transient; run handler, continue
    failure = "failure"  # hard failure; stop with a debuggable error (or escalate)


class Dismiss(_M):
    kind: Literal["dismiss"] = "dismiss"
    target: Target


class AcceptDialog(_M):
    kind: Literal["accept_dialog"] = "accept_dialog"
    accept: bool = True


class RetryFlow(_M):
    """Wait, return to the post-login home screen and re-run the flow from step 1.

    Retrying a *single* step is not generally safe on legacy hosts (a resubmitted POST, a lost form
    state); restarting a flow that has executed no irreversible step is. The engine enforces that."""

    kind: Literal["retry_flow"] = "retry_flow"
    delay_ms: int = 1000
    max_attempts: int = 2


class Reauthenticate(_M):
    """Run the app profile's login procedure, then restart the flow from step 1.
    Only allowed while no irreversible step has executed."""

    kind: Literal["reauthenticate"] = "reauthenticate"
    max_attempts: int = 1


Handler = Annotated[Union[Dismiss, AcceptDialog, RetryFlow, Reauthenticate], Field(discriminator="kind")]


class StateRule(_M):
    id: str
    description: str
    when: Condition
    classification: Classification
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$", description="Stable machine code returned to the caller")
    handler: Handler | None = None
    priority: int = Field(default=50, description="Higher wins when several rules match")

    @model_validator(mode="after")
    def _handler_iff_recoverable(self):
        if (self.classification == Classification.recoverable) != (self.handler is not None):
            raise ValueError(f"rule {self.id}: recoverable rules need a handler; others must not have one")
        return self


# --------------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------------


class Risk(str, Enum):
    read = "read"  # observation only
    safe = "safe"  # navigation / form entry; reversible, idempotent to retry
    irreversible = "irreversible"  # commits a change of record (post, open, transfer, delete...)


class _StepBase(_M):
    id: str = Field(pattern=r"^s\d+$")
    intent: str = Field(description="Why this step exists, in operator language (from the discovery rationale)")
    risk: Risk = Risk.safe
    expect: list[Condition] = Field(default_factory=list, description="Post-conditions (AND) that confirm the step worked")
    timeout_ms: int = 10_000


class Click(_StepBase):
    action: Literal["click"] = "click"
    target: Target


class Fill(_StepBase):
    action: Literal["fill"] = "fill"
    target: Target
    value: ValueSource


class SelectOption(_StepBase):
    action: Literal["select"] = "select"
    target: Target
    value: ValueSource


class PressKey(_StepBase):
    action: Literal["press"] = "press"
    target: Target
    key: str


class Extract(_StepBase):
    action: Literal["extract"] = "extract"
    target: Target
    output: str
    risk: Risk = Risk.read


Step = Annotated[Union[Click, Fill, SelectOption, PressKey, Extract], Field(discriminator="action")]


# --------------------------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------------------------


class Sensitivity(str, Enum):
    public = "public"
    internal = "internal"
    confidential = "confidential"  # financial values: logged masked, returned to caller
    pii = "pii"  # personal identifiers: logged as redacted, returned to caller only if declared


class ParamType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    enum = "enum"


class InputParam(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType = ParamType.string
    description: str = ""
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    sensitivity: Sensitivity = Sensitivity.internal


class OutputType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    money = "money"  # parsed to a decimal string, currency symbols/grouping stripped


class OutputField(_M):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: OutputType = OutputType.string
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.confidential


class AppRef(_M):
    product: str = Field(description="Vendor product family, e.g. corelink-teller")
    versions: str = Field(description="Version range this capability is known to work on")
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"
    profile: str = Field(description="App profile id (shared login, state rules, risk rules)")


class Provenance(_M):
    recorded_at: str
    discovery_run_id: str
    model: str
    goal: str = Field(description="Redacted natural-language goal used during discovery")
    recorded_on_tenant: str = "demo"
    human_interventions: int = 0


class Status(str, Enum):
    draft = "draft"  # produced by discovery; replayable, but irreversible steps are locked
    approved = "approved"  # reviewed by a human; eligible for unattended invocation
    deprecated = "deprecated"


class Capability(_M):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$", description="e.g. corelink.member.read_savings_balance")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str
    status: Status = Status.draft
    app: AppRef
    provenance: Provenance
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    outcomes: list[StateRule] = Field(
        default_factory=list, description="Capability-specific state rules, evaluated before the app profile's"
    )
    steps: list[Step] = Field(min_length=1)
    success: list[Condition] = Field(min_length=1, description="Checkpoint (AND) verified before returning")
    review_notes: list[str] = Field(default_factory=list, description="Open questions for the human reviewer")

    @model_validator(mode="after")
    def _consistent(self):
        params = {p.name for p in self.inputs}
        outs = {o.name for o in self.outputs}
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        extracted = set()
        for s in self.steps:
            v = getattr(s, "value", None)
            if isinstance(v, ParamValue) and v.param not in params:
                raise ValueError(f"step {s.id} references undeclared input '{v.param}'")
            if isinstance(v, SecretValue):
                raise ValueError(f"step {s.id}: secrets belong to the app profile login, not capabilities")
            if isinstance(s, Extract):
                if s.output not in outs:
                    raise ValueError(f"step {s.id} extracts undeclared output '{s.output}'")
                extracted.add(s.output)
        if missing := outs - extracted:
            raise ValueError(f"outputs never extracted: {sorted(missing)}")
        return self

    @property
    def has_irreversible(self) -> bool:
        return any(s.risk == Risk.irreversible for s in self.steps)

    def tool_definition(self, profile_rules: list[StateRule] = ()) -> dict:
        """Function-calling definition so an agent can discover/invoke this capability by name."""
        props: dict = {}
        for p in self.inputs:
            spec: dict = {"type": {"integer": "integer", "decimal": "number"}.get(p.type.value, "string")}
            if p.description:
                spec["description"] = p.description
            if p.pattern:
                spec["pattern"] = p.pattern
            if p.enum:
                spec["enum"] = p.enum
            props[p.name] = spec
        outcome_codes = sorted(
            {o.code for o in [*self.outcomes, *profile_rules] if o.classification == Classification.business_outcome}
        )
        desc = self.description
        if self.outputs:
            desc += " Returns: " + ", ".join(f"{o.name} ({o.type.value})" for o in self.outputs) + "."
        if outcome_codes:
            desc += " Possible business outcomes: " + ", ".join(outcome_codes) + "."
        return {
            "type": "function",
            "function": {
                "name": self.id.replace(".", "__"),
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [p.name for p in self.inputs if p.required],
                    "additionalProperties": False,
                },
            },
        }


# --------------------------------------------------------------------------------------------
# Tenant overlay: specialise a shared capability for one tenant without forking it
# --------------------------------------------------------------------------------------------


class TenantOverlay(_M):
    tenant: str
    profile: str = Field(description="App profile id the overlay applies to")
    label_aliases: dict[str, str] = Field(
        default_factory=dict,
        description="Base label/name -> tenant label. Applied to role_name/label/text locators and text patterns.",
    )
    step_targets: dict[str, dict[str, Target]] = Field(
        default_factory=dict, description="capability id -> step id -> replacement target (structural differences)"
    )
