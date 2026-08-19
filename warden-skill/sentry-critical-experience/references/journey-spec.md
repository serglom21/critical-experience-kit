# Locating and reading the journey spec

The journey spec is the contract this skill reviews against. It lives in the repo being reviewed, not in this skill. Find it before reporting anything.

## Where to look, in order

1. `.agents/journeys/*.md` — the contract `ce report` writes
2. A section in root `AGENTS.md` or `CLAUDE.md` headed with a journey name and "Instrumentation Spec"
3. `docs/SENTRY_*_INSTRUMENTATION.md`
4. A nested `AGENTS.md` in the directory the diff touches — **instruction files resolve by proximity, so a nested spec overrides the root one** for code in that subtree

If none exists, return no findings. Do not infer a contract from the code itself.

## What to extract

| From the spec | Use it to |
| --- | --- |
| Span name table (§2 in the standard template) | Know which span names are exact strings, and each span's `Impact` rating — this drives severity |
| Attribute contract (§3) | Know which keys are `required` vs `recommended`, their types, and the allowed enum values |
| Correlation key | Check propagation across runtime and service boundaries |
| Numbered requirements (`FR-001`…) | Quote the requirement ID in findings |
| Acceptance criteria (`SC-001`…) | Understand which query goes blind when something is missing |
| §8 Out of scope | Suppress findings the spec explicitly excludes |
| §9 `[NEEDS CLARIFICATION]` markers | Treat these as unresolved. Do not report a finding that depends on an open question |

## Severity mapping

Map the spec's per-span `Impact` rating to your finding severity:

| Spec impact | Missing span | Missing required attribute |
| --- | --- | --- |
| Critical | high | medium |
| Important | medium | medium |
| Normal | low | low |

Override to **high** regardless of impact rating when the finding is PII on a span, or when a required outcome attribute is unset on the error path of a payment or order-commit span — in that case a failure is stored as indistinguishable from a success, which corrupts every downstream query.

## Version drift

Specs carry a version and an owner. If the spec version predates the SDK version in `package.json` by a major version, note it once as a `low` finding and do not enforce API details that may have changed. Do not enforce a spec you have reason to believe is stale.
