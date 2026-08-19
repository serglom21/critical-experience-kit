# Critical Experience Instrumentation — Build Plan

Owner: Sergio Lombana · Drafted 2026-08-12 · Status: draft for review

---

## 0. What verification changed

Two findings from checking the actual source rather than the docs. Both are load-bearing.

### Finding 1 — Weaver cannot enforce "this span must exist"

`weaver registry live-check` is **sample-driven only**. Verified in `crates/weaver_live_check/src/live_checker.rs`: lookup maps exist for attributes, metrics (by `metric_name`), events (by `name`), and entities — there is **no `find_span`**. `sample_span.rs` never resolves a registry group (`registry_group` is `None` for spans). The finding-ID enum has `missing_metric` and `missing_event` but **no `missing_span`**, and no expected-but-never-seen finding of any kind.

Consequence: `requirement_level: required` on a **span** group is documentation only. Weaver will never flag a missing checkout span, and journey completeness is not expressible in it.

**Design response — split the two jobs:**

| Job | Tool | Why |
| --- | --- | --- |
| Define the journey; generate docs, typed constants, agent prompts; validate attribute *shape* and detect breaking changes | **Weaver** | Real, sanctioned, and gives four artifacts from one YAML |
| Answer "did the 7 expected checkout spans actually arrive, with the required attributes?" | **Custom checker against Sentry's span query API** | Sentry is already the runtime verifier — spans *are* the telemetry |

This is a better split anyway. It also removes the dependency on OTLP plumbing you'd otherwise need for live-check.

Two partial coverage signals from Weaver are still worth parsing in CI: `weaver registry stats` emits `seen_registry_attributes` with **zero counts for never-seen items**, plus a `registry_coverage` fraction. Attribute-level coverage, free. It never produces a finding or a non-zero exit, so wrap it yourself.

Escape hatch if you later want Weaver to enforce completeness: model journey steps as OTel **events or metrics** instead of spans — those *do* match by name and *do* enforce requirement levels. Not recommended for v1; span trees are what Sentry's product is built around.

### Finding 2 — Sentry's queryability is better than assumed, with two gotchas

Custom span attributes need **no declaration and no indexing step**. Trace Explorer documents them as searchable, sortable, column-addable, `Group By`-able, and — for numeric attributes — visualizable as span metrics computed on the fly (`p50(cart.value)`, `sum(order.value)`). Alerts and dashboard widgets are created from a Trace Explorer query via **Save As**. So every acceptance criterion in the spec is verifiable in-product on day one.

Two gotchas that must shape the spec:

1. **Product-side attributes are string or number only.** Booleans surface as the strings `'true'`/`'false'`. Never model an outcome as a boolean — use a string enum (`checkout.outcome = completed | abandoned | failed`). This is also better for funnel analysis.
2. **Query windows are plan-gated** (Developer 7d / Team 14d / Business 30d), and aggregates are sampling-extrapolated with a warning below ~5% sample rate. Verification must account for sample rate or run against an unsampled environment — otherwise a "missing" span is just a sampled-out span.

Also relevant: **1,000 spans per transaction** is a real limit; tags cap at 200 chars while attributes have no documented length cap. Put IDs and URLs in attributes, never in `op` or `name` (both must stay low-cardinality).

---

## 1. Architecture

**Revised 2026-08-12.** Layer 1 was originally a curated per-industry journey catalog. That's now split into a stable *specification* layer (the grammar) and a generated *implementation* layer (per-customer registries). Rationale and evidence in `GRAMMAR.md §0`; the short version is that Heap measured **84% of funnel analyses delivering misleading data and 63% having an untracked path to conversion**, so a hand-authored canonical journey is not merely expensive, it is usually wrong.

Five layers. Each is independently useful — you can ship Layer 2 alone and get value.

```
┌─ Layer 0 · GRAMMAR (specification) ─────────────────────────────┐
│  7 industry-agnostic roles + invariants. Hand-maintained,       │
│  ~1 page, does not rot. Borrowed from Kimball's accumulating    │
│  snapshot + MetricFlow entity/dimension/measure + SRE good÷valid│
│    journey · correlation key · step marker · outcome ·          │
│    failure reason · magnitude · actor segment                   │
│  See GRAMMAR.md. THIS is the maintained asset.                  │
└─────────────────────────────────────────────────────────────────┘
                            │  instantiated per customer
                            ▼
┌─ Layer 1 · INTAKE + JOURNEY REGISTRY (implementation) ──────────┐
│  Candidates arrive tagged with provenance:                      │
│    declared (ranked first) · discovered:code ·                  │
│    discovered:telemetry · discovered:signal                     │
│  Discovery is OPTIONAL — a declared journey goes straight to    │
│  spec generation with no scan.                                  │
│  Output: a Weaver registry per customer journey. GENERATED,     │
│  not curated. Still gives docs, constants, prompts, diffs.      │
└─────────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼────────────────────┐
        ▼                   ▼                    ▼
┌─ Layer 2 ────────┐ ┌─ Layer 3 ────────┐ ┌─ Layer 4 ──────────┐
│ GAP ANALYSIS +   │ │ COVERAGE CHECKER │ │ WARDEN PR SKILL    │
│ SPEC GENERATOR   │ │                  │ │                    │
│ Diff registry vs │ │ Query Sentry for │ │ Customer-installed.│
│ their live spans │ │ each expected    │ │ Flags PRs touching │
│ → gap matrix     │ │ span+attribute.  │ │ critical paths with│
│ → agent-targeted │ │ Present/partial/ │ │ no instrumentation.│
│   spec .md       │ │ missing + extent │ │ Keeps it from      │
│                  │ │ + example trace  │ │ rotting.           │
│ SE-facing        │ │ Closes the loop  │ │ Customer-facing    │
└──────────────────┘ └──────────────────┘ └────────────────────┘
```

Layer 0 is the only hand-maintained content. Layer 2 is your existing workflow, automated. Layer 3 is what nobody has. Layer 4 is the leave-behind.

---

## 2. Build sequence

### Phase 1 — One journey, end to end (target: 2 weeks)

Prove the whole loop on checkout before generalizing. Deliverables in this kit:

- [x] `GRAMMAR.md` — the 7-role specification layer, provenance/intake model, conformance vocabulary
- [x] `registry/` — Weaver registry for the checkout journey. **Now positioned as a generated exemplar, not a catalog entry** — it instantiates the grammar (`checkout.id` = correlation key, `checkout.step` = step marker, `checkout.outcome` = outcome, `checkout.failure_stage` = failure reason, `cart.value` = magnitude). Verified syntax, passes `weaver registry check --future`
- [x] `SPEC-TEMPLATE.md` — the agent-targeted spec, filled with checkout as the worked example
- [x] `warden-skill/sentry-critical-experience/SKILL.md` — the PR check
- [x] `warden.toml` — customer install config
- [ ] Run the spec through one real coding agent against a fork of the Sentry demo app. Measure: did it produce correct instrumentation unassisted?
- [ ] Build the coverage checker (§3 below)

Gate before Phase 2: the spec must produce correct instrumentation from a cold start, with no human clarification.

### Phase 2 — Coverage checker + score — BUILT

See `gap/` (13-rule catalog in `rules.md`, analyzer, Sentry source layer, 45 tests, offline fixture). The thing that doesn't exist in the market. Design as built:

Two behaviours the runs forced, both worth knowing:

- **Coverage state is classified before scoring.** Sorting by score ascending put four entirely-uninstrumented journeys on top, all on the same meaningless ~15, and buried the one journey with a diagnosable gap at the bottom. Journeys are now `partial` / `complete` / `absent`; `partial` leads the report and `absent` carries no grade at all, because zero instrumented steps is work that hasn't started, not a low score.
- **The absent table shows whether the correlation key already exists.** A declared journey with the key in place and no spans is the cheapest possible win — the plumbing is done.

Original design notes, all implemented:

**Input:** a registry journey + an org slug + environment + time window.
**Output:** per-span-node status with extent and a concrete example.

Rule record format, lifted from the Instrumentation Score spec (`id, description, rationale, criteria, target, impact`), with impact weights `Critical=40 / Important=30 / Normal=20 / Low=10`. The mandatory `rationale` field is what makes a finding persuasive to a customer's engineer — keep it required.

Three things to steal from Elastic's critique of that spec, because binary pass/fail does not survive contact with 50 services:

- **extent** — "38% of checkout traces have no `checkout.payment.authorize` span"
- **entity breakdown** — which projects/services fail, not just the org
- **one concrete example** — a real trace ID the SE can open in the UI

Plus **grade capping**, from SSL Labs: if a journey's terminal outcome span is entirely absent, cap the grade regardless of hygiene. A weighted average alone lets `service.version` hygiene mask total journey blindness.

Avoid the aggregation bug in the reference implementation: the OTel Demo scored 35 overall while every individual service scored higher, because the overall score ANDs across services each failing a *different* rule. Score per journey, then report the distribution — don't AND.

**Implementation note — RESOLVED 2026-08-12.** The checker needs two different data sources, because only one of them is publicly documented.

*Attribute presence — public, documented, stable:*

```
GET /api/0/organizations/{organization_id_or_slug}/trace-items/attributes/
    ?dataset=spans          # logs | preprod | processing_errors | spans | tracemetrics
    &statsPeriod=30d
    &attributeType=string   # array | boolean | number | string
    &substringMatch=checkout
    &query=<sentry search syntax>
```

Scopes: `org:read` (or `org:admin` / `org:write`). Returns per attribute: `key`, `name`, `attributeType`, and — the important part — **`attributeSource.source_type`, which is `sentry` for SDK-provided attributes and `user` for customer-defined ones.** That single field separates "what their SDK gives them" from "what they instrumented themselves," which is the gap analysis. `itemType` is a deprecated alias of `dataset`; use `dataset`.

Also public and useful: *Retrieve a Trace*, *Retrieve Trace Metadata* (for attaching a concrete example trace to a finding), and *List an Organization's Tags*.

*Span rows and aggregates — NOT publicly documented.* The Discover & Performance API section has no span/event query endpoint. `GET /api/0/organizations/{org}/events/?dataset=spans` is what Trace Explorer and the Sentry MCP actually call, but it is absent from the public reference, so treat it as an unstable contract.

**Therefore:** build the row/aggregate half on the Sentry MCP `search_events` (`dataset='spans'`, explicit `fields`, `sort='-count()'`, `period`) rather than on the raw endpoint. Verified working — it returns aggregates and emits a Trace Explorer deep link of the form `/explore/traces/?...&mode=aggregate&table=span`, which is worth attaching to every finding so the SE can open the exact query. Build the attribute half on `trace-items/attributes/` directly; there is **no MCP tool** for it.

**Sampling guard:** read the project's `tracesSampleRate` first. If below ~5%, mark results low-confidence rather than reporting a false negative.

### Phase 3 — Intake, then optional discovery (2–3 weeks)

Replaces the old "generalize the library" phase. Nothing here is a catalog.

**3a · Intake — BUILT.** See `intake/` (resolver, JSON Schema, 40 tests, worked examples). A journey-candidate record in the grammar's seven-role shape, carrying `source` and `confidence`. Sources: `declared`, `discovered:code`, `discovered:telemetry`, `discovered:signal`. `declared` ranks first by default, because the customer has already answered the question no inference can — whether this matters to the business.

Two behaviours worth knowing, both forced by a failing test rather than designed up front:

- **Volume is inert until a human assigns impact.** With volume as a general tiebreaker, a 9.2M-instance `/healthz` probe sorted above a refund flow running a few hundred times a month — the exact failure the ranking rule exists to prevent, displaced one level down into the proposed set.
- **Exclusion is a human act.** `excluded: true` plus a reason drops a candidate from the worklist and keeps it in the JSON for audit. Nothing in telemetry distinguishes a probe from a purchase, so no heuristic can do this — and worklist items a team can never action destroy the artifact's credibility, which is the one thing every service-scorecard vendor converged on independently.

The entry point takes a journey list. **Non-empty → go straight to spec generation. Empty → run discovery to populate it.** A customer who arrives saying "instrument these three flows" must never wait on a code scan. Roles 4–7 (outcome, failure reason, magnitude, segment) are human-owned; leave them as explicit `[NEEDS CLARIFICATION]` markers rather than guessing.

**3b · Discovery (optional enrichment).** Seed from **code**, not telemetry — it's complete (contains the rare-but-critical flows sampling never surfaces), semantically rich (route and handler names; a Stripe or Adyen import in the dependency manifest is a stronger criticality signal than any span count), and non-circular (traces only show what someone already instrumented, and instrumentation is what you're selling). The SE can't read the repo; the customer-side agent can — that asymmetry is what makes this viable.

Then rank candidates by telemetry, and have the human name each one and assign outcome semantics and thresholds.

**Discovery's real job is finding the journeys the customer forgot** — refunds, admin paths, retry-after-failure, plan downgrade. The ones nobody names on a discovery call because they aren't the demo. This is Heap Illuminate's Step Suggestions applied one level up.

**3c · Exemplars, capped.** Keep three to five worked journey registries as few-shot priming for the agent. Checkout is the first. **Stop at five and do not pursue industry coverage** — the exemplars exist to show the agent the shape, not to be selected from. Practitioner rule of thumb is 2–3 critical experiences per engagement anyway.

Keep the `SLI specification vs. implementation` split visible throughout: the grammar says *what matters*; the generated span tree says *how*, per stack. And order journeys by **business impact before touching telemetry** (SRE Workbook ch. 2).

### Phase 4 — Distribution (1 week, mostly config)

No new infrastructure required. Publish a git repo of `SKILL.md` directories; customers run:

```bash
warden add --remote sentry-solutions/warden-skills@<sha> --skill sentry-critical-experience
```

Pinned SHAs cache permanently; unpinned remotes refresh on `WARDEN_SKILL_CACHE_TTL` (default 24h). For the broader agent bundle, `dotagents` gives you `agents.toml` + `agents.lock` with resolved-commit pinning and `--frozen` for CI, materializing into `.claude/`, `.cursor/`, `.codex/` — which sidesteps "our security team only approves our own tooling."

### Phase 5 — Eval harness — BUILT

`eval/` (runner, static grader, task fixture with one correct and five deliberately-wrong solutions, 37 tests), plus `spec/generate.py --rubric`.

Convex published a **~20% lift in AI success rate** writing their code, credible only because they had an open eval harness and tuned guidelines against failing categories. This is that loop, and the reportable number is not any single score — it's the **per-check-kind failure rate across runs**, which points at the paragraph of the spec to rewrite. `--repeat` exists because agents are stochastic.

Two things this phase got wrong before the fixtures caught them:

- **The rubric must be generated, not hand-written.** `--rubric` emits `<id>-RUBRIC.json` from the same requirement list the markdown renders from, so grader and spec cannot drift. Ungradeable requirements are carried as `gradeable: false`, never as silent passes.
- **Guards, or the eval is blind to regressions.** The spec asks only for what is *missing*, which left everything already correct ungraded. The `attribute-typo` fixture — one file writing `checkoutId` instead of `checkout.id` — scored a **clean 100%**, because other files spelled it correctly and the correlation key was never a requirement. The rubric now also emits guards for every span and attribute that already worked; a guard failure is a regression and `clean` requires both. Static counterpart of `gap/diff.py`.

Agent-agnostic: `--agent` is a shell template. `--solution` and `--dry-run` need no agent at all, so the harness is usable before anything is wired up.

---

## 3. Design rules, with the evidence behind them

**Keep the spec short and non-discoverable.** An ETH Zurich study (Feb 2026) measured AGENTS.md/CLAUDE.md files *decreasing* agent task success ~3% while raising inference cost >20%. Cause is not that agents ignore them — agents are too obedient and faithfully execute irrelevant instructions. Codebase overviews were actively harmful. So: ship SDK footguns, deprecated APIs, and journey conventions. Cut every paragraph explaining what Sentry is.

**Lead with prohibitions.** Stripe's `llms.txt` carries an `## Instructions for Large Language Model Agents` section of concrete negatives — "Never recommend the legacy Card Element." Machine-actionable where "prefer modern patterns" is not. The DO-NOT-USE table in the spec is the single highest-value section, because a model trained on 2023 Sentry data will confidently emit `startTransaction` and `span.setData`.

**Branch-only writes, human merge.** Avo's MCP will never merge to main. Adopt verbatim for anything that writes.

**Seed from observed data, never a blank page — but never *only* from observed data.** Avo seeds from Inspector, Segment from 30 days of traffic, Amplitude from "Unexpected" events. Do the same for gap analysis. But seeding is an offer, not a gate: the customer's own declared journeys are a first-class input and outrank anything discovery produces.

**Grammar is maintained; content is generated.** Brendan Gregg shipped one method (USE) plus thin per-OS checklists *derived* from it. The method was the asset. Same here: `GRAMMAR.md` is hand-maintained and ~1 page; every span tree is output.

**Provenance is a field, not a mode.** Every journey candidate carries `source` and `confidence`. Never let the discovered set define the schema — if it does, a customer-declared journey that discovery missed starts looking like a validation failure, and the tool quietly teaches the SE to trust the scan over the customer.

**Exemptions are first-class.** All four service-scorecard vendors converged here independently, and their docs agree: rules a team cannot possibly pass destroy the score's credibility. Ship `applicable` / exemption support in the checker from v1.

**Start advisory, not blocking.** In `warden.toml`, `failOn = "off"` with `minConfidence = "high"`. Earn the right to block.

---

## 4. Failure modes to design against

| Trap | Evidence | Mitigation |
| --- | --- | --- |
| Codegen without verification dies | Segment's Typewriter shipped generation, never shipped a `status` verifier, went maintenance-mode | Build Phase 2 before generalizing Phase 3 |
| Nothing gets implemented | GitLab's public numbers: ~6 features instrumented per milestone against ~30 shipped; engineers "self-serve or give up" | The Warden PR check is the forcing function, not the spec |
| Stale spec gives false confidence | Practitioner consensus; data "rots from entropy, not malice" | Registry is versioned; `registry diff` gates breaking changes; spec carries an owner |
| Over-granularity | Rule of thumb: define ~10 most important user actions; instrumenting everything crowds out meaningful actions | 2–3 critical experiences per engagement, hard cap |
| Silent wrong instrumentation | "No compiler error and no failing test catches a misnamed event" (Avo CTO) — exactly true of span attributes | Coverage checker on attribute *names*, not just presence |
| False-negative verification | Sampling | Read `tracesSampleRate`; low-confidence flag below ~5% |
| Split-brain across agents | Multiple coding agents each internally consistent but mutually blind produce four names for one action | Registry is searched before minting; spec ships exact keys |
| Curated journey is simply wrong | Heap: 84% of funnel analyses misleading, 63% miss an alternative path to conversion | Grammar + per-customer instantiation; never ship a canonical industry journey |
| Discovery becomes a gate | A declared journey the scan missed reads as a validation error | Provenance as a field; `declared` ranks first; discovery is skippable |
| Frequency mistaken for importance | Health checks and polling dominate volume; refunds and disputes are rare and expensive | Rank by business impact, human-assigned — never by span count |
| Chasing industry coverage | The catalog trap re-entered through the exemplar door | Hard cap of five exemplars; they prime the agent, they are not selected from |
| PR check fires constantly | Warden's own security skill: "Prefer no finding over speculative hardening advice" | `minConfidence = "high"`, tight `paths` globs, explicit *What Not To Report* |

---

## 4b. Where GitHub fits — and where it deliberately doesn't

Three distinct roles, and conflating them is the trap.

**1. Discovery seed — read-only, customer-side.** The agent enumerates candidate journeys from routes, handlers, state machines, and dependency manifests. Needs read access only, and it runs on the customer's machine because the SE has no repo access. This is already `intake` source `discovered:code`.

**2. Standing PR check — customer-side, their CI.** The Warden skill. Already a PR-based flow, already built, needs no access from us: the customer installs it and it runs under their own credentials.

**3. Push a PR — YOUR repos only.** This is the one to be careful about. Pushing instrumentation into a *customer's* repo contradicts the entire thesis: the customer's own approved model writes the code, in their repo, under their review. Their security team approved that model, not ours.

Where pushing genuinely pays off is the **eval harness** (§Phase 5): fork or branch your reference/demo app, run `spec/out/<id>-SPEC.md` through a coding agent, push the branch, re-run the gap analysis against the resulting telemetry, score it. That is how the spec becomes a tunable artifact instead of a craft one — and how the Convex-style "measured N% lift" claim becomes something you can say on a call.

Practical note on permissions: nothing here has push rights by default. A PR needs `gh` authenticated with `repo` scope, or a GitHub App / fine-grained PAT with `contents: write` + `pull_requests: write` on the specific repo. Neither `gh` nor a git repo exists in the sandbox this was built in, so **that step runs wherever you run the kit, under your own credentials** — and it should stay scoped to repos you own.

## 4c. Post-implementation validation — BUILT

`gap/diff.py`, plus a paired `observed-customer.after.json` fixture and 31 tests. Takes two `gap.json` snapshots and reports what changed.

Regressions lead the report unconditionally, above the score — a rule that used to pass and now fails is the one thing that must not get buried under a celebratory delta. Comparability is checked before anything is compared: different orgs, different windows, or a sample rate that moved more than 1.5× get flagged rather than producing a confident meaningless number. Findings key on `(rule, entity)` so per-step rules don't conflate.

On the paired fixtures: 8 resolved, 1 regression, Checkout 53.3 → 92.1 and *needs improvement → excellent*. The deliberate regression — `checkout.shipping_submitted` disappearing in a refactor — is the realistic failure mode, and the report surfaces it first.

**Operational requirement:** snapshot `observed.json` *and* `gap.json` at engagement kickoff. Without a baseline there is nothing to diff against later.

### Original notes

Already the structure, not an addition: after the customer ships, re-run `gap/analyze.py` against a fresh `observed.json` and compare. The pieces exist —

- `gap/sentry_source.py` snapshots an org/project scope at a point in time
- `gap/analyze.py` scores each journey and lists findings
- `gap/instrumentation_profile.py` reports the automatic-vs-custom split

What is **missing** is the diff itself: a tool that takes two `gap.json` files and reports what changed — steps newly instrumented, findings resolved, score delta, and any regression. That is the highest-value remaining build, because a before/after visibility diff is the artifact that proves the engagement worked and earns the next conversation. Snapshot `observed.json` and `gap.json` at engagement start so the baseline exists when you need it.

## 5. Open items

- [x] **RESOLVED — span query endpoint.** See Phase 2 implementation note. Short version: attribute discovery is public (`trace-items/attributes/`, with a `source_type: sentry|user` field that is exactly the gap-analysis primitive); span row/aggregate query is **not** publicly documented, so go through the MCP `search_events` rather than the undocumented `/events/?dataset=spans`.
- [x] **RESOLVED — namespace convention.** `getsentry/sentry-conventions` CONTRIBUTING policy is explicit: *"The attribute MUST be namespaced. Example: `nextjs.function_id`, not `function_id`"*, dots as separators, `snake_case` for multi-word, namespace first, and OTel alignment preferred over Sentry-specific synonyms. `cart.value` and `checkout.id` already satisfy this — a company prefix is **not** required. Empirically checked the generated attribute registry: **no `cart.*`, `checkout.*`, `order.*`, `payment.*`, `inventory.*`, or `commerce.*` attributes exist in Sentry's conventions today**, so these namespaces are currently free. Kit stays unprefixed. Residual risk is genuine but small: names are permanent once shipped and the registry does grow, so re-run the collision check (below) before adding a new journey namespace, and prefer a company prefix for anything as generic as `user.*` or `session.*`.
- [ ] No documented cap on attribute count or value length was found in Sentry docs or develop.sentry.dev — do not put a numeric cap in the spec until confirmed

### Repeatable verification recipes

**Any Sentry API or docs claim** — `docs.sentry.io` serves every page as markdown by appending `.md`, and publishes `docs.sentry.io/llms.txt` as a site index. A 404 returns the sibling page list for that section, which is how to enumerate what an API section actually contains. `develop.sentry.dev` does the same. Use this instead of a browser; the HTML pages are client-rendered and fetch empty.

**Namespace collision, before minting a new journey namespace** — two checks:

1. Registry check: grep the generated attribute list at `getsentry.github.io/sentry-conventions/attributes/` for the namespace. Zero matches means it is unclaimed in Sentry's conventions.
2. Live check, per customer: `GET /trace-items/attributes/?dataset=spans&substringMatch=<namespace>` and inspect `attributeSource.source_type`. Any `sentry` result means the product already owns that key — pick another. Any `user` result means the customer is already sending it, which is a gap-analysis finding in its own right, and possibly a naming conflict with their existing instrumentation.
- [ ] Cross-page-navigation journeys: a browser navigation starts a **new trace**, so a multi-page checkout cannot be one trace. Kit handles this by correlating on `checkout.id` across traces — validate this reads well in Trace Explorer before committing to it
- [ ] `createFixPR` / `fixBranchPrefix` are documented for Warden schedule triggers but absent from the v0.43.0 schema — don't rely on them
- [ ] Only `security-review` and `code-review` actually ship as Warden built-ins; the other skills on the landing page are illustrative. Don't reference them to customers as installable

---

## Sources

**Weaver / semconv:** [registry_repo.rs](https://raw.githubusercontent.com/open-telemetry/weaver/main/crates/weaver_semconv/src/registry_repo.rs) · [manifest.rs](https://raw.githubusercontent.com/open-telemetry/weaver/main/crates/weaver_semconv/src/manifest.rs) · [semconv-syntax.md](https://raw.githubusercontent.com/open-telemetry/weaver/main/schemas/semconv-syntax.md) · [weaver_live_check README](https://raw.githubusercontent.com/open-telemetry/weaver/main/crates/weaver_live_check/README.md) · [live_checker.rs](https://raw.githubusercontent.com/open-telemetry/weaver/main/crates/weaver_live_check/src/live_checker.rs) · [usage.md](https://raw.githubusercontent.com/open-telemetry/weaver/main/docs/usage.md) · [model/README.md](https://github.com/open-telemetry/semantic-conventions/blob/main/model/README.md)

**Sentry:** [Tracing instrumentation](https://docs.sentry.io/platforms/javascript/tracing/instrumentation/) · [APIs](https://docs.sentry.io/platforms/javascript/configuration/apis/) · [Trace Explorer](https://docs.sentry.io/product/trace-explorer/) · [Distributed tracing](https://docs.sentry.io/platforms/javascript/tracing/distributed-tracing/) · [Span operations](https://develop.sentry.dev/sdk/telemetry/traces/span-operations/) · [Span protocol](https://develop.sentry.dev/sdk/telemetry/spans/span-protocol/) · [sentry-conventions](https://github.com/getsentry/sentry-conventions/) · [v9→v10 migration](https://docs.sentry.io/platforms/javascript/migration/v9-to-v10/) · [Warden](https://warden.sentry.dev/) · [dotagents](https://dotagents.sentry.dev/)

**Prior art:** [Instrumentation Score spec](https://github.com/instrumentation-score/spec) · [Elastic's critique](https://www.elastic.co/observability-labs/blog/otel-instrumentation-score) · [Avo workflow](https://www.avo.app/docs/workflow/overview) · [Avo validate](https://www.avo.app/docs/workflow/validate) · [Segment Ecommerce V2](https://www.twilio.com/docs/segment/connections/spec/ecommerce/v2) · [Convex Evals](https://stack.convex.dev/convex-evals) · [ETH Zurich context-file study](https://www.infoq.com/news/2026/03/agents-context-file-value-review/) · [spec-kit template](https://raw.githubusercontent.com/github/spec-kit/main/templates/spec-template.md) · [SRE Workbook ch.2](https://sre.google/workbook/implementing-slos/)
