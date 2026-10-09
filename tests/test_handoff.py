"""Human-in-the-loop: the control lease, and real takeover of the live session over CDP."""

from __future__ import annotations

import threading

import pytest

from cua.control import ControlChannel, ControlError, Owner
from cua.discovery import DiscoveryAgent, ParamSpec
from cua.evidence import RunLog
from cua.llm import ScriptedModel, ToolCall
from cua.operator_console import OperatorConsole
from cua.redact import Redactor
from cua.result import Status
from cua.schema import Fill, ParamValue, Risk
from scripts import operator_bot as bot
from target_app import server as app
from tests.test_discovery import ref

CDP, CONSOLE = 19222, 18780
CONSOLE_URL = f"http://127.0.0.1:{CONSOLE}"


def run_operator(fn) -> threading.Thread:
    errors: list = []

    def wrapped():
        try:
            fn()
        except Exception as e:  # surface operator-side failures in the test
            errors.append(e)

    t = threading.Thread(target=wrapped, daemon=True)
    t.errors = errors
    t.start()
    return t


def test_lease_semantics(tmp_path):
    ch = ControlChannel(RunLog("unit", Redactor(), root=tmp_path, echo=False))
    ch.require_automation()
    ch.raise_intervention(kind="failure", subject="x", code="APP_ERROR", reason="boom")
    with pytest.raises(ControlError):
        ch.release("skip_step", "op")  # can't claim to have acted without holding control
    with pytest.raises(ControlError):
        ch.release("approve", "op")  # not a valid resolution for a failure
    ch.claim("op")
    assert ch.owner == Owner.human
    with pytest.raises(ControlError):
        ch.require_automation()  # automation cannot act while a human holds the session
    ch.record_human_event({"type": "click", "element": {"role": "button", "name": "Go"}})
    iv = ch.release("skip_step", "op", "did it by hand")
    assert ch.owner == Owner.automation and iv.human_actions and iv.resolution.value == "skip_step"
    assert (tmp_path / iv.run_id / "interventions" / "iv1.json").exists()


def test_replay_failure_escalates_and_human_completes_step_on_live_session(env, balance_cap):
    app.STATE.faults.error_once = True
    e = env(operator=True, debug_port=CDP)

    def operator():
        bot.wait_for_intervention(CONSOLE_URL)
        bot.claim(CONSOLE_URL)
        bot.on_live_page(CDP, bot.lookup_member("12345"))  # human redoes the lookup in the same browser
        bot.release(CONSOLE_URL, "skip_step", "host crashed; re-ran the inquiry by hand")

    with e.surface() as s:
        with OperatorConsole(e.channel, CONSOLE, CDP):
            t = run_operator(operator)
            from cua.replay import ReplayEngine, ReplayOptions

            r = ReplayEngine(s, balance_cap, e.profile, e.tenant, e.guard, e.run, e.secrets, e.channel,
                             ReplayOptions(escalate=True, human_timeout_s=60)).run_replay({"member_id": "12345"})
            t.join(10)
    assert not t.errors, t.errors
    assert r.status == Status.succeeded, r.error
    assert r.outputs == {"savings_balance": "4210.55"}  # automation resumed and extracted after the handoff
    iv = r.interventions[0]
    assert iv["kind"] == "failure" and iv["code"] == "APP_ERROR" and iv["resolution"] == "skip_step"
    kinds = [(a["type"], a["element"]["role"]) for a in iv["human_actions"]]
    assert ("click", "link") in kinds and ("change", "textbox") in kinds and ("click", "button") in kinds
    assert any(a["element"].get("value") == "{member_id}" for a in iv["human_actions"])  # captured, but redacted
    assert [st.status for st in r.steps] == ["ok", "ok", "performed_by_human", "ok"]


@pytest.mark.parametrize("decision, status", [("approve", Status.succeeded), ("deny", Status.rejected)])
def test_irreversible_step_waits_for_operator_approval(env, balance_cap, decision, status):
    cap = balance_cap.model_copy(deep=True)
    cap.steps[2].risk = Risk.irreversible  # pretend 'Go' commits something
    e = env(operator=True)

    def operator():
        iv = bot.wait_for_intervention(CONSOLE_URL)
        assert iv["kind"] == "approval" and iv["step_id"] == "s3"
        bot.release(CONSOLE_URL, decision, "checked with member")

    with e.surface() as s, OperatorConsole(e.channel, CONSOLE):
        t = run_operator(operator)
        from cua.replay import ReplayEngine, ReplayOptions

        r = ReplayEngine(s, cap, e.profile, e.tenant, e.guard, e.run, e.secrets, e.channel,
                         ReplayOptions(escalate=True, human_timeout_s=60)).run_replay({"member_id": "12345"})
        t.join(10)
    assert not t.errors, t.errors
    assert r.status == status
    if decision == "deny":
        assert r.error.code == "IRREVERSIBLE_DENIED"


def test_discovery_stuck_operator_takes_over_and_actions_are_folded_into_artifact(env, artifact_store):
    def script(screen, turn):
        s = screen.split("CURRENT SCREEN:")[1]
        if "OPERATOR took control" not in screen:
            return ToolCall("click", {"ref": ref(s, r'link "Teller Drawer"'), "reason": "looking for member search"})
        if "OUTPUTS DECLARED SO FAR: (none)" in screen:
            return ToolCall("extract", {"ref": ref(s, r'column="Balance" row="Share Savings"'), "output": "savings_balance",
                                        "type": "money", "description": "savings balance", "reason": "savings row"})
        return ToolCall("finish", {"success": True, "summary": "done with operator help", "checkpoint_refs": [ref(s, r'"MEMBER DETAIL"')]})

    e = env(kind="discovery", operator=True, debug_port=CDP)

    def operator():
        iv = bot.wait_for_intervention(CONSOLE_URL)
        assert iv["kind"] == "stuck" and iv["code"] == "NO_PROGRESS"
        bot.claim(CONSOLE_URL)
        bot.on_live_page(CDP, bot.lookup_member("12345"))
        bot.release(CONSOLE_URL, "resume", "member search is under Member Inquiry")

    with e.surface() as s, OperatorConsole(e.channel, CONSOLE, CDP):
        t = run_operator(operator)
        agent = DiscoveryAgent(surface=s, model=ScriptedModel(script), run=e.run, guard=e.guard, policy=e.policy, profile=e.profile,
                               tenant=e.tenant, secrets=e.secrets, channel=e.channel, goal="Read savings balance for {member_id}",
                               params=[ParamSpec("member_id", "12345")], capability_id="corelink.test.assisted")
        res = agent.run_discovery()
        t.join(10)
    assert not t.errors, t.errors
    assert res.success, res.summary
    cap = res.capability
    fills = [st for st in cap.steps if isinstance(st, Fill)]
    assert fills and fills[0].value == ParamValue(param="member_id")  # human's typed value mapped back to the input
    assert any(st.intent.startswith("(operator)") for st in cap.steps)
    assert cap.provenance.human_interventions == 1 and any("human" in n for n in cap.review_notes)
