"""Configuration: policy (allowlist + risk), app profiles, tenants, secrets.

Layering (see REPORT.md section 4):
    vendor app profile   shared by every tenant running the product (login, state rules, risk rules)
    capability artifact  shared by every tenant running the product (the recorded flow)
    tenant config        base URL, secret namespace, optional overlay (label aliases / step overrides)
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .schema import Condition, Risk, StateRule, Step, TenantOverlay

ROOT = Path(__file__).resolve().parent.parent


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------------
# App profile (vendor-product knowledge, shared across tenants)
# --------------------------------------------------------------------------------------------


class RiskRule(_M):
    """Classify a control as irreversible by its perceived name/label or the form it submits."""

    name_pattern: str | None = None
    form_action_glob: str | None = None
    risk: Risk = Risk.irreversible
    reason: str = ""


class LoginProcedure(_M):
    path: str
    steps: list[Step]
    success: list[Condition]


class AppProfile(_M):
    id: str
    product: str
    versions: str
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"
    login: LoginProcedure
    state_rules: list[StateRule] = Field(default_factory=list)
    risk_rules: list[RiskRule] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Policy (allowlist + risk handling), enforced by PolicyGuard
# --------------------------------------------------------------------------------------------


class Policy(_M):
    allowed_paths: list[str] = Field(description="Glob patterns on URL path; origins come from tenant config")
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["click", "fill", "select", "press", "extract"])
    irreversible: Literal["block", "require_approval"] = "require_approval"
    # Global safety net on top of profile risk rules: any control whose name matches is irreversible.
    irreversible_name_patterns: list[str] = Field(default_factory=list)
    max_discovery_steps: int = 25
    discovery_timeout_s: int = 300
    stuck_after_no_change: int = 3
    human_timeout_s: int = 900


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PolicyGuard:
    """Single enforcement point: every action and every navigation request passes through here."""

    def __init__(self, policy: Policy, allowed_origins: list[str], profile: AppProfile):
        self.policy = policy
        self.origins = {o.rstrip("/") for o in allowed_origins}
        self.profile = profile

    def url_allowed(self, url: str) -> tuple[bool, str]:
        u = urlparse(url)
        if u.scheme in ("about", "data", "blob") or url.startswith("about:"):
            return True, ""
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.origins:
            return False, f"origin {origin} not in allowlist"
        path = u.path or "/"
        if any(fnmatch.fnmatch(path, p) for p in self.policy.denied_paths):
            return False, f"path {path} is explicitly denied"
        if not any(fnmatch.fnmatch(path, p) for p in self.policy.allowed_paths):
            return False, f"path {path} not in allowlist"
        return True, ""

    def check_action(self, action: str) -> None:
        if action not in self.policy.allowed_actions:
            raise PolicyViolation("ACTION_NOT_ALLOWED", f"action '{action}' is not in the allowlist")

    def check_link_target(self, element: dict, current_url: str) -> None:
        """Pre-flight check of where a link would navigate (the network route guard is the backstop)."""
        href = (element.get("attrs") or {}).get("href")
        if not href or href.startswith(("javascript:", "#")):
            return
        from urllib.parse import urljoin

        ok, why = self.url_allowed(urljoin(current_url, href))
        if not ok:
            raise PolicyViolation("NAVIGATION_NOT_ALLOWED", why)

    def classify(self, element: dict) -> tuple[Risk, str]:
        """Risk class of acting on this element. Conservative: name OR form-action rules escalate."""
        names = [element.get("name") or "", element.get("label") or "", element.get("text") or ""]
        form_action = (element.get("attrs") or {}).get("form_action", "")
        for rule in self.profile.risk_rules:
            if rule.name_pattern and any(re.search(rule.name_pattern, n, re.I) for n in names if n):
                return rule.risk, rule.reason or f"matches profile rule /{rule.name_pattern}/"
            if rule.form_action_glob and form_action and fnmatch.fnmatch(form_action, rule.form_action_glob):
                return rule.risk, rule.reason or f"submits to {form_action}"
        for pat in self.policy.irreversible_name_patterns:
            if any(re.search(pat, n, re.I) for n in names if n):
                return Risk.irreversible, f"matches global irreversible pattern /{pat}/"
        return Risk.safe, ""


# --------------------------------------------------------------------------------------------
# Tenants & secrets
# --------------------------------------------------------------------------------------------


class Tenant(_M):
    id: str
    base_url: str
    profile: str
    secret_namespace: str
    overlay: str | None = None


class SecretProvider:
    """Resolves secret refs at the moment of use. Values are never returned to callers that log,
    and every resolved value is registered with the redactor. Production: Vault / AWS SM / HSM."""

    def __init__(self, namespace: str, redactor) -> None:
        self.namespace = namespace
        self.redactor = redactor

    def get(self, ref: str) -> str:
        env = "CUA_SECRET_" + re.sub(r"[^A-Z0-9]", "_", f"{self.namespace}/{ref}".upper())
        val = os.environ.get(env)
        if val is None:
            raise KeyError(f"secret '{ref}' not configured (expected env {env})")
        self.redactor.register(val, f"[SECRET:{ref}]")
        return val


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]  # KEY="value" / KEY='value'
            os.environ.setdefault(k.strip(), v)


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_policy(path: Path = ROOT / "config" / "policy.yaml") -> Policy:
    return Policy.model_validate(_yaml(path))


def load_tenant(tenant_id: str, path: Path = ROOT / "config" / "tenants.yaml") -> Tenant:
    data = _yaml(path)
    if tenant_id not in data:
        raise KeyError(f"unknown tenant '{tenant_id}' (known: {', '.join(data)})")
    return Tenant.model_validate({"id": tenant_id, **data[tenant_id]})


def load_profile(profile_id: str) -> AppProfile:
    return AppProfile.model_validate(_yaml(ROOT / "profiles" / f"{profile_id}.yaml"))


def load_overlay(rel: str | None) -> TenantOverlay | None:
    return TenantOverlay.model_validate(_yaml(ROOT / rel)) if rel else None
