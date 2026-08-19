# Gap rule catalog

Rule record format lifted from the Instrumentation Score spec: `id`, `description`, `rationale`, `criteria`, `target`, `impact`. The `rationale` field is mandatory — it is what makes a finding persuasive to a customer's engineer rather than a scold from a tool.

Impact weights, also from that spec: **Critical = 40, Important = 30, Normal = 20, Low = 10**.

```
score = Σ(passed × weight) / Σ(evaluated × weight) × 100
```

Bands: **90–100 excellent · 75–89 good · 50–74 needs improvement · 0–49 poor**.

## Grade capping

Weighted averages alone let hygiene mask journey blindness — a journey can score "good" on attribute completeness while having no root span at all. Borrowed from SSL Labs, two rules cap the band regardless of score:

| Condition | Cap |
| --- | --- |
| `CE-001` fails (no root span) | **49** — poor. There is no journey to observe |
| `CE-002` fails (no outcome-bearing terminal span) | **74** — needs improvement. Failure is indistinguishable from success |

Deliberately avoided: the aggregation bug in the reference implementation, where the OTel Demo scored 35 overall while every individual service scored higher, because the overall score ANDed across services each failing a *different* rule. Score per journey, report the distribution, never AND.

## A note on the first and last step being weighted twice

`CE-001` and `CE-002` evaluate the same spans that `CE-003` already covers as steps, so the journey's root and terminal spans carry roughly double weight in the score. That is intentional: those two spans matter both as steps *and* as the journey's anchors. A missing middle step degrades one measurement; a missing root removes the journey. Stated here because it materially affects the score and should not be discovered by surprise.

## Rules

### CE-001 · journey root span present

- **Target:** span · **Impact:** Critical · **Caps at:** 49
- **Criteria:** a span exists whose name matches the first step's expected span name.
- **Rationale:** without a root span there is no journey to attach outcomes to and no funnel is queryable. Every other finding is moot until this one passes.

### CE-002 · terminal outcome-bearing span present

- **Target:** span · **Impact:** Critical · **Caps at:** 74
- **Criteria:** a span exists whose name matches the last step's expected span name.
- **Rationale:** a journey that commits its business effect but never records reaching a terminal state cannot distinguish success from failure. This is the most expensive possible blind spot in a funnel.

### CE-003 · every declared step is instrumented

- **Target:** span · **Impact:** per-step (`critical`/`important`/`normal`, default `normal`)
- **Criteria:** for each step, a span exists with the expected name.
- **Rationale:** a missing step is a dark segment in the funnel. Drop-off attributed to the step *before* it is misattributed, and the owning team never sees the problem.

### CE-004 · correlation key attribute present

- **Target:** attribute · **Impact:** Critical
- **Criteria:** the journey's `correlation_key.attribute` appears in the org's span attribute registry.
- **Rationale:** the correlation key is what stitches steps into one journey instance. A browser navigation starts a new Sentry trace, so the trace ID cannot do this job. Without the key you have disconnected spans, not a journey.

### CE-005 · correlation key is customer-defined, not Sentry-provided

- **Target:** attribute · **Impact:** Low
- **Criteria:** the attribute's `attributeSource.source_type` is `user`.
- **Rationale:** a `sentry` source type means the product already owns that key with its own semantics. Using it for a business correlation key risks a silent collision when Sentry's meaning diverges from yours.

### CE-006 · outcome attribute present

- **Target:** attribute · **Impact:** Critical
- **Criteria:** the journey's `outcome.attribute` appears in the attribute registry.
- **Rationale:** the outcome is the numerator of every funnel question. Absent it, you can measure latency and errors but not whether the business process succeeded.

### CE-007 · outcome is a string enum, not a boolean

- **Target:** attribute · **Impact:** Important
- **Criteria:** the observed `attributeType` is not `boolean`.
- **Rationale:** Sentry renders boolean attributes as the strings `'true'`/`'false'`, and a binary collapses `failed`, `abandoned`, and `rejected` into one bucket. Those three route to three different teams.

### CE-008 · failure reason present when the outcome admits failure

- **Target:** attribute · **Impact:** Important
- **Criteria:** if `outcome.values` contains any non-success value, `failure_reason.attribute` is present in the registry.
- **Rationale:** without a coded reason every failure collapses into one undiagnosable bucket. This is usually the single highest-value attribute for the customer, because it is the one that routes a problem to an owning team.

### CE-009 · magnitude present

- **Target:** attribute · **Impact:** Important
- **Criteria:** at least one declared `magnitude` attribute appears in the registry.
- **Rationale:** the magnitude is what turns a latency chart into a revenue chart. Numeric span attributes are chartable in Trace Explorer with no setup.

### CE-010 · magnitude is numeric

- **Target:** attribute · **Impact:** Important
- **Criteria:** every present magnitude attribute has observed `attributeType` of `number`.
- **Rationale:** a value stringified as `"129.99"` cannot be aggregated — `sum()` and `p50()` silently return nothing useful. The instrumentation looks correct and produces no answer.

### CE-011 · actor segment present

- **Target:** attribute · **Impact:** Normal
- **Criteria:** at least one declared `actor_segment` attribute appears in the registry.
- **Rationale:** segments turn "checkout is slow" into "checkout is slow for enterprise customers in the EU on mobile." Usually the cheapest role to fill, since it often already exists in the auth or tenancy layer.

### CE-012 · no dark segment between instrumented steps

- **Target:** span · **Impact:** Important
- **Criteria:** no run of consecutive missing steps sits between two instrumented steps.
- **Rationale:** a gap in the middle is worse than a gap at the end, because latency and drop-off from the dark steps are silently attributed to the last instrumented step. This is the finding that produces the "your checkout is visible to payment, then goes dark" sentence.

### CE-013 · no span name drift

- **Target:** span · **Impact:** Normal
- **Criteria:** no observed span name normalizes to a declared step's expected name while differing literally.
- **Rationale:** `checkout.payment_authorized` versus `checkout.payment.authorize` produces no error, no failing test, and no data — the team believes the step is instrumented while every query returns empty. Reported separately from "missing" because the fix is a rename, not new instrumentation.

## Reporting requirements

Every failed rule must carry three things beyond pass/fail. Binary findings do not survive contact with fifty services — this is the substance of Elastic's critique of the Instrumentation Score spec, filed as spec issue #43:

1. **Extent** — what share of instances are affected, e.g. "38% of checkout instances have no `payment.authorize` span."
2. **Entity breakdown** — which journeys and steps, not just an org-level number.
3. **A concrete example** — a real trace ID or span name the SE can open in the UI.

## Confidence and sampling

A span absent from the data may be uninstrumented, sampled out, or a step that legitimately did not happen. The analyzer reads `traces_sample_rate` and degrades every finding to **low confidence below 5%**, because Trace Explorer aggregates are sampling-extrapolated and warn at that threshold. Below that, verify against an unsampled environment before telling a customer their instrumentation is missing.

Count-derived extent is approximate: it compares per-span-name totals rather than joining on the correlation key, so a step that fires twice per journey will understate the gap. Marked as approximate in the report.
