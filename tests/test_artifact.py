"""Compiler + schema: the fixture mirrors the real successful discovery run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cua.agent.trace import DiscoveryTrace
from cua.artifact.compiler import CompileError, Compiler, load_trace
from cua.artifact.profile import AppProfile
from cua.artifact.schema import Capability
from cua.artifact.store import CapabilityStore, dump_yaml, load_file

FIXTURE = Path(__file__).parent / "fixtures" / "trace_balance_success.json"
PROFILE = AppProfile.load(Path(__file__).parent.parent / "profiles" / "cu-servicing.yaml")


@pytest.fixture
def compiled():
    trace, raw = load_trace(FIXTURE)
    return Compiler(PROFILE).compile(trace, raw, "member.get_savings_balance")


def test_sign_on_is_split_into_its_own_capability(compiled):
    sign_on, cap = compiled
    assert sign_on.id == "session.sign_on" and [s.action for s in sign_on.steps] == ["navigate", "fill", "fill", "click"]
    assert {d.name for d in sign_on.secrets} == {"MOCK_USER", "MOCK_PASS"}
    assert cap.requires.session == "session.sign_on"
    assert cap.requires.start_state.text == "MBR INQ - MEMBER INQUIRY"


def test_business_capability_contract(compiled):
    cap = compiled[1]
    assert [s.id for s in cap.steps] == ["fill_member_number", "click_search", "read_savings_balance"]
    assert cap.inputs["member_id"].pattern == r"^\d+$"
    assert cap.outputs["savings_balance"].type == "decimal"
    assert cap.outputs["savings_balance"].log_policy == "redact"
    assert cap.steps[0].value == "{{inputs.member_id}}"
    assert cap.steps[1].postcondition.check.text == "MBR DTL - MEMBER DETAIL"
    assert cap.success.outputs_present == ["savings_balance"]


def test_business_outcomes_attached_to_the_submitting_step(compiled):
    cap = compiled[1]
    search = next(s for s in cap.steps if s.id == "click_search")
    assert set(search.expected_outcomes) == {"member_not_found", "access_denied", "validation_error"}
    assert {o.code for o in cap.outcomes} == set(search.expected_outcomes)
    assert all(o.source.startswith("profile:cu-servicing#") for o in cap.outcomes)


def test_artifact_contains_no_run_specific_values(compiled):
    text = dump_yaml(compiled[1])
    assert "12345" not in text                              # templated everywhere, incl. step intents
    assert "{{inputs.member_id}}" in text


def test_yaml_round_trip_is_lossless(compiled, tmp_path):
    for cap in compiled:
        p = tmp_path / f"{cap.id}.yaml"
        p.write_text(dump_yaml(cap))
        assert load_file(p).content_hash() == cap.content_hash()


def test_versions_only_bump_when_behavior_changes(compiled, tmp_path):
    store = CapabilityStore(tmp_path)
    cap = compiled[1]
    assert store.save(cap)[1] is True
    assert store.save(cap)[1] is False                      # identical -> no new version
    changed = cap.model_copy(deep=True)
    changed.steps[1].postcondition.timeout_ms = 12000
    path, created = store.save(Capability.model_validate(changed.model_dump()))
    assert created and path.name == "v2.yaml" and store.versions(cap.id) == [1, 2]


def test_tool_contract_hides_the_procedure(compiled):
    contract = compiled[1].tool_contract()
    blob = json.dumps(contract)
    assert contract["name"] == "member__get_savings_balance"
    assert contract["input_schema"]["required"] == ["member_id"]
    assert "member_not_found" in contract["returns"]["business_outcomes"]
    assert "strategies" not in blob and "anchor_text" not in blob


def _as_dict(compiled) -> dict:
    return compiled[1].model_dump(mode="json")


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["steps"][0].update(value="{{inputs.account}}"), "undeclared inputs"),
    (lambda d: d["steps"][2].update(output="nope"), "undeclared output"),
    (lambda d: d["steps"].append(dict(d["steps"][0])), "duplicate step ids"),
    (lambda d: d["steps"][1].update(expected_outcomes=["mystery"]), "undeclared outcomes"),
    (lambda d: d["steps"][0].update(value={"secret": "ROOT_PW"}), "undeclared secret"),
    (lambda d: d.update(id="Member Balance"), "pattern"),
    (lambda d: d["steps"][1].update(surprise=True), "Extra inputs"),
])
def test_schema_rejects_inconsistent_artifacts(compiled, mutate, message):
    data = _as_dict(compiled)
    mutate(data)
    with pytest.raises(ValueError, match=message):
        Capability.model_validate(data)


def test_only_successful_traces_compile():
    data = json.loads(FIXTURE.read_text())
    data["status"] = "stuck"
    with pytest.raises(CompileError, match="only successful"):
        Compiler(PROFILE).compile(DiscoveryTrace.model_validate(data), b"x", "member.get_savings_balance")


def test_sign_on_without_checkpoint_is_refused():
    data = json.loads(FIXTURE.read_text())
    data["steps"][3]["checkpoint"] = None
    with pytest.raises(CompileError, match="sign-on"):
        Compiler(PROFILE).compile(DiscoveryTrace.model_validate(data), b"x", "member.get_savings_balance")


def test_failed_steps_are_dropped():
    data = json.loads(FIXTURE.read_text())
    bad = dict(data["steps"][6], index=7, status="failed", error="TargetNotFound", output=None)
    data["steps"].insert(6, bad)
    sign_on, cap = Compiler(PROFILE).compile(DiscoveryTrace.model_validate(data), b"x", "member.get_savings_balance")
    assert len(cap.steps) == 3


def test_profile_yaml_is_valid():
    assert {c.id for c in PROFILE.business()} == {"member_not_found", "access_denied", "validation_error"}
    assert yaml.safe_load((Path(__file__).parent.parent / "profiles" / "cu-servicing.yaml").read_text())
