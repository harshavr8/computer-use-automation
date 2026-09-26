"""Integration tests: the Playwright driver + actuator against the live mock app."""
from __future__ import annotations

import json

import pytest

from cua.core.actions import Action, SecretRef
from cua.core.targets import (
    AnchorTextStrategy, FieldNameStrategy, RoleStrategy, TableCellStrategy, Target,
)
from cua.driver.base import TargetAmbiguous, TargetNotFound
from cua.driver.web_playwright import WebPlaywrightDriver
from cua.evidence.runlog import RunLog
from cua.runtime.actuator import Actuator
from cua.safety.policy import Approval, Policy, PolicyGate
from cua.safety.redact import SCREENSHOT_MASKS, Redactor

pytestmark = pytest.mark.browser

OPERATOR = Target(description="Operator ID box", strategies=(
    AnchorTextStrategy(text="Operator ID", control="textbox"), FieldNameStrategy(name="f1")))
PASSWORD = Target(description="Password box", strategies=(
    AnchorTextStrategy(text="Password", control="textbox"), FieldNameStrategy(name="f2")))
SIGN_ON = Target(description="Sign On button", strategies=(RoleStrategy(role="button", name="Sign On"),))
MEMBER_BOX = Target(description="Member Number box", frame="main", strategies=(
    AnchorTextStrategy(text="Member Number", control="textbox"), FieldNameStrategy(name="q1")))
SEARCH = Target(description="Search button", frame="main", strategies=(RoleStrategy(role="button", name="Search"),))
SAVINGS_BAL = Target(description="Savings balance cell", frame="main", strategies=(
    TableCellStrategy(row_key="REGULAR SAVINGS", column_header="Balance"),))


@pytest.fixture
def rig(browser_ok, live_app, tmp_path, monkeypatch):
    live_app.reset()
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    drv = WebPlaywrightDriver.launch(live_app.url, resolve_timeout_ms=2000)
    policy = Policy.load("policy.yaml")
    policy.allowed_origins = [live_app.url]
    redactor = Redactor()
    log = RunLog(tmp_path, "test-run", redactor)
    act = Actuator(drv, PolicyGate(policy), log, redactor)
    yield drv, act, log
    log.close()
    drv.close()


def sign_in(act: Actuator) -> None:
    act.do(Action(kind="navigate", route="/login"))
    act.do(Action(kind="fill", target=OPERATOR, value=SecretRef(secret="MOCK_USER")))
    act.do(Action(kind="fill", target=PASSWORD, value=SecretRef(secret="MOCK_PASS")))
    act.do(Action(kind="click", target=SIGN_ON))


def open_member(act: Actuator, member_id: str) -> None:
    act.do(Action(kind="fill", target=MEMBER_BOX, value=member_id))
    act.do(Action(kind="click", target=SEARCH))


def test_snapshot_does_not_cross_iframes_so_observe_walks_frames(rig):
    drv, act, _ = rig
    sign_in(act)
    obs = drv.observe()
    top = next(f for f in obs.frames if f.name is None)
    main = next(f for f in obs.frames if f.name == "main")
    assert "Member Number" not in top.snapshot and "Member Number" in main.snapshot


def test_unlabeled_textbox_gets_anchor_and_field_candidates(rig):
    drv, act, _ = rig
    sign_in(act)
    box = next(e for f in drv.observe().frames if f.name == "main" for e in f.elements if e.role == "textbox")
    assert box.name == ""                       # no accessible name: role+name is useless here
    kinds = [s.by for s in box.candidates]
    assert kinds == ["anchor_text", "field_name"]


def test_search_and_extract_balance_from_table(rig):
    drv, act, _ = rig
    sign_in(act)
    open_member(act, "12345")
    assert act.do(Action(kind="extract", target=SAVINGS_BAL)).extracted == "$2,450.18"


def test_not_found_message_is_detectable(rig):
    drv, act, _ = rig
    sign_in(act)
    open_member(act, "99999")
    assert drv.wait_for_text("MBR-404", "main", 2000)


def test_disagreeing_strategies_raise_ambiguity(rig):
    drv, act, _ = rig
    sign_in(act)
    bad = Target(description="conflicting", frame="main", strategies=(
        RoleStrategy(role="button", name="Search"), FieldNameStrategy(name="q1")))
    with pytest.raises(TargetAmbiguous):
        drv.resolve(bad)


def test_missing_target_raises_not_found_with_detail(rig):
    drv, act, _ = rig
    sign_in(act)
    ghost = Target(description="ghost", frame="main", strategies=(RoleStrategy(role="button", name="Nope"),))
    with pytest.raises(TargetNotFound) as exc:
        drv.resolve(ghost, timeout_ms=300)
    assert exc.value.detail["checks"][0][1] == 0


def test_native_confirm_is_dismissed_unless_expected(rig, live_app):
    drv, act, _ = rig
    live_app.faults(native_confirm=True)
    sign_in(act)
    act.do(Action(kind="navigate", route="/members/12345/subacct"))
    act.do(Action(kind="select", target=Target(description="Product", strategies=(
        AnchorTextStrategy(text="Product", control="combobox"),)), value="HC"))
    act.do(Action(kind="fill", target=Target(description="Deposit", strategies=(
        AnchorTextStrategy(text="Opening Deposit", control="textbox"),)), value="10"))
    act.do(Action(kind="click", target=Target(description="Continue", strategies=(
        RoleStrategy(role="button", name="Continue"),))))
    confirm = Target(description="Confirm & Open", strategies=(RoleStrategy(role="button", name="Confirm & Open"),))
    approval = Approval(approver="human:test", scope="confirm open")

    out = act.do(Action(kind="click", target=confirm), approval=approval)
    assert out.dialogs and out.dialogs[0].handled == "dismissed" and not out.dialogs[0].expected
    assert not drv.text_visible("SA-100231")          # the click "worked" but nothing happened

    drv.expect_dialog("Open this sub-account", accept=True)
    out = act.do(Action(kind="click", target=confirm), approval=approval)
    assert out.dialogs[0].handled == "accepted" and drv.wait_for_text("SA-100231", None, 3000)


def test_gate_blocks_admin_and_holds_irreversible(rig):
    drv, act, _ = rig
    sign_in(act)
    before = drv.page.url
    out = act.do(Action(kind="navigate", route="/__admin/faults"))
    assert out.status == "denied" and drv.page.url == before

    act.do(Action(kind="navigate", route="/members/12345/subacct"))
    act.do(Action(kind="select", target=Target(description="Product", strategies=(
        AnchorTextStrategy(text="Product", control="combobox"),)), value="HC"))
    act.do(Action(kind="fill", target=Target(description="Deposit", strategies=(
        AnchorTextStrategy(text="Opening Deposit", control="textbox"),)), value="10"))
    act.do(Action(kind="click", target=Target(description="Continue", strategies=(
        RoleStrategy(role="button", name="Continue"),))))
    out = act.do(Action(kind="click", target=Target(description="Confirm & Open", strategies=(
        RoleStrategy(role="button", name="Confirm & Open"),))))
    assert out.status == "needs_approval" and "/subacct/review" in drv.page.url


def test_tenant_relabel_falls_back_to_field_name_and_flags_drift(browser_ok, live_app_b):
    live_app_b.reset()
    drv = WebPlaywrightDriver.launch(live_app_b.url, resolve_timeout_ms=1000)
    try:
        drv.page.goto(live_app_b.url + "/login")
        drv.resolve(OPERATOR).handle.fill("teller")
        drv.resolve(PASSWORD).handle.fill("demo-only")
        drv.perform(Action(kind="click", target=SIGN_ON), drv.resolve(SIGN_ON), None)
        r = drv.resolve(MEMBER_BOX)                 # label is "Account #" on this tenant
        assert r.chosen.by == "field_name" and r.chosen_rank == 1 and r.degraded
    finally:
        drv.close()


def test_logs_never_contain_secrets_or_raw_amounts(rig):
    drv, act, log = rig
    sign_in(act)
    open_member(act, "12345")
    act.do(Action(kind="extract", target=SAVINGS_BAL))
    raw = (log.dir / "events.jsonl").read_text()
    assert "demo-only" not in raw and "2,450.18" not in raw
    assert "[SECRET:MOCK_PASS]" in raw and "[AMOUNT]" in raw
    assert all(json.loads(line)["kind"] for line in raw.splitlines())


def test_screenshot_masks_sensitive_text(rig, tmp_path):
    drv, act, _ = rig
    sign_in(act)
    open_member(act, "12345")
    masked = drv.screenshot(tmp_path / "masked.png", SCREENSHOT_MASKS)
    plain = drv.screenshot(tmp_path / "plain.png", [])
    assert masked.stat().st_size > 0 and masked.read_bytes() != plain.read_bytes()
