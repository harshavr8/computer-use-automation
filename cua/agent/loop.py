"""Discovery: an LLM-driven observe -> decide -> act loop that produces a trace.

Every action goes through the Actuator (policy gate + redacted logging). The model
proposes; the runtime verifies: refs must exist, controls must have a stable
locator, expected texts must actually appear, and 'done' needs visible evidence.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.actions import Action, SecretRef
from ..core.observation import Observation
from ..core.targets import LabeledValueStrategy, TableCellStrategy, Target, strategy_label
from ..driver.base import DriverError, SurfaceDriver
from ..evidence.runlog import RunLog
from ..runtime.actuator import Actuator
from ..safety.redact import Redactor
from . import prompt
from .planner import Planner, ToolCall
from .trace import Checkpoint, DiscoveryTrace, OutputDecl, TraceStep

MAX_CONSECUTIVE_FAILURES = 3
MAX_REPEATS = 3


@dataclass
class DiscoveryConfig:
    goal_template: str                       # e.g. "Look up member {member_id} ..."
    start_route: str
    params: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)   # name -> description (never values)
    max_steps: int = 20
    timeout_s: int = 300
    send_screenshots: bool = True
    expect_timeout_ms: int = 5000


@dataclass
class DiscoveryResult:
    trace: DiscoveryTrace
    outputs: dict[str, str]                  # raw values, returned to the caller only
    evidence_dir: Path


class DiscoveryAgent:
    def __init__(self, driver: SurfaceDriver, actuator: Actuator, planner: Planner,
                 log: RunLog, redactor: Redactor, handoff=None, max_handoffs: int = 2) -> None:
        self.driver, self.actuator, self.planner, self.log, self.redactor = driver, actuator, planner, log, redactor
        self.handoff, self.max_handoffs, self._handoffs_used = handoff, max_handoffs, 0
        if handoff is not None:
            actuator.control_check = handoff.assert_automation

    # ------------------------------------------------------------------------------
    def run(self, cfg: DiscoveryConfig) -> DiscoveryResult:
        goal = cfg.goal_template.format(**cfg.params)
        trace = DiscoveryTrace(run_id=self.log.run_id, goal=goal, goal_template=cfg.goal_template,
                               start_route=cfg.start_route, params=cfg.params, model=self.planner.model,
                               status="failed")
        outputs: dict[str, str] = {}
        self.log.event("discovery_start", goal=goal, start_route=cfg.start_route, params=cfg.params,
                       model=self.planner.model, max_steps=cfg.max_steps)

        self._act_logged(Action(kind="navigate", route=cfg.start_route), trace, "navigate",
                         "open the entry point", expect=None)

        feedback: list[str] = []
        failures = 0
        deadline = time.monotonic() + cfg.timeout_s
        while True:
            if len(trace.steps) >= cfg.max_steps:
                trace.status = "max_steps"
                self._escalate(trace, f"step budget of {cfg.max_steps} exhausted")
                break
            if time.monotonic() > deadline:
                trace.status = "timeout"
                self._escalate(trace, f"wall-clock budget of {cfg.timeout_s}s exhausted")
                break

            obs = self.driver.observe()
            text = prompt.render(goal, cfg.params, cfg.secrets, obs, trace.steps, feedback,
                                 len(trace.steps) + 1, cfg.max_steps)
            image = self.driver.snapshot_image() if cfg.send_screenshots else None
            call = self.planner.decide(prompt.SYSTEM, text, image, obs)
            self.log.event("model_decision", step=len(trace.steps) + 1, tool=call.name, input=call.input,
                           reasoning=call.reasoning, usage=call.usage, latency_ms=call.latency_ms,
                           prompt_chars=len(text), image=bool(image))
            feedback = []
            if call.input.get("arrived_text"):
                feedback += self._confirm_arrival(call.input["arrived_text"], trace, cfg.params)

            if call.name == "request_help":
                trace.status = "needs_human"
                if self._human_resumes(trace, call.input.get("reason", "model requested help")):
                    feedback, failures = self._after_human(trace), 0
                    continue
                self._escalate(trace, call.input.get("reason", "model requested help"))
                break
            if call.name == "done":
                finished, done_feedback = self._handle_done(call, trace)
                if finished:
                    break
                self._rejected(trace, "done", call.input.get("summary", ""), done_feedback[0])
                feedback += done_feedback
                failures += 1
            else:
                step, action_feedback = self._handle_action(call, obs, cfg, outputs, trace)
                feedback += action_feedback
                if step.status == "needs_approval":
                    trace.status = "needs_human"
                    self._escalate(trace, f"irreversible step needs approval: {step.action.describe() if step.action else ''}")
                    break
                if step.status == "denied" and step.error and "left allowlist" in step.error:
                    trace.status = "failed"
                    break
                failures = 0 if step.status == "ok" else failures + 1
                if self._repeating(trace):
                    trace.status = "stuck"
                    reason = f"same action repeated {MAX_REPEATS} times without progress"
                    if self._human_resumes(trace, reason):
                        feedback, failures = self._after_human(trace), 0
                        continue
                    self._escalate(trace, reason)
                    break

            if failures >= MAX_CONSECUTIVE_FAILURES:
                trace.status = "stuck"
                reason = f"{failures} consecutive failed actions: {'; '.join(feedback)}"
                if self._human_resumes(trace, reason):
                    feedback, failures = self._after_human(trace), 0
                    continue
                self._escalate(trace, reason)
                break

        self._finish(trace, outputs)
        return DiscoveryResult(trace=trace, outputs=outputs, evidence_dir=self.log.dir)

    # ------------------------------------------------------------------------------
    def _handle_action(self, call: ToolCall, obs: Observation, cfg: DiscoveryConfig,
                       outputs: dict[str, str], trace: DiscoveryTrace) -> tuple[TraceStep, list[str]]:
        args, why = call.input, call.input.get("why", "")
        try:
            if call.name == "navigate":
                action = Action(kind="navigate", route=args["route"])
                return self._act_logged(action, trace, call.name, why, args.get("expect_text"), params=cfg.params)
            if call.name == "extract":
                return self._extract(args, trace, outputs)
            if call.name in ("click", "fill", "select"):
                target, err = self._target_from_ref(obs, args.get("ref", ""))
                if err:
                    return self._rejected(trace, call.name, why, err)
                recorded, live, source, param, err = self._value(call.name, args, cfg.params)
                if err:
                    return self._rejected(trace, call.name, why, err)
                kind = {"click": "click", "fill": "fill", "select": "select"}[call.name]
                return self._act_logged(Action(kind=kind, target=target, value=recorded), trace, call.name,
                                        why, args.get("expect_text"), live_value=live, source=source, param=param,
                                        params=cfg.params)
            return self._rejected(trace, call.name, why, f"unknown tool {call.name!r}")
        except (KeyError, ValueError) as exc:
            return self._rejected(trace, call.name, why, f"bad arguments: {exc}")

    def _target_from_ref(self, obs: Observation, ref: str) -> tuple[Target | None, str | None]:
        el = obs.element(ref)
        if el is None:
            return None, f"ref {ref!r} is not in the current observation"
        if not el.candidates:
            return None, f"control {ref} has no stable locator and cannot be recorded"
        label = el.name or el.text or el.anchor_text or el.field_name
        return Target(description=f"{label} {el.role}".strip(), frame=el.frame,
                      strategies=tuple(el.candidates)), None

    @staticmethod
    def _value(tool: str, args: dict[str, Any], params: dict[str, str]):
        """Returns (recorded_value, live_value, source, param_name, error)."""
        if tool == "click":
            return None, None, "none", None, None
        literal = args.get("text") if tool == "fill" else args.get("option")
        given = [k for k in ("param", "secret") if args.get(k)] + (["literal"] if literal else [])
        if len(given) != 1:
            return None, None, "none", None, "give exactly one of param / secret / text"
        if args.get("param"):
            name = args["param"]
            if name not in params:
                return None, None, "none", None, f"unknown param {name!r}; declared: {sorted(params)}"
            return f"{{{{inputs.{name}}}}}", params[name], "param", name, None
        if args.get("secret"):
            ref = SecretRef(secret=args["secret"])
            return ref, ref, "secret", None, None
        # A literal that equals an input value is really that input: record it as a param.
        for name, value in params.items():
            if literal == value:
                return f"{{{{inputs.{name}}}}}", value, "param", name, None
        return literal, literal, "literal", None, None

    def _act_logged(self, action: Action, trace: DiscoveryTrace, tool: str, why: str, expect: str | None,
                    live_value: Any = None, source: str = "none", param: str | None = None,
                    output_sensitivity: str | None = None, params: dict[str, str] | None = None,
                    ) -> tuple[TraceStep, list[str]]:
        step = TraceStep(index=len(trace.steps) + 1, tool=tool, why=why, status="ok", action=action,
                         value_source=source, param=param, location_before=self._where())  # type: ignore[arg-type]
        live = action if live_value is None else action.model_copy(update={"value": live_value})
        feedback: list[str] = []
        try:
            out = self.actuator.do(live, step_id=f"s{step.index}", output_sensitivity=output_sensitivity)
        except DriverError as exc:
            where = f" (searched frame {action.target.frame or 'top'!r})" if action.target else ""
            step.status, step.error = "failed", f"{type(exc).__name__}: {exc}{where}"
            trace.steps.append(step)
            return step, [step.error]
        step.risk = out.decision.risk
        if out.status in ("denied", "left_allowlist"):
            step.status = "denied"
            reason = out.violation.reason if out.violation else out.decision.reason
            step.error = ("left allowlist: " if out.status == "left_allowlist" else "policy denied: ") + reason
            trace.steps.append(step)
            return step, [step.error]
        if out.status == "needs_approval":
            step.status = "needs_approval"
            step.error = out.decision.reason
            trace.steps.append(step)
            return step, [step.error]

        if out.resolved:
            step.locator_used, step.degraded = strategy_label(out.resolved.chosen), out.resolved.degraded
        step.dialogs = [d.__dict__ for d in out.dialogs]
        if any(not d.expected for d in out.dialogs):
            feedback.append("an unexpected native dialog appeared and was dismissed: " +
                            "; ".join(d.message for d in out.dialogs))
        if expect:
            cp, problem = self._checkpoint(expect, params or {})
            if cp:
                step.checkpoint = cp
            else:
                step.expect_text_unverified = expect
                feedback.append(f"your predicted expect_text {expect!r} was not found; confirm the real "
                                "screen with arrived_text next turn")
        step.location_after = self._where()
        trace.steps.append(step)
        return step, feedback

    def _confirm_arrival(self, text: str, trace: DiscoveryTrace, params: dict[str, str]) -> list[str]:
        """Attach a model-confirmed, runtime-verified checkpoint to the last screen-changing step."""
        pending = next((s for s in reversed(trace.steps)
                        if s.status == "ok" and s.tool in ("click", "navigate")), None)
        if pending is None or pending.checkpoint is not None:
            return []
        cp, problem = self._checkpoint(text, params, timeout_ms=1500)
        if cp is None:
            return [f"arrived_text rejected: {problem}"]
        pending.checkpoint, pending.expect_text_unverified = cp, None
        self.log.event("checkpoint_confirmed", step=pending.index, text=cp.text, frame=cp.frame)
        return [f"checkpoint recorded for step {pending.index}: {cp.text!r}"]

    def _checkpoint(self, text: str, params: dict[str, str], timeout_ms: int = 5000) -> tuple[Checkpoint | None, str | None]:
        """Verify a predicted text is visible, refuse sensitive data, template input values."""
        # Sensitivity first: it is the more instructive refusal, and needs no browser round-trip.
        if self.redactor.text(text) != text:
            return None, ("that text contains a sensitive data value (balance, name, phone...) and can never "
                          "be a checkpoint; use the screen title bar or a fixed application message instead")
        if not self.driver.wait_for_text(text, "*", timeout_ms):
            return None, (f"{text!r} is not the text of any single visible element (text spread across "
                          "several table cells does not count); copy ONE element's text exactly, "
                          "e.g. the screen title bar")
        _, frame = self.driver.locate_text(text)
        for name, value in params.items():   # "...MATCHING 99999" -> "...MATCHING {{inputs.member_id}}"
            if value and value in text:
                text = text.replace(value, f"{{{{inputs.{name}}}}}")
        return Checkpoint(text=text, frame=frame), None

    def _extract(self, args: dict[str, Any], trace: DiscoveryTrace,
                 outputs: dict[str, str]) -> tuple[TraceStep, list[str]]:
        if args["by"] == "table_cell":
            strat = TableCellStrategy(row_key=args["row_key"], column_header=args["column_header"])
        else:
            strat = LabeledValueStrategy(label=args["label"])
        decl = OutputDecl(name=args["output_name"], type=args["output_type"], sensitivity=args["sensitivity"])
        frame = args.get("frame") or None
        if frame in ("top",):
            frame = None
        if not args.get("frame"):
            found = self._frames_containing(strat)
            if len(found) > 1:
                return self._rejected(trace, "extract", args["why"],
                                      f"value found in several frames {found}; pass frame explicitly")
            if not found:
                return self._rejected(trace, "extract", args["why"],
                                      f"{strategy_label(strat)} matched nothing in any frame; check the exact "
                                      "row/column/label text in the snapshot")
            frame = found[0]
            self.log.event("frame_search", strategy=strategy_label(strat), found=frame or "top")
        target = Target(description=f"{decl.name} ({strategy_label(strat)})", frame=frame, strategies=(strat,))
        step, feedback = self._act_logged(Action(kind="extract", target=target), trace, "extract",
                                          args["why"], None, output_sensitivity=decl.sensitivity)
        step.output = decl
        if step.status == "ok" and self.actuator.last_extracted is not None:
            outputs[decl.name] = self.actuator.last_extracted
            feedback.append(f"extracted {decl.name} successfully")
        return step, feedback

    def _frames_containing(self, strat) -> list[str | None]:
        found: list[str | None] = []
        for name in self.driver.frame_urls():
            frame = None if name == "top" else name
            try:
                self.driver.resolve(Target(description="probe", frame=frame, strategies=(strat,)), timeout_ms=300)
                found.append(frame)
            except DriverError:
                continue
        return found

    def _handle_done(self, call: ToolCall, trace: DiscoveryTrace) -> tuple[bool, list[str]]:
        evidence = call.input.get("evidence_text", "")
        cp, problem = self._checkpoint(evidence, trace.params) if evidence else (None, "no evidence_text")
        if cp is None:
            return False, [f"done rejected: {problem}"]
        trace.success = cp
        frame = cp.frame
        trace.summary = call.input.get("summary", "")
        if call.input.get("result") == "business_outcome":
            trace.status, trace.outcome_code = "business_outcome", call.input.get("outcome_code") or "unspecified"
        else:
            trace.status = "success"
        self.log.event("discovery_done", status=trace.status, evidence=evidence, frame=frame,
                       outcome_code=trace.outcome_code, summary=trace.summary)
        return True, []

    def _rejected(self, trace: DiscoveryTrace, tool: str, why: str, err: str) -> tuple[TraceStep, list[str]]:
        step = TraceStep(index=len(trace.steps) + 1, tool=tool, why=why, status="failed", error=err,
                         location_before=self._where())
        trace.steps.append(step)
        self.log.event("action_rejected", step=step.index, tool=tool, error=err)
        return step, [err]

    def _repeating(self, trace: DiscoveryTrace) -> bool:
        tail = [s for s in trace.steps if s.status == "ok"][-MAX_REPEATS:]
        if len(tail) < MAX_REPEATS:
            return False
        keys = {(s.action.describe() if s.action else s.tool, json.dumps(s.location_after, sort_keys=True))
                for s in tail}
        return len(keys) == 1

    def _where(self) -> dict[str, str]:
        return {f: (urlsplit(u).path or u) for f, u in self.driver.frame_urls().items()}

    def _human_resumes(self, trace: DiscoveryTrace, reason: str) -> bool:
        """Give the live session to a human; True if they hand it back for the agent to continue."""
        if self.handoff is None or self._handoffs_used >= self.max_handoffs:
            return False
        self._handoffs_used += 1
        decision = self.handoff.request({"kind": "discovery", "goal": trace.goal, "reason": reason,
                                         "step": len(trace.steps), "location": self._where()})
        trace.steps.append(TraceStep(
            index=len(trace.steps) + 1, tool="human", why=decision.note or reason,
            status="ok" if decision.action == "resume" else "failed",
            error=None if decision.action == "resume" else f"handoff {decision.action}",
            location_before=self._where(), location_after=self._where(),
            human_actions=decision.human_actions))
        if decision.action != "resume":
            return False
        self.handoff.resumed("agent continues after human help")
        trace.status = "failed"            # provisional again; the loop decides the final status
        return True

    def _after_human(self, trace: DiscoveryTrace) -> list[str]:
        acts = trace.steps[-1].human_actions
        summary = "; ".join(f"{a.get('kind')} {a.get('text') or a.get('label') or a.get('path') or ''}".strip()
                            for a in acts if a.get("kind") != "navigated") or "no recorded actions"
        return [f"A human operator took over and handed back control. They did: {summary}. "
                "Continue toward the goal from the CURRENT observation; do not repeat what they did."]

    def _escalate(self, trace: DiscoveryTrace, reason: str) -> None:
        """Write an intervention request. Step 5 wires this to a live operator handoff."""
        shot = self.driver.screenshot(self.log.path("intervention.png"), self.redactor.mask_patterns())
        request = {
            "run_id": trace.run_id, "kind": "discovery", "goal": trace.goal, "reason": reason,
            "status": trace.status, "current_step": len(trace.steps), "location": self._where(),
            "last_steps": [s.model_dump(include={"index", "tool", "status", "error", "why"}) for s in trace.steps[-5:]],
            "screenshot": shot.name,
        }
        self.log.write_json("intervention.json", request)
        self.log.event("intervention_requested", reason=reason, status=trace.status)

    def _finish(self, trace: DiscoveryTrace, outputs: dict[str, str]) -> None:
        self.driver.screenshot(self.log.path("final.png"), self.redactor.mask_patterns())
        self.log.write_json("trace.json", trace.model_dump(mode="json"))
        self.log.write_json("result.json", {
            "status": trace.status, "outcome_code": trace.outcome_code, "steps": len(trace.steps),
            "outputs": outputs, "summary": trace.summary,
        })
        self.log.event("discovery_end", status=trace.status, steps=len(trace.steps),
                       outputs=sorted(outputs))
