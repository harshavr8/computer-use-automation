"""Human-in-the-loop handoff on the SAME live session.

Control model: a lease names exactly one controller of the browser session at any time.

    automation --(stuck / needs approval)--> awaiting_human --(operator claims)--> human
    human --(operator: done [--approve])--> resuming --(re-sync verified)--> automation
    awaiting_human|human --(operator: abort, or timeout)--> automation (run ends, nothing resumed)

The Actuator asks the lease before every action, so automation physically cannot
act while a human holds the session. The operator talks to the running process
through command files in the run's evidence folder (`python -m cua operator ...`);
that is the deliberately minimal stand-in for an operator console. The human works
in the headed browser window automation was using - the same cookies, the same page.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from ..driver.base import SurfaceDriver
from ..evidence.runlog import RunLog
from ..safety.redact import Redactor

State = Literal["automation", "awaiting_human", "human", "resuming"]


class ControlError(Exception):
    """Automation tried to act while it does not hold the session."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class HandoffDecision:
    action: Literal["resume", "abort", "timeout"]
    operator: str | None = None
    approve: bool = False
    note: str = ""
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    waited_s: int = 0


class HandoffController:
    def __init__(self, driver: SurfaceDriver, log: RunLog, redactor: Redactor, timeout_s: int = 900,
                 poll_ms: int = 300, simulate_human: Callable[[Any], None] | None = None) -> None:
        self.driver, self.log, self.redactor = driver, log, redactor
        self.timeout_s, self.poll_ms = timeout_s, poll_ms
        self.simulate_human = simulate_human          # tests only: stands in for a person at the browser
        self.state: State = "automation"
        self.holder = "automation"
        self.history: list[dict[str, Any]] = []
        self.cmd_dir = log.dir / "commands"
        self.cmd_dir.mkdir(parents=True, exist_ok=True)
        self._actions: list[dict[str, Any]] = []
        self._request: dict[str, Any] = {}
        self._write_state()

    # ---- lease ------------------------------------------------------------------------
    def assert_automation(self) -> None:
        if self.state not in ("automation", "resuming"):
            raise ControlError(f"session is held by {self.holder} (state {self.state}); automation may not act")

    def _transition(self, to: State, holder: str, note: str = "") -> None:
        self.history.append({"at": _now(), "from": self.state, "to": to, "holder": holder, "note": note})
        self.log.event("control", from_state=self.state, to_state=to, holder=holder, note=note)
        self.state, self.holder = to, holder
        self._write_state()

    def _write_state(self) -> None:
        self.log.write_json("control.json", {"run_id": self.log.run_id, "state": self.state, "holder": self.holder,
                                             "request": self._request, "history": self.history})

    # ---- the handoff ------------------------------------------------------------------------
    def request(self, context: dict[str, Any]) -> HandoffDecision:
        """Block (keeping the browser responsive) until an operator resumes, aborts, or we time out."""
        shot = self.driver.screenshot(self.log.path(f"handoff-{len(self.history)}.png"), self.redactor.mask_patterns())
        self._request = {**context, "screenshot": shot.name, "requested_at": _now()}
        self._transition("awaiting_human", "nobody", context.get("reason", ""))
        self._banner()
        t0 = time.monotonic()
        simulated = False
        while True:
            for cmd in self._commands():
                decision = self._apply(cmd)
                if decision:
                    decision.waited_s = int(time.monotonic() - t0)
                    return decision
            if self.state == "human" and self.simulate_human and not simulated:
                simulated = True
                self.simulate_human(self)
            if time.monotonic() - t0 > self.timeout_s:
                self._stop_capture()
                self._transition("automation", "automation", "handoff timed out; nobody claimed or finished")
                return HandoffDecision("timeout", waited_s=int(time.monotonic() - t0))
            self.driver.wait(self.poll_ms)   # pumps browser events: capture keeps working while we wait

    def resumed(self, note: str) -> None:
        """Called by automation once it has re-synced with the screen the human left."""
        self._transition("automation", "automation", note)

    def _apply(self, cmd: dict[str, Any]) -> HandoffDecision | None:
        op = str(cmd.get("operator") or "unknown")
        kind = cmd.get("cmd")
        if kind == "claim":
            if self.state != "awaiting_human":
                self.log.event("operator_command_ignored", cmd=kind, operator=op, state=self.state)
                return None
            self._actions = []
            self.driver.start_capture(self._on_human_event)
            self._transition("human", f"human:{op}", cmd.get("note", ""))
            return None
        if kind in ("done", "abort"):
            if self.state != "human" and kind == "done":
                self.log.event("operator_command_ignored", cmd=kind, operator=op, state=self.state,
                               reason="claim the session before marking it done")
                return None
            if self.state == "human" and self.holder != f"human:{op}":
                self.log.event("operator_command_ignored", cmd=kind, operator=op, holder=self.holder)
                return None
            self._stop_capture()
            actions = self.redactor.obj(self._actions)
            self.log.write_json(f"human_actions-{len(self.history)}.json", actions)
            if kind == "abort":
                self._transition("automation", "automation", f"aborted by {op}: {cmd.get('note', '')}")
                return HandoffDecision("abort", op, note=cmd.get("note", ""), human_actions=actions)
            self._transition("resuming", "automation", f"handed back by {op}")
            return HandoffDecision("resume", op, approve=bool(cmd.get("approve")), note=cmd.get("note", ""),
                                   human_actions=actions)
        return None

    # ---- capture ------------------------------------------------------------------------
    def _on_human_event(self, event: dict[str, Any]) -> None:
        if self.state != "human":
            return
        event = {**event, "at": _now()}
        self._actions.append(event)
        self.log.event("human_action", action=event)

    def _stop_capture(self) -> None:
        try:
            self.driver.stop_capture()
        except Exception:
            pass

    # ---- command inbox ------------------------------------------------------------------------
    def _commands(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.cmd_dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except json.JSONDecodeError:
                continue                     # half-written; pick it up next poll
            p.rename(p.with_suffix(".processed"))
        return out

    def _banner(self) -> None:
        r = self._request
        print("\n" + "=" * 78, file=sys.stderr)
        print(f" HUMAN NEEDED  run {self.log.run_id}", file=sys.stderr)
        print(f" reason: {r.get('reason')}", file=sys.stderr)
        print(f" step:   {r.get('step')}", file=sys.stderr)
        print(f" In another terminal:  python -m cua operator claim {self.log.run_id} --as <you>", file=sys.stderr)
        print(" then work in the open browser window, and finish with:", file=sys.stderr)
        print(f"   python -m cua operator done {self.log.run_id} --as <you> [--approve] [--note ...]", file=sys.stderr)
        print("=" * 78 + "\n", file=sys.stderr)


def send_command(run_dir: Path, cmd: str, operator: str, **extra: Any) -> Path:
    """Operator side: drop a command file (atomic rename so the runner never reads half a file)."""
    cmd_dir = run_dir / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.time_ns()}-{cmd}"
    tmp = cmd_dir / f"{stamp}.tmp"
    tmp.write_text(json.dumps({"cmd": cmd, "operator": operator, **extra}))
    final = tmp.with_suffix(".json")
    tmp.rename(final)
    return final


def pending_handoffs(evidence_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(evidence_dir.glob("*/control.json")):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("state") in ("awaiting_human", "human"):
            out.append({**data, "dir": str(p.parent)})
    return out


def asdict_decision(d: HandoffDecision) -> dict[str, Any]:
    return asdict(d)
