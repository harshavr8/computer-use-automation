# Design Write-up: Computer-Use Automation System

A small, end-to-end version of "the model discovers, the artifact becomes a capability, deterministic replay runs it in production". It runs against a deliberately legacy-style mock of a credit-union member-servicing app. Everything in Section 3 of the brief is implemented thinly but for real. Section 3.7 is design, with one piece demonstrated. `evidence/README.md` maps each claim below to a run.

## 1. Architecture

**Shape:** a single Python process driven by a CLI (`discover`, `compile`, `replay`, `scenarios`, `operator`). Artifacts and evidence are files in git. There are no services, queues, or database. The brief rewards a small, correct core over infrastructure, and nothing here needs concurrency to be demonstrated. Each boundary below is an interface, so it could become a service boundary later without changing the pieces on either side.

```
discover (LLM) ─┐                         ┌─ compile ─> capabilities/<id>/vN.yaml
                ├─> Actuator ─> SurfaceDriver (Playwright today)
replay (no LLM) ┘   (policy gate, secrets, redacted log, control lease)
```

**Key decisions:**

- **The target is a local mock app, deliberately hostile.** It is server-rendered, with table layouts, an iframe shell, no ids or test ids, text boxes with no accessible name, and no real headings. It has deterministic fault injection: a notice overlay, a transient 500, session expiry, and native `confirm()` dialogs. A public demo site couldn't produce these error states on demand, and they are the point of the exercise.
- **The seam is the `SurfaceDriver` protocol.** Agent, artifact, and replay speak only in surface-neutral terms: `Target` (a frame plus ranked locator strategies), `Action` (a closed vocabulary of navigate/click/fill/select/extract), and `TextCondition` ("this text is visible").
- **The `Actuator` is the only path from "decide" to "act".** Every action goes: resolve target, then policy gate, then resolve secret, then perform, then verify we are still inside the allowlist, then write a redacted log line. Discovery and replay both call it, so neither can skip the gate or the lease.
- **Perception is accessibility-first.** Each observe walks every frame (Playwright's accessibility snapshot does not descend into iframes, which the mock surfaced immediately). It indexes the controls and proposes locator strategies for each. It keeps only strategies that resolve uniquely to that control. The model can act only on controls with a verified locator, so anything it does is recordable by construction. A screenshot is sent alongside as secondary context.
- **The model proposes; the runtime verifies.** Claude (`claude-sonnet-5`) gets one stateless call per step: rules, goal, a compact log of prior steps, and the current observation, with `tool_choice: any`. Stateless calls keep cost flat and every decision reproducible from the log. The runtime checks everything the model claims:
  - refs must exist in the current observation
  - checkpoints must actually be visible, and must not contain data
  - `done` needs visible evidence

## 2. Artifact schema

A capability (`capabilities/<id>/vN.yaml`, Pydantic-validated) has two readers:

- **The calling agent** needs only the contract: `id`, `description`, typed `inputs` (with patterns), typed `outputs` (with sensitivity), and the `outcomes` it may receive. `tool_contract()` exports exactly that as a function-calling definition. It contains no steps and no locators.
- **The human reviewer** reads the `steps`. Each has an `intent` in operator language, a target with ranked strategies, a `postcondition`, the business outcomes it may trigger, and a risk class. Compiler `review` notes at the bottom flag anything worth a second look: a single-strategy locator, an input pattern inferred from one example, a click with no verified postcondition.

Shaping decisions:

- **Sign-on is its own capability.** Business capabilities declare `requires: {session: session.sign_on, start_state: ...}`. Twenty capabilities per app should not each re-record the login, and replay runs sign-on only when the start state isn't already visible.
- **Business outcomes are in the artifact; recoveries are not.** Outcomes like `member_not_found`, `access_denied`, and `validation_error` are part of what the caller is promised, so the compiler copies them in (with provenance) and attaches each one to the step that can trigger it. Recoveries (dismiss a notice, reload a transient error, re-sign-on) are app weather shared by every capability. They live in a per-product **app profile** (`profiles/cu-servicing.yaml`). This also answers "how does replay know about errors discovery never saw": a human writes them down once per product, not per capability or per tenant.
- **Values are placeholders, credentials are references.** Steps hold `{{inputs.member_id}}`, never `12345`; a test asserts no run-specific value survives anywhere, including step intents. Credentials appear only as `{secret: MOCK_PASS}`.
- **Load-time integrity.** An artifact that loads is internally consistent: every template references a declared input, every extract fills a declared output, every expected outcome and secret is declared, and step ids are unique.
- **Versions mean "the procedure changed".** Versions are immutable. Recompiling an identical flow doesn't create v2, because a content hash ignores timestamps, status, and review notes. A new version in git is therefore always worth reviewing.

Locators are ranked by robustness for legacy UIs:
1. `role` + accessible name, which ports to desktop accessibility trees
2. `anchor_text`: "the text box after 'Member Number:'". This is how an operator finds an unlabeled field, and it is the workhorse here, since the app's inputs have no accessible names.
3. `field_name`: web-only, and meaningless to humans, but the form contract survives tenant relabeling
4. `table_cell` (row text × column header) and `labeled_value` for extraction

## 3. Determinism & error handling

**Resolution is consensus, not first-match.** Replay uses the best-ranked strategy that resolves to exactly one control, and checks every other strategy against it. If two strategies resolve to *different* controls, the result is `target_ambiguous`, a hard stop, never a guess. If a better-ranked strategy no longer matches, the step still works but is reported as **drift** (`degraded`). That is the early-warning signal for UI change.

**Replay waits on conditions, not time.** After every click or navigate, the engine polls the screen and checks in a **fixed order**:
1. business outcomes this step declared, which are returned to the caller
2. hard conditions from the profile, which fail the run
3. recoverable conditions, which trigger a recovery and continued polling
4. the step's postcondition
5. timeout, which is `postcondition_not_met`

The result contract has four statuses:

- `success`: typed outputs, e.g. `"2450.18"` parsed from `$2,450.18`
- `business_outcome`: a declared code, plus the app's own message where useful (e.g. `ERR-104 …`)
- `failed`: category, step, expected, observed (plain text per frame, redacted), a masked screenshot, and a redacted page snapshot
- `escalated`: needs a human

Recoveries are reported but don't change the status: the caller asked for a balance, not a weather report. Cheap checks run before a browser opens: missing secrets or dependencies give `config_error`, and inputs violating the contract give `input_invalid`.

**Recoveries are bounded and conservative.** Each has a maximum attempt count, and running out is its own category (`recovery_exhausted`):
- A transient error is reloaded **with a GET**, never a form resubmit, and never after an irreversible step.
- Session expiry re-signs on and restarts from step 1, *unless* an irreversible step already ran. Then it escalates, because only a human can check whether money moved.
- Native JS dialogs are never auto-accepted. Playwright's default dismisses them, which silently cancels commits, so the driver dismisses and records them unless a step explicitly expects one.

**Evidence:** `python -m cua scenarios` replays 10 cases with injected faults: success, a new input, not-found, access-denied, app validation, bad input, notice overlay, transient 500, session expiry, and persistent 500. Each run writes its own evidence folder.

**The real discovery runs changed the design.** The first real run got stuck, and the trace showed three causes:
1. The model *predicted* a checkpoint for a screen it had never seen ("MAIN MENU").
2. It omitted the frame on an extract.
3. It offered a whole table row (balance included) as `done` evidence three times, because my feedback just said "not visible".

The fixes:
1. Checkpoints are now *confirmed* on the next turn from the visible screen.
2. Frameless extracts search every frame.
3. Sensitive-data refusals are checked first and explained specifically.

The second run succeeded cleanly. Both runs are kept as evidence.

## 4. Heterogeneity & multi-tenant

**Other surfaces.** A new surface means a new `SurfaceDriver`; artifacts, the compiler, and replay don't change.
- **Legacy web** is what the mock already is. Frames are first-class in `Target`.
- **Desktop (Windows):**
  - A UIA driver would map `role` strategies to ControlType + Name, and `anchor_text` to the preceding element in the UIA tree's reading order.
  - `table_cell` would map to the Grid/Table patterns, and frames would map to windows and panes.
  - Owner-drawn controls with no accessibility tree fall back to OCR text anchors, then coordinates. Coordinates are the last resort, always flagged in review, and never the only strategy.
- There is no CSS or XPath in any artifact; `TextCondition` ("visible text") works identically everywhere.

**Reuse across tenants.** Artifacts belong to the **vendor product**, not the tenant. The app profile is keyed by product and version. A tenant gets a thin **override overlay** keyed by step id that replaces only what differs: a relabeled button's strategies, a renamed column header. The base artifact plus an overlay produces an effective capability, and it is the content hash of *that* capability that's versioned and approved.

Drift is detected three ways:
- `degraded` locator hits on real replays, per tenant
- replay success rate per tenant and version
- canary replays whenever a tenant's product version changes

A recurring degradation across tenants signals a vendor UI change: re-record once on the base and re-verify the overlays.

The mock's `tenant_b` variant (same product, relabeled) demonstrates the failure mode this design addresses. Replaying the base capability there, the text box whose label changed ("Member Number" → "Account #") still resolved via its `field_name` fallback, and was flagged as drift. The button renamed "Search" → "Find" failed cleanly with `target_not_found`, which is exactly what an overlay would fix. Overlays are designed but not built.

## 5. Escalation & handoff

**Detecting "stuck":**
- **In discovery:** the model calls `request_help`; three consecutive failed or rejected actions; the same action repeated with no change in location; the step or time budget runs out; or any irreversible click without approval.
- **In replay:** an escalation (e.g. an irreversible step with no approval), or a hard failure that a human could plausibly fix (target not found, postcondition not met, recovery exhausted).

Every handoff carries an intervention request with the capability or goal, the step, the reason, the redacted screen text, and a masked screenshot.

**The control-transfer model is a lease with one holder:**

`automation → awaiting_human → human → resuming → automation`

- The Actuator checks the lease before every action, so automation *cannot* act while a human holds the session.
- `control.json` in the run folder shows the holder and full transition history.
- The operator claims, finishes (optionally with `--approve`), or aborts from a second terminal, via command files in the run's evidence folder.
- The human works in the headed browser automation was using, with the same cookies and the same page. That is why `--handoff` implies headed.
- Listeners injected into every frame record the human's clicks, entries (passwords as `[SECRET]`), and navigations.

**Handing back:** replay re-syncs by resuming after the *newest recorded checkpoint visible on screen*.
- If the human finished the failed step, replay continues after it.
- If they went back a screen, replay redoes from there.
- If no checkpoint is recognizable, the result is `resync_failed`, not a guess.
- It never resumes at a point that would repeat an irreversible step.

**Approving differs from doing.** `--approve` lets automation perform the pending step under the operator's name, recorded in the policy log. Clicking it yourself makes replay re-sync past it. Discovery can hand off too; the compiler then refuses that trace, because the human's clicks were never located and verified.

**Mocked:** the operator console is a CLI plus command files. In production this becomes an intervention queue with assignment and SLAs, a web console, and the live session streamed over CDP screencast or VNC, while keeping the same lease and command semantics.

## 6. Safety

- **Allowlist:** `policy.yaml` covers origins, route globs, explicitly denied routes (the mock's `/__admin` fault controls are denied even though they are same-origin), and allowed action types. Every frame's location is re-checked *after* each action; leaving the allowlist stops the run.
- **Risk classes:**
  - Every action is `read_only`, `reversible`, or `irreversible`, by first-matching rule on action type, route, and control text.
  - Irreversible actions require an `Approval` in both discovery and replay. By default, a missing approval escalates to a human (`require_confirmation`); `block` and `flag` are configurable.
  - I chose confirmation over blocking because at a bank the irreversible steps are usually the point of the capability. They just need a named human to own them.
- **Secrets** are references resolved at the last moment, from environment variables in this demo and a vault in production. The model never sees their values.
- **Redaction:** every log, trace, and result write passes through one redactor. It combines pattern scrubbers (SSN, card via Luhn, account numbers, phone, email, dollar amounts) with a registry of known-sensitive values, such as extracted PII or financial outputs and resolved secrets. The registry also drives black-box masks on screenshots. Checkpoints and `done` evidence that contain sensitive data are refused, so none can end up in an artifact. A scan of all evidence finds no balances or passwords.

**Limits, stated plainly:**
- **Names have no pattern.** They're only scrubbed once registered. My first real run leaked a (fictional) member name through the model's free-text `summary`. The prompt now forbids member data in free text, but that is defense in depth, not a guarantee.
- **The model provider sees raw page content** during discovery. Production needs zero-retention enterprise terms or a self-hosted model, and should run discovery on synthetic or test tenants where possible. Replay involves no model at all.
- **Risk classification by control text can miss** a commit button with an unusual label. Mitigations: route-scoped rules, and human review of each artifact's per-step `risk` before approval.
- **Page text could attempt prompt injection** during discovery. The blast radius is bounded by the closed action vocabulary, the policy gate, and approval for irreversible actions.

## 7. Cuts

Deliberately left out, roughly in the order I'd build them next:

1. **Tenant override overlays** (designed in section 4). This is the biggest lever for the real environment, and the `tenant_b` variant is already there to test against.
2. **Approval gating.** Artifacts carry `draft`/`approved`, but replay doesn't yet refuse unattended runs of drafts. Add that gate, plus a replay-N-times stability score as the evidence for approval.
3. **Bounded assisted fallback.** On `target_not_found`, allow one policy-checked LLM attempt to re-locate that single control, recorded as evidence and proposed as a new strategy for review.
4. **Retry-from-checkpoint** for failed form submissions. Today's transient-error recovery is a GET reload, which is safe but can't recover a lost POST.
5. **Recording human steps** as replayable steps, by locating and verifying the controls the human used, so a rescued discovery can compile.
6. **A desktop (UIA) driver** behind the same protocol.
7. **A real operator console and session streaming** (section 5), and a vault-backed secret store.

Also out of scope by the brief's guidance: queues, multi-tenant storage, and scaling infrastructure.
