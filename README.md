# Critical Experience Instrumentation Kit

Phase 1 scaffolding for automating custom-instrumentation engagements. One journey (Checkout), built end to end, so the loop can be proven before the library is generalized.

Every file format here was verified against source — Weaver's Rust crates, the `@sentry/warden` v0.43.0 npm tarball, and Sentry SDK v10 docs — not against blog posts. Things that could not be verified are flagged in `BUILD-PLAN.md §5`.

```
critical-experience-kit/
├── BUILD-PLAN.md                      ← start here
├── GRAMMAR.md                         ← the maintained asset
├── PROVENANCE.md                      ← what this is built on, and what it isn't
├── SPEC-TEMPLATE.md                   ← the customer deliverable
├── warden.toml                        ← customer install config
├── intake/                            ← Layer 1 resolver (BUILT)
│   ├── resolve.py
│   ├── test_resolve.py                40 tests, passing
│   ├── schema/journey-candidate.schema.json
│   └── candidates/                    worked examples
├── gap/                               ← Layer 2 gap analysis (BUILT)
│   ├── rules.md                       13-rule catalog
│   ├── analyze.py                     journey diff + score
│   ├── diff.py                        before/after visibility diff
│   ├── instrumentation_profile.py     automatic vs custom + recommendations
│   ├── propose.py                     source → journey CANDIDATES (discovered:code)
│   ├── code_scan.py                   source → observed.json (no telemetry needed)
│   ├── sentry_source.py               live org → observed.json (org/project scoped)
│   ├── test_*.py                      155 tests, passing
│   └── fixtures/                      synthetic + real `demo` org data
├── registry_gen/                      ← Layer 1 registry output (BUILT)
│   ├── generate.py                    resolved.json → semconv registry
│   ├── validate.py                    offline semconv validator
│   ├── test_generate.py               36 tests, passing
│   └── out/                           manifest.yaml + registry/ + spans/
├── spec/                              ← Layer 2b spec generation (BUILT)
│   ├── generate.py                    failed rules → customer deliverable
│   └── out/                           <id>-SPEC.md + -WHY.md + -RUBRIC.json
├── eval/                              ← Phase 5 eval harness (BUILT)
│   ├── run.py                         agent-agnostic orchestrator
│   ├── grade.py                       static grader
│   ├── test_grade.py                  37 tests, passing
│   ├── tasks/checkout-js/             1 correct + 5 broken golden solutions
│   └── runtime/                       telemetry-graded half (BUILT)
│       ├── collector.py               local Sentry envelope capture
│       ├── run_runtime.py             boot → drive → capture → analyze
│       ├── test_collector.py          24 tests, passing
│       └── tasks/checkout-js/         real @sentry/node app, 3 variants
├── test_pipeline.py                   ← seam tests across all layers (15)
├── registry/                          ← generated exemplar (checkout)
│   ├── manifest.yaml
│   ├── registry/checkout.yaml         ← attribute definitions
│   └── spans/checkout.yaml            ← span tree
└── warden-skill/
    └── sentry-critical-experience/
        ├── SKILL.md                   ← the PR check
        └── references/
            ├── journey-spec.md
            └── sentry-api.md
```

## Layout: the kit, the service, and the workdir

Install the kit once. Point it at a service. Artifacts go in `ce-work/` inside
that service (gitignored) when the **customer** runs it, or in a separate work
directory when the **SE** has a clone.

```
~/tools/ce-kit                          the kit (pip install the wheel)
~/code/their-service                    their repo
~/code/their-service/ce-work/           gitignored working files
~/code/their-service/.agents/journeys/  tracked specs (after `ce report`)
```

Customer path (no clone needed on the SE side):

```bash
cd ~/code/their-service
ce discover
ce review            # local page: keep 2–3, set business_impact
ce report            # specs → .agents/journeys/ (tracked) + ce-work/ (gitignored)
# see CUSTOMER.md §4 for the agent prompt
```

See `CUSTOMER.md` for what is read, what is written, and what leaves the machine
(nothing, on the default path).

SE path with a clone still works: `--repo` / `--out` can point anywhere.

```bash
# once
python3 -m venv ~/.venvs/ce && source ~/.venvs/ce/bin/activate
pip install -e ~/tools/ce-kit
ce doctor

# per engagement — run these from your work dir
mkdir -p ~/work/acme-checkout && cd ~/work/acme-checkout
ce init --out . --journey-id checkout --journey-name Checkout
# edit journeys.yaml
ce intake --declared journeys.yaml --out-json resolved.json

# read their source — repo can be anywhere
ce scan --repo ~/code/their-service --out observed.json
ce gap --resolved resolved.json --observed observed.json --include-unready \
  --out-md gap.md --out-json gap.json

# or drive the service for real telemetry; --cwd runs the command in their repo
ce local --resolved resolved.json --journey checkout \
  --cwd ~/code/their-service --drive 'npm run e2e' \
  --out-md gap-live.md --out-json gap-live.json

ce profile --observed observed.json --gap gap.json --out-md profile.md
ce spec --resolved resolved.json --gap gap.json --out-dir specs --rubric
```

`ce local` from inside the service repo still leaves `git status` clean except for
`ce-work/` (gitignored) when you omit `--out-*`. Explicit `--out-json` still wins.

## Install and run against a service

```bash
pip install .               # or: pip install -e .  (editable, for kit development)
ce doctor                   # what's installed, what's missing, and what each gap blocks
ce discover                 # from the service root — writes ce-work/
```

Python 3.9+. The only hard dependency is PyYAML — `jsonschema` is optional and degrades to structural checks, everything else is stdlib. Runs on a locked-down machine with no egress.

**`ce local` is the command for a locally running service.** It starts a Sentry envelope collector, exports a DSN into your command's environment, runs it, and grades the telemetry that arrives:

```bash
ce local --resolved resolved.json --journey checkout \
  --drive 'npm run e2e' --out-md gap.md --out-json gap.json
```

**No Sentry account needed, and language-agnostic** — the DSN is the only contract. Verified end to end against a Python service using `sentry-sdk` and a Node service using `@sentry/node`. Use `--dsn-env` if your stack reads a different variable.

### Three starting states, all supported

Which command you start with depends on what the service already has. All three produce a usable report — none of them is an error case.

| State | Service has | Start with | Then |
| --- | --- | --- | --- |
| **A** | Sentry installed, no journey coverage | `ce local --drive '<cmd>'` | `ce profile` → tier reads *automatic only*, the strongest pitch line |
| **B** | **No Sentry at all** | `ce propose --repo .` then `ce scan --repo .` | `ce spec ... --include-absent` — the spec must include SDK install |
| **C** | Some custom instrumentation, improvable | either | `ce spec` asks only for the **delta** and guards what exists |

```bash
# Don't hand-author journeys — derive candidates from the code first
ce propose --repo . --out journeys.yaml --report proposal.md
# review journeys.yaml: set business_impact, confirm step order and outcomes
ce intake --discovered journeys.yaml --out-json resolved.json

# State B or C — read the source, no telemetry required
ce scan --repo . --out observed.json
ce gap --resolved resolved.json --observed observed.json --include-unready --out-md gap.md
```

`ce scan` handles JS/TS and Python, detects each SDK family separately, and stamps `_provenance` on its output: it shows instrumentation that was **written**, not that it *runs*. Counts are synthetic, so the gap analyzer suppresses extent rather than printing a fabricated percentage, and attribute types it can't prove from a literal are listed in `unprovable_types` instead of guessed. Run `ce local` later to confirm execution and resolve types.

For **State C** the spec generator does the right thing by construction — every requirement derives from a *failed* rule, so existing spans and attributes land in the rubric's `guards` (protected against regression) rather than being re-requested.

If a service genuinely emits nothing, `ce local --allow-empty` records a zero-coverage baseline instead of erroring.

Two things to know before pointing it at your own service:

- **`--drive` must actually exercise the journey.** A path never driven looks identical to a path never instrumented.
- **Python SDK:** the journey root must be `sentry_sdk.start_transaction(...)`. A bare `start_span` with no active transaction is an orphan and the SDK **drops it silently** — the app exits 0 and sends nothing. `ce local` names this case explicitly when zero envelopes arrive, because it looks like a collector bug and isn't.

## What each piece does

**`BUILD-PLAN.md`** — the sequenced plan, the architecture, and the verification findings that changed the design. Read this first. The most important: Weaver cannot enforce "this span must exist," which is why journey completeness is checked against Sentry instead.

**`GRAMMAR.md`** — the seven industry-agnostic roles every journey instantiates, the provenance/intake model, and the conformance vocabulary. **This is the only hand-maintained content in the system.** Everything else is generated. It exists because a curated per-industry journey catalog turned out to be not just expensive but usually wrong — Heap measured 84% of funnel analyses delivering misleading data and 63% missing an alternative path to conversion.

**`intake/`** — the resolver. Merges provenance-tagged candidates from any number of sources, computes the declared/discovered 2×2, reports which of the seven roles are filled, and emits a ranked worklist plus JSON for Layer 2. Discovery never gates: a declared-only run is a first-class path. See `intake/README.md`.

```bash
cd intake && python3 resolve.py --declared candidates/declared.example.yaml
```

**`gap/`** — the gap analysis. Diffs resolved journeys against what the org actually contains and produces the report that makes the second call click: dark segments, span name drift, boolean outcomes, stringified magnitudes. Thirteen weighted rules with mandatory rationales, grade capping, and a sampling guard. Runs offline against a fixture. See `gap/README.md`.

```bash
cd gap && python3 analyze.py --resolved ../intake/example-resolved.json \
  --observed fixtures/observed-customer.example.json --include-unready

# what is this service actually sending — automatic, custom, or both?
python3 instrumentation_profile.py --observed fixtures/observed-demo-org.live.json

# did the work land? regressions lead the report, above the score
python3 diff.py --baseline example-gap.json --current example-gap-after.json
```

**`spec/`** — the customer deliverable. Every requirement derives from a *failed* gap rule, so the spec asks for the 9 things that are missing rather than restating the 7 spans and 12 attributes that already work. Emits `<id>-SPEC.md` for their coding agent and `<id>-WHY.md` for the human. See `spec/README.md`.

```bash
cd spec && python3 generate.py --resolved ../intake/example-resolved.json \
  --gap ../gap/example-gap.json --out-dir out
```

**`registry_gen/`** — turns a resolved journey into a Weaver semconv registry. **Generated, never hand-edited** — it's the implementation layer, while `GRAMMAR.md` is the specification. Ships an offline validator, since the weaver binary isn't always installable. See `registry_gen/README.md`.

```bash
cd registry_gen && python3 generate.py --resolved ../intake/example-resolved.json --out-dir out
python3 validate.py --registry out/
```

**`eval/`** — the harness that makes the spec tunable. Runs an agent (any agent) against an uninstrumented fixture repo and grades the result against the spec's own generated rubric. The reportable number is the per-check-kind failure rate across runs, not any single score. See `eval/README.md`.

```bash
cd spec && python3 generate.py --resolved ../intake/example-resolved.json \
  --gap ../gap/example-gap.json --out-dir out --rubric
cd ../eval && python3 run.py --solution all      # prove the grader discriminates
python3 run.py --agent 'claude -p "Implement {spec}" --cwd {repo}' --repeat 5

# and the runtime half — grades real telemetry, no Sentry account needed
npm install --prefix runtime/tasks/checkout-js
python3 runtime/run_runtime.py --variant all
```

**Run everything:**

```bash
for d in intake gap registry_gen eval eval/runtime; do \
  (cd $d && python3 -m unittest discover -s . -p 'test_*.py'); done
python3 -m unittest test_pipeline
python3 -m unittest test_cli
# 40 + 127 + 36 + 37 + 24 + 15 + 21 = 300 tests
```

**`registry/`** — *superseded by `registry_gen/out/`, kept as the reference exemplar.* The checkout journey as machine-readable OTel semantic conventions, generating customer docs, typed constants, agent prompts, and breaking-change diffs. Treat it as a **worked exemplar that primes the agent, not a catalog entry to select from**. Cap the exemplar set at five. Validate with:

```bash
weaver registry check --future -r ./registry
weaver registry stats --future -r ./registry --format json
```

**`SPEC-TEMPLATE.md`** — what you actually hand the customer. Their AI coding agent reads it and implements against it. Filled in with Checkout as a worked example; the SE instructions are in an HTML comment at the top that you delete before sending.

**`warden-skill/`** + **`warden.toml`** — the PR check that keeps instrumentation from rotting. Customer-installed, advisory by default.

## Customer install (Warden)

`ce report` vendors the skill under `.agents/skills/sentry-critical-experience/`
and writes `warden.toml` with `failOn = "off"` (comments on the implementation
PR, never blocks merge). `ce` does not open a GitHub PR.

Once, in the service repo:

```bash
npm install -g @sentry/warden
```

CI needs a **model provider** key (`WARDEN_ANTHROPIC_API_KEY` or
`WARDEN_OPENAI_API_KEY`). No Sentry token. Kit-local iteration (this repo):

```bash
warden --skill ./warden-skill/sentry-critical-experience    # bypasses trigger matching
warden --staged
```

## Order of operations on an engagement

1. **Intake.** Capture the journeys the customer already knows matter. Tag them `declared`. If they can name them, skip straight to step 3 — no scan required.
2. **Discovery (optional).** Only when the list is empty or you want to find what they forgot: seed from their code, rank by telemetry, human names each one. Its best output is the refund/admin/retry flow nobody mentioned on the call.
3. **Instantiate + gap-analyse.** Fill the grammar's seven roles, generate the registry, diff against their live spans → gap matrix. *(Layer 2, `gap/`)*
4. **Profile + generate the spec** — classify what they're sending (automatic vs custom), resolve `[NEEDS CLARIFICATION]` markers, hand over `SPEC.md` + `WHY.md`. *(Layer 2b, `spec/`)*
5. **They implement** with their own approved model.
6. **Coverage check** → re-run the gap analysis and `gap/diff.py` the two snapshots. This is the artifact that proves it worked, and it surfaces regressions before the customer finds them.
7. **Install the Warden skill** so it stays instrumented.

Discovery never gates the pipeline. A declared journey the scan missed is a finding worth raising — *you told me checkout matters and I can find no evidence of it in your code or telemetry* — not a validation error.

## Two things not to skip

**Build the coverage checker before generalizing the library.** Segment shipped Typewriter's codegen without a verifier and the product went maintenance-mode. Verification is what makes the loop close.

**Build an eval harness before tuning the spec.** Convex published a ~20% lift in AI success rate on their code, credible only because it was measured. A small "instrument this repo per the spec" eval set turns the spec from a craft artifact into a tunable one — and into a number you can say on a call.

## Caveats worth repeating

- Sentry surfaces **boolean** span attributes as the strings `'true'`/`'false'`. Every outcome in this kit is a string enum for that reason.
- A browser navigation starts a **new trace**. Multi-page journeys correlate on `checkout.id`, not trace ID.
- Sampling causes false negatives in verification. Read `tracesSampleRate` first; below ~5% treat results as low-confidence.
- Only `security-review` and `code-review` actually ship as Warden built-ins. The other skills on the landing page are illustrative — don't offer them to customers as installable.
