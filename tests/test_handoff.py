"""Human handoff on the same live session: lease, operator protocol, capture, re-sync."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cua.agent.loop import DiscoveryAgent, DiscoveryConfig
from cua.agent.planner import ScriptedPlanner, ToolCall, ref_of
from cua.artifact.compiler import CompileError, Compiler, load_trace
from cua.artifact.profile import AppProfile
from cua.artifact.store import CapabilityStore, load_file
from cua.control.handoff import ControlError, HandoffController, pending_handoffs, send_command
from cua.core.actions import Action
from cua.driver.web_playwright import WebPlaywrightDriver
from cua.evidence.runlog import RunLog
from cua.replay.engine import ReplayEngine
from cua.runtime.actuator import Actuator
from cua.safety.policy import Policy, PolicyGate
from cua.safety.redact import Redactor

FIX = Path(__file__).parent / "fixtures"
PROFILE = AppProfile.load(Path(__file__).parent.parent / "profiles" / "cu-servicing.yaml")
BAL, CLUB = "member.get_savings_balance", "member.open_club_account"
CLUB_IN = {"member_id": "12345", "product": "HC", "amount": "25.00"}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("caps")
    s = CapabilityStore(root)
    trace, raw = load_trace(FIX / "trace_balance_success.json")
    for cap in Compiler(PROFILE).compile(trace, raw, BAL):
        s.save(cap)
    (root / CLUB).mkdir()
    shutil.copy(FIX / f"{CLUB}.v1.yaml", root / CLUB / "v1.yaml")
    return s


@pytest.fixture
def handoff_replay(browser_ok, live_app, store, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    live_app.reset()
    opened = []

    def _run(cap_id, inputs, human=None, timeout_s=30, **faults):
        if faults:
            live_app.faults(**faults)
        drv = WebPlaywrightDriver.launch(live_app.url, resolve_timeout_ms=1500)
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [live_app.url]
        redactor = Redactor()
        log = RunLog(tmp_path, f"handoff-{len(opened)}", redactor)
        opened.append((drv, log))
        ctl = HandoffController(drv, log, redactor, timeout_s=timeout_s, poll_ms=100, simulate_human=human)
        send_command(log.dir, "claim", "tester")          # operator is already waiting at the console
        engine = ReplayEngine(drv, Actuator(drv, PolicyGate(policy), log, redactor), PROFILE, store, log,
                              redactor, handoff=ctl)
        return engine.run(store.load(cap_id), inputs), ctl
    yield _run
    for drv, log in opened:
        log.close()
        drv.close()


def human_reruns_search(ctl):
    """A person at the browser: go back to inquiry, search again, hand back."""
    page = ctl.driver.page
    page.frame(name="nav").click("text=Member Inquiry")
    page.frame(name="main").wait_for_selector("input[name=q1]")
    page.frame(name="main").fill("input[name=q1]", "12345")
    page.frame(name="main").click("input[type=submit]")
    page.frame(name="main").wait_for_selector("text=MBR DTL - MEMBER DETAIL")
    send_command(ctl.log.dir, "done", "tester", note="re-ran the search after the outage")


# ---- replay handoffs ---------------------------------------------------------------------------
@pytest.mark.browser
def test_human_fixes_an_unrecoverable_failure_and_replay_resumes(handoff_replay):
    res, ctl = handoff_replay(BAL, {"member_id": "12345"}, human=human_reruns_search,
                              fail_next=3, fail_route="/members/12345")
    assert res.status == "success" and res.outputs == {"savings_balance": "2450.18"}
    h = res.handoffs[0]
    assert h.decision == "resume" and h.operator == "tester" and h.step == "click_search"
    assert h.resumed_at_step == "read_savings_balance"          # human completed the search step
    did = [(a["kind"], a.get("text") or a.get("value")) for a in h.human_actions if a["kind"] != "navigated"]
    assert ("click", "Member Inquiry") in did and ("fill", "12345") in did and ("click", "Search") in did
    control = json.loads((Path(res.evidence_dir) / "control.json").read_text())
    assert [t["to"] for t in control["history"]] == ["awaiting_human", "human", "resuming", "automation"]


@pytest.mark.browser
def test_human_approves_irreversible_step_and_automation_performs_it(handoff_replay):
    def approve(ctl):
        send_command(ctl.log.dir, "done", "tester", approve=True, note="verified with member by phone")
    res, _ = handoff_replay(CLUB, CLUB_IN, human=approve)
    assert res.status == "success" and res.outputs == {"confirmation_number": "SA-100231"}
    h = res.handoffs[0]
    assert h.approved and h.step == "click_confirm_and_open" and h.resumed_at_step == "click_confirm_and_open"
    events = (Path(res.evidence_dir) / "events.jsonl").read_text()
    assert "approved by human:tester" in events


@pytest.mark.browser
def test_human_performs_irreversible_step_and_replay_resyncs_past_it(handoff_replay):
    def human_confirms(ctl):
        main = ctl.driver.page.frame(name="main")
        main.click("input[type=submit][value='Confirm & Open']")
        ctl.driver.page.frame(name="main").wait_for_selector("text=SUB-ACCOUNT OPENED")
        send_command(ctl.log.dir, "done", "tester")
    res, _ = handoff_replay(CLUB, CLUB_IN, human=human_confirms)
    assert res.status == "success" and res.outputs == {"confirmation_number": "SA-100231"}
    h = res.handoffs[0]
    assert not h.approved and h.resumed_at_step == "read_confirmation_number"
    assert any(a.get("text") == "Confirm & Open" for a in h.human_actions)


@pytest.mark.browser
def test_operator_abort_keeps_the_original_result(handoff_replay):
    res, _ = handoff_replay(CLUB, CLUB_IN, human=lambda ctl: send_command(ctl.log.dir, "abort", "tester",
                                                                        note="member changed their mind"))
    assert res.status == "escalated" and res.handoffs[0].decision == "abort"


@pytest.mark.browser
def test_nobody_answers_times_out(browser_ok, live_app, store, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    live_app.reset()
    drv = WebPlaywrightDriver.launch(live_app.url)
    try:
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [live_app.url]
        redactor = Redactor()
        log = RunLog(tmp_path, "timeout", redactor)
        ctl = HandoffController(drv, log, redactor, timeout_s=1, poll_ms=100)
        res = ReplayEngine(drv, Actuator(drv, PolicyGate(policy), log, redactor), PROFILE, store, log,
                           redactor, handoff=ctl).run(store.load(CLUB), CLUB_IN)
        assert res.status == "escalated" and res.handoffs[0].decision == "timeout"
        log.close()
    finally:
        drv.close()


@pytest.mark.browser
def test_automation_cannot_act_while_a_human_holds_the_session(browser_ok, live_app, tmp_path):
    drv = WebPlaywrightDriver.launch(live_app.url)
    try:
        redactor = Redactor()
        log = RunLog(tmp_path, "lease", redactor)
        ctl = HandoffController(drv, log, redactor)
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [live_app.url]
        act = Actuator(drv, PolicyGate(policy), log, redactor)
        act.control_check = ctl.assert_automation
        ctl._transition("human", "human:someone")
        with pytest.raises(ControlError, match="human:someone"):
            act.do(Action(kind="navigate", route="/login"))
        log.close()
    finally:
        drv.close()


# ---- re-sync rules (no browser) ---------------------------------------------------------------------
class ScreenOnly:
    """Fake driver: only knows which texts are visible."""
    def __init__(self, *visible: str) -> None:
        self.visible = visible

    def find_text(self, text, frame, regex=False):
        return (text, frame) if any(text in v for v in self.visible) else None


def _engine(*visible):
    return ReplayEngine(ScreenOnly(*visible), actuator=None, profile=PROFILE, store=None,  # type: ignore[arg-type]
                        log=None, redactor=Redactor())


def test_resync_resumes_after_the_newest_visible_checkpoint():
    cap = load_file(FIX / f"{CLUB}.v1.yaml")
    assert _engine("SUB-ACCOUNT OPENED")._resync(cap, failed=6, inputs=CLUB_IN) == 7                  # human finished it
    assert _engine("SHR OPN - REVIEW AND CONFIRM")._resync(cap, failed=6, inputs=CLUB_IN) == 6         # human left it for us
    assert _engine("MBR DTL - MEMBER DETAIL")._resync(cap, failed=6, inputs=CLUB_IN) == 2              # human went back: redo
    assert _engine("MBR INQ - MEMBER INQUIRY")._resync(cap, failed=6, inputs=CLUB_IN) == 0             # start state
    assert _engine("SOMETHING ELSE")._resync(cap, failed=6, inputs=CLUB_IN) is None


def test_resync_never_repeats_an_irreversible_step():
    cap = load_file(FIX / f"{CLUB}.v1.yaml")
    e = _engine("SHR OPN - REVIEW AND CONFIRM")
    e._executed_irreversible.add((CLUB, 6))
    assert e._resync(cap, failed=7, inputs=CLUB_IN) is None          # would re-click Confirm & Open: refuse


def test_operator_command_files(tmp_path):
    run = tmp_path / "replay-x"
    (run / "commands").mkdir(parents=True)
    (run / "control.json").write_text(json.dumps({"run_id": "replay-x", "state": "awaiting_human",
                                                  "holder": "nobody", "request": {"reason": "r"}}))
    assert [p["run_id"] for p in pending_handoffs(tmp_path)] == ["replay-x"]
    f = send_command(run, "claim", "harry")
    assert json.loads(f.read_text()) == {"cmd": "claim", "operator": "harry"}


# ---- discovery handoff -------------------------------------------------------------------------------
@pytest.mark.browser
def test_stuck_discovery_hands_off_and_the_trace_records_the_human(browser_ok, live_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_USER", "teller")
    monkeypatch.setenv("MOCK_PASS", "demo-only")
    live_app.reset()
    T = lambda name, **kw: (lambda obs: ToolCall(name, kw))
    R = lambda name, role, label, **kw: (lambda obs: ToolCall(name, {"ref": ref_of(obs, role, label), "why": "t", **kw}))
    script = [R("fill", "textbox", "Operator ID", secret="MOCK_USER"), R("fill", "textbox", "Password", secret="MOCK_PASS"),
              R("click", "button", "Sign On"), T("request_help", reason="not sure how to find the member"),
              T("extract", output_name="savings_balance", output_type="decimal", sensitivity="financial",
                frame="main", by="table_cell", row_key="REGULAR SAVINGS", column_header="Balance", why="goal"),
              T("done", result="success", evidence_text="MBR DTL - MEMBER DETAIL", summary="read savings_balance")]
    drv = WebPlaywrightDriver.launch(live_app.url, resolve_timeout_ms=1500)
    try:
        policy = Policy.load("policy.yaml")
        policy.allowed_origins = [live_app.url]
        redactor = Redactor()
        log = RunLog(tmp_path, "disc-handoff", redactor)
        ctl = HandoffController(drv, log, redactor, timeout_s=30, poll_ms=100, simulate_human=human_reruns_search)
        send_command(log.dir, "claim", "tester")
        agent = DiscoveryAgent(drv, Actuator(drv, PolicyGate(policy), log, redactor), ScriptedPlanner(script),
                               log, redactor, handoff=ctl)
        res = agent.run(DiscoveryConfig(goal_template="Look up member {member_id} and read their savings balance",
                                        start_route="/login", params={"member_id": "12345"},
                                        secrets={"MOCK_USER": "id", "MOCK_PASS": "pw"}, send_screenshots=False))
        log.close()
    finally:
        drv.close()
    assert res.trace.status == "success"
    human = next(s for s in res.trace.steps if s.tool == "human")
    assert any(a.get("text") == "Search" for a in human.human_actions)
    trace, raw = load_trace(res.evidence_dir / "trace.json")
    with pytest.raises(CompileError, match="human-performed"):
        Compiler(PROFILE).compile(trace, raw, BAL)
