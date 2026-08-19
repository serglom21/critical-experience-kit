# Registry generation

Closes the last gap between plan and code. `BUILD-PLAN.md` said Layer 1 emits *a Weaver registry per customer journey*; until now the checkout registry was hand-written.

```
registry_gen/
├── generate.py           resolved.json → semconv registry
├── validate.py           offline validator
├── test_generate.py      36 tests
└── out/                  generated: manifest.yaml + registry/ + spans/
```

## Run it

```bash
python3 generate.py \
  --resolved ../intake/example-resolved.json \
  --schema-url https://acme.example.com/schemas/critical-experience/0.1.0 \
  --out-dir out/

python3 validate.py --registry out/
python3 -m unittest discover -s . -p 'test_*.py'
```

`--journey <id>` limits scope. `--strict-examples` exits 3 while any attribute still carries a placeholder example.

Where the weaver binary is available:

```bash
weaver registry check --future -r out/
weaver registry generate --future -r out/ markdown ./docs
weaver registry stats --future -r out/ --format json      # attribute coverage
weaver registry diff --future -r out/ --baseline-registry <previous>
```

## The registry is output, not an asset

`GRAMMAR.md` is the specification layer and is hand-maintained. This is the implementation layer: regenerate it when the customer's journey definition or stack changes, never hand-edit it. Every file carries a `GENERATED — do not edit by hand` header.

One source of truth then gives you four artifacts through weaver: markdown docs for the customer, typed constants for their codebase, Rego policy validation, and breaking-change detection via `registry diff`.

## What it will not do

**`requirement_level` on a span group is documentation only.** `weaver registry live-check` is sample-driven — verified in `weaver_live_check/src/live_checker.rs`, there is no span lookup and no `missing_span` finding. It will never report a missing span or a missing span attribute. Journey completeness is enforced by `gap/analyze.py` against Sentry's own data. See `BUILD-PLAN.md §0`.

## Placeholders, deliberately

`weaver registry check` requires `examples` on string attributes. For a correlation key or a segment we have no real value, and inventing plausible-looking customer data is worse than flagging it — a fabricated `chk_01HZY8QK3M` in a customer deliverable reads as fact. Those attributes get `REPLACE_WITH_A_REAL_EXAMPLE` and the run prints exactly which ones need filling.

Enums (step marker, outcome) need no examples. `failure_reason` uses `known_values` from the intake record when present.

## Defaults you should review

Two things are derived from a step's `surface` and are guesses, flagged in a header comment in every generated span file:

- **`span_kind`** — `browser → internal`, `node → server`, `worker → consumer`. Nothing in a journey definition distinguishes an outbound call (`client`) from handling an inbound one (`server`).
- **`sentry_op`** — `ui.action` for a browser root, `ui.action.click` for later browser steps, `function` for server, `queue.task` for workers.

## Validation, and why it's a substitute

`weaver registry check --future` is the real validator, but weaver is a Rust binary and may be uninstallable (no cargo, or a proxy blocking GitHub releases — both true in the sandbox this was built in). `validate.py` covers:

**From the official semconv JSON schema** (fetched and cached; the same file VS Code validates against) — group shapes, required fields, attribute types, enum member structure, `requirement_level` forms.

**From weaver's in-code constraints, which the JSON schema does not express:**

- `prefix:` is rejected outright (`InvalidGroupUsesPrefix`)
- attributes may only be *defined* in `attribute_group`s whose id starts with `registry.`
- a `ref` must not carry `id` / `type` / `stability` / `deprecated`
- every `ref` resolves to a local definition or a known upstream OTel attribute
- `examples` required on string attributes
- manifest: `schema_url` required, deprecated keys flagged, dependencies need `schema_url`, at most one dependency (weaver#604)

If the schema can't be fetched it prints a warning and runs the structural half rather than failing — a locked-down CI runner still gets the checks that catch real authoring bugs.

**It earned its keep immediately.** The first generated output was invalid YAML: the `conditionally_required` condition text contains backticks, and emitting it as a bare multi-line mapping value produced `found character '`' that cannot start any token`. It's now a folded scalar, with a regression test. The hand-written exemplar in `../registry/` passes unchanged.

## Feeds

Generated span names must match the spec's bindings exactly, or the coverage checker measures one thing while the customer implements another. `test_pipeline.py` at the kit root asserts that agreement across the whole chain.
