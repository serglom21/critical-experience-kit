# Eval harness

Phase 5. Turns the spec from a craft artifact into a tunable one.

```
eval/
├── run.py                     orchestrator (agent-agnostic)
├── grade.py                   static grader
├── test_grade.py              37 tests
└── tasks/checkout-js/
    ├── before/                uninstrumented fixture repo
    └── solutions/             golden implementations, one right and five wrong
```

## Run it

```bash
# 1. the spec must emit its rubric
cd ../spec && python3 generate.py --resolved ../intake/example-resolved.json \
  --gap ../gap/example-gap.json --out-dir out --rubric

# 2. the floor: grade the untouched repo
cd ../eval && python3 run.py --dry-run

# 3. prove the grader discriminates
python3 run.py --solution all --out-md eval-report.md

# 4. with a real agent — any agent
python3 run.py --agent 'claude -p "Implement {spec}" --cwd {repo}' --repeat 5

python3 -m unittest test_grade
```

`--agent` is a shell template with `{repo}` and `{spec}` substituted. The harness never assumes a model or tool.

## The number that matters

Not any single score — **the per-check-kind failure rate across runs.** "`attribute_numeric` fails 60% of the time" tells you which paragraph of the spec to rewrite. That is the tuning loop, and it's why `--repeat` exists: agents are stochastic, and one run is noise.

Convex published a ~20% lift in AI success rate writing their code, and it was only credible because they had an open eval harness and tuned guidelines against the categories that failed. Same loop, same reason.

Current numbers on the shipped fixtures:

| Source | Verdict | Score | Regressions |
| --- | --- | --- | --- |
| `before` (floor) | NOT CLEAN | 25.0% | 0 |
| `correct` | **CLEAN** | 100.0% | 0 |
| `attribute-typo` | NOT CLEAN | 100.0% | **1** |
| `deprecated-api` | NOT CLEAN | 89.3% | 0 |
| `indeterminate` | NOT CLEAN | 89.3% | 0 |
| `duplicated-span` | NOT CLEAN | 85.7% | 0 |
| `pii` | NOT CLEAN | 85.7% | 0 |

## Two things the build got wrong first

**The rubric is generated, not hand-written.** `spec/generate.py --rubric` emits `<id>-RUBRIC.json` from the same requirement list the markdown is rendered from. A hand-written grader only tests the cases someone remembered, and drifts from the spec the moment the spec changes. Requirements with no machine-checkable form are carried as `gradeable: false` rather than silently passing.

**Guards, or the eval can't see regressions.** The spec deliberately asks only for what's *missing* — which left everything already correct ungraded. The `attribute-typo` fixture proved it: one file writing `checkoutId` instead of `checkout.id` scored a **clean 100%**, because three other files spelled it correctly and the correlation key was never a requirement (the gap analysis found it present).

So the rubric now also emits `guards` — every span and attribute that already worked before the task. A guard failure is a regression and cannot be offset by requirement passes; `clean` requires both. That fixture now reports 100% on requirements and **NOT CLEAN**, which is the honest verdict.

This is the static counterpart of `gap/diff.py`'s regressions, and the reasoning is the same: an eval that can't detect "you broke the thing that worked" is not a verification.

## What the grader can and cannot see

It's the `avo status` / `ampli status` pattern ported to spans — scan the source for the call sites the contract requires, make the count of missing ones the exit code. Segment shipped Typewriter's codegen with no equivalent verifier and the product went to maintenance mode; that's the cautionary case.

**Can prove:** the span-start call sites exist with the exact literal names; required attribute keys are set; a value is stringified (`toFixed`, `String()`, a string literal) and therefore unaggregatable; an outcome is a boolean; a deprecated API is used; a near-miss attribute key coexists with the correct one; PII-shaped keys or values reach a span.

**Cannot prove:** that a span runs on the right code path; that a value is genuinely numeric when it's a property access (`cart.total` is reported as *not stringified*, with the type unconfirmed — that's the honest limit); anything in a language other than JS/TS.

For the rest, use `gap/analyze.py` against telemetry from the instrumented app. Static and runtime verification are complementary — the same split Avo runs with `avo status` plus Inspector.

## Failure-mode fixtures

`solutions/` deliberately contains one correct implementation and five specific defects, because a grader you can't show discriminating is just a script that prints numbers.

| Directory | Defect |
| --- | --- |
| `correct` | none — the golden implementation |
| `attribute-typo` | `checkoutId` instead of `checkout.id` in one file. Requirements all pass; a guard catches it |
| `duplicated-span` | added the contract-named span but left the drifted one — duplicated rather than renamed |
| `deprecated-api` | uses `span.setData()`, removed in v9 |
| `pii` | puts `user.email` and `card.number` on a span |
| `indeterminate` | `order.value` stringified via `.toFixed(2)` — **misnamed**, kept because the mount this was built on refuses directory deletes. Treat it as `stringified-magnitude` |

## The runtime half — BUILT

`runtime/` boots the instrumented app against a local Sentry envelope collector, drives the journey, and grades the telemetry that actually arrived. No Sentry account needed.

```bash
npm install --prefix runtime/tasks/checkout-js
python3 runtime/run_runtime.py --variant all
```

It grades the two defect classes static analysis cannot reach:

| Variant | Steps | Score | Proves |
| --- | --- | --- | --- |
| `correct` | 7/7 | 100.0 | baseline |
| `stringified` | 7/7 | 95.2 | a value's **real type** — `cart.value` observed as `'129.99'` vs `129.99` |
| `skip-terminal` | 6/7 | 74 | a span **written but never executed** — present in source, never emitted |

Static grading says "not stringified, type unconfirmable" for both of the first two, and passes `skip-terminal` outright. There's a test asserting the two halves genuinely disagree, because that disagreement is the justification for building both. See `runtime/README.md`.
