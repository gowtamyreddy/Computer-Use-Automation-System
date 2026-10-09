"""Command line: cua app | discover | replay | invoke | catalog | approve | faults"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, PolicyGuard, SecretProvider, load_dotenv, load_overlay, load_policy, load_profile, load_tenant
from .control import ControlChannel
from .evidence import RunLog
from .redact import Redactor


def _params(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"--param expects name=value, got {it!r}")
        out[k.strip()] = v
    return out


@dataclass
class Runtime:
    tenant: object
    profile: object
    policy: object
    guard: PolicyGuard
    redactor: Redactor
    run: RunLog
    secrets: SecretProvider
    surface: object
    channel: ControlChannel | None
    console_url: str


@contextlib.contextmanager
def runtime(kind: str, tenant_id: str, headed: bool, operator: bool, console_port: int, debug_port: int | None, slow_mo: int = 0):
    from .operator_console import OperatorConsole
    from .surface.web import WebSurface

    load_dotenv()
    tenant = load_tenant(tenant_id)
    profile = load_profile(tenant.profile)
    policy = load_policy()
    redactor = Redactor()
    run = RunLog(kind, redactor)
    guard = PolicyGuard(policy, [tenant.base_url], profile)
    secrets = SecretProvider(tenant.secret_namespace, redactor)
    with WebSurface(guard, headless=not headed, debug_port=debug_port, slow_mo_ms=slow_mo) as surface:
        channel = ControlChannel(run) if operator else None
        console = None
        if channel:
            surface.on_human_event(channel.record_human_event)
            console = OperatorConsole(channel, console_port, debug_port).__enter__()
            print(f"Operator console: {console.url}", file=sys.stderr)
        try:
            yield Runtime(tenant, profile, policy, guard, redactor, run, secrets, surface, channel, console.url if console else "")
        finally:
            if console:
                console.__exit__(None, None, None)
            run.close()
            print(f"Evidence: {run.dir.relative_to(ROOT)}", file=sys.stderr)


def cmd_discover(a) -> int:
    from .discovery import DiscoveryAgent, ParamSpec
    from .llm import OpenAIModel

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set (add it to .env). Discovery is the only step that needs a model.")
    params = []
    for k, v in _params(a.param).items():
        val, _, desc = v.partition("::")
        params.append(ParamSpec(k, val, desc))
    model = OpenAIModel(a.model)
    with runtime("discovery", a.tenant, a.headed, not a.no_operator, a.console_port, a.debug_port, a.slow_mo) as rt:
        agent = DiscoveryAgent(surface=rt.surface, model=model, run=rt.run, guard=rt.guard, policy=rt.policy, profile=rt.profile,
                               tenant=rt.tenant, secrets=rt.secrets, channel=rt.channel, goal=a.goal, params=params,
                               capability_id=a.capability_id, vision=a.vision, console_url=rt.console_url)
        res = agent.run_discovery()
    print(json.dumps({"success": res.success, "summary": res.summary, "run_id": res.run_id, "stop_code": res.stop_code,
                      "artifact": f"artifacts/{res.capability.id}/{res.capability.version}.yaml" if res.capability else None,
                      "turns": res.steps_taken, "usage": res.usage}, indent=2))
    return 0 if res.success else 1


def _replay(artifact: Path, params: dict[str, str], a) -> int:
    from . import store
    from .overlay import apply_overlay
    from .replay import ReplayEngine, ReplayOptions

    cap = store.load(artifact)
    with runtime("replay", a.tenant, a.headed, a.escalate, a.console_port, a.debug_port, a.slow_mo) as rt:
        overlay = load_overlay(rt.tenant.overlay)
        if cap.app.profile != rt.profile.id:
            raise SystemExit(f"capability targets profile {cap.app.profile}, tenant runs {rt.profile.id}")
        cap_t, profile_t = apply_overlay(cap, rt.profile, overlay)
        if overlay:
            rt.run.event("overlay_applied", tenant=overlay.tenant, aliases=len(overlay.label_aliases))
        opts = ReplayOptions(allow_irreversible=a.allow_irreversible, escalate=a.escalate,
                             human_timeout_s=rt.policy.human_timeout_s, console_url=rt.console_url)
        result = ReplayEngine(rt.surface, cap_t, profile_t, rt.tenant, rt.guard, rt.run, rt.secrets, rt.channel, opts).run_replay(params)
    print(result.model_dump_json(indent=2, exclude={"steps"} if not a.verbose else None))
    return {"succeeded": 0, "business_outcome": 0, "failed": 1, "rejected": 2}[result.status.value]


def cmd_replay(a) -> int:
    return _replay(Path(a.artifact), _params(a.param), a)


def cmd_invoke(a) -> int:
    """Agent-facing entry point: invoke a catalogued capability by tool name with JSON args."""
    from . import store

    by_name = {c.tool_definition()["function"]["name"]: c for c in store.catalog()}
    cap = by_name.get(a.name)
    if cap is None:
        raise SystemExit(f"unknown capability {a.name!r}; see `cua catalog`")
    args = {k: str(v) for k, v in json.loads(a.args).items()}
    return _replay(store.STORE / cap.id / f"{cap.version}.yaml", args, a)


def cmd_catalog(a) -> int:
    from . import store

    load_dotenv()
    profile_rules = {}
    out = []
    for cap in store.catalog():
        if cap.app.profile not in profile_rules:
            profile_rules[cap.app.profile] = load_profile(cap.app.profile).state_rules
        d = cap.tool_definition(profile_rules[cap.app.profile])
        d["x-capability"] = {"id": cap.id, "version": cap.version, "status": cap.status.value, "irreversible": cap.has_irreversible}
        out.append(d)
    print(json.dumps(out, indent=2))
    return 0


def cmd_approve(a) -> int:
    from . import store
    from .schema import Status

    cap = store.set_status(a.artifact, Status(a.status), a.reviewer)
    print(f"{cap.id}@{cap.version} -> {cap.status.value}")
    return 0


def cmd_faults(a) -> int:
    """Demo harness only: inject runtime faults into the local target app (outside the agent's policy)."""
    tenant = load_tenant(a.tenant)
    body = {}
    for k, v in _params(a.set).items():
        body[k] = int(v) if v.isdigit() else v.lower() in ("1", "true", "yes")
    if a.reset:
        body["reset"] = True
    req = urllib.request.Request(tenant.base_url + "/__faults", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    print(urllib.request.urlopen(req).read().decode())
    return 0


def cmd_app(a) -> int:
    from target_app.server import main as app_main

    sys.argv = ["target_app", "--port", str(a.port), "--variant", a.variant]
    app_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def live(p):
        p.add_argument("--tenant", default="demo")
        p.add_argument("--headed", action="store_true", help="show the browser (needed for local human takeover)")
        p.add_argument("--debug-port", type=int, default=None, help="expose CDP for remote takeover of a headless browser")
        p.add_argument("--console-port", type=int, default=8780)
        p.add_argument("--slow-mo", type=int, default=0, help="ms delay between browser operations (for watching)")

    p = sub.add_parser("discover", help="LLM-driven discovery run that records a capability")
    live(p)
    p.add_argument("--goal", required=True, help="natural-language goal; may reference {param} names")
    p.add_argument("--param", action="append", default=[], help="name=value[::description] (repeatable)")
    p.add_argument("--capability-id", required=True, help="e.g. corelink.member.read_savings_balance")
    p.add_argument("--model", default=None, help="OpenAI model (default $OPENAI_MODEL or gpt-4.1)")
    p.add_argument("--vision", action="store_true", help="also send a masked screenshot each turn")
    p.add_argument("--no-operator", action="store_true", help="disable human escalation (stuck -> fail)")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("replay", help="deterministic replay of a capability artifact (no LLM)")
    live(p)
    p.add_argument("artifact")
    p.add_argument("--param", action="append", default=[], help="name=value (repeatable)")
    p.add_argument("--escalate", action="store_true", help="on failure/approval, hand the live session to an operator")
    p.add_argument("--allow-irreversible", action="store_true", help="permit irreversible steps (approved artifacts only)")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("invoke", help="invoke a catalogued capability by tool name (agent-facing)")
    live(p)
    p.add_argument("name")
    p.add_argument("--args", default="{}")
    p.add_argument("--escalate", action="store_true")
    p.add_argument("--allow-irreversible", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_invoke)

    p = sub.add_parser("catalog", help="list capabilities as function-calling tool definitions")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("approve", help="set an artifact's lifecycle status")
    p.add_argument("artifact")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--status", default="approved", choices=["draft", "approved", "deprecated"])
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("faults", help="(demo harness) inject faults into the local target app")
    p.add_argument("--tenant", default="demo")
    p.add_argument("--reset", action="store_true")
    p.add_argument("set", nargs="*", help="e.g. notice_once=1 busy_once=1 error_once=1 slow_ms=3000 expire_after=4")
    p.set_defaults(fn=cmd_faults)

    p = sub.add_parser("app", help="run the CoreLink stand-in target app")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--variant", default="base", choices=["base", "lakeside"])
    p.set_defaults(fn=cmd_app)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
