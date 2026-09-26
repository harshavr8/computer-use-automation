"""CLI.

  python -m cua discover --goal "Look up member {member_id} and read their savings balance" \\
      --param member_id=12345
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from .config import load_dotenv


def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep:
            raise SystemExit(f"--param expects NAME=VALUE, got {item!r}")
        out[k.strip()] = v
    return out


def cmd_discover(args: argparse.Namespace) -> int:
    from .agent.loop import DiscoveryAgent, DiscoveryConfig
    from .agent.planner import AnthropicPlanner
    from .driver.web_playwright import WebPlaywrightDriver
    from .evidence.runlog import RunLog, new_run_id
    from .runtime.actuator import Actuator
    from .safety.policy import Policy, PolicyGate
    from .safety.redact import Redactor

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (put it in .env or your shell).", file=sys.stderr)
        return 2
    if args.reset_target:   # test-harness convenience; deliberately NOT done through the agent
        req = urllib.request.Request(args.base_url + "/__admin/reset", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).close()

    redactor = Redactor()
    log = RunLog(Path(args.evidence_dir), new_run_id("discovery"), redactor)
    driver = WebPlaywrightDriver.launch(args.base_url, headless=not args.headed)
    try:
        actuator = Actuator(driver, PolicyGate(Policy.load(args.policy)), log, redactor)
        agent = DiscoveryAgent(driver, actuator, AnthropicPlanner(args.model), log, redactor)
        result = agent.run(DiscoveryConfig(
            goal_template=args.goal, start_route=args.start, params=_kv(args.param),
            secrets={"MOCK_USER": "operator ID for sign-on", "MOCK_PASS": "password for sign-on"},
            max_steps=args.max_steps, timeout_s=args.timeout, send_screenshots=not args.no_screenshots,
        ))
    finally:
        log.close()
        driver.close()

    t = result.trace
    print(f"\nstatus:   {t.status}" + (f" ({t.outcome_code})" if t.outcome_code else ""))
    print(f"steps:    {len(t.steps)}")
    for s in t.steps:
        cp = f"  [checkpoint: {s.checkpoint.text!r}]" if s.checkpoint else ""
        err = f"  <- {s.error}" if s.error and s.status != "ok" else ""
        print(f"  {s.index:2}. {s.status:14} {s.action.describe() if s.action else s.tool}{cp}{err}")
    shown = result.outputs if args.show_outputs else {k: "<hidden: use --show-outputs>" for k in result.outputs}
    print(f"outputs:  {json.dumps(shown)}")
    print(f"evidence: {result.evidence_dir}")
    return 0 if t.status in ("success", "business_outcome") else 1


def cmd_compile(args: argparse.Namespace) -> int:
    from .artifact.compiler import CompileError, Compiler, load_trace
    from .artifact.profile import AppProfile
    from .artifact.store import CapabilityStore

    trace, raw = load_trace(args.trace)
    try:
        caps = Compiler(AppProfile.load(args.profile), variant=args.variant).compile(
            trace, raw, args.id, title=args.title)
    except CompileError as exc:
        print(f"compile failed: {exc}", file=sys.stderr)
        return 1
    store = CapabilityStore(args.out)
    for cap in caps:
        path, created = store.save(cap)
        state = "written" if created else "unchanged (identical to latest version)"
        print(f"{cap.id:32} -> {path}  [{state}]")
        for note in cap.review:
            print(f"    {note.severity:4} {note.step or '':22} {note.note}")
    print("\nAgent-facing contract:")
    print(json.dumps(caps[-1].tool_contract(), indent=2))
    return 0


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m cua")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run against the live app")
    d.add_argument("--goal", required=True, help="Goal; may reference params as {name}")
    d.add_argument("--param", action="append", default=[], help="Input NAME=VALUE (repeatable)")
    d.add_argument("--start", default="/login", help="Entry route")
    d.add_argument("--base-url", default="http://127.0.0.1:5001")
    d.add_argument("--policy", default="policy.yaml")
    d.add_argument("--evidence-dir", default="evidence")
    d.add_argument("--model", default=None, help="default: $CUA_MODEL or claude-sonnet-5")
    d.add_argument("--max-steps", type=int, default=20)
    d.add_argument("--timeout", type=int, default=300, help="seconds")
    d.add_argument("--headed", action="store_true", help="show the browser")
    d.add_argument("--no-screenshots", action="store_true", help="send the model text only")
    d.add_argument("--reset-target", action="store_true", help="reset mock app data first (harness only)")
    d.add_argument("--show-outputs", action="store_true", help="print raw output values to the terminal")
    d.set_defaults(func=cmd_discover)

    c = sub.add_parser("compile", help="compile a discovery trace into capability artifacts")
    c.add_argument("trace", help="path to evidence/<run>/trace.json")
    c.add_argument("--id", required=True, help="capability id, e.g. member.get_savings_balance")
    c.add_argument("--title", default=None)
    c.add_argument("--profile", default="profiles/cu-servicing.yaml")
    c.add_argument("--variant", default="base")
    c.add_argument("--out", default="capabilities")
    c.set_defaults(func=cmd_compile)

    args = p.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
