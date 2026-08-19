# `ce` — handoff

Everything needed to pick this up cold: what it is, why it is shaped this way,
what has been verified against reality, what broke and why, and what is missing.

`AGENTS.md` is the short version — the invariants. This is the reasoning behind
them. Read `GRAMMAR.md` for the domain model and `PROVENANCE.md` for what the
design borrows from.

---

## 1. The problem this started from

A Sentry SE spends most of an engagement on work that is not repeatable:

> It takes time to understand the customer, it takes time to understand the
> architecture, and it takes time to implement the recommendations on my side and
> then wait for the customer to implement them.

The engagement shape was: pitch critical experiences using a generic demo → let it
sit → come back with a demo built around *their* flows → then hand-build
instrumentation recommendations. The second call is where it lands, and it is the
most expensive thing to prepare.

The original ask was for an agent that produces **instructions, not code**, on the
grounds that non-frontier models write instrumentation badly.

## 2. The reframe that shaped everything

The constraint is not model quality. It is **context locality**.

Even a frontier model cannot instrument code it cannot see, and the SE often
does not have the customer's repo. So the customer runs `ce` *in their tree*,
and the code-writing step still belongs on *their* machine, with *their* model —
which their security team already approved. That is not a compromise; it is the
correct architecture, and it makes the deliverable a **specification**.

Everything follows from that. `ce` produces markdown, JSON, and YAML. It has never
emitted a line of application code, and an audit confirms it: the only code-shaped
content is the API reference in a generated spec, and that uses deliberate
placeholders (`"<span name from §2>"`, `<key>`, `doWork()`).

## 3. The second reframe: grammar, not catalog

The first design was a curated library of canonical journeys per industry — "an
ecommerce checkout has these seven spans." That was abandoned for three reasons:

- **Curated journeys are usually wrong.** Heap measured its own corpus: 84% of
  funnel analyses deliver misleading data, 63% miss an alternative path to
  conversion.
- **Shape is not a stable identity.** Meta's ATC'23 trace study found production
  workflows in "constant flux."
- **Content catalogs are expensive.** OTel semconv is the reference
  implementation — enormous, deliberately slow, permanent by policy. Segment
  shipped semantic event specs and told customers to hand-build the library.

What replaced it: a **seven-role grammar**, borrowed rather than invented.
Kimball's accumulating snapshot fact table already formalised "a defined start
point, standard intermediate steps, and defined end point"; his degenerate
dimension is the correlation key. dbt MetricFlow, Cube, and Malloy independently
converged on entity/dimension/measure.

The load-bearing idea is the SRE Workbook's **SLI specification vs
implementation** split: `GRAMMAR.md` is the specification layer and does not rot;
per-customer span trees are implementations, generated and disposable. That is what
dissolved the maintenance problem.

The seven roles: **journey, correlation key, step marker, outcome, failure reason,
magnitude, actor segment.** Spans are the default implementation; Issues, logs,
and Application Metrics are companions on a *failed* gap rule when `SIGNAL.md`
says so — not new roles, and never always-on asks.

## 4. How it works

Thirteen dispatched stages plus six built-ins (`doctor`, `init`, `local`,
`discover`, `review`, `report`), all through `cli.py`. Each stage is a standalone
script; `cli.py` only routes, so nothing has a second copy.

Customer-run wrappers (from the service root):

    ce discover → propose + scan + intake          (workdir: ce-work/)
    ce review   → keep/drop + business_impact      (browser or --apply/--stamp)
    ce report   → gap + profile + spec + publish   (ce-work/ + .agents/journeys/)

```
                     ┌─ ce propose ──┐  source → journey CANDIDATES
                     │               │  (the producer for discovered:code)
  ce init ───────────┤               │
  (hand-authored)    └───────────────┴──→ ce intake ──→ resolved.json
                                              │
        ┌─────────────────────────────────────┤
        │                                     │
   ce scan (source)                    ce local (live telemetry, local)
   ce snapshot (live org)              ce runtime (bundled demo)
        │                                     │
        └────────────→ observed.json ←────────┘
                            │
                      ce gap ──→ gap.json ──→ ce profile   (automatic vs custom)
                            │            └──→ ce spec ──→ SPEC.md / WHY.md / RUBRIC.json
                            │                                   │
                      ce diff (baseline vs current)         ce grade / ce eval
                            │                                   │
                    "did it land, did anything regress"    "does the spec work"
```

`ce registry` / `ce validate-registry` emit and check an OTel semconv registry from
the same resolved journeys.

### Data contracts between stages

These are the seams. Breaking one breaks the pipeline quietly, which is why
`test_pipeline.py` exists — the per-layer suites build their inputs inline and
would not notice.

| Artifact | Produced by | Consumed by | Key fields |
| --- | --- | --- | --- |
| `journeys.yaml` | `ce propose`, `ce init`, human | `ce intake` | `intake/schema/journey-candidate.schema.json` |
| `resolved.json` | `ce intake` | `gap`, `spec`, `registry`, `local` | `id`, `name`, `excluded`, `spec_ready`, `blockers`, `roles{}` |
| `observed.json` | `scan`, `snapshot`, `local`, `collector` | `gap`, `profile` | `span_names[]`, `span_ops[]`, `span_pairs[]`, `attributes[]`, `_synthetic_counts`, `sdk{}`, `_provenance` |
| `gap.json` | `ce gap` | `spec`, `diff` | `coverage_state`, `score`, `caps`, `findings[]`, `dark_segments` |
| `RUBRIC.json` | `ce spec --rubric` | `ce grade`, `ce eval` | `requirements[]` with `check`, `guards[]` |

### The three starting states

All must work; each was a separate bug once.

| State | Service has | Entry point |
| --- | --- | --- |
| A | Sentry installed, no journey coverage | `ce local --drive` — profile reads *automatic only* |
| B | No Sentry at all | `ce propose` + `ce scan`, then `ce spec --include-absent` |
| C | Partial custom instrumentation | either; `ce spec` asks only for the delta, guards the rest |

## 5. Verified external facts

These cost debugging rounds or source reading. Treat them as settled.

**Weaver cannot enforce span existence.** `registry live-check` is sample-driven.
Verified in `crates/weaver_live_check/src/live_checker.rs`: lookup maps exist for
attributes, metrics, events, entities — there is no `find_span`, and
`sample_span.rs` never resolves a registry group. The finding enum has
`missing_metric` and `missing_event`, no `missing_span`. Consequence:
`requirement_level` on a span group is documentation, and journey completeness is
checked against Sentry instead. This single fact shaped the architecture.

**Weaver manifest schema.** File must be `manifest.yaml`. `schema_url` required;
`name`/`semconv_version`/`schema_base_url` deprecated. A dependency needs
`schema_url` — one carrying only `name:` hard-fails, which means Weaver's own
`docs/define-your-own-telemetry-schema.md` example is stale. Max one dependency
(weaver#604). `prefix:` is rejected outright.

**Sentry API surface.** `GET /trace-items/attributes/?dataset=spans` is public and
documented, and returns `attributeSource.source_type` — `sentry` for SDK-provided,
`user` for customer-defined. That field is the automatic-vs-custom discriminator
and beats any naming heuristic. Span row/aggregate query is **not** publicly
documented; `/events/?dataset=spans` is what Trace Explorer and the MCP call, so
it is fenced behind `--unsafe-span-query`.

**Docs are fetchable as markdown.** Append `.md` to any `docs.sentry.io` or
`develop.sentry.dev` URL. A 404 returns the sibling page list for that section,
which is how to enumerate what an API section actually contains. The HTML pages
are client-rendered and fetch empty.

**Sentry envelope wire format** (verified against live `@sentry/node` 10.70.0, not
docs): `Transfer-Encoding: chunked` with no Content-Length, sometimes gzipped —
reading Content-Length alone captured *zero bytes*. Newline-delimited JSON:
envelope header, then alternating item-header/payload. For `type: transaction` the
**root span's name is the payload's top-level `transaction` field**, not anything
in `contexts.trace`. Root attributes live in `contexts.trace.data`; children are in
`spans[]` with the name in `description`. SDK-internal attributes are prefixed
`sentry.`. Values keep real JSON types — which is why runtime grading can resolve
what static analysis cannot.

**Cross-SDK differences.** JS uses `span.setAttribute`; Python uses
`span.set_data`. Both land in `data`. And in `sentry-sdk` 2.x a bare
`start_span()` with no active transaction produces **nothing** — no envelope, no
error, exit 0. Journey roots must be `start_transaction`.

**Warden** (`@sentry/warden` v0.43.0, read from the npm tarball): `SKILL.md`
frontmatter is exactly `name`, `description`, `allowed-tools`. Scoping lives in
`warden.toml`, not frontmatter. Only `security-review` and `code-review` ship as
built-ins — the others on the landing page are illustrative.

**Instrumentation Score** is real, is 100% technical hygiene (no journey rules),
and its conformance clause forbids adding rules that affect the standard score. We
borrow its record format, weights (40/30/20/10), formula, and bands, and run our
`CE-*` rules as a sidecar. Not conformant, and does not claim to be.

## 6. Bug log

Every one of these produced output that looked plausible and described the wrong
thing. All have regression tests.

| Where | Bug | Why it mattered |
| --- | --- | --- |
| `spec/generate.py` | Emitted "rename this span" *and* "create this span" for the same drifted span | Contradictory instructions; an agent duplicates or stalls |
| `gap/analyze.py` | `CE-009` was one aggregate, passing if *any* magnitude existed | `order.value` reported "present" while absent from the org |
| `gap/analyze.py` | Collected `example` trace IDs and never rendered them | Violated its own `rules.md` requirement |
| `intake/resolve.py` | Volume was a general tiebreaker | A 9.2M-instance `/healthz` outranked a refund flow |
| `eval/grade.py` | Comments inside object literals broke pair parsing | `cart.value` read as "never set" when it was right there — worst kind of false negative |
| `eval/grade.py` | Property access → `indeterminate` | A *perfect* solution scored 7/9 |
| `eval/*` | Rubric had no guards | One file typoing `checkoutId` scored a **clean 100%** |
| `gap/instrumentation_profile.py` | Exact op matching | `browser.DNS`, `ui.webvital.cls` fell into "unclassified" — an incomplete table read as a customer finding |
| `gap/instrumentation_profile.py` | Judged span names without their ops | `SELECT * FROM carts` counted as custom business instrumentation, flipping the tier away from *automatic only* |
| `gap/code_scan.py` | `import sentry_sdk` regex only | `import os, sentry_sdk` missed → State C misdiagnosed as State B |
| `gap/propose.py` | `(?:type|)` in the union regex | `export type` never matched, so every state machine was missed |
| `gap/propose.py` | Singularised before stripping `_status` | `checkout_status` → `checkout_statu`; no journey matched its own state machine |
| `gap/propose.py` | Substring hint matching | `"view"` inside `"review"` → refund ordered review → request → settle |
| `gap/propose.py` | Hint-sorted route-derived steps | `shipping` (no lifecycle verb) landed after `payment` and `confirm` |
| `registry_gen/generate.py` | Backticks in a bare mapping value | Emitted invalid YAML; caught by its own validator on first run |
| `cli.py` | Wrote `.ce-observed.json` to cwd | Litter in `git status` next to src/. Now: only under `ce-work/`, gitignored |
| `pyproject.toml` | Declared `>=3.10` on an assumption | Locked out a managed 3.9 install for no reason |

**Pattern worth internalising:** almost all of these were found by running the tool
against realistic input, not by unit tests written alongside the code. The synthetic
fixtures were too clean. When adding a feature, run it against a real-shaped repo or
a real SDK before trusting it.

## 7. Test layout

309 tests, excluding `eval/runtime` (24 more, needs `npm install`). Customer-run
path (`discover`/`review`/`report`) is in `test_cli.py`.

| Suite | Tests | Covers |
| --- | --- | --- |
| `intake/test_resolve.py` | 40 | matching, 2×2 status, role completion, ranking, exclusion |
| `intake/test_review.py` | 5 | POST apply, default-drop of `web`, impact never inferred |
| `gap/test_analyze.py` | 47 | the 13 rules, capping, extent, sampling guard, coverage state |
| `gap/test_diff.py` | 31 | rule classification, comparability, regression precedence |
| `gap/test_instrumentation_profile.py` | 29 | tiers, op families, code-location heuristic, live fixture |
| `gap/test_propose.py` | 29 | state machines, step ordering, what it refuses to decide, secrets |
| `gap/test_code_scan.py` | 21 | SDK detection, synthetic counts, the three states, secrets |
| `registry_gen/test_generate.py` | 36 | semconv syntax, ref hygiene, manifest schema |
| `spec/test_generate.py` | 7 | SDK family for §5/§6 (JS vs Python, State B languages) |
| `spec/test_publish.py` | 3 | Warden paths from evidence, `failOn=off` |
| `eval/test_grade.py` | 37 | golden solutions — one correct, five specifically wrong |
| `eval/runtime/test_collector.py` | 24 | envelope parsing against a real SDK capture |
| `test_pipeline.py` | 15 | **the seams** — artifacts flowing between stages |
| `test_cli.py` | 30 | dispatch, doctor, init, local, discover/review/report, wheel install |
| `test_py39_compat.py` | 5 | the Python floor stays 3.9 |

`test_pipeline.py` matters most for refactors: the per-layer suites build inputs
inline, so a rename in `resolve.py`'s output would leave them all green.

## 8. Known gaps

Ordered by how much they would matter to a customer.

1. **The generated spec's API section is JS and Python.** Go still missing.
2. **`ce grade` static analysis is JS/TS-only.** `ce local` and `ce gap` are
   language-agnostic; the authoring and grading side is not.
3. **`ce propose` covers JS/TS and Python.** Go, Ruby, Java, .NET produce nothing.
4. **`weaver registry check` never run.** The binary was not installable in the
   build environment; `registry_gen/validate.py` substitutes with the official
   semconv JSON schema plus Weaver's in-code constraints. Run the real thing once.
5. **Runtime eval fixture is Node-only.** The collector is SDK-agnostic; the
   bundled demo app is not.
6. **No exemplar library beyond checkout.** `GRAMMAR.md` caps it at five
   deliberately, but only one exists.
7. **Sampling is asserted, never observed.** `ce local` records the rate you pass
   it and does not verify it.
8. **The code-location heuristic in the profile is the one real guess.**
   `source_type` and op families are authoritative; pattern-matching a span
   description is not, and it is labelled as such wherever it surfaces.

## 9. If you change X, check Y

- **`intake/resolve.py` output shape** → `test_pipeline.py`, plus `gap/analyze.py`,
  `spec/generate.py`, `registry_gen/generate.py` all read `roles{}`.
- **A gap rule id or its `entity`** → `gap/diff.py` keys findings on
  `(rule, entity)`; `spec/generate.py` maps failed rules to requirements; the
  rubric's `guards` derive from *passing* rules.
- **`spec/generate.py` requirement construction** → `build_rubric` reads the same
  list, so `eval/grade.py` follows automatically. Add a `check` for anything new
  or it silently becomes ungradeable. Companion signals (`SIGNAL.md`) hang extra
  prose on those same FRs; they must not become a new `CE-*` rule.
- **`observed.json` shape** → four producers (`scan`, `snapshot`, `local`,
  `collector`) and two consumers (`gap`, `profile`). `span_pairs` is optional but
  the profile degrades without it.
- **The Python floor** → `pyproject.toml`, `cli.py::MIN_PYTHON`, and
  `test_py39_compat.py::FLOOR` must agree; the test enforces it.

## 10. Next builds, in order

1. **Multi-language spec generation.** Parameterise §5/§6 of the generated spec by
   the customer's SDK. **Done for JS vs Python** (`--sdk auto` reads scan
   `source_languages` / installed SDK). Go still missing.
2. **Run `weaver registry check`** on generated output and fix whatever it finds.
3. **`ce propose` for one more language** — Go or Ruby, whichever your book needs.
4. **Wire the eval loop to a real agent** and get the per-check-kind failure rate
   across repeated runs. That is the number that makes the spec tunable, and the
   claim you can make on a call.
5. **Second and third exemplar journeys**, from real engagements rather than
   invented ones.
