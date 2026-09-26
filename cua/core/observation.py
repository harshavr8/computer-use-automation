"""What a driver reports back about the surface. Surface-neutral by design."""
from __future__ import annotations

from dataclasses import dataclass, field

from .targets import Strategy


@dataclass
class ElementInfo:
    """One interactable control, as indexed during observe()."""
    ref: str                      # stable only within one observation, e.g. "main:e3"
    frame: str | None
    role: str
    name: str                     # accessible name ("" when the app provides none)
    anchor_text: str              # visible text immediately before it, e.g. "Member Number:"
    field_name: str               # HTML name attr (web only; "" elsewhere)
    text: str                     # own visible text (buttons/links)
    candidates: list[Strategy] = field(default_factory=list)  # validated: each uniquely hits this control


@dataclass
class FrameView:
    name: str | None
    url: str
    snapshot: str                 # accessibility snapshot (YAML-ish text)
    elements: list[ElementInfo]


@dataclass
class DialogEvent:
    kind: str                     # alert | confirm | prompt | beforeunload
    message: str
    handled: str                  # accepted | dismissed
    expected: bool


@dataclass
class Observation:
    url: str
    title: str
    frames: list[FrameView]
    dialogs: list[DialogEvent] = field(default_factory=list)

    def element(self, ref: str) -> ElementInfo | None:
        return next((e for f in self.frames for e in f.elements if e.ref == ref), None)
