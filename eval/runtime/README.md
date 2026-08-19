# Runtime eval — grading from real telemetry

The other half of the eval. `eval/grade.py` reads source; this reads the bytes the SDK put on the wire.

```
eval/runtime/
├── collector.py                  local Sentry envelope capture server
├── run_runtime.py                boot → drive → capture → gap/analyze.py
├── test_collector.py             24 tests
├── tasks/checkout-js/            real @sentry/node app, 3 variants
│   ├── journey.mjs               the instrumented checkout journey
│   ├── drive.mjs                 exercises it, DSN from the environment
│   └── package.json
└── observed/                     generated observed-<variant>.json
```

## Run it

```bash
npm install --prefix tasks/checkout-js          # once
python3 run_runtime.py --variant all --out-md runtime-report.md
python3 -m unittest test_collector
```

**No Sentry account needed.** The runner starts a collector on an ephemeral port and hands the app a DSN pointing at it:

```
http://publickey@127.0.0.1:<port>/1   →   POST /api/1/envelope/
```

## Why static analysis wasn't enough

Two defect classes are invisible to source scanning *by construction*. Both are graded here, and the shipped variants demonstrate each:

| Variant | Steps | Score | What it proves |
| --- | --- | --- | --- |
| `correct` | 7/7 | **100.0** excellent | baseline |
| `stringified` | 7/7 | 95.2 excellent | **a value's real type** |
| `skip-terminal` | 6/7 | 74 needs improvement | **a span written but never executed** |

**Type.** `correct` and `stringified` both write `cart.total` in source — one wrapped in `.toFixed(2)` deeper in the call. `eval/grade.py` can only report *"not stringified, type unconfirmable from source"*. The wire is unambiguous:

```
correct      cart.value → number   sample=[129.99, 130.99]
stringified  cart.value → string   sample=['129.99', '130.99']
```

That's `CE-010` failing on evidence rather than inference. A stringified magnitude means `sum()` and `p50()` silently return nothing useful while the code looks correct.

**Execution.** `skip-terminal` contains `name: "checkout.confirmation_viewed"` in the source — a test asserts it does — and simply never reaches it on that path. Static analysis passes it. Runtime shows the span never arrived, which trips `CE-002` and caps the grade at 74.

## What the collector gets right that the API cannot

`attributeType` here comes from **the real JSON value on the wire**, per observed value. The attributes API reports one type per key; the collector can see a key emitted as a number in one place and a string in another, and surfaces `type_conflict` rather than picking a winner — half the rows being unaggregatable is exactly the kind of defect that hides behind an averaged answer.

The trade: `attributeSource.source_type` is a **namespace heuristic** here, because the documented field only exists on `GET /trace-items/attributes/` against a live org. Both facts are recorded in `_provenance` in every generated `observed.json`.

## Wire-format facts, verified against a live SDK

Established by capturing real traffic from `@sentry/node` 10.70.0, not by reading docs — and the first two cost a debugging round each:

- **`Transfer-Encoding: chunked`, no `Content-Length`.** Reading Content-Length alone captured *zero bytes*. gzip is also possible on larger payloads; both are handled.
- **The root span's name is the payload's top-level `transaction` field**, not anything inside `contexts.trace`.
- Root attributes live in `contexts.trace.data`; child spans are in `spans[]` with the name in `description`.
- SDK-internal attributes are prefixed `sentry.` — `sentry.op`, `sentry.origin`, `sentry.source`, `sentry.sample_rate`.
- Attribute values keep their real JSON types.

The parsing tests use a fixture copied verbatim from that capture, so they encode the format rather than an interpretation of it.

## Fitting into the loop

`run_runtime.py` emits an `observed.json` in exactly the shape `gap/analyze.py` already consumes, so runtime results flow through the same rules, the same scoring, the same capping — and can be diffed:

```bash
python3 run_runtime.py --variant correct --baseline ../../gap/example-gap.json
```

That answers "did shipping this actually move the customer's coverage" using the identical rule catalog the recommendation came from.

## Cross-SDK gotchas, found by running it

The collector is SDK-agnostic — anything speaking the Sentry envelope protocol works. Two differences bit during a real Python run and are worth knowing before pointing this at a service:

**Attribute setter.** JS uses `span.setAttribute(...)`; the Python SDK uses `span.set_data(...)`. Both land in the payload's `data` field, so the collector reads them identically — but a spec written for JS will be wrong for Python. `spec/generate.py` §5 is JS-only today.

**Orphan spans are dropped silently.** In `sentry-sdk` 2.x a bare `sentry_sdk.start_span()` with no active transaction produces **nothing at all** — no envelope, no error, exit code 0. The journey root must be `sentry_sdk.start_transaction(...)`, with `start_span` for children. Verified side by side:

```
start_span only          envelopes=0  spans=[]
start_transaction root   envelopes=1  spans=['signup', 'signup.submitted']
```

`ce local` calls this out by name when zero envelopes arrive, because "my app ran fine and your collector got nothing" is otherwise a long debugging session.

## Honest limits

- **The driver is the fixture's job.** A real customer app needs its own way to exercise the journey — a Playwright script, an integration test, a seeded request. The runner only supplies the DSN and runs a command.
- **Sampling is asserted, not observed.** The runner sets `tracesSampleRate: 1.0` in the fixture and records that claim; it doesn't verify it.
- **A journey only appears if the driver exercises it.** A path never driven reads identically to a path never instrumented. `CE_DECLINE_EVERY` exists so the fixture actually walks the failure branch — otherwise `payment.decline_reason` would never appear and would look missing. A test asserts that branch runs.
- Node/JS fixture only, though the collector itself is SDK-agnostic: anything speaking the Sentry envelope protocol works.
