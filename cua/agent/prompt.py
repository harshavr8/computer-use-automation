"""Prompt construction. Each model call is stateless: system rules + goal + a compact
log of prior steps + the CURRENT observation. The model's memory is our step log,
not a growing chat transcript - that keeps cost flat and every call reproducible."""
from __future__ import annotations

from ..core.observation import Observation
from .trace import TraceStep

SYSTEM = """You operate a legacy back-office banking application on behalf of an automation system.
Your successful run will be recorded and replayed later WITHOUT you, so act like you are
demonstrating the cleanest possible procedure to a new operator.

Rules
- Act only through the tools. Call exactly one tool per turn.
- Only use control refs listed under "Controls" in the CURRENT observation. Refs change every turn.
- Controls marked (no stable locator) cannot be recorded; pick another way or request_help.
- When typing a value that is one of the declared inputs, use fill(param=...), never the literal.
- Sign-on credentials are only available as secrets: use fill(secret=...). You never see their values.
- Take the shortest reliable path. No exploratory clicks, no unnecessary navigation.
- Checkpoints: you cannot know a screen's text before you see it, so do not guess. After a click or
  navigate that changes the screen, on your NEXT turn pass arrived_text: exact text from ONE element of
  the screen you now see (its title bar, e.g. 'MBR INQ - MEMBER INQUIRY'). The runtime verifies it and
  records it as the replay checkpoint for that step. Steps marked "needs arrived_text" are waiting on this.
- Read requested values with extract (by table_cell or labeled_value). Give the frame the value is in;
  if unsure, omit frame and the runtime searches all frames. Then call done.
- done.evidence_text must be the text of ONE element on the current screen that proves the result
  (a screen title or an application message). Never use a data value (balance, name, phone): it is
  sensitive and different for every member, so it can never be a checkpoint and will be refused.
- If the application says the task cannot be done for a legitimate business reason (no such record,
  not authorized, validation rule), that is a result, not an error: call done(result=business_outcome).
- Never click anything that commits money or records (confirm, submit, post, transfer) unless the goal
  explicitly says to complete it; the runtime will ask a human to approve such steps.
- Never repeat a call the runtime just rejected; read the feedback and change what it names.
- Your why and summary texts are logged. Never put member data in them (names, balances, phones,
  addresses); refer to outputs by name, e.g. "read savings_balance".
- If you are stuck, the screen is unexpected, or you are unsure, call request_help. Do not guess.
"""


def render(goal: str, params: dict[str, str], secrets: dict[str, str], obs: Observation,
           history: list[TraceStep], feedback: list[str], step_no: int, max_steps: int) -> str:
    lines = [f"GOAL: {goal}", ""]
    if params:
        lines.append("Declared inputs (use fill(param=NAME)): " +
                     ", ".join(f"{k} = {v!r}" for k, v in params.items()))
    if secrets:
        lines.append("Available secrets (use fill(secret=NAME)): " +
                     ", ".join(f"{k} ({desc})" for k, desc in secrets.items()))
    lines.append(f"Step {step_no} of at most {max_steps}.")
    lines.append("")

    if history:
        lines.append("Steps so far:")
        for s in history[-12:]:
            desc = s.action.describe() if s.action else s.tool
            mark = "ok" if s.status == "ok" else s.status.upper()
            extra = f" | checkpoint: {s.checkpoint.text!r}" if s.checkpoint else ""
            if s.status == "ok" and s.tool in ("click", "navigate") and not s.checkpoint and s.index > 1:
                extra += " | needs arrived_text"
            if s.expect_text_unverified:
                extra += f" | expected {s.expect_text_unverified!r} NOT seen"
            if s.error:
                extra += f" | error: {s.error}"
            lines.append(f"  {s.index}. [{mark}] {desc}{extra}")
        lines.append("")

    if feedback:
        lines.append("Runtime feedback on your last action:")
        lines.extend(f"  - {f}" for f in feedback)
        lines.append("")

    if obs.dialogs:
        lines.append("Native dialogs that appeared (auto-dismissed): " +
                     "; ".join(f"{d.kind}: {d.message!r}" for d in obs.dialogs))
        lines.append("")

    lines.append(f"CURRENT OBSERVATION  (page title: {obs.title!r})")
    for f in obs.frames:
        lines.append(f"--- frame: {f.name or 'top'}   url: {_path(f.url)}")
        snap = f.snapshot if len(f.snapshot) < 5000 else f.snapshot[:5000] + "\n  ...(truncated)"
        lines.append("Accessibility snapshot:")
        lines.append(snap)
        lines.append("Controls:")
        if not f.elements:
            lines.append("  (none)")
        for e in f.elements:
            label = e.name or e.text or (f"after {e.anchor_text!r}" if e.anchor_text else "")
            flag = "" if e.candidates else "  (no stable locator)"
            lines.append(f"  [{e.ref}] {e.role} {label}{flag}")
    return "\n".join(lines)


def _path(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return p.path or url
