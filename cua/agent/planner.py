"""The model boundary. The loop only knows the Planner protocol, so tests can drive
the full loop with a scripted planner and the real run uses Anthropic."""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..core.observation import Observation
from .tools import TOOLS

DEFAULT_MODEL = "claude-sonnet-5"


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    reasoning: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


class Planner(Protocol):
    model: str

    def decide(self, system: str, prompt: str, image: bytes | None, obs: Observation) -> ToolCall: ...


class AnthropicPlanner:
    """One stateless Messages call per step, forced to choose exactly one tool."""

    def __init__(self, model: str | None = None, max_tokens: int = 1024) -> None:
        import anthropic  # lazy: tests never need the SDK or a key

        self.model = model or os.environ.get("CUA_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY

    def decide(self, system: str, prompt: str, image: bytes | None, obs: Observation) -> ToolCall:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
        t0 = time.monotonic()
        msg = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, tools=TOOLS,
            tool_choice={"type": "any"}, messages=[{"role": "user", "content": content}],
        )
        latency = int((time.monotonic() - t0) * 1000)
        text = " ".join(b.text for b in msg.content if b.type == "text").strip()
        call = next((b for b in msg.content if b.type == "tool_use"), None)
        usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
        if call is None:
            return ToolCall("request_help", {"reason": "model returned no tool call"}, text, usage, latency)
        return ToolCall(call.name, dict(call.input), text, usage, latency)


class ScriptedPlanner:
    """Deterministic stand-in for tests: each step is a function of the observation."""

    model = "scripted"

    def __init__(self, script: list[Callable[[Observation], ToolCall]]) -> None:
        self.script = list(script)
        self.calls = 0

    def decide(self, system: str, prompt: str, image: bytes | None, obs: Observation) -> ToolCall:
        self.calls += 1
        if not self.script:
            return ToolCall("request_help", {"reason": "script exhausted"})
        return self.script.pop(0)(obs)


def ref_of(obs: Observation, role: str, label: str) -> str:
    """Test helper: find a control ref by role and name/anchor/text."""
    for f in obs.frames:
        for e in f.elements:
            if e.role == role and label in (e.name, e.anchor_text, e.text):
                return e.ref
    raise LookupError(f"no {role} {label!r} in observation")
