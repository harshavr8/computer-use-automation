"""App profile: shared, human-reviewed knowledge about one vendor product.

Discovery sees one happy path; it cannot learn about session timeouts or the
"no such member" screen unless it happened to hit them. That knowledge belongs to
the *product*, is the same for every capability and every tenant running that
product, and is written/reviewed once here. The compiler copies the relevant
business outcomes into each capability; replay reads the recoveries at runtime.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..core.targets import Target
from .schema import TextCondition


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recovery(_Strict):
    kind: Literal["retry_step", "click", "resign_on"]
    target: Target | None = None           # for click
    max_attempts: int = 2
    backoff_ms: int = 500


class KnownCondition(_Strict):
    id: str
    category: Literal["business", "recoverable", "hard"]
    description: str
    detect: TextCondition
    on_routes: list[str] = Field(default_factory=list, description="Screens where it can appear; empty = anywhere")
    result_code: str | None = None         # business
    capture_message: bool = False          # business: return the app's message text to the caller
    recovery: Recovery | None = None       # recoverable


class SessionSpec(_Strict):
    sign_on_capability: str
    signed_in: TextCondition


class AppProfile(_Strict):
    profile: str
    product: str
    session: SessionSpec
    conditions: list[KnownCondition]

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def business(self) -> list[KnownCondition]:
        return [c for c in self.conditions if c.category == "business"]
