"""The replay result contract: what an AI agent gets back when it invokes a capability.

Four statuses, never ambiguous:
  success           - outputs present and typed; success check verified
  business_outcome  - the app legitimately answered "no" (not found, not authorized,
                      validation); `outcome.code` is one the capability declared
  failed            - a hard failure; `failure` says which step, what was expected,
                      what was observed, and where the evidence is
  escalated         - the run needs a human (e.g. an irreversible step without approval)
Recoveries (dismissed notices, reloaded transient errors, re-sign-on) are reported
but do not change the status: the caller asked for a balance, not for app weather.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FailureCategory = Literal[
    "config_error", "input_invalid", "precondition_not_met", "session_failed", "target_not_found", "target_ambiguous",
    "postcondition_not_met", "app_error", "recovery_exhausted", "policy_violation",
    "success_check_failed", "output_parse_error",
]


class Outcome(BaseModel):
    code: str
    message: str | None = None          # app's own message, when the capability captures it
    step: str


class Failure(BaseModel):
    category: FailureCategory
    step: str | None
    expected: str
    observed: str
    detail: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class Escalation(BaseModel):
    reason: str
    step: str | None
    request_file: str


class RecoveryRecord(BaseModel):
    condition: str
    step: str | None
    action: str
    attempt: int


class StepRecord(BaseModel):
    id: str
    capability: str
    status: Literal["ok", "failed"]
    locator: str | None = None
    locator_rank: int | None = None
    degraded: bool = False
    duration_ms: int = 0


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    version: int
    status: Literal["success", "business_outcome", "failed", "escalated"]
    outputs: dict[str, str] = Field(default_factory=dict)
    outcome: Outcome | None = None
    failure: Failure | None = None
    escalation: Escalation | None = None
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    drift: list[str] = Field(default_factory=list)       # degraded locators: works today, review soon
    steps: list[StepRecord] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str = ""
