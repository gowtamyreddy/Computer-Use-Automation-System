"""Text rendering of an Observation: what the model sees during discovery, and what a failure report
shows as 'observed'. Values in cells labelled as personal data are masked by label; everything
then passes through the pattern redactor (money, SSN, phone, dates, session tokens, input values)."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from .redact import Redactor, label_is_sensitive
from .surface.base import Observation

CONTROL_ROLES = {"link", "button", "textbox", "combobox", "checkbox", "radio"}


def render_item(fkey: str, it: dict) -> str:
    role = it.get("role")
    parts = [f"[{fkey}:{it['ref']}]", role]
    if it.get("name") and (role in CONTROL_ROLES or role == "heading"):
        parts.append(json.dumps(it["name"]))
    label = it.get("label")
    grid = it.get("grid") or {}
    if label and label != it.get("name") and not grid:
        parts.append(f"label={json.dumps(label)}")
    text = it.get("text")
    name_shown = it.get("name") and (role in CONTROL_ROLES or role == "heading")
    if text and not (name_shown and text == it.get("name")):
        sensitive = label_is_sensitive(label) or label_is_sensitive(grid.get("column"))
        parts.append(json.dumps("[REDACTED-PII]" if sensitive else text))
    if grid:
        parts.append(f"(column={json.dumps(grid.get('column', ''))} row={json.dumps(grid.get('row_key', ''))})")
    if "value" in it:
        parts.append(f"value={json.dumps(it['value'])}")
    if it.get("options"):
        parts.append(f"options={json.dumps(it['options'])}")
    if it.get("emphasis") == "error":
        parts.append("(shown as ERROR)")
    if not it.get("enabled", True):
        parts.append("(disabled)")
    href = (it.get("attrs") or {}).get("href")
    if href:
        parts.append(f"-> {href}")
    return " ".join(p for p in parts if p)


def render_observation(obs: Observation, redactor: Redactor, max_items_per_frame: int = 150) -> str:
    lines = []
    for f in obs.frames:
        name = "/".join(f.path) or "top"
        lines.append(f"== frame {f.key} [{name}] path={urlparse(f.url).path} title={json.dumps(f.title)}")
        if not f.items:
            lines.append("  (no visible content)")
        for it in f.items[:max_items_per_frame]:
            lines.append("  " + render_item(f.key, it))
        if len(f.items) > max_items_per_frame:
            lines.append(f"  ... {len(f.items) - max_items_per_frame} more items")
    return redactor.text("\n".join(lines))
