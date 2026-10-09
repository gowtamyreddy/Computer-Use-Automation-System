"""Apply a tenant overlay to a shared capability + app profile.

Resolution order for a tenant: base capability -> overlay.label_aliases -> overlay.step_targets.
The base artifact is never modified; the specialised copy exists only for the run (and the run log
records which overlay was applied), so one reviewed capability serves every tenant on the product.
"""

from __future__ import annotations

import re
from typing import Any

from .config import AppProfile
from .schema import Capability, TenantOverlay

_LOCATOR_TEXT_FIELDS = {"role_name": ["name"], "label": ["label"], "text": ["text"], "table_cell": ["column", "row_key"]}


def _walk(node: Any, aliases: dict[str, str]) -> Any:
    if isinstance(node, list):
        return [_walk(n, aliases) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _walk(v, aliases) for k, v in node.items()}
    kind = out.get("kind")
    for field in _LOCATOR_TEXT_FIELDS.get(kind, []):
        if out.get(field) in aliases:
            out[field] = aliases[out[field]]
    if kind == "text_present":
        pat = out["pattern"]
        for base, tenant in aliases.items():
            pat = pat.replace(re.escape(base), f"(?:{re.escape(base)}|{re.escape(tenant)})")
            if re.escape(base) != base:
                pat = pat.replace(base, f"(?:{re.escape(base)}|{re.escape(tenant)})")
        out["pattern"] = pat
    return out


def apply_overlay(cap: Capability, profile: AppProfile, overlay: TenantOverlay | None) -> tuple[Capability, AppProfile]:
    if overlay is None:
        return cap, profile
    if overlay.profile != profile.id:
        raise ValueError(f"overlay for {overlay.profile} cannot apply to profile {profile.id}")
    cap_d = _walk(cap.model_dump(mode="json"), overlay.label_aliases)
    for step in cap_d["steps"]:
        repl = overlay.step_targets.get(cap.id, {}).get(step["id"])
        if repl is not None:
            step["target"] = repl.model_dump(mode="json")
    prof_d = _walk(profile.model_dump(mode="json"), overlay.label_aliases)
    return Capability.model_validate(cap_d), AppProfile.model_validate(prof_d)
