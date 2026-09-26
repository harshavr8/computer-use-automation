"""The seam between "how we perceive/act on a surface" and "the recorded flow".

Everything above this interface (agent, artifact, replay, policy) speaks only in
Targets and Actions. A web app, a legacy frameset app, and a Windows desktop app
differ only in which SurfaceDriver implements this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..core.actions import Action
from ..core.observation import DialogEvent, Observation
from ..core.targets import Strategy, Target


class DriverError(Exception):
    """Base class. Carries enough detail to explain a failure in a result contract."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


class FrameNotFound(DriverError):
    pass


class TargetNotFound(DriverError):
    """No strategy resolved to exactly one control."""


class TargetAmbiguous(DriverError):
    """Two strategies resolved to different controls. Never guess between them."""


@dataclass
class StrategyCheck:
    strategy: Strategy
    matches: int                  # 0 = gone, 1 = unique, >1 = ambiguous on its own
    agrees: bool | None = None    # does its unique match equal the chosen control?


@dataclass
class Resolved:
    """A target pinned to one concrete control, plus the evidence of how."""
    target: Target
    chosen: Strategy
    chosen_rank: int              # 0 = best-ranked strategy matched
    checks: list[StrategyCheck]
    frame_url: str
    control_text: str             # visible text/name of the control (for policy + logs)
    handle: Any = field(repr=False, default=None)  # driver-private

    @property
    def degraded(self) -> bool:
        """Drift signal: a better-ranked strategy no longer matches uniquely."""
        return self.chosen_rank > 0 or any(c.matches != 1 for c in self.checks)


class SurfaceDriver(Protocol):
    def observe(self) -> Observation: ...
    def resolve(self, target: Target) -> Resolved: ...
    def perform(self, action: Action, resolved: Resolved | None, value: str | None) -> str | None:
        """Execute an already-resolved, already-authorized action. Returns extracted text for `extract`."""
        ...
    def frame_url(self, frame: str | None) -> str: ...
    def url_for(self, route: str) -> str: ...
    def frame_urls(self) -> dict[str, str]: ...
    def expect_dialog(self, pattern: str, accept: bool) -> None: ...
    def drain_dialogs(self) -> list[DialogEvent]: ...
    def wait_for_text(self, text: str, frame: str | None, timeout_ms: int) -> bool: ...
    def text_visible(self, text: str, frame: str | None) -> bool: ...
    def locate_text(self, text: str) -> tuple[bool, str | None]: ...
    def snapshot_image(self) -> bytes: ...
    def screenshot(self, path: Path, mask_patterns: list[str]) -> Path: ...
    def close(self) -> None: ...
