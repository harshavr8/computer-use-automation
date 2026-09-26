"""Deterministic replay against the live mock app, including injected runtime faults."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cua.artifact.compiler import Compiler, load_trace
from cua.artifact.profile import AppProfile
from cua.artifact.store import CapabilityStore
from cua.driver.web_playwright import WebPlaywrightDriver
from cua.evidence.runlog import RunLog
from cua.replay.engine import ReplayEngine
from cua.runtime.actuator import Actuator
from cua.safety.policy import Approval, Policy, PolicyGate
from cua.safety.redact import Redactor

pytestmark = pytest.mark.browser
FIX = Path(__file__).parent / "fixtures"
PROFILE = AppProfile.load(Path(__file__).parent.parent / "profiles" / "cu-servicing.yaml")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("caps")
    s = CapabilityStore(root)
    trace, raw = load_trace(FIX / "trace_balance_success.json")
    for cap in Compiler(PROFILE).compile(trace, raw, "member.get_savings_balance"):
        s.save(cap)
    (root / "member.open_club_account").mkdir()
    shutil.copy(FIX / "member.open_club_account.v1.yaml", root / "member.open_club_account" / "v1.yaml")
    return s


@pytest.fixture
def replay(browser_ok, live_app, store, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    live_app.reset()
    opened = []

    def _run(cap_id, inputs, app=live_app, approval=None, **faults):
        if faults:
            app.faults(**faults)
        drv = WebPlaywrightDriver.launch(app.url, resolve_timeout_ms=1500)
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [app.url]
        redactor = Redactor()
        log = RunLog(tmp_path, f"replay-{len(opened)}", redactor)
        opened.append((drv, log))
        engine = ReplayEngine(drv, Actuator(drv, PolicyGate(policy), log, redactor), PROFILE, store, log, redactor)
        return engine.run(store.load(cap_id), inputs, approval=approval)
    yield _run
    for drv, log in opened:
        log.close()
        drv.close()


BAL = "member.get_savings_balance"


def evidence_text(res) -> str:
    return "".join(p.read_text(errors="ignore") for p in Path(res.evidence_dir).iterdir() if p.suffix in (".json", ".jsonl", ".txt"))


# ---- success paths --------------------------------------------------------------------------
def test_replay_returns_typed_output_without_any_llm(replay):
    res = replay(BAL, {"member_id": "12345"})
    assert res.status == "success" and res.outputs == {"savings_balance": "2450.18"}
    assert [s.id for s in res.steps] == ["open_login", "fill_operator_id", "fill_password", "click_sign_on",
                                         "fill_member_number", "click_search", "read_savings_balance"]
    assert "2450.18" not in evidence_text(res) and "2,450.18" not in evidence_text(res)


def test_same_capability_new_input(replay):
    assert replay(BAL, {"member_id": "23456"}).outputs == {"savings_balance": "15.00"}


# ---- business outcomes: legitimate answers, not crashes -----------------------------------------
def test_not_found_is_a_business_outcome(replay):
    res = replay(BAL, {"member_id": "99999"})
    assert res.status == "business_outcome" and res.outcome.code == "member_not_found"
    assert res.outcome.step == "click_search" and res.failure is None


def test_access_denied_is_a_business_outcome(replay):
    res = replay(BAL, {"member_id": "55555"})
    assert res.status == "business_outcome" and res.outcome.code == "access_denied"


def test_app_validation_returns_the_apps_message(replay):
    res = replay(BAL, {"member_id": "1234"})            # passes our pattern, the app wants 5 digits
    assert res.outcome.code == "validation_error" and res.outcome.message.startswith("ERR-104")


def test_bad_input_rejected_before_touching_the_ui(replay):
    res = replay(BAL, {"member_id": "12ab"})
    assert res.status == "failed" and res.failure.category == "input_invalid" and res.steps == []


# ---- recoverable conditions: handled, reported, status unchanged ---------------------------------
def test_notice_overlay_is_dismissed(replay):
    res = replay(BAL, {"member_id": "12345"}, notice_dialog=True)
    assert res.status == "success" and [r.condition for r in res.recoveries] == ["system_notice"]


def test_transient_app_error_is_reloaded(replay):
    res = replay(BAL, {"member_id": "12345"}, fail_next=1, fail_route="/members/12345")
    assert res.status == "success" and res.recoveries[0].condition == "transient_app_error"
    assert res.recoveries[0].action == "reload"


def test_session_expiry_resigns_on_and_restarts(replay):
    res = replay(BAL, {"member_id": "12345"}, expire_next=1)
    assert res.status == "success" and res.outputs["savings_balance"] == "2450.18"
    assert [r.condition for r in res.recoveries] == ["session_expired"]
    assert [s.id for s in res.steps].count("click_sign_on") == 2


# ---- hard failures: stop, explain, attach evidence ------------------------------------------------
def test_persistent_app_error_exhausts_recovery_with_evidence(replay):
    res = replay(BAL, {"member_id": "12345"}, fail_next=10, fail_route="/members/12345")
    f = res.failure
    assert res.status == "failed" and f.category == "recovery_exhausted" and f.step == "click_search"
    assert "APP-500" in f.observed and set(f.evidence) == {"failure.png", "failure_snapshot.txt"}
    assert (Path(res.evidence_dir) / "failure.png").exists()


def test_other_tenant_relabeling_fails_clearly_and_flags_drift(replay, live_app_b):
    live_app_b.reset()
    res = replay(BAL, {"member_id": "12345"}, app=live_app_b)
    assert res.status == "failed" and res.failure.category == "target_not_found"
    assert res.failure.step == "click_search"                       # "Search" is "Find" on tenant B
    assert any("fill_member_number" in d and "field_name" in d for d in res.drift)   # label drifted, fallback held


# ---- irreversible steps -----------------------------------------------------------------------------
CLUB = "member.open_club_account"
CLUB_IN = {"member_id": "12345", "product": "HC", "amount": "25.00"}


def test_irreversible_step_escalates_without_approval(replay):
    res = replay(CLUB, CLUB_IN)
    assert res.status == "escalated" and res.escalation.step == "click_confirm_and_open"
    req = json.loads((Path(res.evidence_dir) / res.escalation.request_file).read_text())
    assert req["completed_steps"][-1] == "click_continue"


def test_irreversible_step_runs_with_explicit_approval(replay):
    res = replay(CLUB, CLUB_IN, approval=Approval(approver="human:test", scope="open club account"))
    assert res.status == "success" and res.outputs == {"confirmation_number": "SA-100231"}


def test_business_rule_on_the_form_is_an_outcome(replay):
    res = replay(CLUB, {"member_id": "23456", "product": "HC", "amount": "100.00"})
    assert res.outcome.code == "validation_error" and "ERR-221" in res.outcome.message
    assert res.outcome.step == "click_continue"


def test_replay_code_never_imports_the_model():
    src = (Path(__file__).parent.parent / "cua" / "replay" / "engine.py").read_text()
    assert "agent" not in src and "anthropic" not in src and "planner" not in src


def test_missing_secret_is_a_config_error_not_a_crash(replay, monkeypatch):
    monkeypatch.delenv("MOCK_PASS")
    res = replay(BAL, {"member_id": "12345"})
    assert res.status == "failed" and res.failure.category == "config_error"
    assert "MOCK_PASS" in res.failure.observed and res.steps == []
