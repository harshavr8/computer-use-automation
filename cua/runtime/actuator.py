"""Actuator: the only path from "decide" to "act". Discovery and replay both use it.

    resolve target -> policy gate -> resolve secret -> perform -> verify location -> log

Putting the gate here (not in the agent, not in replay) means neither caller can
forget it or route around it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from ..core.actions import Action, SecretRef
from ..core.observation import DialogEvent
from ..core.targets import strategy_label
from ..driver.base import DriverError, Resolved, SurfaceDriver
from ..evidence.runlog import RunLog
from ..safety.policy import Approval, Decision, PolicyGate
from ..safety.redact import Redactor


class SecretStore:
    """Env-var backed for the demo. Production: a vault; the interface stays the same."""

    def get(self, ref: SecretRef) -> str:
        value = os.environ.get(ref.secret)
        if value is None:
            raise KeyError(f"secret {ref.secret!r} is not set")
        return value


@dataclass
class ActOutcome:
    status: Literal["done", "denied", "needs_approval", "left_allowlist"]
    decision: Decision
    resolved: Resolved | None = None
    extracted: str | None = None
    dialogs: list[DialogEvent] = field(default_factory=list)
    violation: Decision | None = None


class Actuator:
    def __init__(
        self, driver: SurfaceDriver, gate: PolicyGate, log: RunLog,
        redactor: Redactor, secrets: SecretStore | None = None,
    ) -> None:
        self.driver, self.gate, self.log, self.redactor = driver, gate, log, redactor
        self.secrets = secrets or SecretStore()

    def do(self, action: Action, approval: Approval | None = None, step_id: str | None = None) -> ActOutcome:
        resolved: Resolved | None = None
        if action.target is not None:
            try:
                resolved = self.driver.resolve(action.target)
            except DriverError as exc:
                self.log.event("resolve_failed", step=step_id, action=action.describe(),
                               error=type(exc).__name__, message=str(exc), detail=exc.detail)
                raise

        where = self.driver.url_for(action.route or "") if action.kind == "navigate" else resolved.frame_url  # type: ignore[union-attr]
        control_text = resolved.control_text if resolved else ""
        decision = self.gate.evaluate(action, where, control_text, approval)
        self.log.event("policy", step=step_id, action=action.describe(), verdict=decision.verdict,
                       risk=decision.risk, reason=decision.reason, location=decision.location)
        if decision.verdict == "deny":
            return ActOutcome("denied", decision, resolved)
        if decision.verdict == "needs_approval":
            return ActOutcome("needs_approval", decision, resolved)

        value: str | None = None
        if isinstance(action.value, SecretRef):
            value = self.secrets.get(action.value)
            self.redactor.register(value, "secret")
        elif action.value is not None:
            value = action.value

        extracted = self.driver.perform(action, resolved, value)
        dialogs = self.driver.drain_dialogs()
        self.log.event(
            "action", step=step_id, action=action.describe(), risk=decision.risk,
            locator=strategy_label(resolved.chosen) if resolved else None,
            locator_rank=resolved.chosen_rank if resolved else None,
            degraded=resolved.degraded if resolved else False,
            strategy_checks=[{"strategy": strategy_label(c.strategy), "matches": c.matches}
                             for c in resolved.checks] if resolved else None,
            extracted=extracted,
            dialogs=[d.__dict__ for d in dialogs],
            url_after=self.driver.frame_urls(),
        )

        for frame, url in self.driver.frame_urls().items():
            bad = self.gate.check_location(url)
            if bad:
                self.log.event("policy_violation", step=step_id, frame=frame, reason=bad.reason, location=bad.location)
                return ActOutcome("left_allowlist", decision, resolved, extracted, dialogs, violation=bad)
        return ActOutcome("done", decision, resolved, extracted, dialogs)
