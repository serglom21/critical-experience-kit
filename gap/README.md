# Gap analysis

Layer 2. Diffs resolved journeys against what a customer's Sentry org actually contains, and produces the report that makes the second call click.

```
gap/
├── rules.md                                  the rule catalog — read first
├── analyze.py                                journey diff + score
├── diff.py                                   before/after visibility diff
├── instrumentation_profile.py                automatic vs custom + recommendations
├── sentry_source.py                          builds observed.json from a live org
├── test_analyze.py                           53 tests
├── test_instrumentation_profile.py           23 tests
├── fixtures/
│   ├── observed-customer.example.json         synthetic, exercises every rule
│   └── observed-demo-org.live.json            REAL data from the `demo` org
├── example-gap-report.md                     generated
├── example-profile.md                        generated
└── example-profile-demo-org.md               generated, from live data
```

## Run it

```bash
# Offline, against the shipped fixture
python3 analyze.py \
  --resolved ../intake/example-resolved.json \
  --observed fixtures/observed-customer.example.json \
  --include-unready --out-md gap-report.md --out-json gap.json

python3 -m unittest discover -s . -p 'test_*.py'
```

## Against a real org, via the Sentry MCP

The selection step happens in-session: ask the SE which org and projects, then run the two queries and hand the result to the tooling.

**1. Pick the scope.** `find_organizations()` lists orgs with their `regionUrl` (`https://us.sentry.io` or `https://de.sentry.io` — that's the `--host`), and `find_projects(organizationSlug=...)` lists projects. Or, without the MCP:

```bash
python3 sentry_source.py --org acme --token $SENTRY_AUTH_TOKEN --list-projects
```

**2. Pull the two span queries** with the MCP and concatenate them into one JSON file:

```
# names — what journey steps are matched against
search_events(organizationSlug='acme', dataset='spans',
              fields=['span.description','count()'], sort='-count()',
              period='30d', limit=100)

# ops — what the automatic-vs-custom classification is built on
search_events(organizationSlug='acme', dataset='spans',
              fields=['span.op','count()'], sort='-count()',
              period='30d', limit=50)
```

**3. Snapshot, profile, analyze.**

```bash
python3 sentry_source.py --org acme --token $SENTRY_AUTH_TOKEN \
  --host https://us.sentry.io --project frontend --project api \
  --stats-period 30d --traces-sample-rate 0.25 \
  --from-mcp mcp-spans.json --out observed.json

python3 instrumentation_profile.py --observed observed.json --gap gap.json --out-md profile.md
python3 analyze.py --resolved resolved.json --observed observed.json --out-md gap-report.md
```

`--project` takes slugs or numeric ids and resolves slugs for you. Attributes come from the documented endpoint scoped to those projects; span names and ops come from the MCP.

## Automatic vs custom: what the service is actually sending

`instrumentation_profile.py` answers the question that opens the conversation — *is this service sending automatic instrumentation, custom instrumentation, or both?* — and turns the answer into ranked recommendations.

Two signals, and only one of them is a heuristic:

| Signal | Authority |
| --- | --- |
| `attributeSource.source_type` (`sentry` vs `user`) | **Documented field.** Definitive |
| Span `op` family, longest-prefix matched against Sentry's documented op vocabulary | **Documented vocabulary.** Definitive |
| Span description "looks like a code location" | **Heuristic.** Labelled as such everywhere it surfaces |

That third one earns its keep. Run against the real `demo` org, `src.db.get_products` and `UIKit.NavigationBarContentView.__backButtonAction` arrive with `op: function` / `ui.action` — genuinely custom ops, but SDK-derived code-level tracing, not business instrumentation. `items_added_to_cart`, `processCheckout`, and `handleApplyPromoCode` are the real business spans. Without the split, a service looks instrumented for its funnel when it is only instrumented for its call stack.

The profile places the org in one of five tiers, each with a different opening line:

| Tier | The conversation |
| --- | --- |
| `none` | Confirm the SDK is sending at all |
| `automatic only` | The whole pitch. Healthy syntactic layer, zero business intent |
| `attributes without journey spans` | Cheapest start — the plumbing exists, only spans are missing |
| `spans without business attributes` | Funnel counted in requests rather than outcomes |
| `custom instrumentation present` | Completeness and correctness, not adoption |

Live `demo` org result: 10 automatic families (24.5M UI rendering spans, 1.8M HTTP client, 1.1M database), 6 custom business spans, **0 customer-defined attributes** → *spans without business attributes*. A low custom share is normal — Heap reports roughly 10% of the events in their own reports are manually tagged. **Zero is the finding.**

`--include-unready` also analyzes journeys the intake resolver marked not-spec-ready. Useful early: a journey missing its outcome role still has steps worth diffing.

## The two data sources, and why they're treated differently

**Attribute presence — public, documented, safe to depend on.**

```
GET /api/0/organizations/{org}/trace-items/attributes/?dataset=spans&statsPeriod=30d
```

Returns `key`, `attributeType`, and `attributeSource.source_type` — `sentry` for SDK-provided attributes, `user` for customer-defined ones. That last field is the whole gap analysis in one value: it separates what their SDK gives them from what they instrumented themselves. Scopes: `org:read`.

**Span names and counts — not publicly documented.** The Discover & Performance API section contains no span query endpoint. `/organizations/{org}/events/?dataset=spans` is what Trace Explorer and the MCP call, but its absence from the public reference makes it an unstable contract. `sentry_source.py` implements it behind `--unsafe-span-query` with a warning; the supported path is `--from-mcp`.

Hence the fixture: the analyzer is fully testable with no credentials and no dependency on an undocumented endpoint.

## What it finds

Thirteen rules, defined in `rules.md` using the Instrumentation Score record format (`id`, `description`, `rationale`, `target`, `impact`) with weights Critical 40 / Important 30 / Normal 20 / Low 10. The mandatory `rationale` is what makes a finding persuasive to a customer's engineer rather than a scold from a tool — it appears verbatim in the report under "why these matter," ready to say out loud.

The four findings worth building the call around:

- **CE-012 dark segment** — a run of missing steps *bounded on both sides*. This produces the "your checkout is visible to payment, then goes dark" sentence. A gap at the end is not a dark segment; it's a truncated journey, and CE-002 handles that.
- **CE-013 name drift** — an observed span name that normalizes to a declared step but differs literally. In the fixture, `checkout.payment.authorized` is being sent while every query looks for `checkout.payment_authorized`. No error, no failing test, no data. The fix is a rename, not new instrumentation, which is why it's reported separately.
- **CE-007 boolean outcome** — Sentry renders booleans as the strings `'true'`/`'false'`, and a binary collapses failed, abandoned, and rejected into one bucket. Those three route to three different teams.
- **CE-010 stringified magnitude** — `cart.value` sent as `"129.99"` cannot be aggregated. `sum()` and `p50()` silently return nothing useful. The instrumentation looks correct and produces no answer.

## Two design decisions the runs forced

**Coverage state before score.** The first version sorted journeys by score ascending. Four entirely-uninstrumented journeys landed on top, all on the same meaningless ~15 (only the "not declared" rules varied), and the one journey with a real diagnosable gap sat at the bottom. Journeys are now classified `partial` / `complete` / `absent` first:

- **partial** leads the report — the only actionable tier, and the worst combination, because a partial funnel looks like a working one
- **complete** follows
- **absent** gets a compact table and **no grade at all**. Zero instrumented steps is not a low score, it's work that hasn't started

One nice consequence: the absent table shows whether the correlation key is already present. A journey with the key in place and no spans is the cheapest possible win — the plumbing exists.

**Grade capping.** Weighted averages let attribute hygiene mask journey blindness. Borrowed from SSL Labs: `CE-001` (no root span) caps at 49, `CE-002` (no terminal outcome span) caps at 74. Checkout in the fixture scores 62.9 raw and reports **53.7 capped**, because a journey that never records reaching a terminal state cannot tell success from failure.

Also deliberately avoided: the aggregation bug in the reference implementation, where the OTel Demo scores 35 overall while every individual service scores higher, because the overall score ANDs across services each failing a *different* rule. Score per journey, report the distribution, never AND.

## Before/after: proving it worked

```bash
# at engagement start — keep these, they are the baseline
python3 analyze.py --resolved resolved.json --observed observed-baseline.json \
  --out-json gap-baseline.json

# after they ship
python3 analyze.py --resolved resolved.json --observed observed-now.json \
  --out-json gap-now.json
python3 diff.py --baseline gap-baseline.json --current gap-now.json \
  --out-md visibility-diff.md
```

Snapshot `observed.json` **and** `gap.json` at kickoff or the baseline won't exist when you need it.

Three design priorities, in this order:

**1. Regressions first, always.** A rule that used to pass and now fails leads the report, above the score. Instrumentation rots — a refactor drops a span, an SDK upgrade renames an op, someone cleans up an attribute. A report that only celebrates wins is how a tool loses trust. `--fail-on-regression` exits 3, for scheduled monitoring.

**2. Comparability before comparison.** Different orgs, different windows, or a sample rate that moved more than 1.5× make the numbers meaningless. Those get flagged loudly instead of printing a confident delta. A span that looks "new" may simply have become visible.

**3. Coverage transitions over score deltas.** `absent → partial` is a finding; 53.3 → 92.1 is a summary. The report leads with the state change and the resolved-rule list.

Findings are keyed on `(rule, entity)`, not rule alone — `CE-003` fires once per step, and conflating them would report one step's fix as another step's regression.

On the paired fixtures (the same org before and two months after implementing the generated spec): **8 findings resolved, 1 regression**, Checkout 53.3 → 92.1 and *needs improvement → excellent*. The rename closed the `payment_authorized` dark segment, the terminal span released the `CE-002` grade cap — and `checkout.shipping_submitted` vanished in a refactor, which is exactly the kind of quiet rot the report exists to surface.

## Honest limits

- **Extent is approximate.** It compares per-span-name totals rather than joining on the correlation key, so a step firing twice per journey understates the gap. Labelled as approximate in the report.
- **Sampling causes false negatives.** Below a 5% `traces_sample_rate` every finding degrades to low confidence and the report says so, because Trace Explorer aggregates are sampling-extrapolated and warn at that threshold. A missing span may be uninstrumented, sampled out, or a step that legitimately didn't happen.
- **Span-name matching is literal.** That's deliberate — a loose match would hide the drift finding, which is one of the most valuable outputs.
- **Query windows are plan-gated:** Developer 7 days, Team 14, Business 30.

## Feeds

`example-gap.json` carries per-finding `rule`, `rationale`, `impact`, `weight`, `passed`, `extent`, `entity`, and `example`, plus `coverage_state` and `caps` per journey. Next consumer is spec generation: the failed rules become the requirements, and `rationale` becomes the `WHY.md` content.
