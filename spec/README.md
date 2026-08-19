# Spec generation

Layer 2b. Turns resolved journeys + gap findings into the two files the customer receives.

```
spec/
├── generate.py           the generator
└── out/                  generated
    ├── checkout-SPEC.md  for their AI coding agent
    └── checkout-WHY.md   for the human
```

## Run it

```bash
python3 generate.py \
  --resolved ../intake/example-resolved.json \
  --gap ../gap/example-gap.json \
  --out-dir out

python3 generate.py ... --journey checkout       # limit to one journey
python3 generate.py ... --include-absent         # also spec uninstrumented journeys
```

## The one rule that shapes everything

**Only ask for what is missing.** Every requirement is derived from a *failed* gap rule. On the fixture, Checkout has 7 declared steps and 5 already instrumented, so the spec asks for 9 things instead of restating 7 spans and 12 attributes. Handing a customer a full spec when most of it already exists wastes their engineers' time and destroys the credibility of the exercise. Issues, logs, and Application Metrics are companions on those same failed rules when [`SIGNAL.md`](../SIGNAL.md) says so — never always-on asks.

§2 of the generated spec marks each span either **MISSING — add it** or *present, leave alone*, and §0 instructs the agent not to touch the latter.

Corollary: journeys with `coverage_state == "absent"` are **skipped by default**. Zero instrumented steps needs a kickoff conversation about the journey itself, not a gap-driven diff. `--include-absent` overrides.

## Two ordering bugs the first run produced

**Drift superseded create.** A drifted span fails both `CE-013` (wrong name) and `CE-003` (nothing at the bound name). The first version emitted both "rename it" *and* "create it" for the same span — contradictory instructions that make an agent either duplicate the span or stall. Drift now claims the span and suppresses the create requirement, and the rename requirement says so explicitly.

**Per-attribute magnitude.** `CE-009` was a single aggregate that passed as soon as *any* magnitude attribute existed, so `order.value` was reported "present" in the spec while being entirely absent from the org. The rule is now one finding per declared magnitude, and the generator asks only for the absent ones.

## What the two files contain

**`<id>-SPEC.md`** — for the agent. Terse and imperative.

| § | Content |
| --- | --- |
| 0 | Instructions for AI coding agents — including *don't touch what already works* |
| 1 | Why this journey, from the gap findings (dark segments, drift volume) |
| 2 | Span contract with current state per span |
| 3 | Attribute contract with per-attribute state |
| 4 | Numbered requirements, `FR-001…`, RFC-2119 MUST, one per failed rule |
| 5 | SDK API reference — only current v10 APIs |
| 6 | Do-not-use table — the deprecated APIs a model will emit from stale training data |
| 7 | Acceptance criteria, `SC-001…`, each checkable in Trace Explorer |
| 8 | Out of scope |
| 9 | Open questions as `[NEEDS CLARIFICATION]`, carried from intake |

Section 5 adapts: the distributed-tracing block only appears when the journey crosses runtimes.

**`<id>-WHY.md`** — for the human. Reuses each gap rule's `rationale` verbatim, because those were written to be said out loud, and closes with what becomes queryable once the work lands — derived from the actual roles, e.g. `sum(cart.value)` grouped by `payment.decline_reason` for revenue-at-risk.

## Design constraints carried from the research

- **Short and non-discoverable only.** An ETH Zurich study (Feb 2026) measured agent instruction files *decreasing* task success ~3% while raising cost >20% — agents are too obedient and faithfully execute irrelevant instructions. No "what Sentry is" prose, no repo overviews.
- **Lead with prohibitions.** Stripe's `llms.txt` ships concrete negatives; §6 is the highest-value section because a model trained on 2023 Sentry data will confidently emit `startTransaction` and `span.setData`.
- **spec-kit skeleton.** Stable IDs, MUST, measurable acceptance criteria, explicit `[NEEDS CLARIFICATION]` markers so the agent asks instead of inventing.

`test_pipeline.py` at the kit root asserts the generated spec contains no deprecated API outside §6, and that it doesn't re-request spans that already exist.
