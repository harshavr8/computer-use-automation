# Mock target: legacy CU member-servicing app

A local stand-in for a legacy core-banking back-office screen. All data is fictional.

It is deliberately hostile to automation, the way real legacy apps are:
- no doctype (quirks mode)
- table layouts and an iframe shell (`/app` loads `nav` and `main` frames)
- no `id`s or test ids, and non-semantic field names (`q1`, `f1`, `amt`)
- no `<label for>`, so text boxes have **no accessible name**
- no real headings, and no `<th>` elements

## Run

    python -m mock_app --variant base --port 5001

Sign on with operator `teller` / `demo-only`. These are fake credentials; override them with `MOCK_USER` / `MOCK_PASS`.

## Flows

1. **Member inquiry:** member search, then member detail, then read the savings (S00) balance.
2. **Open sub-account:** form, then review, then **Confirm & Open**. This last step is irreversible: it moves money and issues a confirmation number `SA-…`.

## Seed members

| Member | Behavior |
|---|---|
| 12345 | Normal member; savings $2,450.18 |
| 23456 | Savings $15.00, which triggers ERR-221 on larger deposits |
| 55555 | Restricted record: SEC-403 access denied |
| any other 5 digits | MBR-404 not found |
| non-5-digit input | ERR-104 validation error |

## Runtime faults

Set faults with `--faults '<json>'`, the `MOCK_FAULTS` env var, or at runtime with `POST /__admin/faults`. `POST /__admin/reset` restores seed data and clears all faults.

| Fault | Effect |
|---|---|
| `slow_ms` | Adds latency to every page |
| `notice_dialog` | Shows a "System Notice" overlay on member detail, covering the balance table |
| `native_confirm` | Adds a JS `confirm()` to Confirm & Open. Playwright dismisses (cancels) these by default |
| `session_timeout_s` | Idle timeout, after which the user lands on sign-on with SGN-009 |
| `fail_next` / `fail_route` | The next N matching requests return APP-500 |

All faults are deterministic, so evidence runs are reproducible.

The `/__admin` routes are test-harness only. They must be **outside** the agent's allowlist.

## Variants

`--variant tenant_b` runs the same product under different branding and labels: "Account #" instead of "Member Number", "Find" instead of "Search", "Current Bal" instead of "Balance", and so on. This is the stand-in for a second tenant on the same vendor product.
