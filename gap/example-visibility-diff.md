# Visibility diff — `acme-commerce`

Baseline: 30d window · Current: 30d window

> ## 1 regression(s)

> A rule that used to pass and now fails. Instrumentation rots — a refactor drops a span, an SDK upgrade renames an op, an attribute gets 'cleaned up'. Read this section first.

**8 finding(s) resolved** · **1 regression(s)** · **0 journey(s) improved coverage**

## Regressions

| Journey | Rule | Impact | Was | Now |
| --- | --- | --- | --- | --- |
| Checkout | CE-003 `checkout.shipping_submitted` | normal | expected span `checkout.shipping_submitted` — 46,900 observed | **expected span `checkout.shipping_submitted` — not found** |

**Checkout · CE-003 Step 2 `shipping_submitted` instrumented** — A missing step is a dark segment. Drop-off gets attributed to the step before it, so the owning team never sees the problem.

## Journeys

| Journey | Coverage | Score | Grade | Resolved | Regressed | Headline |
| --- | --- | --- | --- | --- | --- | --- |
| Checkout | 5/7 → 6/7 | 53.3 → 92.1 (+38.8) | needs improvement → excellent | 8 | 1 | 1 regression(s) |
| Health Probe | 0/2 | 16.7 (—) | poor | 0 | 0 | no change |
| Password Reset | 0/3 | 15.6 (—) | poor | 0 | 0 | no change |
| Plan Downgrade | 0/3 | 15.6 (—) | poor | 0 | 0 | no change |
| Refund Request | 0/3 | 15.6 (—) | poor | 0 | 0 | no change |
| Subscription Upgrade | 0/3 | 27.8 (—) | poor | 0 | 0 | no change |

## Checkout

- Dark segment **closed**: payment_authorized now emits.
- **New dark segment**: shipping_submitted.

**Resolved**

- CE-002 `checkout.confirmation_viewed` — Terminal outcome-bearing span present. Now: expected span `checkout.confirmation_viewed` — 40,560 observed
- CE-003 `checkout.payment.authorize` — Step 4 `payment_authorized` instrumented. Now: expected span `checkout.payment.authorize` — 46,810 observed
- CE-003 `checkout.confirmation_viewed` — Step 7 `confirmation_viewed` instrumented. Now: expected span `checkout.confirmation_viewed` — 40,560 observed
- CE-007 `checkout.outcome` — Outcome is a string enum, not a boolean. Now: `checkout.outcome` present (type `string`, source `user`)
- CE-008 `payment.decline_reason` — Failure reason present (outcome admits failure). Now: `payment.decline_reason` present (type `string`, source `user`)
- CE-009 `order.value` — Magnitude `order.value` present. Now: `order.value` present (type `number`, source `user`)
- CE-010 `cart.value` — Magnitude `cart.value` is numeric. Now: `cart.value` present (type `number`, source `user`)
- CE-013 — No span name drift. Now: none detected

**Still open**

- CE-012 (important) — shipping_submitted

**Newly measured** (rule evaluated now, absent from the baseline — usually a journey definition change, not a code change)

- CE-010 (order.value)

---

Scores are per journey and never ANDed. A score delta is a summary; the coverage transition and the resolved-rule list are the findings. Rule definitions in `rules.md`.
