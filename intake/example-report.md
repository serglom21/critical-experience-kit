# Journey intake — resolved candidates

**6** candidate journeys · **1** spec-ready · 1 excluded

| Status | Count | Meaning |
| --- | --- | --- |
| corroborated | 2 | Declared *and* found by discovery — highest confidence |
| declared_unconfirmed | 1 | Customer says it matters, no corroborating evidence. **A finding, not an error** |
| proposed | 3 | Discovery found it, nobody declared it. Propose; do not assume it matters |

## Worklist

Ranked: declared first, then human-assigned impact, then spec-readiness. Volume tiebreaks *only* once a human has assigned impact — before that it is inert, because frequency is not importance.

| # | Journey | Status | Impact | Sources | Ready | Roles |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Checkout | corroborated | critical | declared, discovered:code | yes | `+1 +2 +3 +4 +5 +6 +7` |
| 2 | Refund Request | declared_unconfirmed | critical | declared | no | `+1 +2 +3 -4 -5 -6 -7` |
| 3 | Subscription Upgrade | corroborated | important | declared, discovered:telemetry | no | `+1 +2 +3 +4 -5 -6 -7` |
| 4 | Health Probe | proposed | — | discovered:telemetry | no | `+1 -2 +3 -4 -5 -6 -7` |
| 5 | Password Reset | proposed | — | discovered:code | no | `+1 -2 +3 -4 -5 -6 -7` |
| 6 | Plan Downgrade | proposed | — | discovered:code | no | `+1 +2 +3 -4 -5 -6 -7` |

## Findings — declared but not corroborated

Raise each of these with the customer. Wording that works: *you told me this matters and I can find no evidence of it in your code or telemetry.* Three explanations, all worth knowing: it lives in a service you can't see, it's aspirational, or it's completely dark today.

- **Refund Request** (critical) — owner dana@customer.example

## Proposed — discovered, not declared

Discovery's real value: journeys nobody named on the call. Refunds, admin paths, retry-after-failure, plan downgrade. Each needs a human to confirm it matters, name it, and assign outcome semantics.

- **Health Probe** (discovered:telemetry, confidence high · 9,200,000 observed)
- **Password Reset** (discovered:code, confidence medium)
- **Plan Downgrade** (discovered:code, confidence medium)

> Volume shown for context only. It does not affect ranking for candidates with no human-assigned business impact — a high-volume probe must not outrank a low-volume refund flow. Mark noise with `excluded: true` rather than hoping a heuristic will catch it.

## Excluded

Human-marked noise. Reported for auditability, dropped from the worklist.

- **Static Asset Load** · 1,872,000 observed — resource.* spans from the CDN. No business journey, no owner, no action available.

## Per-journey detail

### Checkout  ·  `checkout`

- Status: **corroborated** · sources: declared, discovered:code · confidence: high
- Business impact: critical · owner: dana@customer.example

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Checkout |
| 2 correlation key | filled | `checkout.id` |
| 3 step marker | filled | cart_reviewed → shipping_submitted → payment_submitted → payment_authorized → inventory_reserved → order_created → confirmation_viewed |
| 4 outcome | filled | `checkout.outcome` = **completed**, failed, abandoned, rejected |
| 5 failure reason | filled | `payment.decline_reason` |
| 6 magnitude | filled | cart.value, order.value |
| 7 actor segment | filled | user.plan_tier, payment.method |

### Refund Request  ·  `refund_request`

- Status: **declared_unconfirmed** · sources: declared · confidence: high
- Business impact: critical · owner: dana@customer.example

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Refund Request |
| 2 correlation key | filled | `refund.id` |
| 3 step marker | filled | refund_requested → refund_approved → refund_settled |
| 4 outcome | MISSING | — |
| 5 failure reason | MISSING | — |
| 6 magnitude | MISSING | — |
| 7 actor segment | MISSING | — |

**Blockers before spec generation**

- role 4 outcome not defined

**Carry into the spec as `[NEEDS CLARIFICATION]`**

- [4 outcome] not defined — ask the customer
- [5 failure reason] not defined — ask the customer
- [6 magnitude] not defined — ask the customer
- [7 actor segment] not defined — ask the customer

### Subscription Upgrade  ·  `subscription_upgrade`

- Status: **corroborated** · sources: declared, discovered:telemetry · confidence: medium
- Business impact: important
- Observed volume: 1,420

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Subscription Upgrade |
| 2 correlation key | filled | `upgrade.id` |
| 3 step marker | filled | plan_selected → payment_authorized → entitlement_granted |
| 4 outcome | filled | `upgrade.outcome` = **completed**, failed, abandoned |
| 5 failure reason | MISSING | — |
| 6 magnitude | MISSING | — |
| 7 actor segment | MISSING | — |

**Blockers before spec generation**

- outcome defines non-success values (abandoned, failed) but no failure reason attribute

**Carry into the spec as `[NEEDS CLARIFICATION]`**

- Is entitlement granted synchronously, or by an async worker that could fail after payment succeeds?
- [5 failure reason] not defined — ask the customer
- [6 magnitude] not defined — ask the customer
- [7 actor segment] not defined — ask the customer

### Health Probe  ·  `health_probe`

- Status: **proposed** · sources: discovered:telemetry · confidence: high
- Business impact: **unassigned**
- Observed volume: 9,200,000

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Health Probe |
| 2 correlation key | MISSING | — |
| 3 step marker | filled | probe_received → deps_checked |
| 4 outcome | MISSING | — |
| 5 failure reason | MISSING | — |
| 6 magnitude | MISSING | — |
| 7 actor segment | MISSING | — |

**Blockers before spec generation**

- role 2 correlation key not defined
- role 4 outcome not defined

**Carry into the spec as `[NEEDS CLARIFICATION]`**

- [4 outcome] not defined — ask the customer
- [5 failure reason] not defined — ask the customer
- [6 magnitude] not defined — ask the customer
- [7 actor segment] not defined — ask the customer
- [business impact] unassigned — must be set by a human, never derived from volume

### Password Reset  ·  `password_reset`

- Status: **proposed** · sources: discovered:code · confidence: medium
- Business impact: **unassigned**

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Password Reset |
| 2 correlation key | MISSING | — |
| 3 step marker | filled | reset_requested → email_sent → password_changed |
| 4 outcome | MISSING | — |
| 5 failure reason | MISSING | — |
| 6 magnitude | MISSING | — |
| 7 actor segment | MISSING | — |

**Blockers before spec generation**

- role 2 correlation key not defined
- role 4 outcome not defined

**Carry into the spec as `[NEEDS CLARIFICATION]`**

- [4 outcome] not defined — ask the customer
- [5 failure reason] not defined — ask the customer
- [6 magnitude] not defined — ask the customer
- [7 actor segment] not defined — ask the customer
- [business impact] unassigned — must be set by a human, never derived from volume

### Plan Downgrade  ·  `plan_downgrade`

- Status: **proposed** · sources: discovered:code · confidence: medium
- Business impact: **unassigned**

| Role | State | Value |
| --- | --- | --- |
| 1 journey | filled | Plan Downgrade |
| 2 correlation key | filled | `subscription.id` |
| 3 step marker | filled | downgrade_requested → proration_calculated → entitlement_revoked |
| 4 outcome | MISSING | — |
| 5 failure reason | MISSING | — |
| 6 magnitude | MISSING | — |
| 7 actor segment | MISSING | — |

**Blockers before spec generation**

- role 4 outcome not defined

**Carry into the spec as `[NEEDS CLARIFICATION]`**

- Revenue-affecting but nobody named it. Does the business track downgrade completion rate?
- [4 outcome] not defined — ask the customer
- [5 failure reason] not defined — ask the customer
- [6 magnitude] not defined — ask the customer
- [7 actor segment] not defined — ask the customer
- [business impact] unassigned — must be set by a human, never derived from volume

---

Generated by `intake/resolve.py`. Roles are the seven in `GRAMMAR.md`. Missing semantic roles (4–7) are expected on discovered journeys — a human owns those, and guessing them is what produces instrumentation nobody asked for.
