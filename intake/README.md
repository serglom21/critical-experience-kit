# Intake — journey candidate resolver

Layer 1 of the kit. Turns provenance-tagged journey candidates into a ranked, spec-ready worklist.

```
intake/
├── resolve.py                              the resolver
├── test_resolve.py                         40 tests
├── schema/journey-candidate.schema.json    the 7-role record
├── candidates/
│   ├── declared.example.yaml               customer-declared (the only required input)
│   ├── discovered-code.example.json        simulated code scan
│   └── discovered-telemetry.example.json   simulated trace-variant pass
├── example-report.md                       generated
└── example-resolved.json                   generated
```

## Run it

```bash
pip install pyyaml --break-system-packages     # only needed for YAML inputs

# The normal case: customer already knows what matters. No discovery.
python3 resolve.py --declared candidates/declared.example.yaml

# With optional enrichment passes
python3 resolve.py \
  --declared   candidates/declared.example.yaml \
  --discovered candidates/discovered-code.example.json \
  --discovered candidates/discovered-telemetry.example.json \
  --out-md report.md --out-json resolved.json

python3 -m unittest discover -s . -p 'test_*.py'
```

Both flags are repeatable and both are optional — but at least one file must yield candidates (exit 2 otherwise). **A declared-only run is a first-class path**, not a degraded one.

## What it does

**Merges across sources.** Candidates match on id, normalized name, explicit `aliases`, or a shared `correlation_key.attribute` — and merging is transitive, so a third source can bridge two previously separate groups. Matching is deliberately conservative: no fuzzy step-overlap heuristics, because a false merge silently swallows a declared journey, which is worse than a missed one.

In the examples, `Subscription Upgrade` (declared) and `Upgrade Path` (telemetry) merge on `upgrade.id` alone despite unrelated names. Declared names and ids always win, so the business name survives contact with an inferred one.

**Computes the 2×2.**

| Status | Meaning | What to do |
| --- | --- | --- |
| `corroborated` | Declared *and* discovered | Highest confidence — proceed |
| `declared_unconfirmed` | Declared, nothing corroborates it | **Raise it.** Lives in a service you can't see, is aspirational, or is entirely dark |
| `proposed` | Discovered, nobody declared it | Propose; do not assume it matters |

There is no fourth cell — not-declared-and-not-discovered can't arrive.

**Reports role completion, never fills it in.** Roles 1–3 (journey, correlation key, step marker) are structural and often inferrable. Roles 4–7 (outcome, failure reason, magnitude, actor segment) are semantic and human-owned; missing ones become `[NEEDS CLARIFICATION]` markers carried into the spec. Guessing them is how you end up with instrumentation nobody asked for.

**Blocks spec generation on four things only:** journey, correlation key, ≥2 steps, and an outcome with ≥2 values and declared success values. Plus one conditional — an outcome admitting non-success values requires a `failure_reason`, or every failure collapses into one undiagnosable bucket. `Subscription Upgrade` in the examples trips exactly this.

Magnitude and actor segment are *not* blockers. A journey is specifiable without them, just less useful.

## Two rules the code enforces

**Volume is inert until a human assigns impact.** The first version used `observed_volume` as a general tiebreaker. A test caught the consequence: a 9.2M-instance `/healthz` probe sorted above a refund flow running a few hundred times a month — the exact failure the ranking rule exists to prevent, displaced one level down into the proposed set. Volume now only breaks ties among candidates that already have human-assigned `business_impact`. See `test_volume_does_not_lift_unassigned`.

**Exclusion is a human act, not a heuristic.** Discovery surfaces health probes, CDN asset spans, and polling loops. Nothing in telemetry distinguishes a probe from a purchase, so `excluded: true` plus a reason is the answer. Excluded candidates drop from the worklist and stay in the JSON for auditability. This mirrors the one thing every service-scorecard vendor converged on independently: worklist items a team can never action destroy the artifact's credibility.

Note what the resolver deliberately does *not* claim: it cannot rank `Health Probe` last. That needs semantics no telemetry contains.

## Authoring a declared set

Fill what you know, omit what you don't. A journey with nothing but `id`, `name`, and `source` is valid input — it'll come back with six missing roles and a clear list of what to ask.

```yaml
version: 1
journeys:
  - id: refund_request
    name: Refund Request          # business name, never "POST /api/refunds"
    source: declared
    business_impact: critical     # human-assigned; never derived from volume
    correlation_key:
      attribute: refund.id        # NOT the trace ID — navigation starts a new trace
      persists_across: [service, async_job]
    steps:
      - { id: refund_requested, surface: browser }
      - { id: refund_settled,   surface: worker }
```

Validate against the schema:

```bash
pip install --upgrade jsonschema pyyaml --break-system-packages
python3 - <<'PY'
import json, yaml
from jsonschema import Draft202012Validator
s = json.load(open('schema/journey-candidate.schema.json'))
d = yaml.safe_load(open('candidates/declared.example.yaml'))
errs = list(Draft202012Validator(s).iter_errors(d))
print('OK' if not errs else [(list(e.path), e.message) for e in errs])
PY
```

## Feeds

`example-resolved.json` is the input to Layer 2 (gap analysis + spec generation). Downstream consumers filter on `excluded == false` and `spec_ready == true`, and carry `needs_clarification` straight into the spec's §9.
