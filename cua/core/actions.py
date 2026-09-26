"""The closed action vocabulary. The LLM may only choose from these; so may replay.

A small fixed vocabulary is what makes actions policy-checkable and compilable
into an artifact. Values can be literal, a reference to a secret (resolved at the
last moment, never logged), or an input-parameter placeholder filled at replay.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .targets import Target

ActionKind = Literal["navigate", "click", "fill", "select", "extract"]


class SecretRef(BaseModel):
    """Pointer to a credential in the secret store (env vars in this demo)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    secret: str = Field(description="Secret name, e.g. 'MOCK_PASS'")

    def __str__(self) -> str:
        return f"[SECRET:{self.secret}]"


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActionKind
    target: Target | None = None
    route: str | None = Field(default=None, description="navigate only: path, e.g. /members/search")
    value: str | SecretRef | None = None

    def describe(self) -> str:
        """Log-safe one-liner. Secrets render as their reference, never the value."""
        parts = [self.kind]
        if self.route:
            parts.append(self.route)
        if self.target:
            parts.append(f"<{self.target.description}>")
        if self.value is not None:
            parts.append(f"= {self.value}")
        return " ".join(parts)
