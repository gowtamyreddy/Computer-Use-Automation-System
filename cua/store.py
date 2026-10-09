"""Artifact store: artifacts/<capability id>/<semver>.yaml -- plain files, diffable, reviewable in a PR.

Saving runs a persistence guard: the serialized artifact must survive redaction unchanged, i.e. it
must contain no registered input values, secrets or sensitive patterns. Production: the same files in
a git-backed registry with review (draft -> approved) as a pull request.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import ROOT
from .redact import Redactor
from .schema import Capability, Status

STORE = ROOT / "artifacts"


class UnsafeArtifact(Exception):
    pass


_STEP_ORDER = ["id", "action", "intent", "risk", "target", "value", "key", "output", "expect", "timeout_ms"]


def dump(cap: Capability) -> str:
    d = cap.model_dump(mode="json", exclude_none=True)
    d["steps"] = [{k: s[k] for k in _STEP_ORDER if k in s} for s in d["steps"]]  # reviewer-friendly order
    return yaml.safe_dump(d, sort_keys=False, width=120, allow_unicode=True)


def load(path: str | Path) -> Capability:
    return Capability.model_validate(yaml.safe_load(Path(path).read_text()))


def next_version(cap_id: str) -> str:
    d = STORE / cap_id
    versions = sorted((tuple(int(x) for x in p.stem.split(".")) for p in d.glob("*.yaml")), reverse=True) if d.exists() else []
    if not versions:
        return "1.0.0"
    major, minor, _ = versions[0]
    return f"{major}.{minor + 1}.0"


def save(cap: Capability, redactor: Redactor, root: Path | None = None) -> Path:
    text = dump(cap)
    if not redactor.is_clean(text):
        leaked = [ln for ln in text.splitlines() if not redactor.is_clean(ln)]
        raise UnsafeArtifact(f"artifact would persist sensitive data: {leaked[:3]}")
    path = (root or STORE) / cap.id / f"{cap.version}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def set_status(path: str | Path, status: Status, reviewer: str) -> Capability:
    cap = load(path)
    cap = cap.model_copy(update={"status": status, "review_notes": [*cap.review_notes, f"status -> {status.value} by {reviewer}"]})
    Path(path).write_text(dump(cap))
    return cap


def catalog(root: Path | None = None) -> list[Capability]:
    latest = {}
    for p in sorted((root or STORE).glob("*/*.yaml")):
        cap = load(p)
        cur = latest.get(cap.id)
        if cur is None or tuple(map(int, cap.version.split("."))) > tuple(map(int, cur.version.split("."))):
            latest[cap.id] = cap
    return list(latest.values())
