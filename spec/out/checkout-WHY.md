# Why this matters — Checkout

Today this journey is **5 of 7 steps instrumented**, scoring 53.3 (needs improvement).

> CE-002 (no terminal outcome span) caps the score at 74

## Where you're blind right now

The journey goes dark at **payment_authorized**.

Everything downstream of that point is unattributable. Drop-off that actually happens in the dark segment gets recorded against the last instrumented step, so the team that owns the real problem never sees it.

- `checkout.payment_authorize` is being sent 43,800 times — this is the drifted step, one rename from working
- outcome admits non-success values: abandoned, failed, rejected

## What each gap costs you

**Terminal outcome-bearing span present**

A journey that commits its business effect but never records reaching a terminal state cannot distinguish success from failure — the most expensive possible blind spot in a funnel.

**Step 4 `payment_authorized` instrumented**

A missing step is a dark segment. Drop-off gets attributed to the step before it, so the owning team never sees the problem.

Extent: 100% of the 48,210 observed journey instances (approx.)

**No span name drift**

A near-miss name produces no error, no failing test, and no data — the team believes the step is instrumented while every query returns empty. Reported apart from 'missing' because the fix is a rename, not new instrumentation.

Example to open: `checkout.payment_authorize`

**Outcome is a string enum, not a boolean**

Sentry renders boolean attributes as the strings 'true'/'false', and a binary collapses failed, abandoned, and rejected into one bucket. Those three route to three different teams.

**Failure reason present (outcome admits failure)**

Without a coded reason every failure collapses into one undiagnosable bucket. Usually the highest-value attribute for the customer, because it is the one that routes a problem to an owning team.

**Magnitude `order.value` present**

The magnitude is what turns a latency chart into a revenue chart. Numeric span attributes are chartable in Trace Explorer with no setup.

**Magnitude `cart.value` is numeric**

A value stringified as "129.99" cannot be aggregated — sum() and p50() silently return nothing useful. The instrumentation looks correct and produces no answer.

## What you'll be able to see once this lands

- Completion rate for the whole journey, and drop-off per step, from `checkout.outcome`.
- Revenue at risk: `sum(cart.value)` grouped by `payment.decline_reason` — how much money each failure mode is costing.
- Which failure mode dominates, from `payment.decline_reason`, routed to the team that owns it.
- All of the above sliced by `user.plan_tier`, `payment.method`.
- Alerts and dashboard widgets built directly from any of these queries via **Save As** in Trace Explorer.

---

Every claim above is derived from measured gaps in your own Sentry org, not from a generic checklist. Rule definitions are in the gap analysis report.
