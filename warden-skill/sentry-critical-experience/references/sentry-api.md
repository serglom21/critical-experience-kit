# Sentry tracing API — current vs removed

Load only when the diff touches Sentry APIs and you need to judge whether the code is current. Verified against `@sentry/*` v10.

## Current APIs — correct usage

| API | Notes |
| --- | --- |
| `Sentry.startSpan(options, cb)` | Default choice. Auto-ends. Returns the callback's value. Async-aware |
| `Sentry.startSpanManual(options, cb)` | Caller must call `span.end()` |
| `Sentry.startInactiveSpan(options)` | No callback; never becomes a parent. Caller must call `span.end()` |
| `Sentry.setActiveSpanInBrowser(span)` | Browser only, SDK ≥ 10.15.0. Pairs with `startInactiveSpan` for long-lived spans |
| `span.setAttribute(k, v)` / `span.setAttributes({})` | `string \| number \| boolean \| non-mixed array` only. No nested objects. `undefined` removes |
| `Sentry.getActiveSpan()` / `Sentry.getRootSpan(span)` | The pattern for setting an attribute on the journey root from a nested context |
| `Sentry.updateSpanName(span, name)` | Preferred over `span.updateName()` |
| `Sentry.setHttpStatus(span, code)` | |
| `span.setStatus({ code })` | 0 unknown, 1 ok, 2 error |
| `Sentry.withActiveSpan(span, cb)`, `Sentry.startNewTrace(cb)`, `Sentry.suppressTracing(cb)` | |
| `Sentry.getTraceData()`, `Sentry.continueTrace({ sentryTrace, baggage }, cb)` | For non-HTTP propagation (queues, websockets) |
| `span.spanContext().traceId` / `.spanId`, `spanToJSON(span).status`, `spanIsSampled(span)` | Direct property access on spans was removed |
| `Sentry.captureException` | Issues companion when the spec asks. Fingerprint the coded failure reason; message is not the grouper |
| `Sentry.logger.*` | Structured logs when the spec asks. Requires `enableLogs: true` (JS ≥ 9.41) |
| `Sentry.metrics.count` / `gauge` / `distribution` | Application Metrics when the spec asks (JS ≥ 10.25). Not the removed v9 `increment` API |

`Sentry.setTag` / `setTags` and `Sentry.setContext` are still current for **error events** — not deprecated. But tag keys and values cap at 200 characters and `setContext` data is not searchable, so business values belong in span attributes.

## Removed or deprecated — flag these

| Found in code | Replacement | Status |
| --- | --- | --- |
| `Sentry.startTransaction()` | `Sentry.startSpan()` | removed v8 |
| `transaction.startChild()` / `span.startChild()` | separate `startSpan()`, or `parentSpan` option | removed v8 |
| `span.setData()` | `span.setAttribute()` | deprecated |
| `span.setTag()` | `span.setAttribute()` | deprecated |
| `span.finish()` | `span.end()` | removed |
| `span.setName()` | `Sentry.updateSpanName()` | deprecated |
| `Sentry.configureScope()` | `getCurrentScope()` / `withScope()` | removed |
| `Sentry.getCurrentHub()`, `Hub` | scope APIs | removed v9 |
| `Sentry.metrics.increment` / legacy `Sentry.metrics` (pre–Application Metrics) | span attributes; if the spec asks, `Sentry.metrics.count\|gauge\|distribution` (SDK ≥ 10.25) | removed v9; Application Metrics is a different API |
| `Sentry.setMeasurement()` | span attributes | deprecated |
| `enableTracing` | `tracesSampleRate` | removed |
| `tracingOrigins` | `tracePropagationTargets` | removed |
| `new Sentry.Replay()` and other class integrations | `Sentry.replayIntegration()` | removed |
| `Sentry.addOpenTelemetryInstrumentation()` | `openTelemetryInstrumentations: []` in `init()` | removed v9 |
| returning `null` from `beforeSendSpan` | `ignoreSpans` | unsupported since v9 |

Severity guidance: report a deprecated API as **low** when behaviour is unaffected and a modern equivalent exists. Report as **medium** when the deprecated call is how a required attribute is set — `span.setData("cart.value", …)` does not produce a queryable span attribute, so the data is silently lost even though the code runs.

## Constraints to check against

- **1,000 spans** maximum per transaction.
- Span `name` and `op` must be low-cardinality — no IDs, URLs, emails, or free text interpolated in.
- Documented `op` vocabulary: `function`, `ui`, `ui.action`, `ui.action.click`, `ui.render`, `pageload`, `navigation`, `http.client`, `http.server`, `db`, `db.query`, `db.sql.query`, `cache.get_item`, `middleware.*`, `template.render`, `queue.task.*`, `serialize`, `resource.*`, `browser.*`. An unset `op` defaults to `default`.
- Product-side attributes are **string or number**; booleans render as `'true'`/`'false'` strings. A business value stringified (`"129.99"`) cannot be aggregated — report as wrong-type.

## Distributed tracing

Propagation uses the `sentry-trace` and `baggage` headers. Since v8, with `tracePropagationTargets` unset, headers attach to **same-origin requests only** — a different origin *or a different port* gets none. The receiving server must allow both headers through CORS.

Flag a new cross-origin `fetch`/`axios` call on an instrumented journey path when `tracePropagationTargets` does not cover the target. Do not flag same-origin calls.

The Node SDK is OpenTelemetry-based, so OTel-instrumented spans are ingested automatically and OTel APIs are usable there. Prefer `Sentry.startSpan()` regardless — it is the documented recommendation. The browser SDK is **not** OTel-based; OTel API usage in browser code is a defect.
