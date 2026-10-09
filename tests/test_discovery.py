"""Discovery loop -> recorder -> artifact -> replay round trip, with a scripted stand-in for the LLM.

The scripted model reads the same rendered screen a real model gets and picks refs by pattern, so this
exercises the real loop, policy checks, record-time locator validation and artifact assembly. The
real-model run lives in /evidence (see README); this test exists so the pipeline is checked in CI
without an API key.
"""

from __future__ import annotations

import re

import pytest

from cua import store
from cua.discovery import DiscoveryAgent, ParamSpec
from cua.llm import ScriptedModel, ToolCall
from cua.redact import fingerprint
from cua.result import Status
from cua.schema import Extract, Fill, ParamValue, TableCell
from target_app import server as app


def ref(screen: str, pattern: str) -> str:
    for line in screen.splitlines():
        if re.search(pattern, line):
            return re.search(r"\[(f\d+:\d+)\]", line).group(1)
    raise AssertionError(f"no element matching {pattern!r} in:\n{screen}")


def balance_script(screen: str, turn: int) -> ToolCall:
    s = screen.split("CURRENT SCREEN:")[1]
    steps = {
        1: lambda: ToolCall("click", {"ref": ref(s, r'link "Member Inquiry"'), "reason": "open member inquiry"}),
        2: lambda: ToolCall("fill", {"ref": ref(s, r'textbox label="Member #"'), "param": "member_id", "reason": "enter member"}),
        3: lambda: ToolCall("click", {"ref": ref(s, r'button "Go"'), "reason": "search"}),
        4: lambda: ToolCall("extract", {"ref": ref(s, r'column="Balance" row="Share Savings"'), "output": "savings_balance",
                                        "type": "money", "description": "Share Savings balance", "reason": "savings row"}),
        5: lambda: ToolCall("finish", {"success": True, "summary": "read balance", "checkpoint_refs": [ref(s, r'"MEMBER DETAIL"')],
                                       "capability_title": "Read savings balance", "capability_description": "Return a member's savings balance."}),
    }
    return steps[turn]()


def make_agent(e, surface, script, goal="Look up member 12345 and read their savings balance", capability_id="corelink.test.read_balance"):
    return DiscoveryAgent(surface=surface, model=ScriptedModel(script), run=e.run, guard=e.guard, policy=e.policy, profile=e.profile,
                          tenant=e.tenant, secrets=e.secrets, channel=e.channel, goal=goal,
                          params=[ParamSpec("member_id", "12345", "5-digit member number")], capability_id=capability_id)


def test_discovery_records_a_replayable_capability(env, artifact_store):
    e = env(kind="discovery")
    with e.surface() as s:
        res = make_agent(e, s, balance_script).run_discovery()
    assert res.success, res.summary
    cap = res.capability
    # contract
    assert [i.name for i in cap.inputs] == ["member_id"] and cap.inputs[0].sensitivity.value == "pii"
    assert [(o.name, o.type.value) for o in cap.outputs] == [("savings_balance", "money")]
    # steps: input parameterised, targets validated & multi-strategy, extraction by grid coordinates
    fill = next(st for st in cap.steps if isinstance(st, Fill))
    assert fill.value == ParamValue(param="member_id")
    assert [x.kind for x in fill.target.strategies] == ["label", "css"]
    ext = next(st for st in cap.steps if isinstance(st, Extract))
    assert isinstance(ext.target.strategies[0], TableCell)
    go = cap.steps[2]
    assert go.expect[0].path == "/mbrdtl" and go.expect[0].query == {"mbr": "{member_id}"}
    # the model never saw the member number; the artifact never stored it
    assert "12345" not in (e.run.dir / "llm_transcript.jsonl").read_text()
    assert "12345" not in store.dump(cap)
    # round trip: replay without the model, with a different input
    r = env().replay(store.load(artifact_store / cap.id / "1.0.0.yaml"), {"member_id": "23456"})
    assert r.status == Status.succeeded and r.outputs == {"savings_balance": "15.00"}
    # and the same input yields the same value as discovery saw (compared by fingerprint)
    r2 = env().replay(cap, {"member_id": "12345"})
    disc_fp = [l for l in (e.run.dir / "events.jsonl").read_text().splitlines() if '"extracted"' in l][0]
    assert fingerprint(r2.outputs["savings_balance"]) in disc_fp


def test_policy_blocks_navigation_outside_allowlist(env, artifact_store):
    def script(screen, turn):
        s = screen.split("CURRENT SCREEN:")[1]
        if turn == 1:
            return ToolCall("click", {"ref": ref(s, r'link "System Admin"'), "reason": "explore"})
        assert "REJECTED (NAVIGATION_NOT_ALLOWED" in screen
        return ToolCall("finish", {"success": False, "summary": "blocked"})

    e = env(kind="discovery")
    with e.surface() as s:
        res = make_agent(e, s, script).run_discovery()
    assert not res.success and res.stop_code == "AGENT_GAVE_UP"


def test_sensitive_literals_are_refused(env, artifact_store):
    def script(screen, turn):
        s = screen.split("CURRENT SCREEN:")[1]
        if turn == 1:
            return ToolCall("click", {"ref": ref(s, r'link "Member Inquiry"'), "reason": "open"})
        if turn == 2:
            return ToolCall("fill", {"ref": ref(s, r'textbox label="Member #"'), "text": "123-45-6789", "reason": "ssn"})
        assert "looks sensitive" in screen
        return ToolCall("finish", {"success": False, "summary": "stop"})

    e = env(kind="discovery")
    with e.surface() as s:
        assert not make_agent(e, s, script).run_discovery().success


def test_no_progress_without_operator_stops_as_stuck(env, artifact_store):
    def script(screen, turn):
        s = screen.split("CURRENT SCREEN:")[1]
        return ToolCall("click", {"ref": ref(s, r'link "Teller Drawer"'), "reason": "loop"})

    e = env(kind="discovery")
    with e.surface() as s:
        res = make_agent(e, s, script).run_discovery()
    assert not res.success and res.stop_code == "NO_PROGRESS"


def test_host_business_state_is_shown_to_the_model(env, artifact_store):
    seen = {}

    def script(screen, turn):
        s = screen.split("CURRENT SCREEN:")[1]
        if turn == 1:
            return ToolCall("click", {"ref": ref(s, r'link "Member Inquiry"'), "reason": "open"})
        if turn == 2:
            return ToolCall("fill", {"ref": ref(s, r'textbox label="Member #"'), "param": "member_id", "reason": "enter"})
        if turn == 3:
            return ToolCall("click", {"ref": ref(s, r'button "Go"'), "reason": "search"})
        seen["host"] = re.search(r"HOST STATE: (\S+)", screen).group(1)
        return ToolCall("finish", {"success": False, "summary": "member not found"})

    e = env(kind="discovery")
    with e.surface() as s:
        agent = make_agent(e, s, script)
        agent.params["member_id"] = "99999"
        res = agent.run_discovery()
    assert not res.success and seen["host"] == "RECORD_NOT_FOUND"
