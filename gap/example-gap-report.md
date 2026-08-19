# Instrumentation gap analysis — `acme-commerce`

Window: 30d · sample rate: 0.25 · 11 distinct span names · 15 span attributes

**1 partially instrumented** (where visibility breaks) · **0 complete** · **5 not instrumented**

## Where visibility breaks

The actionable tier. These journeys are instrumented enough to be trusted and incomplete enough to mislead — the worst combination, because a partial funnel looks like a working one.

| Journey | Coverage | Score | Grade | Goes dark at |
| --- | --- | --- | --- | --- |
| Checkout | 5/7 steps | 53.3 | needs improvement | payment_authorized |

## Not instrumented

No spans found for any declared step. These are not low scores — they are work that hasn't started, so they carry no grade. Scoring them produces the same meaningless number for every one and drowns out the tier above.

| Journey | Steps | Correlation key | Notes |
| --- | --- | --- | --- |
| Password Reset | 3 declared, 0 found | absent | CE-004, CE-006, CE-009, CE-011 |
| Plan Downgrade | 3 declared, 0 found | absent | CE-004, CE-006, CE-009, CE-011 |
| Refund Request | 3 declared, 0 found | absent | CE-004, CE-006, CE-009, CE-011 |
| Health Probe | 2 declared, 0 found | absent | CE-004, CE-006, CE-009, CE-011 |
| Subscription Upgrade | 3 declared, 0 found | present | CE-006, CE-008, CE-009, CE-011 |

> An absent journey whose **correlation key is already present** is the cheapest possible win: the plumbing exists, only the spans are missing.

Scored per journey and reported as a distribution — never ANDed across journeys. That aggregation bug is why the OTel Demo scores 35 overall while every individual service scores higher.

## Not analyzed

- **Static Asset Load** — excluded during intake

## Detail

### Checkout — 53.3 (needs improvement) · 5/7 steps

> **Capped.** CE-002 (no terminal outcome span) caps the score at 74

**Goes dark at:** payment_authorized. Latency and drop-off from these steps are being attributed to the last instrumented step.

**Missing spans:** `checkout.payment.authorize`, `checkout.confirmation_viewed`

- `checkout.payment_authorize` is being sent 43,800 times — this is the drifted step, one rename from working
- outcome admits non-success values: abandoned, failed, rejected

| Rule | Impact | Result | Detail | Extent | Example |
| --- | --- | --- | --- | --- | --- |
| CE-001 | critical | pass | expected span `checkout` — 48,210 observed | — | `7f3c1a9b4e2d48f0a1c6b8e5d2409af1` |
| CE-002 | critical | **FAIL** | expected span `checkout.confirmation_viewed` — not found | — | — |
| CE-003 | normal | pass | expected span `checkout` — 48,210 observed | — | `7f3c1a9b4e2d48f0a1c6b8e5d2409af1` |
| CE-003 | normal | pass | expected span `checkout.shipping_submitted` — 46,900 observed | ~3% of journey instances lack this span (approx.) | `a18e6c05f9b74d23bc4a71e8305d9f6b` |
| CE-003 | important | pass | expected span `checkout.payment_submitted` — 44,100 observed | ~9% of journey instances lack this span (approx.) | `b2e91d47c8a34f6db05e3ac71f8d6204` |
| CE-003 | critical | **FAIL** | expected span `checkout.payment.authorize` — not found | 100% of the 48,210 observed journey instances (approx.) | — |
| CE-003 | important | pass | expected span `checkout.inventory.reserve` — 41,050 observed | ~15% of journey instances lack this span (approx.) | `d09b4e716fa24c85be3170dc529af846` |
| CE-003 | critical | pass | expected span `checkout.order.create` — 38,200 observed | ~21% of journey instances lack this span (approx.) | `c41d0f9a6b7e4238ad1c95e07b3f8621` |
| CE-003 | important | **FAIL** | expected span `checkout.confirmation_viewed` — not found | 100% of the 48,210 observed journey instances (approx.) | — |
| CE-012 | important | **FAIL** | payment_authorized | — | — |
| CE-013 | normal | **FAIL** | expected `checkout.payment.authorize`, found `checkout.payment_authorize` | — | `checkout.payment_authorize` |
| CE-004 | critical | pass | `checkout.id` present (type `string`, source `user`) | — | — |
| CE-005 | low | pass | `checkout.id` source is `user` | — | — |
| CE-006 | critical | pass | `checkout.outcome` present (type `boolean`, source `user`) | — | — |
| CE-007 | important | **FAIL** | `checkout.outcome` is type `boolean` | — | — |
| CE-008 | important | **FAIL** | `payment.decline_reason` not found in the org's span attributes | — | — |
| CE-009 | important | pass | `cart.value` present (type `string`, source `user`) | — | — |
| CE-009 | important | **FAIL** | `order.value` not found in the org's span attributes | — | — |
| CE-010 | important | **FAIL** | `cart.value` is type `string`, expected `number` | — | — |
| CE-011 | normal | pass | `user.plan_tier`, `payment.method` | — | — |

**Open one of these traces to see it:**

- `checkout` → trace `7f3c1a9b4e2d48f0a1c6b8e5d2409af1`
- `checkout` → trace `7f3c1a9b4e2d48f0a1c6b8e5d2409af1`
- `checkout.shipping_submitted` → trace `a18e6c05f9b74d23bc4a71e8305d9f6b`

**Why these matter** — use this language with the customer:

- **CE-002 Terminal outcome-bearing span present.** A journey that commits its business effect but never records reaching a terminal state cannot distinguish success from failure — the most expensive possible blind spot in a funnel.
- **CE-003 Step 4 `payment_authorized` instrumented.** A missing step is a dark segment. Drop-off gets attributed to the step before it, so the owning team never sees the problem.
- **CE-012 No dark segment between instrumented steps.** A gap in the middle is worse than a gap at the end: latency and drop-off from the dark steps are silently attributed to the last instrumented step. This is the finding that produces the 'visible to payment, then goes dark' sentence.
- **CE-013 No span name drift.** A near-miss name produces no error, no failing test, and no data — the team believes the step is instrumented while every query returns empty. Reported apart from 'missing' because the fix is a rename, not new instrumentation.
- **CE-007 Outcome is a string enum, not a boolean.** Sentry renders boolean attributes as the strings 'true'/'false', and a binary collapses failed, abandoned, and rejected into one bucket. Those three route to three different teams.
- **CE-008 Failure reason present (outcome admits failure).** Without a coded reason every failure collapses into one undiagnosable bucket. Usually the highest-value attribute for the customer, because it is the one that routes a problem to an owning team.
- **CE-009 Magnitude `order.value` present.** The magnitude is what turns a latency chart into a revenue chart. Numeric span attributes are chartable in Trace Explorer with no setup.
- **CE-010 Magnitude `cart.value` is numeric.** A value stringified as "129.99" cannot be aggregated — sum() and p50() silently return nothing useful. The instrumentation looks correct and produces no answer.

---

Rule definitions, weights, capping, and the extent/entity/example requirement are in `rules.md`. Extent is count-derived and approximate: it compares per-span-name totals rather than joining on the correlation key, so a step firing twice per journey understates the gap.
