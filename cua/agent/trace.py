"""The discovery trace: what actually happened, step by step, in structured form.

This (not the model transcript) is what the compiler turns into a capability.
Recorded actions already carry placeholders ({{inputs.member_id}}) and secret
references, so no parameter-specific or sensitive literal survives into it.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..core.actions import Action

ValueSource = Literal["none", "literal", "param", "secret"]
OutputType = Literal["string", "decimal", "integer", "date"]
Sensitivity = Literal["public", "internal", "pii", "financial"]


class Checkpoint(BaseModel):
    """Text the model predicted would appear, *verified visible* by the runtime."""
    text: str
    frame: str | None


class OutputDecl(BaseModel):
    name: str
    type: OutputType
    sensitivity: Sensitivity


class TraceStep(BaseModel):
    index: int
    tool: str
    why: str
    status: Literal["ok", "failed", "denied", "needs_approval"]
    action: Action | None = None           # as recorded: placeholders, not values
    value_source: ValueSource = "none"
    param: str | None = None
    location_before: dict[str, str] = Field(default_factory=dict)   # frame -> path
    location_after: dict[str, str] = Field(default_factory=dict)
    checkpoint: Checkpoint | None = None
    expect_text_unverified: str | None = None
    locator_used: str | None = None
    degraded: bool = False
    risk: str | None = None
    output: OutputDecl | None = None
    dialogs: list[dict] = Field(default_factory=list)
    error: str | None = None


class DiscoveryTrace(BaseModel):
    run_id: str
    goal: str
    goal_template: str
    start_route: str
    params: dict[str, str]                 # name -> value used in this run (non-secret)
    model: str
    status: Literal["success", "business_outcome", "needs_human", "stuck", "failed", "max_steps", "timeout"]
    outcome_code: str | None = None
    success: Checkpoint | None = None
    summary: str = ""
    steps: list[TraceStep] = Field(default_factory=list)
