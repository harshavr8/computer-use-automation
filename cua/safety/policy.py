"""Policy gate: the allowlist and risk model every action passes before it runs.

The gate is pure (no browser, no I/O), so it is trivially testable and the same
object guards discovery and replay. It answers three questions:
  1. Is this action type allowed at all?
  2. Is the place it happens (origin + route) allowed, and not explicitly denied?
  3. How risky is it: read_only, reversible, or irreversible?
Irreversible actions need an Approval; what happens without one is configurable
(block, require_confirmation -> escalate to a human, or flag-and-proceed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..core.actions import Action, ActionKind

RiskClass = Literal["read_only", "reversible", "irreversible"]
Verdict = Literal["allow", "deny", "needs_approval"]


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk: RiskClass
    action: ActionKind | None = None
    route: str | None = None               # glob: * = one segment, ** = anything
    control_text: str | None = None        # regex on the control's visible text
    reason: str


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: int
    allowed_origins: list[str]
    allow_routes: list[str]
    deny_routes: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionKind]
    default_risk: dict[ActionKind, RiskClass]
    risk_rules: list[RiskRule] = Field(default_factory=list)
    on_irreversible: Literal["block", "require_confirmation", "flag"] = "require_confirmation"

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    out = re.escape(glob).replace(r"\*\*", ".*").replace(r"\*", "[^/]+")
    return re.compile(f"^{out}$")


def route_matches(glob: str, path: str) -> bool:
    return bool(_glob_to_regex(glob).match(path))


@dataclass(frozen=True)
class Approval:
    """Who approved an irreversible action, and for which exact control/route."""
    approver: str             # "human:<operator id>" or "token:<caller-supplied id>"
    scope: str                # what was approved, e.g. "click Confirm & Open @ /members/*/subacct/review"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    risk: RiskClass
    reason: str
    location: str             # origin+path the decision was made about (no query string)

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check_location(self, url: str) -> Decision | None:
        """Deny if a URL is outside the allowlist. Returns None when fine."""
        parts = urlsplit(url)
        if parts.scheme in ("about", "data", "chrome-error"):
            return None                                   # blank/new frames, error pages
        origin, path = f"{parts.scheme}://{parts.netloc}", parts.path or "/"
        loc = origin + path
        if origin not in self.policy.allowed_origins:
            return Decision("deny", "read_only", f"origin {origin} not in allowlist", loc)
        if any(route_matches(g, path) for g in self.policy.deny_routes):
            return Decision("deny", "read_only", f"route {path} is explicitly denied", loc)
        if not any(route_matches(g, path) for g in self.policy.allow_routes):
            return Decision("deny", "read_only", f"route {path} not in allowlist", loc)
        return None

    def classify(self, action: Action, path: str, control_text: str) -> tuple[RiskClass, str]:
        for rule in self.policy.risk_rules:
            if rule.action and rule.action != action.kind:
                continue
            if rule.route and not route_matches(rule.route, path):
                continue
            if rule.control_text and not re.search(rule.control_text, control_text or ""):
                continue
            return rule.risk, rule.reason
        return self.policy.default_risk.get(action.kind, "irreversible"), "default for action type"

    def evaluate(
        self, action: Action, where_url: str, control_text: str = "",
        approval: Approval | None = None,
    ) -> Decision:
        """where_url: for navigate, the destination; otherwise the URL of the frame acted in."""
        if action.kind not in self.policy.allowed_actions:
            return Decision("deny", "irreversible", f"action type {action.kind!r} not allowed", where_url)
        denied = self.check_location(where_url)
        if denied:
            return denied
        parts = urlsplit(where_url)
        loc = f"{parts.scheme}://{parts.netloc}{parts.path}"
        risk, why = self.classify(action, parts.path or "/", control_text)
        if risk != "irreversible":
            return Decision("allow", risk, why, loc)
        if approval is not None:
            return Decision("allow", risk, f"{why}; approved by {approval.approver}", loc)
        mode = self.policy.on_irreversible
        if mode == "flag":
            return Decision("allow", risk, f"{why}; FLAGGED (policy on_irreversible=flag)", loc)
        if mode == "block":
            return Decision("deny", risk, f"{why}; irreversible actions are blocked", loc)
        return Decision("needs_approval", risk, why, loc)
