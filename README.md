# Computer-Use Automation System

An LLM discovers how to do a task in a legacy back-office UI. The run is compiled into a typed, versioned **capability** artifact, and that artifact is **replayed deterministically, with no LLM**, with new inputs, runtime-error handling, and a human handoff on the same live session.

- Design and trade-offs: [`REPORT.md`](REPORT.md)
- Demonstration runs: [`evidence/README.md`](evidence/README.md)

The target is a local mock of a credit-union member-servicing app (`mock_app/`). It is deliberately legacy-style (iframes, table layouts, no ids or test ids, unlabeled inputs) and has switchable runtime faults. All data is fictional.

## Setup

Requires Python 3.10+.

    python -m venv .venv
    source .venv/bin/activate            # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    python -m playwright install chromium
    cp .env.example .env                 # then fill in ANTHROPIC_API_KEY

`.env` holds:
- `ANTHROPIC_API_KEY`: needed **only** for `discover`
- `MOCK_USER` / `MOCK_PASS`: the mock app's fake operator credentials (`teller` / `demo-only`)
- `CUA_MODEL`: optional model override (default `claude-sonnet-5`)

`.env` is gitignored.

### Running without live services

- `pytest` runs the full suite: 105 tests, about 2.5 minutes. It needs **no API key**; discovery tests use a scripted planner, and browser tests start their own mock app on a random port.
- `compile`, `replay`, `scenarios` and `operator` never call a model.

## Demo path

**Terminal 1: the target app.**

    python -m mock_app --port 5001

**Terminal 2:**

**1. Discovery: a real LLM run.**

    python -m cua discover --headed --reset-target \
      --goal "Look up member {member_id} and read their current savings balance" \
      --param member_id=12345

This prints each step with its verified checkpoint, and writes `evidence/discovery-<ts>/`: `trace.json`, `events.jsonl`, `result.json`, and a masked `final.png`.

**2. Compile the trace into capabilities.**

    python -m cua compile evidence/<discovery-run>/trace.json --id member.get_savings_balance

This writes `capabilities/session.sign_on/v1.yaml` and `capabilities/member.get_savings_balance/v1.yaml`, then prints the review notes and the agent-facing tool contract.

**3. Replay deterministically with a different input. No LLM.**

    python -m cua replay member.get_savings_balance --input member_id=23456 --show-outputs

Expected output: `status: success`, `savings_balance: "15.00"`.

**4. Replay with errors and exceptional states.**

    python -m cua replay member.get_savings_balance --input member_id=99999    # business_outcome: member_not_found
    python -m cua scenarios                                                    # 10 fault-injected replays

**5. Human handoff on the live session.**

First, install the hand-authored capability that has an irreversible step:

    mkdir -p capabilities/member.open_club_account
    cp tests/fixtures/member.open_club_account.v1.yaml capabilities/member.open_club_account/v1.yaml

Then run it with handoff enabled:

    python -m cua replay member.open_club_account --handoff \
      --input member_id=12345 --input product=VC --input amount=40.00 --show-outputs

It pauses before *Confirm & Open*. From **terminal 3** (in the project folder, venv active):

    python -m cua operator status
    python -m cua operator claim latest --as <you>
    python -m cua operator done latest --as <you> --approve

Instead of `--approve`, you can click *Confirm & Open* yourself in the browser and then run `done`; replay re-syncs past the step you performed.

## Commands

| Command | What it does |
|---|---|
| `python -m cua discover` | LLM-driven observe→decide→act loop (`--handoff` to hand a stuck run to a human) |
| `python -m cua compile` | Trace → versioned capability YAML |
| `python -m cua replay` | Deterministic replay (`--approve <id>` for irreversible steps, `--handoff` for a human) |
| `python -m cua scenarios` | Fault-injection replay suite, which writes evidence |
| `python -m cua operator` | `status` / `claim` / `done [--approve]` / `abort` for paused runs |
| `python -m cua.probe` | Print every control the agent would see, frame by frame |

## Layout

| Path | Contents |
|---|---|
| `mock_app/` | Legacy-style target with fault injection and a second tenant variant (`--variant tenant_b`) |
| `cua/core/` | Surface-neutral vocabulary: `Target`, locator strategies, `Action`, `Observation` |
| `cua/driver/` | `SurfaceDriver` protocol (the seam) and its Playwright implementation |
| `cua/runtime/actuator.py` | The single path from decide to act: resolve, policy, secrets, act, location check, log |
| `cua/agent/` | Discovery loop, tool schemas, prompt, planners (Anthropic, or scripted for tests) |
| `cua/artifact/` | Capability schema, compiler, versioned store, app-profile model |
| `cua/replay/` | Deterministic replay engine, result contract, scenario suite |
| `cua/control/` | Human handoff: control lease, operator command inbox, action capture |
| `cua/safety/` | Policy gate and redaction |
| `profiles/cu-servicing.yaml` | Per-product known conditions: business outcomes, recoveries, hard failures |
| `policy.yaml` | Allowlist and risk rules |
| `capabilities/` | Compiled artifacts (`<id>/v<N>.yaml`) |
| `evidence/` | Discovery, replay, and handoff runs (see `evidence/README.md`) |
| `tests/` | 105 tests; browser tests are marked `browser` |
