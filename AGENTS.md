# Working on `ce`

Read `HANDOFF.md` before making architectural changes. This file is the short
version: the invariants that look arbitrary from inside a single file and are
expensive to rediscover.

## What this is

`ce` helps a Sentry Solutions Engineer get a customer's **business journeys**
instrumented — without writing the customer's code. It produces documents
(markdown, JSON, YAML) that the customer's own AI coding agent implements. Nothing
here emits application code, by design.

The SE often has no clone. The customer runs `ce discover` / `ce review` /
`ce report` from their service root; working files land in `ce-work/`. Specs
the agent and Warden need are copied to tracked `.agents/journeys/`. See
`CUSTOMER.md`.

Pipeline: `propose → intake → scan|local|snapshot → gap → profile → spec → diff`,
plus `grade`/`eval`/`runtime` for verifying the spec works and `registry` for
OTel semconv output. Customer wrappers: `discover` (propose+scan+intake),
`review` (keep/drop + business_impact), `report` (gap+profile+spec+publish).

## Invariants — do not break these

1. **Never generate application code.** The constraint is context locality, not
   model quality: even when the customer runs `ce` in their repo, their security
   team approved *their* model to write code. The deliverable is a spec.
2. **Only ask for what is missing.** Every requirement in a generated spec derives
   from a *failed* gap rule. Things that already work become rubric `guards`, not
   repeated asks.
3. **Never rank by volume.** Health checks dominate traffic; refunds are rare and
   expensive. `observed_volume` is inert until a human sets `business_impact`.
   See `intake/resolve.py::rank`.
4. **Never infer `business_impact` or `success_values`.** Nothing in code or
   telemetry decides which flow earns revenue or what counts as success. Leave
   unset and flag in `needs_clarification`.
5. **Never fabricate a statistic.** A static scan has no frequency data, so
   `_synthetic_counts` suppresses extent instead of printing a percentage derived
   from a count of 1.
6. **Regressions lead every report**, above the score. Applies to `gap/diff.py`
   and to rubric guards in `eval/grade.py`.
7. **Declared beats discovered.** A customer-named journey outranks anything a
   scan proposed, and a declared journey a scan cannot corroborate is a *finding*,
   not a validation error.
8. **Write working files only under a designated workdir** (default `ce-work/`),
   gitignored. Never `.ce-observed.json` in cwd. The customer-facing **contract**
   is the exception: `ce report` copies `*-SPEC.md` to tracked `.agents/journeys/`
   and may scaffold `AGENTS.md`, a Cursor rule, and `warden.toml` — Warden no-ops
   without a spec in-repo. Tested.
9. **Provenance on every artifact.** Source-derived vs telemetry-derived vs
   heuristic must be stated in `_provenance`, and heuristics labelled as such
   wherever they surface.

## External facts already verified — do not re-litigate

- **Weaver cannot enforce "this span must exist."** `live-check` is sample-driven;
  there is no `find_span` and no `missing_span` finding. `requirement_level` on a
  span group is documentation only. Journey completeness is checked against
  Sentry. (`weaver_live_check/src/live_checker.rs`)
- **Sentry span query is not publicly documented.** `trace-items/attributes/` is;
  `/events/?dataset=spans` is not. The MCP is the supported path for span names.
- **`attributeSource.source_type`** (`sentry` vs `user`) is the documented
  automatic-vs-custom discriminator. Use it over any naming heuristic.
- **Sentry envelopes arrive `Transfer-Encoding: chunked`** with no Content-Length,
  sometimes gzipped. The root span's name is the payload's top-level
  `transaction` field, *not* anything in `contexts.trace`.
- **Python SDK drops orphan spans silently.** A bare `start_span` with no active
  transaction sends nothing, exit code 0. Journey roots need `start_transaction`.
- **Python floor is 3.9**, not 3.10. Every module has
  `from __future__ import annotations`, so PEP 604 unions are never evaluated.
  Guarded by `test_py39_compat.py`.

## Conventions

- Python 3.9+, stdlib only except PyYAML (hard) and jsonschema (optional,
  degrades). Do not add dependencies without a strong reason.
- Every module: `from __future__ import annotations` at the top.
- Every stage is a standalone script with its own `argparse` and exit codes;
  `cli.py` only dispatches. Do not move logic into `cli.py`.
- Comments explain *why*, especially where a line encodes a bug that was found by
  running the thing. Those comments are load-bearing — keep them.
- Tests assert behaviour that was wrong once. When fixing a bug, add the
  regression test and say in the test docstring what the wrong output looked like.

## Running

```bash
source .venv/bin/activate     # or: python3 -m venv .venv && pip install -e .
ce doctor
for d in intake gap registry_gen eval eval/runtime spec; do
  (cd $d && python3 -m unittest discover -s . -p 'test_*.py'); done
python3 -m unittest test_pipeline test_cli test_py39_compat
```

`eval/runtime` needs `npm install --prefix eval/runtime/tasks/checkout-js`.
