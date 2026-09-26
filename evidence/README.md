# Evidence

Every run writes its own folder. All values are redacted at write time, and screenshots are masked.

| Folder | What it shows |
|---|---|
| `discovery-20260926T044142Z-28d536` | **First real LLM run: escalated as stuck.** The model predicted a checkpoint it hadn't seen, omitted a frame, and offered a table row with a balance as `done` evidence three times. The system stopped and wrote `intervention.json`. These findings drove the fixes described in REPORT §3. |
| `discovery-20260926T045800Z-37c647` | **Real LLM run: success.** 7 steps, verified checkpoints on both clicks, input recorded as `{{inputs.member_id}}`, and credentials as secret references only. Its `trace.json` compiles into `capabilities/member.get_savings_balance/v1.yaml`. |
| `replay-happy-path-*` | Replay of the compiled capability with the recorded input, no LLM |
| `replay-new-input-*` | Same capability with a member the model never saw (`savings_balance: 15.00`) |
| `replay-not-found-*` | `business_outcome: member_not_found` (MBR-404), not a failure |
| `replay-access-denied-*` | `business_outcome: access_denied` (SEC-403) |
| `replay-app-validation-*` | `business_outcome: validation_error`, with the app's own message |
| `replay-bad-input-*` | `failed: input_invalid`, rejected before a browser opens |
| `replay-notice-dialog-*` | Recovered: maintenance overlay dismissed; status still `success` |
| `replay-transient-500-*` | Recovered: error page reloaded with a GET |
| `replay-session-expired-*` | Recovered: re-signed on and restarted |
| `replay-persistent-500-*` | `failed: recovery_exhausted`, with expected/observed, `failure.png`, and `failure_snapshot.txt` |
| handoff runs (`replay-*` with `control.json`) | Human handoffs. `control.json` holds the lease history, and `human_actions-*.json` records what the operator did. One run is an approval of an irreversible step; one is a human rescuing a failed replay. |

**Files in a run folder:**

| File | Contents |
|---|---|
| `events.jsonl` | Redacted structured log: model decisions (discovery), policy verdicts, actions with the locator used, recoveries, and control transitions |
| `trace.json` | Discovery only: the structured recording the compiler consumes |
| `result.json` | Final status and outputs (sensitive outputs redacted) |
| `failure.png`, `failure_snapshot.txt` | The richer failure signal: masked screenshot and redacted page snapshot |
| `intervention.json`, `handoff-*.png` | Escalations and handoff requests |

**Example artifact:** [`example-artifact.member.get_savings_balance.v1.yaml`](example-artifact.member.get_savings_balance.v1.yaml) is a copy of `capabilities/member.get_savings_balance/v1.yaml`, compiled from the successful discovery run above.
