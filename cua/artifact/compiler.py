"""Compile a discovery trace into capability artifacts.

    trace.json  ->  session.sign_on (vN)          if the run signed on
                ->  <capability id> (vN)           the business flow

Rules
- Only steps with status "ok" become artifact steps; failures stay in the evidence.
- Sign-on is split into its own capability and referenced via requires.session,
  so every business capability doesn't re-record the same login.
- Verified checkpoints become postconditions; the done-evidence becomes success.
- Business outcomes are copied from the app profile for the screens this flow submits on.
- Anything a reviewer should look at becomes a review note, not a silent guess.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from ..agent.trace import DiscoveryTrace, TraceStep
from ..core.actions import SecretRef
from ..core.targets import strategy_label
from ..safety.policy import route_matches
from .profile import AppProfile
from .schema import (
    AppRef, Capability, InputSpec, OutcomeSpec, OutputSpec, Postcondition, Provenance, Requires,
    ReviewNote, SecretDecl, Step, Success, TextCondition,
)

COMPILER_VERSION = "0.1.0"
_RISK_ORDER = {"read_only": 0, "reversible": 1, "irreversible": 2}
_ROLE_WORDS = re.compile(r"\s+(textbox|button|link|combobox|checkbox|radio)$", re.I)


class CompileError(Exception):
    pass


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "step"


def _frame_path(step: TraceStep, which: str) -> str | None:
    loc = step.location_before if which == "before" else step.location_after
    frame = step.action.target.frame if step.action and step.action.target else None
    return loc.get(frame or "top")


def _template(text: str, params: dict[str, str]) -> str:
    for name, value in params.items():
        if value:
            text = text.replace(value, f"{{{{inputs.{name}}}}}")
    return text


class Compiler:
    def __init__(self, profile: AppProfile, variant: str = "base") -> None:
        self.profile, self.variant = profile, variant

    def compile(self, trace: DiscoveryTrace, trace_bytes: bytes, capability_id: str,
                title: str | None = None) -> list[Capability]:
        if trace.status != "success":
            raise CompileError(f"only successful discovery runs compile into capabilities (got {trace.status!r})")
        if trace.success is None:
            raise CompileError("trace has no verified success checkpoint")
        if any(s.tool == "human" and s.status == "ok" for s in trace.steps):
            raise CompileError(
                "trace contains human-performed steps; their controls were never located and verified, so "
                "they cannot be replayed. Re-run discovery (the human's actions show the path) or hand-author "
                "those steps.")
        ok = [s for s in trace.steps if s.status == "ok" and s.action is not None]
        prelude, body = self._split_sign_on(ok)
        provenance = Provenance(
            recorded_from_run=trace.run_id, model=trace.model, compiled_at=datetime.now(timezone.utc),
            compiler_version=COMPILER_VERSION, source_trace_sha256=hashlib.sha256(trace_bytes).hexdigest(),
        )
        app = AppRef(profile=self.profile.profile, product=self.profile.product, variant=self.variant)
        out: list[Capability] = []

        requires = Requires()
        if prelude:
            sign_on = self._sign_on(prelude, app, provenance)
            out.append(sign_on)
            requires = Requires(session=sign_on.id, start_state=sign_on.success.check)
        if not body:
            raise CompileError("nothing left to compile after sign-on")
        out.append(self._business(trace, body, capability_id, title, app, requires, provenance))
        return out

    # ---- sign-on ----------------------------------------------------------------------
    @staticmethod
    def _split_sign_on(steps: list[TraceStep]) -> tuple[list[TraceStep], list[TraceStep]]:
        """Prelude = everything up to the first click after the last secret was typed."""
        last_secret = max((i for i, s in enumerate(steps) if s.value_source == "secret"), default=None)
        if last_secret is None:
            return [], steps
        end = next((i for i in range(last_secret + 1, len(steps)) if steps[i].tool == "click"), None)
        if end is None:
            return [], steps
        return steps[: end + 1], steps[end + 1:]

    def _sign_on(self, prelude: list[TraceStep], app: AppRef, prov: Provenance) -> Capability:
        last = prelude[-1]
        if last.checkpoint is None:
            raise CompileError("the sign-on click has no verified checkpoint; cannot tell if sign-on worked")
        steps, notes = self._steps(prelude, {})
        secrets = [SecretDecl(name=s.action.value.secret, purpose=f"typed into {s.action.target.description}")
                   for s in prelude if s.action and isinstance(s.action.value, SecretRef) and s.action.target]
        return Capability(
            id=self.profile.session.sign_on_capability, title="Sign on",
            description="Establish an authenticated operator session and land on the home screen.",
            app=app, risk_class=self._risk(steps), secrets=secrets, steps=steps,
            outcomes=[], success=Success(check=steps[-1].postcondition.check), review=notes, provenance=prov,
        )

    # ---- business capability ---------------------------------------------------------
    def _business(self, trace: DiscoveryTrace, body: list[TraceStep], cap_id: str, title: str | None,
                  app: AppRef, requires: Requires, prov: Provenance) -> Capability:
        used = {s.param for s in body if s.param}
        inputs = {name: self._input_spec(name, trace.params[name]) for name in sorted(used)}
        params = {k: v for k, v in trace.params.items() if k in used}
        steps, notes = self._steps(body, params)

        outputs: dict[str, OutputSpec] = {}
        for s in body:
            if s.output:
                outputs[s.output.name] = OutputSpec(
                    type=s.output.type, sensitivity=s.output.sensitivity,
                    description=("Read from " + strategy_label(s.action.target.strategies[0])
                                 if s.action and s.action.target else "extracted value"),
                    parse="currency" if s.output.type == "decimal" else "none")

        outcomes: dict[str, OutcomeSpec] = {}
        for step, ts in zip(steps, body):
            if step.action not in ("click", "navigate"):
                continue
            here = {p for p in (_frame_path(ts, "before"), _frame_path(ts, "after")) if p}
            for cond in self.profile.business():
                if cond.on_routes and not any(route_matches(g, p) for g in cond.on_routes for p in here):
                    continue
                code = cond.result_code or cond.id
                outcomes.setdefault(code, OutcomeSpec(
                    code=code, description=cond.description, detect=cond.detect,
                    capture_message=cond.capture_message, source=f"profile:{self.profile.profile}#{cond.id}"))
                step.expected_outcomes.append(code)

        for name in set(trace.params) - used:
            notes.append(ReviewNote(severity="warn", note=f"run parameter {name!r} was never used; not an input"))
        for name, spec in inputs.items():
            notes.append(ReviewNote(severity="info", note=(
                f"input {name!r}: type/pattern inferred from one example ({spec.pattern}); tighten if the app is stricter")))

        success = Success(check=TextCondition(text=_template(trace.success.text, params), frame=trace.success.frame),
                          outputs_present=sorted(outputs))
        return Capability(
            id=cap_id, title=title or trace.goal_template.format(**{k: f"<{k}>" for k in trace.params}),
            description=self._description(trace, outputs),
            app=app, risk_class=self._risk(steps), requires=requires, inputs=inputs, outputs=outputs,
            steps=steps, outcomes=list(outcomes.values()), success=success, review=notes, provenance=prov,
        )

    # ---- steps ------------------------------------------------------------------------
    def _steps(self, trace_steps: list[TraceStep], params: dict[str, str]) -> tuple[list[Step], list[ReviewNote]]:
        steps: list[Step] = []
        notes: list[ReviewNote] = []
        seen: set[str] = set()
        for ts in trace_steps:
            a = ts.action
            assert a is not None
            if a.kind == "navigate":
                base = f"open_{slug(a.route or '')}"
            elif a.kind == "extract" and ts.output:
                base = f"read_{ts.output.name}"
            else:
                label = _ROLE_WORDS.sub("", a.target.description) if a.target else a.kind
                base = f"{a.kind}_{slug(label)}"
            sid, n = base, 2
            while sid in seen:
                sid, n = f"{base}_{n}", n + 1
            seen.add(sid)

            post = None
            if ts.checkpoint:
                post = Postcondition(check=TextCondition(text=_template(ts.checkpoint.text, params),
                                                         frame=ts.checkpoint.frame))
            steps.append(Step(
                id=sid, intent=_template(ts.why, params) or a.kind, action=a.kind, target=a.target,
                route=a.route, value=a.value, postcondition=post, risk=ts.risk or "reversible",  # type: ignore[arg-type]
                output=ts.output.name if ts.output else None,
            ))

            if a.kind in ("click", "navigate") and post is None:
                notes.append(ReviewNote(severity="warn", step=sid, note=(
                    "no verified postcondition: replay can only confirm this step through later steps")))
            if a.target and len(a.target.strategies) == 1 and a.kind != "extract":
                notes.append(ReviewNote(severity="info", step=sid, note=(
                    "single locator strategy: no cross-check, no fallback if it drifts")))
            if ts.degraded:
                notes.append(ReviewNote(severity="warn", step=sid, note="locator was already degraded at record time"))
            if ts.value_source == "literal":
                notes.append(ReviewNote(severity="info", step=sid, note=(
                    f"fixed literal value {a.value!r}: confirm it should not be an input")))
        return steps, notes

    # ---- helpers ----------------------------------------------------------------------
    @staticmethod
    def _input_spec(name: str, example: str) -> InputSpec:
        if re.fullmatch(r"\d+", example):
            return InputSpec(type="string", pattern=r"^\d+$", description=f"{name} (digits)")
        return InputSpec(type="string", description=name)

    @staticmethod
    def _risk(steps: list[Step]) -> str:
        return max((s.risk for s in steps), key=_RISK_ORDER.__getitem__)  # type: ignore[return-value]

    @staticmethod
    def _description(trace: DiscoveryTrace, outputs: dict[str, OutputSpec]) -> str:
        goal = trace.goal_template.replace("{", "<").replace("}", ">")
        returns = ", ".join(f"{n} ({o.type})" for n, o in outputs.items()) or "nothing"
        return f"{goal}. Returns {returns}."


def load_trace(path: str | Path) -> tuple[DiscoveryTrace, bytes]:
    raw = Path(path).read_bytes()
    return DiscoveryTrace.model_validate_json(raw), raw
