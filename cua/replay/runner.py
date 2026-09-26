"""Wiring for CLI replay and the scenario suite (evidence generation)."""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..artifact.profile import AppProfile
from ..artifact.store import CapabilityStore
from ..driver.web_playwright import WebPlaywrightDriver
from ..evidence.runlog import RunLog, new_run_id
from ..runtime.actuator import Actuator
from ..safety.policy import Approval, Policy, PolicyGate
from ..safety.redact import Redactor
from .engine import ReplayEngine
from .result import ReplayResult


@dataclass
class ReplayConfig:
    base_url: str = "http://127.0.0.1:5001"
    policy: str = "policy.yaml"
    profile: str = "profiles/cu-servicing.yaml"
    capabilities: str = "capabilities"
    evidence_dir: str = "evidence"
    headed: bool = False


def replay_once(cfg: ReplayConfig, cap_id: str, inputs: dict[str, str], version: int | None = None,
                approval: Approval | None = None, label: str = "replay") -> ReplayResult:
    store = CapabilityStore(cfg.capabilities)
    redactor = Redactor()
    log = RunLog(Path(cfg.evidence_dir), new_run_id(label), redactor)
    driver = WebPlaywrightDriver.launch(cfg.base_url, headless=not cfg.headed)
    try:
        actuator = Actuator(driver, PolicyGate(Policy.load(cfg.policy)), log, redactor)
        engine = ReplayEngine(driver, actuator, AppProfile.load(cfg.profile), store, log, redactor)
        return engine.run(store.load(cap_id, version), inputs, approval=approval)
    finally:
        log.close()
        driver.close()


def _admin(base_url: str, path: str, body: dict | None = None) -> None:
    req = urllib.request.Request(base_url + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).close()


@dataclass
class Scenario:
    name: str
    capability: str
    inputs: dict[str, str]
    faults: dict = field(default_factory=dict)
    expect: str = "success"
    note: str = ""


SCENARIOS: list[Scenario] = [
    Scenario("happy-path", "member.get_savings_balance", {"member_id": "12345"}, note="recorded input"),
    Scenario("new-input", "member.get_savings_balance", {"member_id": "23456"}, note="same capability, different member"),
    Scenario("not-found", "member.get_savings_balance", {"member_id": "99999"}, expect="business_outcome",
             note="MBR-404 -> member_not_found"),
    Scenario("access-denied", "member.get_savings_balance", {"member_id": "55555"}, expect="business_outcome",
             note="SEC-403 -> access_denied"),
    Scenario("app-validation", "member.get_savings_balance", {"member_id": "1234"}, expect="business_outcome",
             note="app rejects 4 digits (ERR-104), message returned"),
    Scenario("bad-input", "member.get_savings_balance", {"member_id": "12ab"}, expect="failed",
             note="rejected by the input contract before touching the UI"),
    Scenario("notice-dialog", "member.get_savings_balance", {"member_id": "12345"}, {"notice_dialog": True},
             note="overlay dismissed (recoverable)"),
    Scenario("transient-500", "member.get_savings_balance", {"member_id": "12345"},
             {"fail_next": 1, "fail_route": "/members/12345"}, note="error page reloaded (recoverable)"),
    Scenario("session-expired", "member.get_savings_balance", {"member_id": "12345"}, {"expire_next": 1},
             note="re-sign-on and restart (recoverable)"),
    Scenario("persistent-500", "member.get_savings_balance", {"member_id": "12345"},
             {"fail_next": 10, "fail_route": "/members/12345"}, expect="failed",
             note="recovery exhausted -> hard failure with evidence"),
]


def run_scenarios(cfg: ReplayConfig, names: list[str] | None = None) -> list[tuple[Scenario, ReplayResult]]:
    out = []
    for sc in SCENARIOS:
        if names and sc.name not in names:
            continue
        _admin(cfg.base_url, "/__admin/reset")                 # harness, not the agent
        if sc.faults:
            _admin(cfg.base_url, "/__admin/faults", sc.faults)
        out.append((sc, replay_once(cfg, sc.capability, sc.inputs, label=f"replay-{sc.name}")))
    _admin(cfg.base_url, "/__admin/reset")
    return out
