# Signal companions

When a generated spec may also ask for Issues, structured logs, or Application
Metrics. The seven-role grammar is unchanged: those products are
*implementations* of a role, not new roles. There is no `CE-014` “must emit a
log.”

Default implementation is always the **span plus attribute** the spec already
asks for (Trace Explorer join, correlation key). Companions fire only if **all**
of their predicates match. If none match, the spec stays span-only.

Every companion hangs off a **failed, already-actionable** gap rule
(`spec/generate.py` `ACTIONABLE` / `build_requirements`). Deterministic; no LLM.
`ce` still emits no application code.

The 5% sample-rate guard is the same threshold `gap/rules.md` already uses
(`traces_sample_rate` below 5% is low-confidence for span absence). If the rate
is unknown, do not invent one.

---

## Custom error — `Sentry.captureException` / `sentry_sdk.capture_exception`

Implements *failure reason* as something Seer and Issues can triage.

**When** (all of):

1. `CE-008` failed.
2. `outcome.values` includes a non-success that is not *only* `abandoned`
   (e.g. `failed` / `declined` / `rejected`). Cancelled-only is treated like
   abandoned-only.

**Also:** attach the coded `failure_reason` as fingerprint/tag and the
correlation key on the scope. The exception *message* is not the grouper.

**Never:**

- abandoned-only journeys (`completed, abandoned`)
- health / HTTP 500 that auto-instrument already files
- as a substitute for the span attribute

---

## Structured logs — `Sentry.logger.*` / `sentry_sdk.logger.*`

Implements *correlation / failure context* that sampling would drop. JS requires
`enableLogs: true`. Python logs are on by default (`sentry-sdk` ≥ 2.35).

**When** (all of):

1. `CE-004` or `CE-008` failed.
2. **Either** observed `traces_sample_rate` is set and **< 0.05**, **or** the
   journey `correlation_key.persists_across` more than one surface (browser +
   node), so one sampled trace cannot hold the instance.

**Log body:** parameterized / low-cardinality. Attributes: correlation key, step
id, coded failure reason. No PII, no raw provider payload (same prohibition as
spans).

**Never:**

- as the only place outcome or correlation key lives
- on every info-level happy path
- if sample rate is unknown **and** the journey does not persist across surfaces
  — skip; do not invent a rate

---

## Application Metrics — `Sentry.metrics.count` / `gauge` / `distribution`

Implements *magnitude* when span `sum()` / `p50()` would lie because most traces
are sampled out. JS SDK ≥ 10.25 (enabled by default). Python `sentry-sdk` ≥ 2.44
(`sentry_sdk.metrics.*`).

**When** (all of):

1. `CE-009` or `CE-010` failed.
2. A magnitude attribute is declared.
3. **Either** `traces_sample_rate` is set and **< 0.05**, **or** `business_impact`
   is `critical` **and** the sample rate is known and **< 1**.

Metric name is derived from the attribute (`checkout.amount` → count/distribution
with that attribute on the metric). Include the correlation key as a metric
attribute only if cardinality is safe: a journey id is an **instance** id — **do
not** put it on metrics. Put `outcome` / `actor_segment` if declared.

**Never:**

- the **removed v9** `Sentry.metrics.increment` / pre–Application Metrics
  `Sentry.metrics` namespace
- metrics as the only magnitude store
- stringified numbers
- inventing a sample rate so the companion can fire
