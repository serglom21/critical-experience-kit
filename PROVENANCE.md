# Provenance

What this kit is built on, what it borrows the shape of, and what it deliberately declines to adopt. Written because "is this built on the Instrumentation Score spec?" is a fair question with a nuanced answer, and because every borrowed idea here should be defensible by citation rather than vibes.

---

## 1. Runtime dependencies — the actual list

Deliberately tiny.

| Dependency | Where | Required? |
| --- | --- | --- |
| Python 3.10+ stdlib | everywhere | yes |
| **PyYAML** | `intake` (YAML candidate files), `registry_gen/validate.py` | yes for YAML input; JSON works without it |
| **jsonschema** | `registry_gen/validate.py` | optional — degrades to structural checks |
| **Sentry API** — `GET /trace-items/attributes/` | `gap/sentry_source.py` | only for live runs; fixtures work offline |
| **Sentry MCP** — `search_events` | span names + ops for live runs | only for live runs |
| **OTel Weaver** (Rust binary) | consumes `registry_gen/out/` | optional — nothing here calls it |
| **Warden** (`@sentry/warden`) | loads `warden-skill/` in the customer's CI | customer-side only |

Nothing depends on the Instrumentation Score spec, `sentry-for-ai`, or `dotagents` at runtime.

---

## 2. Instrumentation Score — shape borrowed, conformance declined

**What's taken, verbatim:**

- The **rule record format**: `id`, `description`, `rationale`, `criteria`, `target`, `impact`. The mandatory `rationale` field is the single best idea in the spec — it's what makes a finding persuasive to a customer's engineer instead of a scold from a tool, and it's reused verbatim in the generated `WHY.md`.
- The **impact weights**: Critical 40 / Important 30 / Normal 20 / Low 10.
- The **score formula**: `Σ(passed × weight) / Σ(evaluated × weight) × 100`.
- The **bands**: 90–100 excellent, 75–89 good, 50–74 needs improvement, 0–49 poor.

**What's explicitly not taken, and why it can't be:**

The spec's conformance rules are closed. An implementation MUST implement all its rules, MUST use its formula, and **MUST NOT add rules that affect the standard score**. Every rule in it is technical hygiene — `RES-*` (`service.name` present), `SPA-*` (orphan spans, INTERNAL span count), `MET-*` (attribute cardinality), `LOG-*` (severity numbers). **There is not one rule about business journeys or outcome coverage.**

So this kit is **not conformant and does not claim to be.** Our thirteen rules live in our own `CE-*` namespace and produce a score entirely outside the standard one — the same move Elastic made with `SPA-C-001` as a sidecar. The honest positioning on a call: *their score says your spans are well-formed; ours says the right things have spans.* Complementary, not competing.

**Two things taken from its critics rather than the spec:**

- **Extent + entity breakdown + a concrete example on every failed rule** — Elastic's finding that binary pass/fail doesn't survive fifty services, filed as spec issue #43. Implemented as the `extent`, `entity`, and `example` fields, and `rules.md` makes them a stated requirement.
- **Don't AND across entities** — the reference implementation scores the OTel Demo at 35 overall while every individual service scores higher, because each fails a *different* rule. We score per journey and report the distribution. Asserted by `test_journeys_scored_independently_not_anded`.

**One thing from prior art the spec itself cites:** SSL Labs **grade capping**. Weighted averages let attribute hygiene mask total journey blindness, so `CE-001` (no root span) caps at 49 and `CE-002` (no terminal outcome span) caps at 74.

Sources: [spec](https://github.com/instrumentation-score/spec) · [specification.md](https://raw.githubusercontent.com/instrumentation-score/spec/main/specification.md) · [Elastic's critique](https://www.elastic.co/observability-labs/blog/otel-instrumentation-score) · [issue #43](https://github.com/instrumentation-score/spec/issues/43)

Maintainers to know if this ever goes upstream: Antoine Toulme (Splunk), Daniel Gomez Blanco (New Relic), Juraci Kröhling (OllyGarden), Michele Mancioppi (Dash0). Initiated by OllyGarden — **not** Datadog or Grafana, despite a common assumption. Not yet donated to CNCF/OTel.

---

## 3. sentry-for-ai / Warden / dotagents — we're a consumer, not a descendant

No code from any of these is used. The relationship is the other direction: **we ship an artifact they load.**

- **Warden** — `warden-skill/sentry-critical-experience/SKILL.md` is authored against the real v0.43.0 format (frontmatter is exactly `name`, `description`, `allowed-tools`; skill dirs live at `.agents/skills/<name>/`; scoping lives in `warden.toml`, not frontmatter). Verified by reading the published npm tarball, not the docs. Warden runs it; we don't import it.
- **`warden.toml`** — real schema, deliberately advisory (`failOn = "off"`, `minConfidence = "high"`). Earn the right to block.
- **Distribution** — `warden add --remote <org>/<repo>@<sha> --skill sentry-critical-experience`. A git repo of `SKILL.md` dirs, pinned by commit. No npm publish needed.
- **dotagents** (`agents.toml` + `agents.lock`, resolved-commit pinning, `--frozen` for CI) — the intended bundle-distribution path. Referenced in the plan, not yet used.
- **`npx @sentry/ai install` / `getsentry/sentry-for-ai`** — cited as evidence the distribution rails already exist, so Layer 4 needs no new infrastructure. Not a dependency.
- **Agent Skills spec** (agentskills.io) — the format `SKILL.md` follows.

Worth knowing: only `security-review` and `code-review` actually ship as Warden built-ins. The other skills on its landing page are illustrative — don't offer them to customers as installable.

---

## 4. What it genuinely is built on

**OpenTelemetry semantic conventions + Weaver.** This is the one real technical foundation. `registry_gen/generate.py` emits actual semconv YAML with a pinned dependency on `semantic-conventions@v1.44.0`, and `registry_gen/validate.py` fetches and validates against the official `schemas/semconv.schema.json` — the same file VS Code uses. Constraints were verified against weaver's Rust source, not its docs, which is how we learned the docs' own dependency example is stale (`- name: otel` now hard-fails).

The critical finding, from `weaver_live_check/src/live_checker.rs`: **live-check is sample-driven and has no span lookup.** No `find_span`, no `missing_span` finding, `registry_group` is `None` for spans. `requirement_level` on a span group is documentation only. That single fact is why journey completeness is checked against Sentry instead of Weaver, and it shaped the whole architecture.

**Sentry's API and conventions.**

- `GET /trace-items/attributes/?dataset=spans` — public, documented, stable. `attributeSource.source_type` (`sentry` vs `user`) is the automatic-vs-custom discriminator the whole profile rests on.
- The **undocumented** span query. `/organizations/{org}/events/?dataset=spans` is what Trace Explorer and the MCP call but is absent from the public reference, so it's fenced behind `--unsafe-span-query` with a warning and the MCP is the supported path.
- `develop.sentry.dev` **span operation vocabulary** — the `AUTO_FAMILIES` table in the profile classifier.
- `getsentry/sentry-conventions` — naming policy (attributes MUST be namespaced, dots as separators, `snake_case`, namespace first, OTel alignment preferred) and the source for the collision check. Also the reason the kit stays unprefixed: `cart.value` already satisfies "namespaced," and no `cart.*` / `checkout.*` / `order.*` / `payment.*` attributes exist in the registry today.
- **SDK v10 API surface** — §5 and §6 of the generated spec, including the do-not-use table.

**Kimball dimensional modeling.** The grammar isn't invented. An **accumulating snapshot fact table** is a business journey formally — "a defined start point, standard intermediate steps, and defined end point," with milestone timestamps and lag measures. A **degenerate dimension** is the correlation key. Fact-table column roles (foreign keys / measurements / degenerate dimensions) map onto the attribute roles.

**Google SRE Workbook, ch. 2.** The load-bearing architectural idea: **SLI specification vs implementation**. `GRAMMAR.md` is the specification and doesn't rot; `registry_gen/out/` is the implementation and is disposable. That split is what dissolved the maintenance problem in the original per-industry-catalog design. Also `good ÷ valid` as the outcome ratio form, and ordering journeys by business impact before touching telemetry.

**dbt MetricFlow / Cube / Malloy.** Not used, but three tools independently converging on entity / dimension / measure is the evidence that the grammar's core triple is irreducible rather than arbitrary.

**GitHub spec-kit.** The `SPEC.md` skeleton: stable IDs (`FR-001`), RFC-2119 MUST, measurable acceptance criteria, explicit `[NEEDS CLARIFICATION]` markers so the agent asks instead of inventing.

---

## 5. Patterns borrowed from adjacent industries

| From | Idea | Where it lives |
| --- | --- | --- |
| **Avo** | Journey → spec → *agent implementation prompt* → verify loop; two-layer verification (static + runtime); branch-only writes; seed from observed traffic | The whole pipeline shape |
| **Segment Protocols** | Semantic event spec structure (event → when to call → property table). Typewriter as the cautionary tale: codegen without a verifier died | `SPEC-TEMPLATE.md`; "build the checker first" |
| **Amplitude** | Hybrid recipe — autocapture to explore, hand-instrument 10–20 core metrics; Observe's four schema statuses | The five-attribute ask; drift detection |
| **Heap** | Illuminate's Step Suggestions → propose journeys nobody named; the 90/10 manual ratio | Discovery's reframed job; the profile's "zero is the finding" |
| **Process mining** | Case notion, trace variants, trace clustering, alignments (sync / log-only / **model-only** moves), fitness / precision / generalization / simplicity, concept drift, OCEL | `rules.md` and `GRAMMAR.md §5` vocabulary |
| **Service scorecards** (Soundcheck, Cortex, Datadog, OpsLevel) | Facts-vs-rules separation; **exemptions first-class**; staged rollout | `excluded: true` in intake; advisory-first Warden config |
| **Brendan Gregg (USE)** | One method + thin *generated* checklists. The method is the asset | Grammar maintained, registries generated |
| **SSL Labs** | Grade capping on critical flaws | `CAP_POOR`, `CAP_NEEDS_IMPROVEMENT` |
| **Honeycomb** | Observability Maturity Model as capabilities not checklists; wide events / schema-on-read | Grammar-over-catalog; no predefined schema requirement |
| **Stripe `llms.txt`** | Ship concrete prohibitions, not prose | §6 of the generated spec |
| **Convex** | Build the eval harness before tuning the prompt | Phase 5 |
| **Tracetest / Malabi** | Trace assertions as a CI gate | Referenced, not built |

---

## 6. Empirical claims and their sources

Every number quoted in a customer-facing artifact traces to a citation, not an assertion.

| Claim | Source |
| --- | --- |
| 84% of funnel analyses deliver misleading data; 63% miss an alternative path to conversion; ~20 interactions untracked per measured action | Heap's own corpus study |
| ~90% of events in reports autocaptured vs ~10% manually tagged; hand-track 5–10 core KPIs | Heap, "How autocapture actually works" |
| Agent instruction files *decreased* task success ~3% while raising cost >20% | ETH Zurich, Feb 2026 |
| ~20% lift in AI success rate writing Convex code | Convex Evals |
| Production request workflows are "highly dynamic," topology in "constant flux" | Meta, USENIX ATC'23 |
| "Auto-instrumentation… cannot determine the intent of the instrumented services" | Grafana |
| ~6 features instrumented per milestone against ~30 shipped | GitLab handbook |
| Process mining on microservice traces detects missing/reordered activities | Kamboj et al. 2025; Rubin & van der Aalst 2014 |

---

## 7. Deliberately not adopted

- **Instrumentation Score conformance.** Its rules don't cover journeys and its conformance clause forbids adding rules that affect the score. Sidecar instead.
- **A per-industry journey catalog.** The original design. Killed by the Heap 84% finding, Meta's flux finding, and the maintenance cost visible in OTel semconv and Segment's never-shipped-as-artifacts specs.
- **Weaver for completeness enforcement.** Can't do it — sample-driven, no span lookup.
- **eBPF / zero-code instrumentation** as a substitute. Grafana's own hierarchy puts manual API calls as the only layer that reaches business intent; the Collector can recombine what's on the wire but cannot invent an order value.
- **LLM code generation into customer repos.** The founding constraint. Their model, their repo, their review.
- **Frequency as an importance signal.** Health checks dominate volume; refunds are rare and expensive. Volume is inert in ranking until a human assigns impact.
- **Auto-naming journeys.** Doesn't exist as semantics anywhere in the market — every "automatic name" in Fullstory, Dynatrace, or Glassbox is derived from a URL or DOM element.

---

## 8. Honest caveats about the lineage

- The evidence that small grammars beat content catalogs is **adoption-based, not experimental**. RED, USE, and the golden signals won on uniformity and organizational scaling. No controlled study compares them to per-domain checklists. Don't overclaim it internally.
- **No grammar produces a journey definition without domain input.** Roles 4–7 need a human every time. This is a hybrid, not an automation.
- The **code-location heuristic** in the profile classifier is the one component that is genuinely a guess. `source_type` and op families are authoritative; pattern-matching a span description is not. It's labelled as a heuristic everywhere it surfaces.
- `weaver registry check --future` is the real validator. `registry_gen/validate.py` is a substitute built because the binary wasn't installable, and it says so in its own output.
