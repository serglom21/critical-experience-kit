# Runtime eval — telemetry-graded

Each variant boots the instrumented app against a local Sentry envelope collector, drives the journey, and scores the spans that actually arrived. No Sentry account involved.

| Variant | Envelopes | Spans seen | Coverage | Score | Grade |
| --- | --- | --- | --- | --- | --- |
| `correct` | 6 | 7 | 7/7 | 100.0 | excellent |
| `stringified` | 6 | 7 | 7/7 | 95.2 | excellent |
| `skip-terminal` | 6 | 6 | 6/7 | 74 | needs improvement |

## What only the wire could tell us

### `correct` — 100.0 (excellent)

- `cart.value` observed as **number** · e.g. 129.99
- `order.value` observed as **number** · e.g. 134.48000000000002
- `checkout.outcome` observed as **string** · e.g. 'completed'

### `stringified` — 95.2 (excellent)

- `cart.value` observed as **string** · e.g. '129.99'
- `order.value` observed as **number** · e.g. 134.48000000000002
- `checkout.outcome` observed as **string** · e.g. 'completed'

| Rule | Impact | Detail |
| --- | --- | --- |
| CE-010 | important | `cart.value` is type `string`, expected `number` |

### `skip-terminal` — 74 (needs improvement)

- Never emitted: `checkout.confirmation_viewed`
- `cart.value` observed as **number** · e.g. 129.99
- `order.value` observed as **number** · e.g. 134.48000000000002
- `checkout.outcome` observed as **string** · e.g. 'completed'

| Rule | Impact | Detail |
| --- | --- | --- |
| CE-002 | critical | expected span `checkout.confirmation_viewed` — not found |
| CE-003 | important | expected span `checkout.confirmation_viewed` — not found |

---

Static grading (`eval/grade.py`) and this are complementary, the same split Avo runs with `avo status` plus Inspector: static proves the call sites exist, runtime proves they executed and what types they carried. `attributeType` here comes from the real JSON value on the wire; `attributeSource` is a namespace heuristic, since the documented `source_type` field needs a live org.
