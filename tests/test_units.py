"""Schema, redaction, policy and overlay units (no browser)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cua import store
from cua.config import PolicyGuard, load_overlay, load_policy, load_profile
from cua.overlay import apply_overlay
from cua.redact import Redactor
from cua.replay import parse_output, url_matches
from cua.schema import Capability, OutputType, Risk, StateRule, UrlMatches
from cua.surface.base import FrameState
from tests.conftest import FIXTURE


@pytest.fixture
def cap_dict():
    import yaml

    return yaml.safe_load(FIXTURE.read_text())


def test_fixture_round_trips(cap_dict):
    cap = Capability.model_validate(cap_dict)
    assert Capability.model_validate(__import__("yaml").safe_load(store.dump(cap))) == cap


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["steps"][1]["value"].update(param="nope"), "undeclared input"),
        (lambda d: d["steps"][3].update(output="other"), "undeclared output"),
        (lambda d: d["outputs"].append({"name": "never", "type": "string"}), "never extracted"),
        (lambda d: d["steps"][0]["target"].update(strategies=[{"kind": "css", "selector": "a"}]), "not only css"),
        (lambda d: d["steps"][1].update(value={"kind": "secret", "ref": "x/y"}), "secrets belong"),
        (lambda d: d.update(version="1.0"), "version"),
    ],
)
def test_schema_rejects_inconsistent_contracts(cap_dict, mutate, msg):
    mutate(cap_dict)
    with pytest.raises(ValidationError, match=msg):
        Capability.model_validate(cap_dict)


def test_recoverable_rule_requires_handler():
    with pytest.raises(ValidationError, match="need a handler"):
        StateRule(id="x", description="x", when={"kind": "text_present", "pattern": "x"}, classification="recoverable", code="XX")


def test_tool_definition_is_an_agent_contract(cap_dict):
    cap = Capability.model_validate(cap_dict)
    d = cap.tool_definition(load_profile("corelink-teller").state_rules)["function"]
    assert d["name"] == "corelink__member__read_savings_balance"
    assert d["parameters"]["required"] == ["member_id"]
    assert "RECORD_NOT_FOUND" in d["description"] and "savings_balance (money)" in d["description"]


def test_redactor_layers():
    r = Redactor()
    r.register("12345", "{member_id}")
    r.register("hunter22", "[SECRET:pw]")
    s = r.text("mbr=12345 pw=hunter22 ssn 123-45-6789 bal $4,210.55 ph (555) 010-2233 dob 03/12/1984 "
               "/main?sid=abcdef&x=1 card 4111111111111111 run discovery-20261009-114302")
    assert s == ("mbr={member_id} pw=[SECRET:pw] ssn [SSN] bal $[AMOUNT] ph [PHONE] dob [DATE] "
                 "/main?sid=[REDACTED]&x=1 card [PAN] run discovery-20261009-114302")


def test_pan_needs_card_shape_and_luhn():
    r = Redactor()
    assert r.text("4111-1111-1111-1111 4111111111111111") == "[PAN] [PAN]"
    assert r.text("4111111111111112") == "4111111111111112"  # fails Luhn
    # timestamps / run ids never look like cards, whatever their checksum
    for i in range(200):
        s = f"discovery-20261009-{120000 + i:06d}"
        assert r.text(s) == s


def test_persist_guard_refuses_leaky_artifact(cap_dict, tmp_path):
    cap_dict["steps"][1]["value"] = {"kind": "literal", "value": "123-45-6789"}
    cap = Capability.model_validate(cap_dict)
    with pytest.raises(store.UnsafeArtifact):
        store.save(cap, Redactor(), root=tmp_path)


def test_policy_allowlist_and_risk():
    prof = load_profile("corelink-teller")
    g = PolicyGuard(load_policy(), ["http://127.0.0.1:8765"], prof)
    assert g.url_allowed("http://127.0.0.1:8765/mbrinq?sid=x")[0]
    assert not g.url_allowed("http://127.0.0.1:8765/admin/users")[0]
    assert not g.url_allowed("http://127.0.0.1:8765/__faults")[0]
    assert not g.url_allowed("https://evil.example/mbrinq")[0]
    assert not g.url_allowed("http://127.0.0.1:8765/unlisted")[0]
    assert g.classify({"role": "button", "name": "Confirm"})[0] == Risk.irreversible
    assert g.classify({"role": "button", "name": "Save", "attrs": {"form_action": "/subacct/commit"}})[0] == Risk.irreversible
    assert g.classify({"role": "button", "name": "Transfer funds"})[0] == Risk.irreversible  # global safety net
    assert g.classify({"role": "button", "name": "Review"})[0] == Risk.safe
    with pytest.raises(Exception, match="not in the allowlist"):
        g.check_action("drag")


def test_url_matching_with_params():
    cond = UrlMatches(frame={"path": ["work"]}, path="/mbrdtl", query={"mbr": "{member_id}"})
    st = [FrameState(["work"], "http://h/mbrdtl?sid=abc&mbr=12345", "")]
    assert url_matches(cond, st, {"member_id": "12345"})
    assert not url_matches(cond, st, {"member_id": "23456"})
    assert not url_matches(cond, [FrameState(["nav"], "http://h/mbrdtl?mbr=12345", "")], {"member_id": "12345"})


@pytest.mark.parametrize("raw, t, out", [("$4,210.55", OutputType.money, "4210.55"), ("($15.00)", OutputType.money, "-15.00"),
                                         ("1,024", OutputType.integer, "1024"), (" OPEN ", OutputType.string, "OPEN")])
def test_output_parsing(raw, t, out):
    assert parse_output(raw, t) == out


def test_output_parsing_rejects_garbage():
    with pytest.raises(ValueError):
        parse_output("N/A", OutputType.money)


def test_overlay_aliases_labels_without_touching_base(cap_dict):
    cap = Capability.model_validate(cap_dict)
    prof = load_profile("corelink-teller")
    cap_t, _ = apply_overlay(cap, prof, load_overlay("overlays/lakeside.yaml"))
    assert cap_t.steps[1].target.strategies[0].label == "Member No."
    assert cap_t.steps[2].target.strategies[0].name == "Search"
    assert cap.steps[1].target.strategies[0].label == "Member #"  # base untouched
