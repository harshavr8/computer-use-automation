"""Discovery loop, driven end-to-end in a real browser by a scripted planner.

The real run uses AnthropicPlanner; everything the loop does around the model
(verification, templating, redaction, policy, escalation) is exercised here.
"""
from __future__ import annotations

import json

import pytest

from cua.agent.loop import DiscoveryAgent, DiscoveryConfig
from cua.agent.planner import ScriptedPlanner, ToolCall, ref_of
from cua.core.actions import SecretRef
from cua.driver.web_playwright import WebPlaywrightDriver
from cua.evidence.runlog import RunLog
from cua.runtime.actuator import Actuator
from cua.safety.policy import Policy, PolicyGate
from cua.safety.redact import Redactor

pytestmark = pytest.mark.browser
SECRETS = {"MOCK_USER": "operator id", "MOCK_PASS": "password"}


@pytest.fixture
def run_agent(browser_ok, live_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    live_app.reset()
    handles = []

    def _run(script, params, goal="Look up member {member_id} and read their savings balance"):
        drv = WebPlaywrightDriver.launch(live_app.url, resolve_timeout_ms=1500)
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [live_app.url]
        redactor = Redactor()
        log = RunLog(tmp_path, f"disc-{len(handles)}", redactor)
        handles.append((drv, log))
        agent = DiscoveryAgent(drv, Actuator(drv, PolicyGate(policy), log, redactor),
                               ScriptedPlanner(script), log, redactor)
        return agent.run(DiscoveryConfig(goal_template=goal, start_route="/login", params=params,
                                         secrets=SECRETS, max_steps=15, send_screenshots=False))
    yield _run
    for drv, log in handles:
        log.close()
        drv.close()


def T(name, **kw):
    return lambda obs: ToolCall(name, kw)


def ref(name, role, label, **kw):
    return lambda obs: ToolCall(name, {"ref": ref_of(obs, role, label), "why": "test", **kw})


SIGN_ON = [
    ref("fill", "textbox", "Operator ID", secret="MOCK_USER"),
    ref("fill", "textbox", "Password", secret="MOCK_PASS"),
    ref("click", "button", "Sign On", expect_text="MBR INQ - MEMBER INQUIRY"),
]
SEARCH = [
    ref("fill", "textbox", "Member Number", param="member_id"),
    ref("click", "button", "Search", expect_text="MBR DTL - MEMBER DETAIL"),
]
EXTRACT = T("extract", output_name="savings_balance", output_type="decimal", sensitivity="financial",
            frame="main", by="table_cell", row_key="REGULAR SAVINGS", column_header="Balance", why="goal")
DONE = T("done", result="success", evidence_text="MBR DTL - MEMBER DETAIL", summary="read balance")


def test_happy_path_produces_a_clean_trace(run_agent):
    res = run_agent(SIGN_ON + SEARCH + [EXTRACT, DONE], {"member_id": "12345"})
    t = res.trace
    assert t.status == "success" and res.outputs == {"savings_balance": "$2,450.18"}
    fill_member = next(s for s in t.steps if s.param == "member_id")
    assert fill_member.action.value == "{{inputs.member_id}}"          # placeholder, not 12345
    assert [s.action.value for s in t.steps if s.value_source == "secret"] == [
        SecretRef(secret="MOCK_USER"), SecretRef(secret="MOCK_PASS")]
    search = next(s for s in t.steps if s.checkpoint and "DETAIL" in s.checkpoint.text)
    assert search.checkpoint.frame == "main"
    assert t.success.text == "MBR DTL - MEMBER DETAIL"

    evidence = "".join(p.read_text() for p in res.evidence_dir.glob("*.json*"))
    assert "2,450.18" not in evidence and "demo-only" not in evidence
    assert json.loads((res.evidence_dir / "result.json").read_text())["outputs"]["savings_balance"] == "[FINANCIAL]"


def test_literal_equal_to_input_is_recorded_as_param(run_agent):
    typed = ref("fill", "textbox", "Member Number", text="12345")
    res = run_agent(SIGN_ON + [typed, SEARCH[1], EXTRACT, DONE], {"member_id": "12345"})
    step = next(s for s in res.trace.steps if s.tool == "fill" and s.value_source != "secret")
    assert step.value_source == "param" and step.action.value == "{{inputs.member_id}}"


def test_not_found_is_a_business_outcome_with_templated_evidence(run_agent):
    done = T("done", result="business_outcome", outcome_code="member_not_found",
             evidence_text="MBR-404 NO MEMBER FOUND MATCHING 99999", summary="no such member")
    search = [SEARCH[0], ref("click", "button", "Search", expect_text="MBR INQ - MEMBER INQUIRY")]
    res = run_agent(SIGN_ON + search + [done], {"member_id": "99999"})
    assert res.trace.status == "business_outcome" and res.trace.outcome_code == "member_not_found"
    assert res.trace.success.text == "MBR-404 NO MEMBER FOUND MATCHING {{inputs.member_id}}"


def test_sensitive_evidence_is_refused(run_agent):
    bad_done = T("done", result="success", evidence_text="$2,450.18", summary="x")
    res = run_agent(SIGN_ON + SEARCH + [EXTRACT, bad_done, DONE], {"member_id": "12345"})
    assert res.trace.status == "success" and res.trace.success.text == "MBR DTL - MEMBER DETAIL"


def test_unverified_expectation_is_fed_back_not_recorded(run_agent):
    wrong = ref("click", "button", "Sign On", expect_text="WELCOME TO ONLINE BANKING")
    res = run_agent(SIGN_ON[:2] + [wrong, T("request_help", reason="confused")], {"member_id": "12345"})
    step = next(s for s in res.trace.steps if s.tool == "click")
    assert step.checkpoint is None and step.expect_text_unverified == "WELCOME TO ONLINE BANKING"


def test_irreversible_step_escalates_instead_of_clicking(run_agent, live_app):
    flow = SIGN_ON + [
        T("navigate", route="/members/12345/subacct", why="t", expect_text="SHR OPN - OPEN SUB-ACCOUNT"),
        ref("select", "combobox", "Product", option="HC"),
        ref("fill", "textbox", "Opening Deposit", text="10"),
        ref("click", "button", "Continue", expect_text="SHR OPN - REVIEW AND CONFIRM"),
        ref("click", "button", "Confirm & Open", expect_text="SUB-ACCOUNT OPENED"),
    ]
    res = run_agent(flow, {}, goal="Open a holiday club sub-account for member 12345")
    assert res.trace.status == "needs_human"
    assert res.trace.steps[-1].status == "needs_approval"
    req = json.loads((res.evidence_dir / "intervention.json").read_text())
    assert "approval" in req["reason"] and (res.evidence_dir / "intervention.png").exists()


def test_policy_denial_is_fed_back(run_agent):
    flow = SIGN_ON + [T("navigate", route="/__admin/faults", why="t", expect_text="x"),
                      T("request_help", reason="blocked")]
    res = run_agent(flow, {"member_id": "12345"})
    denied = next(s for s in res.trace.steps if s.status == "denied")
    assert "explicitly denied" in denied.error and res.trace.status == "needs_human"


def test_repeated_failures_mean_stuck(run_agent):
    bad = T("click", ref="main:e999", why="t", expect_text="x")
    res = run_agent(SIGN_ON + [bad, bad, bad], {"member_id": "12345"})
    assert res.trace.status == "stuck"
    assert (res.evidence_dir / "intervention.json").exists()


def test_extract_without_frame_searches_all_frames(run_agent):
    frameless = T("extract", output_name="savings_balance", output_type="decimal", sensitivity="financial",
                  by="table_cell", row_key="REGULAR SAVINGS", column_header="Balance", why="goal")
    res = run_agent(SIGN_ON + SEARCH + [frameless, DONE], {"member_id": "12345"})
    step = next(s for s in res.trace.steps if s.tool == "extract")
    assert res.trace.status == "success" and step.action.target.frame == "main"


def test_checkpoint_confirmed_from_the_screen_the_model_sees(run_agent):
    blind_click = ref("click", "button", "Sign On")                      # no prediction
    confirm = ref("fill", "textbox", "Member Number", param="member_id",
                  arrived_text="MBR INQ - MEMBER INQUIRY")               # confirms step 4 next turn
    res = run_agent(SIGN_ON[:2] + [blind_click, confirm, SEARCH[1], EXTRACT, DONE], {"member_id": "12345"})
    sign_on = next(s for s in res.trace.steps if s.tool == "click")
    assert sign_on.checkpoint.text == "MBR INQ - MEMBER INQUIRY" and sign_on.checkpoint.frame == "main"


def test_wrong_prediction_is_repaired_by_arrival(run_agent):
    wrong = ref("click", "button", "Sign On", expect_text="MAIN MENU")   # what the real model did
    confirm = ref("fill", "textbox", "Member Number", param="member_id", arrived_text="MBR INQ - MEMBER INQUIRY")
    res = run_agent(SIGN_ON[:2] + [wrong, confirm, SEARCH[1], EXTRACT, DONE], {"member_id": "12345"})
    step = next(s for s in res.trace.steps if s.tool == "click")
    assert step.checkpoint.text == "MBR INQ - MEMBER INQUIRY" and step.expect_text_unverified is None


def test_rejected_done_is_visible_in_the_trace(run_agent):
    bad_done = T("done", result="success", evidence_text="$2,450.18", summary="balance read")
    res = run_agent(SIGN_ON + SEARCH + [EXTRACT, bad_done, DONE], {"member_id": "12345"})
    rejected = next(s for s in res.trace.steps if s.tool == "done")
    assert rejected.status == "failed" and "sensitive" in rejected.error
    assert res.trace.status == "success"


def test_sensitive_arrival_text_is_refused(run_agent):
    leak = T("done", result="success", evidence_text="MBR DTL - MEMBER DETAIL", summary="x",
             arrived_text="JANE Q SAMPLE")
    extract_name = T("extract", output_name="member_name", output_type="string", sensitivity="pii",
                     frame="main", by="labeled_value", label="Name:", why="t")
    res = run_agent(SIGN_ON + [SEARCH[0], ref("click", "button", "Search")] + [extract_name, leak],
                    {"member_id": "12345"})
    search = next(s for s in res.trace.steps if s.tool == "click" and "Search" in s.action.describe())
    assert search.checkpoint is None                                      # name refused as checkpoint
    assert res.trace.status == "success"


def test_the_real_runs_done_call_gets_actionable_feedback(run_agent):
    """Regression for the first real run: the model offered a whole table row, balance included."""
    real = T("done", result="success", evidence_text="S00 REGULAR SAVINGS $2,450.18 OPEN", summary="x")
    row_only = T("done", result="success", evidence_text="S00 REGULAR SAVINGS OPEN", summary="x")
    res = run_agent(SIGN_ON + SEARCH + [EXTRACT, real, row_only, DONE], {"member_id": "12345"})
    first, second = [s for s in res.trace.steps if s.tool == "done"]
    assert "sensitive data value" in first.error
    assert "several table cells" in second.error
    assert res.trace.status == "success"


def test_scripted_discovery_compiles_into_valid_capabilities(run_agent, tmp_path):
    from cua.artifact.compiler import Compiler, load_trace
    from cua.artifact.profile import AppProfile
    from cua.artifact.store import CapabilityStore

    res = run_agent(SIGN_ON + SEARCH + [EXTRACT, DONE], {"member_id": "12345"})
    trace, raw = load_trace(res.evidence_dir / "trace.json")      # the redacted file on disk
    caps = Compiler(AppProfile.load("profiles/cu-servicing.yaml")).compile(trace, raw, "member.get_savings_balance")
    store = CapabilityStore(tmp_path)
    assert [store.save(c)[1] for c in caps] == [True, True]
    assert store.load("member.get_savings_balance").steps[-1].output == "savings_balance"
