"""Deterministic replay: run a capability artifact with new inputs. No LLM anywhere.

Per step:
    resolve target (ranked strategies, consensus check)  ->  policy gate  ->  act
    then poll the screen, in a fixed order, until something decides the step:
        1. a business outcome this step declared        -> return it to the caller
        2. a hard condition from the app profile        -> fail, with evidence
        3. a recoverable condition from the app profile -> recover, keep polling
        4. the step's postcondition                     -> next step
        (timeout)                                       -> fail: postcondition_not_met
The same inputs against the same screen state always take the same path.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..artifact.profile import AppProfile, KnownCondition
from ..artifact.schema import Capability, OutcomeSpec, Step, TextCondition
from ..artifact.store import CapabilityStore
from ..core.actions import Action, SecretRef
from ..core.targets import strategy_label
from ..driver.base import DriverError, SurfaceDriver, TargetAmbiguous
from ..evidence.runlog import RunLog
from ..runtime.actuator import Actuator
from ..safety.policy import Approval
from ..safety.redact import Redactor
from .result import (
    Escalation, Failure, HandoffRecord, Outcome, RecoveryRecord, ReplayResult, StepRecord,
)

_TEMPLATE = re.compile(r"\{\{inputs\.([A-Za-z_][A-Za-z0-9_]*)\}\}")
POLL_MS = 150


# ---- internal control flow ------------------------------------------------------------
class _Stop(Exception):
    pass


class _Business(_Stop):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


class _Hard(_Stop):
    def __init__(self, failure: Failure) -> None:
        self.failure = failure


class _Escalate(_Stop):
    def __init__(self, reason: str, step: str | None, needs_approval: bool = False) -> None:
        self.reason, self.step, self.needs_approval = reason, step, needs_approval


class _Restart(Exception):
    """Session was re-established; the capability must start again from step 1."""


class ReplayEngine:
    def __init__(self, driver: SurfaceDriver, actuator: Actuator, profile: AppProfile, store: CapabilityStore,
                 log: RunLog, redactor: Redactor, max_restarts: int = 1, handoff=None, max_handoffs: int = 2) -> None:
        self.driver, self.actuator, self.profile, self.store = driver, actuator, profile, store
        self.log, self.redactor, self.max_restarts = log, redactor, max_restarts
        self.handoff, self.max_handoffs = handoff, max_handoffs
        self._handoffs: list[HandoffRecord] = []
        self._step_approvals: dict[str, Approval] = {}
        self._executed_irreversible: set[tuple[str, int]] = set()
        if handoff is not None:
            actuator.control_check = handoff.assert_automation
        self._recoveries: list[RecoveryRecord] = []
        self._records: list[StepRecord] = []
        self._drift: list[str] = []
        self._irreversible_done = False

    # ------------------------------------------------------------------------------------
    def run(self, cap: Capability, inputs: dict[str, str], approval: Approval | None = None) -> ReplayResult:
        t0 = time.monotonic()
        result = ReplayResult(run_id=self.log.run_id, capability=cap.id, version=cap.version,
                              status="failed", evidence_dir=str(self.log.dir))
        for name, spec in cap.inputs.items():
            if spec.sensitivity in ("pii", "financial") and inputs.get(name):
                self.redactor.register(inputs[name], spec.sensitivity)
        self.log.event("replay_start", capability=cap.id, version=cap.version, status=cap.status,
                       inputs=inputs)
        restarts = 0
        try:
            self._preflight(cap)
            self._validate_inputs(cap, inputs)
            pos: int | None = None                   # None = establish the start state first
            outputs: dict[str, str] = {}
            while True:
                try:
                    if pos is None:
                        self._ensure_start_state(cap)
                        pos = 0
                    self._execute(cap, inputs, approval, start=pos, outputs=outputs)
                    self._verify_success(cap, inputs, outputs)
                    result.status, result.outputs = "success", outputs
                    break
                except _Restart:
                    restarts += 1
                    if restarts > self.max_restarts:
                        raise _Hard(Failure(category="recovery_exhausted", step=None,
                                            expected="session to stay valid after re-sign-on",
                                            observed="session expired again", detail={"restarts": restarts}))
                    self.log.event("replay_restart", reason="session re-established", attempt=restarts)
                    pos, outputs = None, {}
                except (_Hard, _Escalate) as stop:
                    pos = self._hand_to_human(cap, stop, inputs)   # re-raises if no human resumes
        except _Business as b:
            result.status, result.outcome = "business_outcome", b.outcome
        except _Hard as h:
            if self._records:                      # pre-UI failures (config, inputs) have no screen to capture
                h.failure.evidence = self._failure_evidence(h.failure)
            result.status, result.failure = "failed", h.failure
        except _Escalate as e:
            result.status = "escalated"
            result.escalation = Escalation(reason=e.reason, step=e.step, request_file=self._intervention(cap, e))

        result.recoveries, result.steps, result.drift = self._recoveries, self._records, self._drift
        result.handoffs = self._handoffs
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        self.log.write_json("result.json", result.model_dump(mode="json"))
        self.log.event("replay_end", status=result.status,
                       outcome=result.outcome.code if result.outcome else None,
                       failure=result.failure.category if result.failure else None,
                       recoveries=len(result.recoveries), duration_ms=result.duration_ms)
        return result

    # ---- configuration / inputs / preconditions ---------------------------------------------
    def _preflight(self, cap: Capability) -> None:
        """Everything that can be checked without a browser: dependencies and secrets exist."""
        caps = [cap]
        if cap.requires.session:
            try:
                caps.append(self.store.load(cap.requires.session))
            except FileNotFoundError:
                raise _Hard(Failure(category="config_error", step=None,
                                    expected=f"capability {cap.requires.session!r} in the store",
                                    observed="not found; compile or install it first"))
        missing = []
        for c in caps:
            for decl in c.secrets:
                try:
                    self.actuator.secrets.get(SecretRef(secret=decl.name))
                except KeyError:
                    missing.append(f"{c.id}:{decl.name}")
        if missing:
            raise _Hard(Failure(category="config_error", step=None, expected="all declared secrets available",
                                observed=f"missing secrets: {missing}"))

    def _validate_inputs(self, cap: Capability, inputs: dict[str, str]) -> None:
        """Reject bad inputs before touching the UI. Cheap, and keeps junk out of the app's audit log."""
        missing, extra = set(cap.inputs) - set(inputs), set(inputs) - set(cap.inputs)
        problems = [f"missing input {m!r}" for m in sorted(missing)] + [f"unexpected input {e!r}" for e in sorted(extra)]
        for name, spec in cap.inputs.items():
            v = inputs.get(name)
            if v is not None and spec.pattern and not re.fullmatch(spec.pattern, v):
                problems.append(f"input {name!r} does not match {spec.pattern}")
        if problems:
            raise _Hard(Failure(category="input_invalid", step=None, expected="inputs matching the capability contract",
                                observed="; ".join(problems)))

    def _ensure_start_state(self, cap: Capability) -> None:
        start = cap.requires.start_state
        if start and self._visible(start, {}):
            return
        if cap.requires.session:
            session_cap = self.store.load(cap.requires.session)
            self.log.event("session_start", capability=session_cap.id, version=session_cap.version)
            try:
                self._execute(session_cap, {}, None)
            except _Business as b:        # a sign-on has no business outcomes of its own
                raise _Hard(Failure(category="session_failed", step=b.outcome.step, expected="signed-on session",
                                    observed=f"business outcome {b.outcome.code}"))
            except _Hard as h:
                h.failure.category = "session_failed" if h.failure.category != "input_invalid" else h.failure.category
                raise
            if session_cap.success and not self._await_visible(session_cap.success.check, {}, 8000):
                raise _Hard(Failure(category="session_failed", step=None,
                                    expected=f"visible: {session_cap.success.check.text!r}",
                                    observed=self._observed()))
        if start and not self._await_visible(start, {}, 5000):
            raise _Hard(Failure(category="precondition_not_met", step=None,
                                expected=f"start state visible: {start.text!r} (frame {start.frame})",
                                observed=self._observed()))

    # ---- steps ------------------------------------------------------------------------------
    def _execute(self, cap: Capability, inputs: dict[str, str], approval: Approval | None,
                 start: int = 0, outputs: dict[str, str] | None = None) -> dict[str, str]:
        outputs = {} if outputs is None else outputs
        for idx in range(start, len(cap.steps)):
            step = cap.steps[idx]
            t0 = time.monotonic()
            self._run_step(cap, step, inputs, self._step_approvals.get(step.id, approval), outputs)
            if step.risk == "irreversible":
                self._executed_irreversible.add((cap.id, idx))
            rec = next(r for r in reversed(self._records) if r.id == step.id and r.capability == cap.id)
            rec.duration_ms = int((time.monotonic() - t0) * 1000)
        return outputs

    def _run_step(self, cap: Capability, step: Step, inputs: dict[str, str], approval: Approval | None,
                  outputs: dict[str, str]) -> None:
        action = Action(kind=step.action, target=step.target, route=self._fill(step.route, inputs),
                        value=self._fill(step.value, inputs))
        record = StepRecord(id=step.id, capability=cap.id, status="failed")
        self._records.append(record)
        spec = cap.outputs.get(step.output) if step.output else None

        attempts: dict[str, int] = {}
        while True:
            try:
                out = self.actuator.do(action, approval=approval, step_id=f"{cap.id}:{step.id}",
                                       output_sensitivity=spec.sensitivity if spec else None)
                break
            except TargetAmbiguous as exc:
                raise _Hard(self._target_failure(step, exc, "target_ambiguous"))
            except DriverError as exc:
                # The control is missing. Maybe the *screen* explains why (error page, expired session...).
                if not self._classify(cap, step, inputs, attempts, settle_only=True):
                    raise _Hard(self._target_failure(step, exc, "target_not_found"))
                # a recovery ran; try again (bounded by that recovery's max_attempts)

        if out.status == "needs_approval":
            raise _Escalate(f"irreversible step needs approval: {out.decision.reason}", step.id, needs_approval=True)
        if out.status in ("denied", "left_allowlist"):
            reason = out.violation.reason if out.violation else out.decision.reason
            raise _Hard(Failure(category="policy_violation", step=step.id, expected="action inside the allowlist",
                                observed=reason))
        if step.risk == "irreversible":
            self._irreversible_done = True
        if out.resolved:
            record.locator, record.locator_rank = strategy_label(out.resolved.chosen), out.resolved.chosen_rank
            record.degraded = out.resolved.degraded
            if out.resolved.degraded:
                self._drift.append(f"{cap.id}:{step.id} matched via fallback {record.locator} "
                                   f"(rank {record.locator_rank}); preferred locator no longer matches")
        unexpected = [d for d in out.dialogs if not d.expected]

        if step.action in ("click", "navigate") or step.postcondition:
            self._classify(cap, step, inputs, attempts, dialogs=[d.message for d in unexpected])

        if step.action == "extract" and step.output:
            raw = out.extracted or ""
            parsed = self._parse(step, raw, spec.parse if spec else "none")
            if spec and spec.sensitivity in ("pii", "financial"):
                self.redactor.register(parsed, spec.sensitivity)
            outputs[step.output] = parsed
        record.status = "ok"

    # ---- the classifier ----------------------------------------------------------------------
    def _classify(self, cap: Capability, step: Step, inputs: dict[str, str], attempts: dict[str, int],
                  dialogs: list[str] | None = None, settle_only: bool = False) -> bool:
        """Returns True if a recovery was performed (settle_only mode)."""
        business = [o for o in cap.outcomes if o.code in step.expected_outcomes]
        hard = [c for c in self.profile.conditions if c.category == "hard"]
        recoverable = [c for c in self.profile.conditions if c.category == "recoverable"]
        post = step.postcondition
        timeout = (post.timeout_ms if post else 1500)
        deadline = time.monotonic() + timeout / 1000
        recovered_once = False

        while True:
            for o in business:
                hit = self._find(o.detect, inputs)
                if hit:
                    raise _Business(Outcome(code=o.code, step=step.id,
                                            message=hit[0] if o.capture_message else None))
            for c in hard:
                hit = self._find(c.detect, inputs)
                if hit:
                    raise _Hard(Failure(category="app_error", step=step.id, expected=self._expected(post),
                                        observed=f"{c.id}: {hit[0]}", detail={"condition": c.id}))
            rec = next(((c, h) for c in recoverable if (h := self._find(c.detect, inputs))), None)
            if rec:
                self._recover(rec[0], rec[1][1], step, attempts)
                recovered_once = True
                if settle_only:
                    return True
                deadline = time.monotonic() + timeout / 1000     # fresh budget after a recovery
                continue
            if settle_only:
                return False
            if post is None or self._visible(post.check, inputs):
                return recovered_once
            if time.monotonic() > deadline:
                observed = self._observed()
                if dialogs:
                    observed += f" | native dialog dismissed: {dialogs}"
                raise _Hard(Failure(
                    category="postcondition_not_met", step=step.id, expected=self._expected(post),
                    observed=observed, detail={"recovered_during_step": recovered_once}))
            self.driver.wait(POLL_MS)

    def _recover(self, cond: KnownCondition, frame: str | None, step: Step, attempts: dict[str, int]) -> None:
        r = cond.recovery
        assert r is not None
        attempts[cond.id] = attempts.get(cond.id, 0) + 1
        n = attempts[cond.id]
        if n > r.max_attempts:
            raise _Hard(Failure(category="recovery_exhausted", step=step.id,
                                expected=f"{cond.id} to clear within {r.max_attempts} recoveries",
                                observed=self._observed(), detail={"condition": cond.id, "attempts": n - 1}))
        self._recoveries.append(RecoveryRecord(condition=cond.id, step=step.id, action=r.kind, attempt=n))
        self.log.event("recovery", condition=cond.id, step=step.id, action=r.kind, attempt=n, frame=frame)
        if r.kind == "click" and r.target:
            self.actuator.do(Action(kind="click", target=r.target), step_id=f"recover:{cond.id}")
        elif r.kind == "reload":
            if step.risk == "irreversible":
                raise _Escalate(f"{cond.id} after an irreversible step: outcome unknown, not retrying", step.id)
            self.driver.wait(r.backoff_ms)
            self.driver.reload_frame(frame)
        elif r.kind == "resign_on":
            if self._irreversible_done:
                raise _Escalate("session expired after an irreversible step; state must be checked by a human",
                                step.id)
            raise _Restart()

    # ---- human handoff -----------------------------------------------------------------------
    _HANDOFF_ELIGIBLE = {"target_not_found", "target_ambiguous", "postcondition_not_met", "recovery_exhausted",
                         "app_error"}

    def _hand_to_human(self, cap: Capability, stop: _Stop, inputs: dict[str, str]) -> int:
        """Pause, let a human work in the same session, then return the step index to resume at."""
        step_id = stop.failure.step if isinstance(stop, _Hard) else stop.step    # type: ignore[union-attr]
        idx = next((i for i, s in enumerate(cap.steps) if s.id == step_id), None)
        eligible = (self.handoff is not None and idx is not None and len(self._handoffs) < self.max_handoffs
                    and (isinstance(stop, _Escalate) or stop.failure.category in self._HANDOFF_ELIGIBLE))  # type: ignore[union-attr]
        if not eligible:
            raise stop
        reason = stop.reason if isinstance(stop, _Escalate) else (
            f"{stop.failure.category}: expected {stop.failure.expected}")                  # type: ignore[union-attr]
        decision = self.handoff.request({
            "kind": "replay", "capability": cap.id, "version": cap.version, "step": step_id, "reason": reason,
            "needs_approval": isinstance(stop, _Escalate) and stop.needs_approval,
            "screen": self._observed(), "completed_steps": [r.id for r in self._records if r.status == "ok"],
        })
        record = HandoffRecord(reason=reason, step=step_id, decision=decision.action, operator=decision.operator,
                               approved=decision.approve, note=decision.note, human_actions=decision.human_actions,
                               waited_s=decision.waited_s)
        self._handoffs.append(record)
        if decision.action != "resume":
            raise stop

        if decision.approve and isinstance(stop, _Escalate) and stop.needs_approval:
            self._step_approvals[cap.steps[idx].id] = Approval(
                approver=f"human:{decision.operator}", scope=f"{cap.id}:{cap.steps[idx].id}")
        resume = self._resync(cap, idx, inputs)
        if resume is None:
            self.handoff.resumed("could not re-sync after handoff")
            raise _Hard(Failure(category="resync_failed", step=step_id,
                                expected="a recorded checkpoint (or the start state) visible after the handoff",
                                observed=self._observed()))
        record.resumed_at_step = cap.steps[resume].id if resume < len(cap.steps) else "(success check)"
        self.log.event("resync", resume_at=record.resumed_at_step, after_handoff=len(self._handoffs))
        self.handoff.resumed(f"resuming at {record.resumed_at_step}")
        return resume

    def _resync(self, cap: Capability, failed: int, inputs: dict[str, str]) -> int | None:
        """Where is the screen now? Resume after the latest step whose postcondition is visible.

        1. The human finished work past the failure: newest visible postcondition at/after it.
        2. The human restored an earlier screen: newest visible postcondition before it (redo from there).
        3. Otherwise the start state.
        Never resume at or before an irreversible step that already ran: that would repeat it.
        """
        resume: int | None = None
        for k in range(len(cap.steps) - 1, failed - 1, -1):
            post = cap.steps[k].postcondition
            if post and self._visible(post.check, inputs):
                resume = k + 1
                break
        if resume is None:
            for k in range(failed - 1, -1, -1):
                post = cap.steps[k].postcondition
                if post and self._visible(post.check, inputs):
                    resume = k + 1
                    break
        if resume is None and (cap.requires.start_state is None or self._visible(cap.requires.start_state, inputs)):
            resume = 0
        if resume is None or any(c == cap.id and i >= resume for c, i in self._executed_irreversible):
            return None
        return resume

    # ---- success / outputs ------------------------------------------------------------------
    def _verify_success(self, cap: Capability, inputs: dict[str, str], outputs: dict[str, str]) -> None:
        missing = [o for o in cap.success.outputs_present if o not in outputs]
        if missing or not self._visible(cap.success.check, inputs):
            raise _Hard(Failure(category="success_check_failed", step=None,
                                expected=f"visible {cap.success.check.text!r} and outputs {cap.success.outputs_present}",
                                observed=f"missing outputs {missing}; {self._observed()}"))

    @staticmethod
    def _parse(step: Step, raw: str, how: str) -> str:
        if how == "none":
            return raw
        cleaned = raw.replace("$", "").replace(",", "").strip()
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise _Hard(Failure(category="output_parse_error", step=step.id, expected=f"a {how} value",
                                observed="unparseable text (withheld: sensitive output)"))
        return str(-value if negative else value)

    # ---- detection helpers ------------------------------------------------------------------
    @staticmethod
    def _fill(value: Any, inputs: dict[str, str]) -> Any:
        if isinstance(value, SecretRef) or value is None:
            return value
        return _TEMPLATE.sub(lambda m: inputs[m.group(1)], value)

    def _find(self, cond: TextCondition, inputs: dict[str, str]):
        return self.driver.find_text(self._fill(cond.text, inputs), cond.frame, cond.regex)

    def _visible(self, cond: TextCondition, inputs: dict[str, str]) -> bool:
        return self._find(cond, inputs) is not None

    def _await_visible(self, cond: TextCondition, inputs: dict[str, str], timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._visible(cond, inputs):
                return True
            self.driver.wait(POLL_MS)
        return False

    @staticmethod
    def _expected(post) -> str:
        return f"visible {post.check.text!r} in frame {post.check.frame}" if post else "no error condition"

    def _observed(self) -> str:
        """Where we are and what the screen says - redacted, short, human-readable."""
        parts = []
        for frame, url in self.driver.frame_urls().items():
            text = self.driver.visible_text(None if frame == "top" else frame, 160)
            parts.append(f"[{frame} {urlsplit(url).path or url}] {text}")
        return self.redactor.text(" | ".join(parts))

    def _target_failure(self, step: Step, exc: DriverError, category: str) -> Failure:
        return Failure(category=category, step=step.id,  # type: ignore[arg-type]
                       expected=f"exactly one control for {step.target.description if step.target else '?'}",
                       observed=self._observed(), detail=self.redactor.obj(exc.detail))

    # ---- evidence ------------------------------------------------------------------------
    def _failure_evidence(self, failure: Failure) -> list[str]:
        files = []
        try:
            files.append(self.driver.screenshot(self.log.path("failure.png"), self.redactor.mask_patterns()).name)
            obs = self.driver.observe()
            snap = "\n\n".join(f"=== frame {f.name or 'top'} {f.url}\n{f.snapshot}" for f in obs.frames)
            self.log.path("failure_snapshot.txt").write_text(self.redactor.text(snap), encoding="utf-8")
            files.append("failure_snapshot.txt")
        except Exception as exc:                      # evidence must never mask the real failure
            self.log.event("evidence_error", error=str(exc))
        return files

    def _intervention(self, cap: Capability, e: _Escalate) -> str:
        shot = self.driver.screenshot(self.log.path("intervention.png"), self.redactor.mask_patterns())
        self.log.write_json("intervention.json", {
            "run_id": self.log.run_id, "kind": "replay", "capability": cap.id, "version": cap.version,
            "step": e.step, "reason": e.reason, "location": self.driver.frame_urls(),
            "screen": self._observed(), "screenshot": shot.name,
            "completed_steps": [r.id for r in self._records if r.status == "ok"],
        })
        self.log.event("intervention_requested", reason=e.reason, step=e.step)
        return "intervention.json"
