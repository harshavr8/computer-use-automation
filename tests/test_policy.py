from pathlib import Path

import pytest

from cua.core.actions import Action
from cua.core.targets import RoleStrategy, Target
from cua.safety.policy import Approval, Policy, PolicyGate, route_matches

BASE = "http://127.0.0.1:5001"
BTN = Target(description="button", strategies=(RoleStrategy(role="button", name="x"),))


@pytest.fixture
def gate():
    return PolicyGate(Policy.load(Path(__file__).parent.parent / "policy.yaml"))


def click():
    return Action(kind="click", target=BTN)


def test_route_globs():
    assert route_matches("/members/*", "/members/12345")
    assert not route_matches("/members/*", "/members/12345/subacct")
    assert route_matches("/__admin/**", "/__admin/faults")


def test_read_is_allowed(gate):
    d = gate.evaluate(Action(kind="navigate", route="/members/search"), BASE + "/members/search")
    assert d.allowed and d.risk == "read_only"


def test_admin_route_denied_even_though_same_origin(gate):
    d = gate.evaluate(Action(kind="navigate", route="/__admin/faults"), BASE + "/__admin/faults")
    assert d.verdict == "deny" and "explicitly denied" in d.reason


def test_foreign_origin_denied(gate):
    d = gate.evaluate(Action(kind="navigate", route="/"), "https://evil.example/")
    assert d.verdict == "deny" and "origin" in d.reason


def test_unlisted_route_denied(gate):
    d = gate.evaluate(click(), BASE + "/reports/export")
    assert d.verdict == "deny" and "not in allowlist" in d.reason


def test_confirm_on_review_needs_approval(gate):
    d = gate.evaluate(click(), BASE + "/members/12345/subacct/review", control_text="Confirm & Open")
    assert d.verdict == "needs_approval" and d.risk == "irreversible"


def test_approval_unlocks_irreversible(gate):
    d = gate.evaluate(click(), BASE + "/members/12345/subacct/review", "Confirm & Open",
                      approval=Approval(approver="human:op1", scope="confirm open"))
    assert d.allowed and "human:op1" in d.reason


def test_continue_button_is_reversible(gate):
    d = gate.evaluate(click(), BASE + "/members/12345/subacct", control_text="Continue")
    assert d.allowed and d.risk == "reversible"


def test_block_mode(gate):
    gate.policy.on_irreversible = "block"
    d = gate.evaluate(click(), BASE + "/members/1/subacct/review", "Confirm & Open")
    assert d.verdict == "deny"


def test_disallowed_action_type(gate):
    gate.policy.allowed_actions = ["navigate", "extract"]
    assert gate.evaluate(click(), BASE + "/members/search").verdict == "deny"
