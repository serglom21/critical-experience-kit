# Critical Experience Grammar

The industry-agnostic layer. Replaces the per-industry journey catalog.

Owner: Sergio Lombana · v0.1 · 2026-08-12

---

## 0. Why a grammar instead of a catalog

The first draft of this system was a curated library: *an ecommerce checkout has these 7 spans, a SaaS signup has these 5*. Three pieces of evidence killed it.

**Curated journeys are not just expensive — they are systematically wrong.** Heap measured its own corpus and found **84% of funnel analyses deliver misleading data, 63% have an untracked alternative path to conversion, and ~20 interactions go untracked per measured action**. A hand-authored journey encodes the *assumed* happy path, and the assumption is wrong most of the time.

**The shape isn't stable enough to be an identity.** Meta's production trace study (USENIX ATC'23) found request workflows are "highly dynamic," with topology in "constant flux." "The N canonical shapes of checkout" is not a small stable N.

**Content catalogs are known to be expensive.** OTel semantic conventions is the reference implementation of one — enormous, deliberately slow (mandatory review grace period, no urgency exception), and permanent by policy since names shipped in an SDK can't be recalled. Segment shipped semantic event specs and then told customers to hand-build the library themselves. Neither is a model worth copying.

**The architecture to copy instead is Brendan Gregg's USE method:** one method, plus thin per-OS checklists *derived* from it. The method is the asset; the checklists are cheap generated artifacts. Grammar first, catalog as output.

### The load-bearing distinction

From the Google SRE Workbook, ch. 2:

> **SLI specification** — the assessment of service outcome that you think matters to users, *independent of how it is measured*.
> **SLI implementation** — the SLI specification *and a way to measure it*.

One specification, many implementations, traded off on quality, coverage, and cost.

Apply it directly: **this grammar is the specification layer.** It is small, abstract, and does not rot when a customer changes framework. A customer's span tree is an **implementation** — generated on demand, disposable, regenerated when their stack changes. Specifications don't rot; implementations do, and those are cheap. Issues (`captureException`), structured logs, and Application Metrics may appear in a spec when [`SIGNAL.md`](SIGNAL.md) says they are relevant; they implement a role, they do not add one.

---

## 1. The seven roles

Borrowed, not invented. Kimball formalized business-process measurement in the 1990s; dbt MetricFlow, Cube, and Malloy independently converged on entity / dimension / measure, which is good evidence the core triple is irreducible.

Kimball's **accumulating snapshot fact table** is a business journey, formally: *"summarizes the measurement events occurring at predictable steps between the beginning and the end of a process… a defined start point, standard intermediate steps, and defined end point,"* with milestone timestamps and lag measures. Checkout, signup, claims processing, order fulfillment — one structure.

| # | Role | Invariant | Lineage |
| --- | --- | --- | --- |
| 1 | **Journey** | Has a defined start, ordered milestones, and a defined end | Kimball accumulating snapshot |
| 2 | **Correlation key** | Present on *every* span in the journey instance | Kimball degenerate dimension |
| 3 | **Step marker** | Steps are enumerable and ordered | Accumulating snapshot milestones |
| 4 | **Outcome** | Every instance resolves to exactly one terminal state | SRE `good ÷ valid` |
| 5 | **Failure reason** | Bounded, coded, low-cardinality. Never free text | — |
| 6 | **Magnitude** | Numeric, attached at the value-bearing step | Kimball measurement / MetricFlow measure |
| 7 | **Actor segment** | Categorical slicing axes | MetricFlow dimensions |

*(Optional 8th: **Resource identifier** — which system or entity served the step. Maps to OTel's "identifying" attribute class. Use for attribution when a journey crosses tenants, regions, or providers.)*

### 1 · Journey

A named business process with a defined start, ordered milestones, and a defined end.

The name is a *business* name, not a technical one — "Checkout," not "POST /api/orders." No inference can produce this name; it comes from a human. Every automatic "journey name" in the market (Fullstory, Dynatrace, Glassbox) is a string derived from a URL or DOM element, and Glassbox's page-grouping is state of the art while still being structural pattern matching.

### 2 · Correlation key

One identifier stitching all steps of one journey instance together.

**Not the trace ID.** A browser navigation starts a new trace, async workers and webhooks run in their own traces, and a journey may span sessions. Process mining calls this the *case notion*, and picking it wrong is the classic failure — producing *convergence* (one event duplicated across cases) or *divergence* (events in one case that can't be separated). When one key genuinely can't express the journey, the discipline's answer is an object-centric log (OCEL) relating an event to several objects; in practice, one well-chosen business ID covers most journeys.

This is the single highest-leverage attribute in the whole grammar. Without it there is no journey, only disconnected spans.

### 3 · Step marker

Which milestone this span represents, and its position in the ordering.

Enumerable and ordered. The enum *is* the journey definition — it's what makes drop-off computable.

### 4 · Outcome

The terminal state of the instance.

Must be a **string enum**, never a boolean — Sentry surfaces boolean attributes as the strings `'true'`/`'false'`, and more importantly a binary hides the distinction that matters. `completed | failed | abandoned | rejected` answers far more questions than success/failure, and it maps directly onto the SRE `good ÷ valid` ratio form.

Initialise to the abandonment value and overwrite on a terminal event. That's what makes an abandoned journey distinguishable from an uninstrumented one.

### 5 · Failure reason

The coded cause on any non-success outcome.

Bounded and low-cardinality so it survives a `Group By`. Never a free-text message, never a raw upstream payload. This is usually the highest-value single attribute for the customer, because it's the one that routes a problem to an owning team.

### 6 · Magnitude

The business measure attached at the value-bearing step — cart value, seats, MRR, tokens, claim amount.

Must be numeric. Numeric span attributes are chartable in Trace Explorer with no setup (`p50()`, `sum()`), which is what turns a latency chart into a revenue chart. A value stringified as `"129.99"` cannot be aggregated and is a defect.

### 7 · Actor segment

Categorical axes for slicing — plan tier, tenant, geo, device, cohort, experiment arm.

The dimensions that turn "checkout is slow" into "checkout is slow for enterprise customers in the EU on mobile."

---

## 2. The honest instrumentation ask

The grammar makes the ask smaller and stack-independent. Auto-instrumentation already gives you the syntactic layer: HTTP server and client spans, DB and queue spans, routes, status codes, durations. What it can never give you is intent — Grafana states it plainly: *"Auto-instrumentation can only capture technical insights… it cannot determine the intent of the instrumented services."*

So the irreducible manual set is five things:

1. Correlation key
2. Step marker
3. Outcome
4. Failure reason
5. Magnitude

Segments usually already exist in their auth or tenancy layer.

That ratio matches the market. Heap — the *autocapture* company — still tells customers to hand-track 5–10 core KPIs, and reports roughly **90% of events in their reports are autocaptured versus 10% manually tagged**. Amplitude's published hybrid recipe is autocapture to explore while hand-instrumenting 10–20 core metrics. Five attributes per journey, on two or three journeys, is a far easier conversation than "implement this 7-span spec."

---

## 3. Intake: provenance is a field, not a mode

Candidates arrive from several sources. Each lands in the same seven-role shape and carries its origin.

| `source` | What it is |
| --- | --- |
| `declared` | The customer named it — on a call, in their docs, or in dashboards and alerts they already built |
| `discovered:code` | Enumerated from routes, handlers, state machines |
| `discovered:telemetry` | Trace variants and existing spans in their org |
| `discovered:signal` | Dependency manifests, existing alerts, support-ticket themes |

**`declared` ranks first by default.** It carries the one thing no inference can produce: the customer has already decided this matters to the business. Frequency is actively misleading as an importance proxy — health checks and polling dominate volume, while refund and dispute flows are rare and expensive.

**Discovery is optional and skippable.** The pipeline entry point takes a journey list. Non-empty means spec generation can run immediately; empty means discovery populates it. A customer who arrives saying "instrument these three flows" never waits on a code scan.

### Corroboration, not gatekeeping

| | Found by discovery | Not found |
| --- | --- | --- |
| **Declared** | High confidence — proceed to spec | **A finding in its own right.** The flow lives in a service you can't see, is aspirational, or is entirely dark today |
| **Not declared** | Propose it; do not assume it matters | — |

The bottom-left cell is where discovery earns its keep. The top-right is often the most valuable output of a whole run: *you told me checkout matters and I can find no evidence of it in your code or telemetry.* That's a conversation, not an error.

### Role completion by source

| Role | `declared` | `discovered:code` | `discovered:telemetry` |
| --- | --- | --- | --- |
| 1 Journey | From the customer | Inferred name, needs confirmation | Inferred name, needs confirmation |
| 2 Correlation key | Often known | Inferrable from handler signatures | Sometimes present already |
| 3 Step marker | Often known | Inferrable from routes/state machines | Inferrable from trace variants |
| 4 Outcome | Ask | `[NEEDS CLARIFICATION]` | `[NEEDS CLARIFICATION]` |
| 5 Failure reason | Ask | Partially inferrable from error types | Partially inferrable |
| 6 Magnitude | Ask | Inferrable from domain models | Rarely present |
| 7 Actor segment | Usually exists | Inferrable from auth/tenancy | Often present |

Roles 4–7 are the semantic layer and a human owns them. Leave them as explicit `[NEEDS CLARIFICATION]` markers rather than guessing — that's what makes the downstream coding agent ask instead of invent.

---

## 4. Discovery: seed from code, corroborate with telemetry

When discovery does run, **code is the better primary seed**, for three reasons:

1. **Complete** — routes, handlers, and state machines contain the rare-but-critical flows that sampling and low-traffic paths never surface.
2. **Semantically rich** — route and handler names carry vocabulary traces lack. Dependency manifests are the strongest criticality signal available: a Stripe, Adyen, or Braintree import says more about what matters than any span count.
3. **Not circular** — traces only reveal what someone already chose to instrument, and instrumentation is the thing being sold.

The SE cannot read the repo. The customer-side agent can. That asymmetry is what makes code-seeded discovery viable at all.

Pipeline: **propose from code → rank by telemetry → human names and assigns semantics.**

Discovery's best job is not enumerating journeys — it is **finding the ones the customer forgot**. The refund flow, the admin path, the retry-after-failure path, the plan-downgrade flow: the journeys nobody names on a discovery call because they aren't the demo. This is Heap Illuminate's Step Suggestions applied one level up.

**Anti-pattern:** never let the discovered set define the schema. If discovery output becomes the source of truth, a customer-declared journey that discovery missed starts to look like a validation failure — and the tool quietly teaches the SE to trust the scan over the customer.

---

## 5. Conformance vocabulary for the coverage checker

Process mining is the mature field for "derive a model from an event log, then measure how well reality conforms." A distributed trace maps onto an event log: case ID, activity, timestamp. Borrow the vocabulary rather than inventing one.

| Term | Use for |
| --- | --- |
| **Case notion** | The correlation key decision, and its failure modes (convergence, divergence) |
| **Trace variant** / **trace clustering** | "Every customer's checkout differs." Real logs are a few dominant variants plus a long tail — cluster first, analyse per cluster |
| **Alignment** | Per-instance deviation: *sync moves*, *log-only moves* (unexpected activity), **model-only moves (expected activity absent)** at minimum cost. This is the coverage report |
| **Fitness** | How much observed behaviour the definition explains |
| **Precision** | Behaviour the definition *permits but never occurs* — grades the journey definition itself as over-permissive or over-fitted. A hand-written rule can never do this |
| **Generalization**, **simplicity** | The other two quality dimensions; keep for completeness |
| **Concept drift** | The journey changed after a deploy |
| **Local process model** | Frequent *fragments* rather than an end-to-end model — the pragmatic option when a full model would be spaghetti |
| **Spaghetti model** | The failure mode when activity granularity is too fine (raw span names) |
| **Event abstraction** | Mapping low-level span names to business activities. Skipping this is what produces spaghetti |

Prior art worth citing: Rubin & van der Aalst, *"Process mining can be applied to software too!"* (2014/2015), and Kamboj et al. (2025), which mines a Petri net from microservice traces and uses conformance checking to detect **missing or reordered activities**. PM4Py is the embeddable Python implementation. Nothing is productized in observability — that's the opening.

---

## 6. What this does not solve

Be honest about the limits when presenting this internally.

- **No grammar produces a journey definition without domain input.** Roles 4–7 need a human every time. This is a hybrid, not an automation.
- **Discovery cannot rank by business value.** Nothing in telemetry or source code says which flow generates revenue.
- **Outcome semantics are never inferrable.** HTTP 200 ≠ order placed. A structurally "successful" trace can end in an abandoned cart.
- **Thresholds are business decisions.** p95 = 400ms vs 4s is not a technical answer.
- **"Missing" is ambiguous.** A span absent from the data may be uninstrumented, sampled out, or a step that legitimately didn't happen. The checker must read `tracesSampleRate` and degrade to low-confidence below ~5%.
- **The evidence for grammars over catalogs is adoption-based, not experimental.** No controlled study compares them. RED, USE, and the golden signals won on uniformity and organizational scaling, not measured accuracy — don't overclaim.
- **Maintenance doesn't go to zero.** It moves from *content* (7 spans × N industries, forever) to *mechanism* (case notion, event abstraction, noise threshold). Smaller and industry-agnostic, but not free.

---

## Sources

[Heap: funnel analyses miss interactions](https://www.heap.io/press/heap-insights-report-indicates-that-over-80-of-funnel-analyses-miss-important-interactions-in-the-customer-journey) · [Heap: how autocapture actually works](https://www.heap.io/blog/how-autocapture-actually-works) · [Meta production trace study, USENIX ATC'23](https://www.usenix.org/conference/atc23/presentation/huye) · [Kimball: accumulating snapshot fact tables](https://www.kimballgroup.com/2012/05/design-tip-145-time-stamping-accumulating-snapshot-fact-tables/) · [Kimball: degenerate dimension](https://www.kimballgroup.com/data-warehouse-business-intelligence-resources/kimball-techniques/dimensional-modeling-techniques/degenerate-dimension/) · [dbt MetricFlow semantic models](https://docs.getdbt.com/docs/build/semantic-models) · [SRE Workbook: Implementing SLOs](https://sre.google/workbook/implementing-slos/) · [Gregg: methodologies / USE](https://www.brendangregg.com/methodology.html) · [Wilkie: the RED method](https://grafana.com/blog/the-red-method-how-to-instrument-your-services/) · [Grafana: eBPF and SDKs](https://grafana.com/blog/why-opentelemetry-instrumentation-needs-both-ebpf-and-sdks/) · [OTel entity data model](https://opentelemetry.io/docs/specs/otel/entities/data-model/) · [Kamboj et al., Petri nets for microservice workflow anomalies (2025)](https://pbg.cs.illinois.edu/papers/kamboj25petrinets.pdf) · [Process mining can be applied to software too (IEEE)](https://ieeexplore.ieee.org/document/7338234/) · [PM4Py](https://pm4py.fit.fraunhofer.de/implemented-approaches) · [Amplitude: autocapture vs manual tracking](https://amplitude.com/explore/data/autocapture-vs-manual-tracking) · [Heap Illuminate](https://www.heap.io/platform/illuminate)
