"""Turn what happened in a discovery run into a capability artifact.

The recorder never sees the model transcript; it consumes a trace of *executed* actions, each with
the perceived description of the element acted on and the frame URLs before/after. That is what
decouples the artifact from the model: the artifact is derived from facts about the UI.

Target construction ("record-time validation"): for the element the agent acted on, generate every
applicable locator (role+name, visual label, grid coordinates, css), then keep only those that
resolve -- on the live page, at that moment, using the replay resolver -- to exactly that element.
A locator that is ambiguous at record time is dropped rather than discovered to be ambiguous in
production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .redact import Redactor, label_is_sensitive
from .render import CONTROL_ROLES
from .schema import (
    AppRef,
    Capability,
    Click,
    Condition,
    Css,
    ElementPresent,
    Extract,
    Fill,
    Fingerprint,
    FrameRef,
    InputParam,
    OutputField,
    PressKey,
    Provenance,
    Risk,
    RoleName,
    SelectOption,
    TableCell,
    Target,
    UrlMatches,
    ValueSource,
    VisibleText,
    VisualLabel,
)
from .surface.base import FrameState

_NUMERICISH = re.compile(r"^[\s$#\-+.,0-9()%]*$")


@dataclass
class TraceEntry:
    action: str  # click | fill | select | press | extract
    target: Target
    intent: str
    risk: Risk = Risk.safe
    value: ValueSource | None = None
    key: str | None = None
    output: str | None = None
    expect: list[Condition] = field(default_factory=list)
    source: str = "agent"  # agent | human


def _dynamic(s: str | None, param_values: set[str]) -> bool:
    return not s or s in param_values or bool(_NUMERICISH.match(s))


def candidate_locators(item: dict, param_values: set[str], for_extract: bool = False) -> list:
    role, name, label = item.get("role"), item.get("name"), item.get("label")
    grid = item.get("grid") or {}
    out: list = []
    if role in CONTROL_ROLES:
        if not _dynamic(name, param_values):
            out.append(RoleName(role=role, name=name))
        if not _dynamic(label, param_values) and label != name:
            out.append(VisualLabel(label=label, role=role))
    else:
        if grid.get("column") and not _dynamic(grid.get("row_key"), param_values):
            out.append(TableCell(column=grid["column"], row_key=grid["row_key"]))
        if not _dynamic(label, param_values):
            out.append(VisualLabel(label=label))
        if role == "heading" and not _dynamic(name, param_values):
            out.append(RoleName(role=role, name=name))
        # Static text (screen titles, field captions) identifies itself -- but never for an extract
        # target, whose text is the data and changes on every run.
        if not for_extract and not _dynamic(item.get("text"), param_values) and not label_is_sensitive(item.get("label")):
            out.append(VisibleText(text=item["text"]))
    attrs = item.get("attrs") or {}
    tag = item.get("tag")
    if attrs.get("name"):
        out.append(Css(selector=f'{tag}[name="{attrs["name"]}"]'))
    elif tag == "a" and attrs.get("href") and not attrs["href"].startswith("javascript"):
        out.append(Css(selector=f'a[href^="{attrs["href"]}"]'))
    return out


def describe_item(item: dict) -> str:
    grid = item.get("grid") or {}
    if grid.get("column"):
        return f"'{grid['column']}' cell in row '{grid.get('row_key', '')}'"
    role = item.get("role")
    if item.get("name") and role in CONTROL_ROLES | {"heading"}:
        return f"{role} '{item['name']}'"
    if item.get("label"):
        return f"{role} labelled '{item['label']}'"
    if item.get("text") and len(item["text"]) <= 60:
        return f"{role} '{item['text']}'"
    return f"{role} element"


def build_target(surface, frame_path: list[str], item: dict, param_values: set[str], validate: bool = True,
                 for_extract: bool = False) -> tuple[Target, list[str]]:
    fp = Fingerprint(tag=item.get("tag"), input_type=item.get("input_type"), role=item.get("role"))
    desc = describe_item(item)
    frame = FrameRef(path=list(frame_path))
    cands = candidate_locators(item, param_values, for_extract)
    notes: list[str] = []
    kept = []
    if validate:
        for loc in cands:
            probe = Target.model_construct(description=desc, frame=frame, strategies=[loc], fingerprint=fp)
            res = surface.resolve(probe)
            if res.handle is not None and res.hit_ref == item.get("ref"):
                kept.append(loc)
            else:
                notes.append(f"{desc}: dropped {loc.kind} locator (attempts={res.attempts})")
    if not any(loc.kind != "css" for loc in kept):
        neutral = [c for c in cands if c.kind != "css"]
        if not neutral:
            raise ValueError(f"no surface-neutral way to identify {desc}; refusing to record a css-only target")
        notes.append(f"{desc}: locators not validated on the live page -- review before approval")
        kept = neutral + [c for c in cands if c.kind == "css"]
    return Target(description=desc, frame=frame, strategies=kept, fingerprint=fp), notes


# --------------------------------------------------------------------------------------------
# URL post-conditions
# --------------------------------------------------------------------------------------------


def canon_path(path: str, params: dict[str, str]) -> str:
    by_value = {v: k for k, v in params.items()}
    segs = []
    for seg in path.split("/"):
        if seg in by_value:
            segs.append("{" + by_value[seg] + "}")
        elif seg.isdigit():
            segs.append("*")
        else:
            segs.append(seg)
    return "/".join(segs)


def url_expectations(pre: list[FrameState], post: list[FrameState], params: dict[str, str]) -> list[UrlMatches]:
    """One url_matches per frame whose location changed; query keys kept only when they carry an input."""
    by_value = {v: k for k, v in params.items()}
    before = {tuple(s.path): urlparse(s.url) for s in pre}
    out = []
    for s in post:
        u = urlparse(s.url)
        if u.scheme not in ("http", "https"):
            continue
        now_q = {k: v[0] for k, v in parse_qs(u.query).items() if v}
        q = {k: "{" + by_value[v] + "}" for k, v in now_q.items() if v in by_value}
        b = before.get(tuple(s.path))
        if b is not None and b.path == u.path:
            prev_q = {k: v[0] for k, v in parse_qs(b.query).items() if v}
            if all(prev_q.get(k) == now_q[k] for k in q):
                continue  # same screen (e.g. a form re-rendered in place)
        out.append(UrlMatches(frame=FrameRef(path=s.path), path=canon_path(u.path, params), query=q))
    return out


# --------------------------------------------------------------------------------------------
# Capability assembly
# --------------------------------------------------------------------------------------------


@dataclass
class CapabilityMeta:
    id: str
    title: str
    description: str
    goal: str
    model: str
    run_id: str
    tenant: str
    profile_id: str
    product: str
    versions: str
    surface: str


def build_capability(
    meta: CapabilityMeta,
    inputs: list[InputParam],
    outputs: list[OutputField],
    trace: list[TraceEntry],
    final_states: list[FrameState],
    checkpoint_targets: list[Target],
    params: dict[str, str],
    version: str,
    notes: list[str],
    redactor: Redactor,
    human_interventions: int,
) -> Capability:
    steps = []
    for i, t in enumerate(trace, start=1):
        common = dict(id=f"s{i}", intent=redactor.text(t.intent)[:200], risk=t.risk, expect=t.expect, target=t.target)
        if t.action == "click":
            steps.append(Click(**common))
        elif t.action == "fill":
            steps.append(Fill(**common, value=t.value))
        elif t.action == "select":
            steps.append(SelectOption(**common, value=t.value))
        elif t.action == "press":
            steps.append(PressKey(**common, key=t.key))
        elif t.action == "extract":
            steps.append(Extract(**common, output=t.output))
    # Checkpoint: final location of every frame the flow navigated + the proof elements + extract targets.
    touched = {tuple(c.frame.path) for t in trace for c in t.expect if isinstance(c, UrlMatches)}
    success: list[Condition] = [c for c in url_expectations([], final_states, params) if tuple(c.frame.path) in touched]
    success += [ElementPresent(target=t) for t in checkpoint_targets]
    success += [ElementPresent(target=t.target) for t in trace if t.action == "extract"]
    if human_interventions:
        notes.append(f"{human_interventions} human intervention(s) during discovery; steps with source=human need review")
    return Capability(
        id=meta.id,
        version=version,
        title=meta.title,
        description=meta.description,
        app=AppRef(product=meta.product, versions=meta.versions, surface=meta.surface, profile=meta.profile_id),
        provenance=Provenance(
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            discovery_run_id=meta.run_id,
            model=meta.model,
            goal=redactor.text(meta.goal),
            recorded_on_tenant=meta.tenant,
            human_interventions=human_interventions,
        ),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        success=success,
        review_notes=[redactor.text(n) for n in notes],
    )
