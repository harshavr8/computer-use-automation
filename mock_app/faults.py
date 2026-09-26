"""Runtime fault injection.

Faults are the reason this mock exists: the brief says the interesting failures
are runtime conditions, not layout drift. Every fault is deterministic
(count- or flag-based, never random) so an evidence run can be reproduced.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


@dataclass
class FaultConfig:
    # Added latency on every content page (transient slowness).
    slow_ms: int = 0
    # Show a "System Notice" overlay on the member detail page that must be dismissed.
    notice_dialog: bool = False
    # Put a native JS confirm() on the final "Confirm & Open" button.
    # Note: Playwright dismisses native dialogs by default, i.e. clicks *Cancel*.
    native_confirm: bool = False
    # Idle seconds before the session expires and the user is bounced to sign-on.
    session_timeout_s: int = 900
    # Next N content requests return an application error page (HTTP 500).
    fail_next: int = 0
    # If set, fail_next only applies to paths containing this substring.
    fail_route: str | None = None
    # Expire the session on the next N form submits under /members (deterministic timeout).
    expire_next: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "FaultConfig":
        cfg = cls()
        cfg.update(data)
        return cfg

    @classmethod
    def from_env(cls) -> "FaultConfig":
        raw = os.environ.get("MOCK_FAULTS")
        return cls.from_dict(json.loads(raw)) if raw else cls()

    def update(self, data: dict) -> None:
        known = {f.name for f in fields(self)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown fault(s): {sorted(unknown)}; known: {sorted(known)}")
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict(self) -> dict:
        return asdict(self)
