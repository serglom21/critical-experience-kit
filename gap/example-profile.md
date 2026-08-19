# Instrumentation profile — `acme-commerce`

Window: 30d · scope: all projects

## Custom Instrumentation Present

Both layers are in place. The work is completeness and correctness, not adoption.

| Layer | What we found |
| --- | --- |
| Automatic (SDK-provided) | 5 integration families · 6 attributes |
| Custom business | 4 span names · 9 attributes |
| Code-level (not business) | 2 span names |
| Custom share of span volume | 0.46% |

> A low custom share is normal, not a failure — Heap reports roughly 10% of the events in their own reports are manually tagged. **Zero** is the finding.

### Automatic instrumentation detected

| Integration family | Span volume |
| --- | --- |
| UI rendering | 23,062,785 |
| HTTP server (inbound requests) | 9,120,000 |
| Database | 4,210,500 |
| HTTP client (outbound calls) | 1,842,890 |
| Browser resources | 905,899 |

This is the syntactic layer: status codes, durations, query shapes. It cannot tell you what a request *meant* — that boundary is the entire case for custom instrumentation.

### Custom business spans

| Span name | Volume |
| --- | --- |
| `checkout` | 48,210 |
| `checkout.shipping_submitted` | 46,900 |
| `checkout.payment_submitted` | 44,100 |
| `checkout.payment_authorize` | 43,800 |

### Code-level spans (not business instrumentation)

Real spans, but they name a code location rather than a business step, so they answer *where time went* and not *what the user was trying to do*. Mostly SDK-derived function tracing.

| Span name | Volume |
| --- | --- |
| `checkout.inventory.reserve` | 41,050 |
| `checkout.order.create` | 38,200 |

> This split is the one **heuristic** in the profile — pattern-matching on the span description. `source_type` and op families are authoritative; this is not. Check the table before quoting it.

### Customer-defined attributes

`cart.currency`, `cart.item_count`, `cart.value`, `checkout.id`, `checkout.outcome`, `checkout.step`, `payment.method`, `upgrade.id`, `user.plan_tier`

Identified by `attributeSource.source_type == "user"` — a documented field, not a guess.

## Recommendations

### 1. Checkout: one rename, not new instrumentation  ·  _critical_

expected `checkout.payment.authorize`, found `checkout.payment_authorize`. The step is already emitting; every query against the bound name returns empty.

**Ask:** Rename the span. Cheapest fix in the whole engagement.

### 2. Checkout: funnel goes dark at payment_authorized  ·  _critical_

Drop-off from these steps is being attributed to the last instrumented step, so the owning team never sees it.

**Ask:** Instrument the missing steps before adding any new journey.

### 3. Checkout: outcome is a string enum, not a boolean  ·  _important_

Sentry renders boolean attributes as the strings 'true'/'false', and a binary collapses failed, abandoned, and rejected into one bucket. Those three route to three different teams.

**Ask:** `checkout.outcome` is type `boolean` — fix the type.

### 4. Checkout: magnitude `cart.value` is numeric  ·  _important_

A value stringified as "129.99" cannot be aggregated — sum() and p50() silently return nothing useful. The instrumentation looks correct and produces no answer.

**Ask:** `cart.value` is type `string`, expected `number` — fix the type.

### 5. Declared journeys whose correlation key already exists  ·  _important_

The correlation key is present for: Subscription Upgrade. Only the spans are missing.

**Ask:** Sequence these next — they are the shortest path to a working funnel.

---

Automatic vs custom is derived from two signals: `attributeSource.source_type` on `GET /trace-items/attributes/`, and span `op` families from Sentry's documented operation vocabulary. Neither is inferred from naming conventions alone.
