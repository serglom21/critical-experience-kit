# Sentry Instrumentation Spec — Checkout

| | |
| --- | --- |
| **Journey** | Checkout (`checkout`) |
| **Current coverage** | 5/7 steps instrumented · score 53.3 (needs improvement) |
| **Measured against** | org `acme-commerce`, window 30d |
| **Minimum SDK** | `9.x`; `10.15.0+` for `setActiveSpanInBrowser` |
| **Requirements** | 9 |
| **Status** | Draft — review before implementing |

## 0. Instructions for AI coding agents

You are adding **only the missing pieces** of one business journey's Sentry instrumentation. Read this whole file first.

- Everything in §2 that is marked *present* already works. **Do not touch it.**
- Implement only the numbered requirements in §4.
- Use **exactly** the span names and attribute keys given. They are a contract: the verification queries in §7 match on these literal strings. A misnamed attribute produces no error, no failing test, and no data.
- Every API you need is in §5. **Do not use any Sentry API not listed there.** §6 lists APIs that will be in your training data and are removed or deprecated.
- Never put an ID, URL, email, or free-text message in a span `name` or `op`.
- If the codebase has no clear location for a required span, stop and ask. Do not invent one.
- When done, output the §4 list with each item marked and the file:line where you implemented it.

## 1. Why this journey

- `checkout.payment_authorize` is being sent 43,800 times — this is the drifted step, one rename from working
- outcome admits non-success values: abandoned, failed, rejected
- The journey currently goes dark at **payment_authorized**. Latency and drop-off from those steps are being attributed to the last instrumented step, so the owning team never sees the problem.

## 2. Span contract — current state

| # | Span name | Surface | Step | State |
| --- | --- | --- | --- | --- |
| 1 | `checkout` | browser | `cart_reviewed` | present, leave alone |
| 2 | `checkout.shipping_submitted` | browser | `shipping_submitted` | present, leave alone |
| 3 | `checkout.payment_submitted` | browser | `payment_submitted` | present, leave alone |
| 4 | `checkout.payment.authorize` | node | `payment_authorized` | **MISSING — add it** |
| 5 | `checkout.inventory.reserve` | node | `inventory_reserved` | present, leave alone |
| 6 | `checkout.order.create` | node | `order_created` | present, leave alone |
| 7 | `checkout.confirmation_viewed` | browser | `confirmation_viewed` | **MISSING — add it** |

## 3. Attribute contract

Sentry surfaces boolean attributes as the strings `'true'`/`'false'`, so no attribute here is a boolean.

| Key | Type | Role | State |
| --- | --- | --- | --- |
| `checkout.id` | string | correlation key (every span) | present |
| `checkout.outcome` | string enum (completed, failed, abandoned, rejected) | outcome (root span) | **FIX — `checkout.outcome` is type `boolean`** |
| `payment.decline_reason` | string, coded | failure reason | **FIX — `payment.decline_reason` not found in the org's span attributes** |
| `cart.value` | double (number) | magnitude | **FIX — `cart.value` is type `string`, expected `number`** |
| `order.value` | double (number) | magnitude | **FIX — `order.value` not found in the org's span attributes** |
| `user.plan_tier` | string | actor segment | present |
| `payment.method` | string | actor segment | present |

## 4. Requirements

**FR-001** — Rename the existing span so it matches the contract exactly. expected `checkout.payment.authorize`, found `checkout.payment_authorize`. Do **not** add a new span — the instrumentation already exists and is emitting; only the name is wrong. This single change also satisfies the step requirement for that span. *(rename, not new code)*

**FR-002** — A span named `checkout.confirmation_viewed` MUST be created in the browser runtime for step `confirmation_viewed`. Create it as a child of the surrounding span.

**FR-003** — `checkout.outcome` MUST be a string enum (completed | failed | abandoned | rejected), not a boolean. Sentry renders boolean attributes as the strings 'true'/'false', so the current value cannot express more than two states.

**FR-004** — `checkout.outcome` MUST be initialised to `abandoned` when the journey starts and overwritten on a terminal event.

**FR-005** — When the outcome is not a success value, the span MUST carry `payment.decline_reason` with the provider's or system's **coded** reason. Known values: insuffic_funds, do_not_honor, 3ds_required, gateway_timeout. Never a free-text message, never a raw upstream payload.

**FR-006** — The span for step `order_created` MUST carry `order.value` as a **double** (a JavaScript number, not a string).

**FR-007** — `cart.value` is type `string`, expected `number`. It MUST be emitted as a number. A stringified value cannot be aggregated — `sum()` and `p50()` return nothing useful while the code looks correct.

**FR-008** — Existing `Sentry.init()` options, sampling rates, and unrelated instrumentation MUST NOT be modified.

**FR-009** — No PII, card data, token, or raw provider payload may appear in any span name, `op`, or attribute.

## 5. Sentry SDK API reference

Everything you need. Verified against `@sentry/*` v10.

### `Sentry.startSpan` — the default choice

Wraps a block, auto-ends the span, returns the callback's value. Works with sync or async callbacks; for async the span ends when the promise settles, and a throw or rejection marks the span errored.

```ts
const order = await Sentry.startSpan(
  { name: "<span name from §2>", op: "function", attributes: { "<key>": value } },
  async (span) => {
    const created = await doWork();
    span.setAttribute("<key>", created.id);
    return created;
  },
);
```

Options: `name` (only required field), `op`, `attributes`, `startTime`, `parentSpan` (pass `null` to force a root span), `onlyIfParent`, `forceTransaction`.

### Other span starters

- `Sentry.startSpanManual(options, cb)` — same signature, but **you** call `span.end()`. Use when the end is triggered by an event.
- `Sentry.startInactiveSpan(options)` — returns a span that never becomes a parent. You call `span.end()`.
- **Long-lived browser spans** (a journey root that outlives one callback): pair `startInactiveSpan` with `Sentry.setActiveSpanInBrowser(span)` so later spans nest under it. Requires SDK ≥ 10.15.0, browser only. Ending the span clears it.

### Attributes

```ts
span.setAttribute("cart.value", 129.99);          // number, not "129.99"
span.setAttributes({ "a": "x", "b": true });

// set an attribute on the journey root from a nested context
const active = Sentry.getActiveSpan();
const root = active ? Sentry.getRootSpan(active) : undefined;
root?.setAttribute("<key>", value);
```

Allowed value types: `string`, `number`, `boolean`, or a non-mixed array of those. **Nested objects are not allowed** — flatten to dotted keys. Passing `undefined` removes the attribute.

Also available: `Sentry.updateSpanName(span, name)`, `Sentry.setHttpStatus(span, code)`, `span.setStatus({ code: 2 })` (0 unknown, 1 ok, 2 error), `Sentry.withActiveSpan(span, cb)`, `Sentry.getTraceData()`, `Sentry.continueTrace({ sentryTrace, baggage }, cb)`.

### Distributed tracing

This journey crosses 2 runtimes (browser, node). Two headers propagate the trace: `sentry-trace` and `baggage`. Since v8, with `tracePropagationTargets` unset, headers attach to **same-origin requests only** — a different origin *or a different port* gets none, and the server must allow both headers through CORS.

```ts
Sentry.init({
  dsn: process.env.SENTRY_DSN,
  tracePropagationTargets: ["localhost:3000", /^https:\/\/api\.example\.com/],
});
```

### Constraints

- A transaction holds a maximum of **1,000 spans**.
- Span `name` and `op` must stay low-cardinality — never interpolate an ID or URL.
- Attribute values have no documented length cap; scope tags cap at 200 characters.
- Use the documented `op` vocabulary: `function`, `ui.action`, `ui.action.click`, `http.client`, `db`, `db.query`, `cache.get_item`, `queue.task.*`, `template.render`, `serialize`, `middleware.*`.

## 6. Do not use — removed or deprecated

Your training data likely contains these. All are wrong for v9/v10.

| Never emit | Use instead |
| --- | --- |
| `Sentry.startTransaction()` | Sentry.startSpan() — removed in v8 |
| `transaction.startChild() / span.startChild()` | a separate Sentry.startSpan(), or the parentSpan option |
| `span.setData(...)` | span.setAttribute(...) |
| `span.setTag(...)` | span.setAttribute(...) |
| `span.finish()` | span.end() |
| `span.setName(...)` | Sentry.updateSpanName(span, name) |
| `Sentry.configureScope(cb)` | Sentry.getCurrentScope() / Sentry.withScope() |
| `Sentry.getCurrentHub(), Hub` | scope APIs — fully removed in v9 |
| `Sentry.metrics.increment/gauge/distribution` | removed in v9 — use span attributes |
| `Sentry.setMeasurement(...)` | span attributes |
| `enableTracing, tracingOrigins` | tracesSampleRate, tracePropagationTargets |
| `new Sentry.Replay() and other class integrations` | Sentry.replayIntegration() |
| `span.traceId / span.spanId / span.status` | span.spanContext().traceId / .spanId, spanToJSON(span).status |

The Node SDK is built on OpenTelemetry, so OTel-instrumented spans are picked up automatically and OTel APIs are usable there. **Use `Sentry.startSpan()` anyway** — it is the documented recommendation, and mixing the two makes the §7 verification queries unreliable. The browser SDK is not OTel-based; do not use OTel APIs there.

## 7. Acceptance criteria

Each is checkable in Trace Explorer with no setup — custom span attributes are searchable, groupable, and (when numeric) chartable with no declaration step.

**SC-001** — Given one completed journey, when I search spans for `checkout.id` matching that instance, then all 7 spans in §2 are present.

**SC-002** — Given any completed journey, when I filter on `checkout.outcome`, then every instance resolves to exactly one value.

**SC-003** — Given the rename is complete, when I search for the old span name, then no new spans appear under it.

**SC-004** — Given completed journeys, when I query `sum(cart.value)`, then a numeric result renders — proving the attribute was stored as a number.

**SC-005** — Given a failed journey, when I group spans by `payment.decline_reason`, then every result has a non-empty coded reason.

**SC-006** — Given the journey crosses runtimes, when I open a trace for the hand-off step, then spans from both runtimes appear in the same trace.

**SC-007** — Given any span in this journey, when I inspect all attributes, then no PII, card data, or raw provider payload is present.

Query windows are plan-gated: Developer 7 days, Team 14, Business 30. Aggregates are sampling-extrapolated and warn below ~5%.

## 8. Out of scope

Do not, as part of this task: change sampling rates or `Sentry.init()` beyond the propagation requirement; instrument other journeys; create dashboards or alerts; modify or remove existing spans beyond the rename in §4; refactor unrelated code; add dependencies.

