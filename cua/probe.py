"""See the app the way the agent will: `python -m cua.probe --member 12345`.

Signs on through the guarded Actuator, opens a member, and prints every indexed
control per frame with its validated locator candidates. Also writes a masked
screenshot and the redacted event log to ./probe_out/<run-id>/.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .core.actions import Action, SecretRef
from .core.targets import AnchorTextStrategy, FieldNameStrategy, RoleStrategy, Target, strategy_label
from .driver.web_playwright import WebPlaywrightDriver
from .evidence.runlog import RunLog, new_run_id
from .runtime.actuator import Actuator
from .safety.policy import Policy, PolicyGate
from .safety.redact import Redactor


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:5001")
    p.add_argument("--member", default="12345")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    args = p.parse_args()
    os.environ.setdefault("MOCK_USER", "teller")
    os.environ.setdefault("MOCK_PASS", "demo-only")

    redactor = Redactor()
    log = RunLog(Path("probe_out"), new_run_id("probe"), redactor)
    drv = WebPlaywrightDriver.launch(args.base_url, headless=not args.headed)
    act = Actuator(drv, PolicyGate(Policy.load("policy.yaml")), log, redactor)
    try:
        act.do(Action(kind="navigate", route="/login"))
        for label, field, secret in [("Operator ID", "f1", "MOCK_USER"), ("Password", "f2", "MOCK_PASS")]:
            act.do(Action(kind="fill", value=SecretRef(secret=secret), target=Target(
                description=f"{label} box",
                strategies=(AnchorTextStrategy(text=label, control="textbox"), FieldNameStrategy(name=field)))))
        act.do(Action(kind="click", target=Target(description="Sign On", strategies=(
            RoleStrategy(role="button", name="Sign On"),))))
        act.do(Action(kind="fill", value=args.member, target=Target(
            description="Member Number box", frame="main",
            strategies=(AnchorTextStrategy(text="Member Number", control="textbox"), FieldNameStrategy(name="q1")))))
        act.do(Action(kind="click", target=Target(description="Search", frame="main", strategies=(
            RoleStrategy(role="button", name="Search"),))))

        obs = drv.observe()
        print(f"URL: {obs.url}\n")
        for f in obs.frames:
            print(f"== frame {f.name or 'top'}  {f.url}")
            for e in f.elements:
                cands = ", ".join(strategy_label(s) for s in e.candidates) or "(no unique locator!)"
                print(f"  {e.ref:12} {e.role:9} name={e.name!r:22} -> {cands}")
        shot = drv.screenshot(log.path("masked.png"), redactor.mask_patterns())
        print(f"\nmasked screenshot: {shot}\nredacted log:      {log.path('events.jsonl')}")
    finally:
        log.close()
        drv.close()


if __name__ == "__main__":
    main()
