"""The capability artifact: a typed, versioned, reviewable contract for one UI flow.

Two audiences read it:
  - a calling AI agent: id, description, inputs, outputs, and the outcomes it may
    get back (the contract, exportable as a tool definition);
  - a human reviewer: every step's intent, how each control is found and why, what
    must be true after it, and compiler notes on anything that deserves a second look.

Deliberate split: *business outcomes* the caller can receive are part of the
capability (copied from the app profile so the artifact is self-describing), while
*recoveries* for app weather (notices, transient errors, expired sessions) live in
the shared app profile - they are operational, not part of what the caller is promised.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.actions import ActionKind, SecretRef
from ..core.targets import Target

SCHEMA_VERSION = "1.0"
RiskClass = Literal["read_only", "reversible", "irreversible"]
ValueType = Literal["string", "integer", "decimal", "date"]
Sensitivity = Literal["public", "internal", "pii", "financial"]
_TEMPLATE = re.compile(r"\{\{inputs\.([A-Za-z_][A-Za-z0-9_]*)\}\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextCondition(_Strict):
    """Visible text, optionally in a named frame ('*' = any). Surface-neutral on purpose:
    'what the operator sees' works for web, legacy framesets, and desktop apps alike."""
    text: str
    frame: str | None = None
    regex: bool = False


class InputSpec(_Strict):
    type: ValueType
    description: str
    pattern: str | None = None
    sensitivity: Sensitivity = "internal"


class OutputSpec(_Strict):
    type: ValueType
    description: str
    sensitivity: Sensitivity
    parse: Literal["none", "number", "currency"] = "none"

    @property
    def log_policy(self) -> str:
        return "redact" if self.sensitivity in ("pii", "financial") else "plain"


class SecretDecl(_Strict):
    name: str
    purpose: str


class Postcondition(_Strict):
    """What must be visible after a step. Replay waits on this, not on time."""
    check: TextCondition
    timeout_ms: int = 8000


class Step(_Strict):
    id: str
    intent: str = Field(description="Why this step exists, in operator language")
    action: ActionKind
    target: Target | None = None
    route: str | None = None
    value: str | SecretRef | None = None
    postcondition: Postcondition | None = None
    expected_outcomes: list[str] = Field(default_factory=list,
                                         description="Business outcomes that may legitimately appear after this step")
    risk: RiskClass
    output: str | None = Field(default=None, description="extract only: which declared output this fills")


class OutcomeSpec(_Strict):
    """A legitimate non-success result the caller must handle (not a crash)."""
    code: str
    description: str
    detect: TextCondition
    capture_message: bool = False
    source: str = Field(description="Where the detector came from, e.g. profile:cu-servicing#member_not_found")


class Requires(_Strict):
    session: str | None = Field(default=None, description="Capability that establishes the session")
    start_state: TextCondition | None = Field(default=None, description="Must be visible before step 1")


class Success(_Strict):
    check: TextCondition
    outputs_present: list[str] = Field(default_factory=list)


class AppRef(_Strict):
    profile: str
    product: str
    variant: str = "base"


class ReviewNote(_Strict):
    severity: Literal["info", "warn"]
    step: str | None = None
    note: str


class Provenance(_Strict):
    recorded_from_run: str
    model: str
    compiled_at: datetime
    compiler_version: str
    source_trace_sha256: str


class Capability(_Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: int = 1
    status: Literal["draft", "approved", "retired"] = "draft"
    title: str
    description: str
    app: AppRef
    risk_class: RiskClass
    requires: Requires = Field(default_factory=Requires)
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    secrets: list[SecretDecl] = Field(default_factory=list)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    success: Success
    review: list[ReviewNote] = Field(default_factory=list)
    provenance: Provenance

    # ---- integrity: an artifact that loads is internally consistent ------------------
    @model_validator(mode="after")
    def _consistent(self) -> "Capability":
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")

        used_inputs: set[str] = set()
        texts = [self.success.check.text] + [o.detect.text for o in self.outcomes]
        if self.requires.start_state:
            texts.append(self.requires.start_state.text)
        for s in self.steps:
            if isinstance(s.value, str):
                texts.append(s.value)
            if s.postcondition:
                texts.append(s.postcondition.check.text)
        for t in texts:
            used_inputs |= set(_TEMPLATE.findall(t))
        unknown = used_inputs - set(self.inputs)
        if unknown:
            raise ValueError(f"templates reference undeclared inputs: {sorted(unknown)}")

        secret_names = {d.name for d in self.secrets}
        for s in self.steps:
            if isinstance(s.value, SecretRef) and s.value.secret not in secret_names:
                raise ValueError(f"step {s.id} uses undeclared secret {s.value.secret!r}")
            if s.action == "extract" and s.output not in self.outputs:
                raise ValueError(f"step {s.id} extracts into undeclared output {s.output!r}")
            if s.action in ("click", "fill", "select", "extract") and s.target is None:
                raise ValueError(f"step {s.id} ({s.action}) needs a target")
            missing = set(s.expected_outcomes) - {o.code for o in self.outcomes}
            if missing:
                raise ValueError(f"step {s.id} expects undeclared outcomes {sorted(missing)}")

        produced = {s.output for s in self.steps if s.output}
        if set(self.outputs) - produced:
            raise ValueError(f"outputs never produced by a step: {sorted(set(self.outputs) - produced)}")
        if set(self.success.outputs_present) - set(self.outputs):
            raise ValueError("success.outputs_present names undeclared outputs")
        return self

    # ---- identity / contract ----------------------------------------------------------
    def content_hash(self) -> str:
        """Hash of what the capability *does* - ignores version, status, review, provenance."""
        body = self.model_dump(mode="json", exclude={"version", "status", "review", "provenance"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def tool_contract(self) -> dict:
        """What a calling agent sees: a function-calling style definition. No steps, no locators."""
        json_type = {"string": "string", "integer": "integer", "decimal": "string", "date": "string"}
        props = {}
        for name, spec in self.inputs.items():
            prop: dict = {"type": json_type[spec.type], "description": spec.description}
            if spec.pattern:
                prop["pattern"] = spec.pattern
            props[name] = prop
        return {
            "name": self.id.replace(".", "__"),
            "description": self.description,
            "input_schema": {"type": "object", "properties": props, "required": sorted(self.inputs),
                             "additionalProperties": False},
            "returns": {
                "status": ["success", "business_outcome", "failed", "escalated"],
                "outputs": {n: {"type": o.type, "sensitivity": o.sensitivity} for n, o in self.outputs.items()},
                "business_outcomes": {o.code: o.description for o in self.outcomes},
            },
            "risk_class": self.risk_class,
            "status": self.status,
            "version": self.version,
        }
