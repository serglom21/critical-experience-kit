# Eval report — golden solutions

**6 run(s)** · clean rate **17%** · mean **91.7%** (median 89.3, σ 6.7, range 85.7–100.0) · 1 regression(s)

## Runs

| Task | Source | Verdict | Score | Passed | Failed | Regressions |
| --- | --- | --- | --- | --- | --- | --- |
| checkout-js | attribute-typo | **NOT CLEAN** | 100.0% | 9/9 | 0 | 1 |
| checkout-js | correct | CLEAN | 100.0% | 9/9 | 0 | 0 |
| checkout-js | deprecated-api | **NOT CLEAN** | 89.3% | 8/9 | 1 | 0 |
| checkout-js | duplicated-span | **NOT CLEAN** | 85.7% | 8/9 | 1 | 0 |
| checkout-js | indeterminate | **NOT CLEAN** | 89.3% | 8/9 | 1 | 0 |
| checkout-js | pii | **NOT CLEAN** | 85.7% | 8/9 | 1 | 0 |

## Where the spec fails — the tuning signal

Sorted by failure rate. A high rate here means the corresponding section of the spec is unclear, not that the agent is bad. Rewrite that paragraph and re-run.

| Check kind | Fail rate | pass | fail | indeterminate |
| --- | --- | --- | --- | --- |
| `span_renamed` | 17% | 5 | 1 | 0 |
| `no_deprecated_api` | 17% | 5 | 1 | 0 |
| `no_pii` | 17% | 5 | 1 | 0 |
| `attribute_numeric` | 8% | 11 | 1 | 0 |
| `span_present` | 0% | 6 | 0 | 0 |
| `attribute_not_boolean` | 0% | 6 | 0 | 0 |
| `literal_present` | 0% | 6 | 0 | 0 |
| `attribute_present` | 0% | 6 | 0 | 0 |

## Existing instrumentation broken

Each of these worked before the task. Requirement passes do not offset them.

- `checkout.id` — broken in 1 run(s)

### checkout-js / attribute-typo

- **GD-006** REGRESSION `checkout.id` — `checkout.id` is set, but so is the near-miss `checkoutId` — spans using the wrong key drop out of every journey query with no error

### checkout-js / deprecated-api

- **FR-008** (important, `no_deprecated_api`) — uses `.setData(` in src/orders/create.ts

### checkout-js / duplicated-span

- **FR-001** (critical, `span_renamed`) — both `checkout.payment_authorize` and `checkout.payment.authorize` exist — the span was duplicated rather than renamed

### checkout-js / indeterminate

- **FR-006** (important, `attribute_numeric`) — `order.value` set to a string — cannot be aggregated

### checkout-js / pii

- **FR-009** (critical, `no_pii`) — attribute key `user.email`; value of `user.email`: user.email; attribute key `card.number`

---

Static grading only — it proves the call sites exist with the right literal names and plausible types. Pair with `gap/analyze.py` against telemetry from the instrumented app for the runtime half.
