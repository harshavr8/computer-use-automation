# Computer-Use Automation System (interface.ai take-home)

> Work in progress. The final README (demo path: discover, then replay) lands with the agent and replay engine.

## Setup

    python -m venv .venv
    # Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
    pip install -r requirements.txt
    python -m playwright install chromium

## Run the tests

    pytest

Browser tests start their own copy of the mock app on a random port. If Chromium isn't installed, they are skipped with a message.

## Run the mock target and probe it

    python -m mock_app --port 5001          # terminal 1
    python -m cua.probe --member 12345      # terminal 2 (add --headed to watch)

The probe signs on through the policy-guarded actuator, opens a member, and prints every control the agent would see, frame by frame, with its validated locator candidates. It writes a masked screenshot and the redacted event log to `probe_out/`.

## Layout

| Path | Contents |
|---|---|
| `mock_app/` | Legacy-style target app with fault injection (see `mock_app/README.md`) |
| `cua/core/` | Surface-neutral vocabulary: `Target`, locator strategies, `Action`, `Observation` |
| `cua/driver/` | `SurfaceDriver` protocol (the seam) and its web implementation, `WebPlaywrightDriver` |
| `cua/safety/` | Policy gate (allowlist and risk classes) and redactor |
| `cua/runtime/actuator.py` | The only path from decide to act: resolve, gate, act, verify location, log |
| `cua/evidence/` | Redacted JSONL run log |
| `policy.yaml` | Guardrail configuration |
