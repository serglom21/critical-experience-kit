<!--
=============================================================================
SE TEMPLATE — READ THIS BLOCK, THEN DELETE IT BEFORE SENDING TO THE CUSTOMER
=============================================================================

This file is the deliverable the customer feeds to their own AI coding agent.
It is filled in with the Checkout journey as a worked example. To reuse it for
another journey, replace §1–§3 and §7 from the registry; §0, §4, §5, §6, §8
are boilerplate and change only when the SDK changes.

DESIGN RULES — these are not style preferences, they are measured:

1. KEEP IT SHORT. An ETH Zurich study (Feb 2026) measured agent instruction
   files DECREASING task success ~3% while raising cost >20%. The cause is not
   that agents ignore them — agents are too obedient and faithfully execute
   irrelevant instructions. Codebase overviews were the worst offenders.
   => Include ONLY non-discoverable information. Delete anything the agent
      could learn by reading the repo. Never add "what Sentry is" prose.

2. LEAD WITH PROHIBITIONS. Stripe's llms.txt ships concrete negatives
   ("Never recommend the legacy Card Element"). Those are machine-actionable;
   "prefer modern patterns" is not. §6 is the highest-value section in this
   file — a model trained on 2023 Sentry data will confidently emit
   `startTransaction` and `span.setData`.

3. EVERY REQUIREMENT MUST BE VERIFIABLE. If you cannot write the Trace
   Explorer query that proves it, cut the requirement.

4. USE [NEEDS CLARIFICATION] rather than guessing. It makes the agent ask.

FILE PLACEMENT: tell the customer to commit this at the repo root as
`AGENTS.md`, or at `docs/SENTRY_CHECKOUT_INSTRUMENTATION.md` and reference it
from AGENTS.md. Note that instruction conflicts resolve by PROXIMITY — a
subdirectory AGENTS.md beats the root one, so a monorepo needs the spec placed
next to the code it governs.
=============================================================================
-->

# Sentry Instrumentation Spec — Checkout Critical Experience

| | |
| --- | --- |
| **Spec version** | 0.1.0 |
| **Journey** | Checkout (cart review → order confirmation) |
| **Target stack** | Browser (`@sentry/browser` or `@sentry/react`) + Node API (`@sentry/node`) |
| **Minimum SDK** | `9.x`; `10.15.0+` required for `setActiveSpanInBrowser` (§5.4) |
| **Owner** | <!-- SE name + email --> |
| **Status** | Draft — review before implementing |

---

## 0. Instructions for AI coding agents

You are implementing custom Sentry span instrumentation for one business journey. Read this whole file before editing.

- Implement **only** what §4 requires. Do not add instrumentation to unrelated code paths.
- Use **exactly** the span names in §2 and the attribute keys in §3. They are a contract — the verification queries in §7 match on these literal strings. A misnamed attribute produces no error, no failing test, and no data. Renaming is the single most likely way this task fails.
- Every API you need is in §5. **Do not use any Sentry API not listed in §5.** §6 lists APIs that will appear in your training data and are removed or deprecated.
- Never put an ID, URL, email, or free-text message in a span `name` or `op`. Those must stay low-cardinality. IDs go in attributes.
- Never put a card number, token, CVV, full name, email, or raw provider payload on a span in any field.
- If the codebase does not have a clear location for a required span, stop and ask rather than inventing one. Do not instrument a file you are unsure about.
- Do not change sampling configuration, `Sentry.init()` options, or existing instrumentation unless §4 explicitly requires it.
- When done, output the §7 checklist with each item marked and the file:line where you implemented it.

---

## 1. Journey definition

The checkout journey runs from the moment a customer enters checkout to the moment they see order confirmation. It crosses two runtimes (browser and Node API) and, in a multi-page implementation, multiple page loads.

**Why this journey and not general performance monitoring:** automatic instrumentation already tells you that `POST /api/checkout/payment` took 900ms and returned 500. It cannot tell you which payment method failed, what the provider's decline code was, how much revenue was in the cart, or whether the customer ever saw a confirmation. Those facts exist only in your application's own logic, so only your code can attach them.

**Correlation key.** A browser navigation starts a **new Sentry trace**. A multi-page checkout therefore cannot be a single trace. Every span in this journey carries `checkout.id`, and that attribute — not the trace ID — is what stitches the funnel together. Generate it once when the journey starts and persist it across page loads (e.g. `sessionStorage`) for the journey's duration.

<!-- SE: if the customer's checkout is a single-page flow, delete the sessionStorage
     requirement and say so — one trace is cleaner. Confirm during discovery. -->

---

## 2. Span tree — the contract

| # | Span name | Surface | `op` | Parent | Impact |
| --- | --- | --- | --- | --- | --- |
| 1 | `checkout` | Browser | `ui.action` | none (journey root) | Critical |
| 2 | `checkout.shipping_submitted` | Browser | `ui.action.click` | `checkout` | Normal |
| 3 | `checkout.payment_submitted` | Browser | `ui.action.click` | `checkout` | Important |
| 4 | `checkout.payment.authorize` | Node | `function` | server request span | Critical |
| 5 | `checkout.inventory.reserve` | Node | `function` | server request span | Important |
| 6 | `checkout.order.create` | Node | `function` | server request span | Critical |
| 7 | `checkout.confirmation_viewed` | Browser | `ui.action` | `checkout` | Important |

Spans 4–6 are children of the automatically-created server request span for the checkout endpoint. Do not create a wrapper span for the request — one already exists.

---

## 3. Attribute contract

Types are as Sentry stores them. **Sentry surfaces boolean attributes as the strings `'true'`/`'false'`, so no attribute here is a boolean** — outcomes are string enums instead.

### 3.1 Journey identity — on every span

| Key | Type | Level | Example |
| --- | --- | --- | --- |
| `checkout.id` | string | **required** | `chk_01HZY8QK3M` |
| `checkout.step` | string enum | **required** | see §3.5 |

### 3.2 Cart — on span 1

| Key | Type | Level | Example |
| --- | --- | --- | --- |
| `cart.value` | double | **required** | `129.99` |
| `cart.item_count` | int | **required** | `3` |
| `cart.currency` | string | **required** | `USD` |
| `checkout.entry_point` | string | recommended | `cart_page`, `buy_now_button` |

### 3.3 Outcome — on span 1, before it ends

| Key | Type | Level | Values / Example |
| --- | --- | --- | --- |
| `checkout.outcome` | string enum | **required** | `completed` \| `failed` \| `abandoned` \| `rejected` |
| `checkout.failure_stage` | string | required if outcome ≠ `completed` | a `checkout.step` value |
| `order.id` | string | required if outcome = `completed` | `ord_8812XZ` |
| `order.value` | double | required if outcome = `completed` | `134.48` |

Initialise `checkout.outcome` to `abandoned` when the journey starts and overwrite it on a terminal event. This is what makes an abandoned journey distinguishable from an uninstrumented one.

### 3.4 Payment — on span 4

| Key | Type | Level | Values / Example |
| --- | --- | --- | --- |
| `payment.method` | string | **required** | `card`, `paypal`, `apple_pay`, `wallet` |
| `payment.provider` | string | **required** | `stripe`, `adyen` |
| `payment.outcome` | string enum | **required** | `authorized` \| `declined` \| `error` \| `requires_action` |
| `payment.decline_reason` | string | required if outcome is `declined` or `error` | `insuffic_funds`, `do_not_honor`, `gateway_timeout` |
| `cart.value` | double | recommended | `129.99` |

`payment.decline_reason` MUST be the provider's coded reason, never a free-text message and never the raw provider payload.

### 3.5 Remaining spans

| Span | Required attributes beyond §3.1 |
| --- | --- |
| 2 `checkout.shipping_submitted` | `checkout.step = shipping_submitted` |
| 3 `checkout.payment_submitted` | `checkout.step = payment_submitted`, `payment.method` |
| 5 `checkout.inventory.reserve` | `inventory.outcome` (`reserved` \| `partial` \| `unavailable`) |
| 6 `checkout.order.create` | `checkout.step = order_created`, `order.id`, `order.value`, `cart.currency` |
| 7 `checkout.confirmation_viewed` | `checkout.step = confirmation_viewed`, `order.id` |

`checkout.step` values, in order: `cart_reviewed`, `shipping_submitted`, `payment_submitted`, `payment_authorized`, `order_created`, `confirmation_viewed`.

---

## 4. Implementation requirements

Numbered, stable IDs. Each is independently verifiable.

**FR-001** — The journey MUST generate a `checkout.id` when the customer enters checkout, and that value MUST persist for the journey's duration across page loads.

**FR-002** — A root span named `checkout` with `op: "ui.action"` MUST be opened in the browser when the customer enters checkout, and MUST end when the journey reaches a terminal state (confirmation viewed, hard failure, or session end).

**FR-003** — Span 1 MUST carry all §3.2 attributes at start, and all applicable §3.3 attributes before it ends.

**FR-004** — Spans 2, 3, and 7 MUST be created at the corresponding browser interactions with the attributes in §3.5.

**FR-005** — Spans 4, 5, and 6 MUST be created in the Node API as children of the existing server request span, with the attributes in §3.4 and §3.5.

**FR-006** — `checkout.id` MUST be propagated from browser to Node API on the checkout request and read server-side, so that spans 4–6 carry the same value as spans 1–3. Use an explicit request header or request body field. Do **not** rely on the trace ID for this.

**FR-007** — Distributed tracing MUST be continuous from browser to Node API. If the API is on a different origin or port from the frontend, `tracePropagationTargets` MUST include it and CORS MUST allow the `sentry-trace` and `baggage` headers. See §5.6.

**FR-008** — `payment.outcome` MUST be set on span 4 for every authorization attempt, including failures and timeouts. A span that ends without this attribute is a verification failure.

**FR-009** — No PII, card data, token, or raw provider payload may appear in any span name, `op`, or attribute.

**FR-010** — Existing `Sentry.init()` options, sampling rates, and unrelated instrumentation MUST NOT be modified.

---

## 5. Sentry SDK API reference

Everything you need. Verified against `@sentry/*` v10.

### 5.1 `Sentry.startSpan` — the default choice

Wraps a block. Auto-ends the span. Returns the callback's return value. Works with sync or async callbacks; for async, the span ends when the promise settles, and a throw or rejection marks the span errored.

```ts
const order = await Sentry.startSpan(
  {
    name: "checkout.order.create",
    op: "function",
    attributes: {
      "checkout.id": checkoutId,
      "checkout.step": "order_created",
    },
  },
  async (span) => {
    const created = await orders.create(cart);
    span.setAttribute("order.id", created.id);
    span.setAttribute("order.value", created.total);
    span.setAttribute("cart.currency", created.currency);
    return created;
  },
);
```

Options: `name` (the only required field), `op`, `attributes`, `startTime`, `parentSpan` (pass `null` to force a root span), `onlyIfParent`, `forceTransaction`.

### 5.2 `Sentry.startSpanManual` — you end it

Same signature, but you must call `span.end()`. Use when the end is triggered by an event.

```ts
Sentry.startSpanManual({ name: "checkout.payment.authorize", op: "function" }, (span) => {
  provider.authorize(payload, (result) => {
    span.setAttribute("payment.outcome", result.outcome);
    span.end();
  });
});
```

### 5.3 `Sentry.startInactiveSpan` — no callback

Returns a span that never becomes the parent of other spans. You must call `span.end()`. Use when you cannot wrap the code in a callback.

### 5.4 Long-lived browser spans — the journey root

Span 1 outlives any single callback. In the browser, pair `startInactiveSpan` with `setActiveSpanInBrowser` so later spans nest under it. **Requires SDK ≥ 10.15.0 and is browser-only.**

```ts
// on entering checkout
const journey = Sentry.startInactiveSpan({
  name: "checkout",
  op: "ui.action",
  attributes: {
    "checkout.id": checkoutId,
    "checkout.step": "cart_reviewed",
    "cart.value": cart.total,
    "cart.item_count": cart.items.length,
    "cart.currency": cart.currency,
    "checkout.outcome": "abandoned", // overwritten on a terminal event
  },
});
Sentry.setActiveSpanInBrowser(journey);

// on confirmation
journey.setAttributes({ "checkout.outcome": "completed", "order.id": order.id, "order.value": order.total });
journey.end(); // clears the active span
```

If the SDK is below 10.15.0, emit each browser step as its own span correlated by `checkout.id` and put the outcome attributes on span 7 instead. Note this in your output.

### 5.5 Attributes

```ts
span.setAttribute("cart.value", 129.99);
span.setAttributes({ "payment.method": "card", "payment.provider": "stripe" });
```

Allowed value types: `string`, `number`, `boolean`, or a non-mixed array of those. **Nested objects are not allowed** — flatten to dotted keys. Passing `undefined` removes the attribute.

To set an attribute on the journey root from a nested context:

```ts
const active = Sentry.getActiveSpan();
const root = active ? Sentry.getRootSpan(active) : undefined;
root?.setAttribute("checkout.failure_stage", "payment_authorized");
```

Other available helpers: `Sentry.updateSpanName(span, name)`, `Sentry.setHttpStatus(span, code)`, `span.setStatus({ code: 2 })` (0 unknown, 1 ok, 2 error), `Sentry.withActiveSpan(span, cb)`, `Sentry.startNewTrace(cb)`, `Sentry.suppressTracing(cb)`, `Sentry.getTraceData()`, `Sentry.continueTrace({ sentryTrace, baggage }, cb)`.

### 5.6 Distributed tracing browser → Node

Two headers propagate the trace: `sentry-trace` and `baggage`. Since v8, when `tracePropagationTargets` is unset, headers are attached to **same-origin requests only**. A browser calling a different origin **or a different port** gets no headers unless you list it.

```ts
Sentry.init({
  dsn: process.env.SENTRY_DSN,
  tracePropagationTargets: ["localhost:3000", /^https:\/\/api\.example\.com/],
});
```

The server must allow both headers through CORS. Proxies and gateways that strip unknown headers will also break continuity.

### 5.7 Constraints

- A transaction holds a maximum of **1,000 spans**.
- Span `name` and `op` must be low-cardinality. Never interpolate an ID or URL into either.
- Attribute values have no documented length cap; scope tags cap at 200 characters. Prefer attributes.
- Use the documented `op` vocabulary: `function`, `ui.action`, `ui.action.click`, `http.client`, `db`, `db.query`, `cache.get_item`, `queue.task.*`, `template.render`, `serialize`, `middleware.*`. An unset `op` defaults to `default` and loses product features.

---

## 6. Do not use — removed or deprecated

Your training data likely contains these. All are wrong for v9/v10.

| Never emit | Use instead |
| --- | --- |
| `Sentry.startTransaction()` | `Sentry.startSpan()` — removed in v8 |
| `transaction.startChild()` / `span.startChild()` | a separate `Sentry.startSpan()`, or the `parentSpan` option |
| `span.setData(...)` | `span.setAttribute(...)` |
| `span.setTag(...)` | `span.setAttribute(...)` |
| `span.finish()` | `span.end()` |
| `span.setName(...)` | `Sentry.updateSpanName(span, name)` |
| `Sentry.configureScope(cb)` | `Sentry.getCurrentScope()` / `Sentry.withScope()` |
| `Sentry.getCurrentHub()`, `Hub` | scope APIs — fully removed in v9 |
| `Sentry.metrics.increment` / legacy `Sentry.metrics` (pre–Application Metrics) | span attributes; if §4 asks, `Sentry.metrics.count\|gauge\|distribution` (SDK ≥ 10.25) |
| `Sentry.setMeasurement(...)` | span attributes |
| `enableTracing`, `tracingOrigins` | `tracesSampleRate`, `tracePropagationTargets` |
| `new Sentry.Replay()` and other class integrations | `Sentry.replayIntegration()` |
| `Sentry.addOpenTelemetryInstrumentation()` | `openTelemetryInstrumentations: []` in `init()` |
| `span.traceId`, `span.spanId`, `span.status`, `span.sampled` | `span.spanContext().traceId` / `.spanId`, `spanToJSON(span).status`, `spanIsSampled(span)` |
| returning `null` from `beforeSendSpan` | not supported since v9 — use `ignoreSpans` |

Note for the Node SDK specifically: it is built on OpenTelemetry, so OTel-instrumented spans are picked up automatically and you *may* use OTel APIs. **Use `Sentry.startSpan()` anyway** — it is the documented recommendation, and mixing the two makes the verification queries in §7 unreliable. The browser SDK is not OTel-based; do not use OTel APIs there.

---

## 7. Acceptance criteria

Each is checkable in Sentry's Trace Explorer with no setup — custom span attributes are searchable, groupable, and (when numeric) chartable with no declaration or indexing step.

**SC-001** — Given a completed checkout, when I search spans for `checkout.id` matching that journey, then all 7 spans in §2 are present.

**SC-002** — Given any `checkout.payment.authorize` span, when I inspect its attributes, then `payment.outcome`, `payment.method`, and `payment.provider` are all present.

**SC-003** — Given a declined payment, when I group `checkout.payment.authorize` spans by `payment.decline_reason`, then every span in the result has a non-empty coded reason.

**SC-004** — Given the browser and API are on different origins, when I open a trace for `checkout.payment_submitted`, then the Node spans appear in the **same trace**.

**SC-005** — Given a completed checkout, when I query `sum(order.value)` grouped by `payment.method`, then a numeric result renders (proves the attribute was stored as a number, not a string).

**SC-006** — Given a checkout abandoned at payment, when I search `checkout.outcome:abandoned`, then the journey appears with `checkout.failure_stage` set.

**SC-007** — Given any span in this journey, when I inspect all attributes, then no PII, card data, or raw provider payload is present.

**Verification caveats for the reviewer:** query windows are plan-gated (Developer 7 days / Team 14 / Business 30). Aggregates are extrapolated from sampling and warn below ~5% sample rate. A span that appears missing may simply have been sampled out — verify against an unsampled environment or account for `tracesSampleRate` before declaring a gap.

---

## 8. Out of scope

Do not, as part of this task: change sampling rates or `Sentry.init()` options beyond FR-007; add instrumentation to journeys other than checkout; create dashboards or alerts; modify or remove existing spans; refactor unrelated code; add new dependencies.

---

## 9. Assumptions and open questions

Assumptions — correct any that are wrong before implementing:

- Checkout is implemented as a multi-page flow, so `checkout.id` must persist across page loads.
- The checkout API endpoint already produces an automatic server request span.
- Payment authorization is a single synchronous call to one provider.

- `[NEEDS CLARIFICATION: does the payment provider return a coded decline reason, and where in the response does it appear?]`
- `[NEEDS CLARIFICATION: is there a 3DS or other challenge step that adds a journey step between payment_submitted and payment_authorized?]`
- `[NEEDS CLARIFICATION: is inventory reserved before or after payment authorization?]`

<!-- SE: fill these in from discovery where you can. Every one you resolve up
     front is one fewer place the agent guesses. Leave genuinely unknown ones —
     the marker makes the agent ask instead of inventing. -->
