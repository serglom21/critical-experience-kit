# Discovery benchmark

`eval/` grades whether an agent *implemented* a spec. Nothing graded whether discovery
found the **right journeys**, so changes to it were guesses. This is that measurement.

```
bench/
├── ground_truth/<repo>.yaml   hand-labeled journeys, labeled BEFORE the new code existed
├── score.py                   recall@k, precision@k, per-tier recall, evidence-exists check
├── test_score.py
└── results/                   JSON per run + the explorer's artifacts for each repo
```

## Method

- **Truth** comes from the repo's own artifacts: `tier: e2e` = its Playwright/e2e suites
  exercise it; `tier: code` = a revenue/identity flow with a dedicated interface directory
  (billing, sign-in, project creation) but no e2e coverage. Each truth journey has `anchors`
  (path prefixes) and `aliases` (name tokens).
- **Match**: a proposed journey matches when a file it cites is under an anchor (primary), or
  its id contains an alias as a whole token (weak, and flatters the baseline).
- **Metrics**: recall@k, precision@k, recall by tier, and the share of cited paths that exist.
  recall@k is capped at k / truth-count, so read recall@5 against precision@5.

```bash
python3 bench/score.py --truth bench/ground_truth/supabase.yaml \
  --proposed ce-work/journeys.yaml --repo /path/to/supabase --k 5 10
```

## Results (n = 2 repos; read the caveats)

| Repo | Method | Proposed | Recall | Precision | Paths that exist |
| --- | --- | --- | --- | --- | --- |
| Supabase (14 truth) | old `propose`, top 5 | 5 | 0.00 | 0.00 | 1.00 |
| | old `propose`, cap 30 | 28 | 0.36 | 0.25 | 0.99 |
| | **`explore` (Claude Sonnet 5.5)** | 14 | **0.86** | **0.93** | 1.00 |
| MetaMask (13 truth) | old `propose`, top 5 | 5 | 0.15 | 0.60 | 1.00 |
| | old `propose`, cap 30 | 30 | 0.69 | 0.43 | 1.00 |
| | **`explore` (Claude Sonnet 5.5)** | 12 | **0.92** | **1.00** | 1.00 |

Missed: Supabase `logs`, `realtime` (the explorer stopped before tracing them);
MetaMask `ramps-buy`. The validator dropped one citation the model got wrong (a mistyped
directory), so the kept evidence is 100% real by construction.

## Caveats — do not quote these numbers without them

1. **Not blind.** The same session wrote the labels and ran the explorer. Labels were written
   first and the explorer worked only from the emitted task, but the contamination is real.
2. **n = 2**, both TypeScript, both large product apps. Add at least four more repos across
   languages (Go, Python, Ruby/Rails, Java) before claiming "any codebase".
3. **The OpenRouter path was never run live** (no network in the build sandbox). It is
   covered by tests with a fake model. The model id is unverified: run once with a real key.
4. Matching is by cited files and name tokens. It does not grade **step quality or order**;
   steps were proposed by reading code and are flagged "confirm" in every candidate.
5. The baseline is scored generously (alias matching) and was given a 30-journey cap.

## Live OpenRouter run (25 Nov 2026, `anthropic/claude-sonnet-5.5`, before the seed gate)

| Repo | Recall | Precision | Proposals | Turns / calls | Cost |
|---|---|---|---|---|---|
| supabase | 0.286 | 0.700 | 10 | 11 / 41 | $0.835 |
| metamask | 0.462 | 0.875 | 8 | 18 / 57 | $0.716 |

Far below the in-session run (0.857 / 0.923). Neither run neared the turn cap: the model
submitted early and ignored all 27 Supabase e2e groups. Fix: numbered e2e "seeds" in the
brief, a required `seed_coverage` in the output, and a mechanical gate in `run_agent` that
bounces a submission (max 2 times) until every seed is mapped to a journey or rejected with
a reason. Re-run both repos to see whether the gate closes the gap; record results here.

## Live run 2 (seed gate v1, directory-level) and the change that followed

| Repo | Recall | Precision | Proposals | Cost |
|---|---|---|---|---|
| supabase | 0.357 | 0.714 | 14 | $1.309 |
| metamask | 0.692 | 0.917 | 12 | $1.475 |

The gate never fired (0 bounces). The model mapped all 32 `e2e/studio/features` tests to one
journey and rejected 25 of 27 seeds, mostly support code, and the check accepted it. Gate v2:
support/example dirs and setup stems are excluded from seeds mechanically; every test name
(stem) must be mapped to a *validated* journey that shares vocabulary with it, or rejected
with a fixed category (`not_consequence_bearing` needs a verifiable citation); the brief shows
all stems (it capped at 14 before), and the gate runs on validator output, so a mapping to a
journey the validator drops no longer counts. Not yet measured live.

## Live run 3 (gate v2) and the follow-up

| Repo | Recall | Precision | Proposals | Wall | Tokens in | Cost |
|---|---|---|---|---|---|---|
| supabase | 0.857 | 0.833 | 18 | 222 s | 0.70 M | $1.715 |
| metamask | 0.923 | 0.909 | 22 | 638 s | 2.68 M | $6.393 |

Gate v2 matched the in-session recall on both repos (tuned on these two repos, so this shows the
mechanism works, not that it generalises). Weak spots seen: rejections cited unrelated files
and one said "I did not trace it"; `send_funds` held 41 test names; input tokens grew
quadratically with turns. Follow-up (gate v3): a rejection's citation must share vocabulary
with the rejected stems, reasons that admit the code was not read do not count, journeys over
15 stems are bounced as overloaded, old tool results are elided from the conversation (keep 10,
batch of 6), and the static prompt prefix is marked cacheable for `anthropic/*` models.
Not yet measured live; caching in particular is untested against OpenRouter.

## Live run 4 (gate v3) and the follow-up

| Repo | Recall | Precision | Proposals | Wall | Tokens in (cached) | Cost |
|---|---|---|---|---|---|---|
| supabase | 0.929 | 0.867 | 15 | 211 s | 0.56 M (0.17 M) | $0.720 |
| metamask | 0.923 | 0.900 | 30 | 904 s | 1.52 M (0.56 M) | $3.668 |

Elision and prefix caching cut cost 58% and 43% with recall held. MetaMask hit the 45-turn cap
after its first bounce; that last submit skipped the gate, leaving 53 of 392 test names
unaccounted for (including the buy-ramps deep link, whose rejection the gate had already
flagged). Follow-up: turn budget scales with the number of test names (45 + stems/8, max 120),
each bounce adds 10 turns, and a cap submit is marked in the report. Not yet measured live.
Remaining misses are judgment calls the gate cannot settle (Supabase realtime inspector was
rejected as read-only with a related citation).

## Live run 5 (auto turn budget) and the follow-up

| Repo | Recall | Precision | Proposals | Turns (budget) | Wall | Tokens in (cached) | Cost |
|---|---|---|---|---|---|---|---|
| supabase | 0.929 | 0.800 | 20 | 31 (48) | 301 s | 0.95 M (0.24 M) | $1.456 |
| metamask | 1.000 | 0.968 | 31 | 103 (94+10) | 1,752 s | 5.61 M (1.31 M) | $11.622 |

Best recall so far (MetaMask found ramps-buy), but the larger budget was spent in full: 303
greps, the cap hit again, cost tripled. A first MetaMask attempt died on an unretried
ConnectionResetError after $4.87 with nothing saved. Follow-up: connection errors are
retried; every submission is checkpointed to `discovery.partial.json`; an input-token ceiling
(`--max-input-tokens`, default 3M) asks for a submit at 80%; the model is told when 12 turns
remain so a gated submit still fits; superseded submissions are elided from history.
Not yet measured live.

## Live run 6 (token ceiling) — stopping point for tuning on these two repos

| Repo | Recall | Precision | Proposals | Wall | Tokens in (cached) | Cost |
|---|---|---|---|---|---|---|
| supabase | 0.929 | 0.867 | 15 | 215 s | 0.68 M (0.20 M) | $0.955 |
| metamask | 1.000 | 0.912 | 34 | 1,061 s | 2.91 M (0.88 M) | $6.144 |

The 3M-token ceiling halved MetaMask's cost with recall held at 1.0. Last fix from this run: a
`not_consequence_bearing` citation must relate to each rejected stem, not just one of them
(Supabase's realtime miss came from an 8-stem bundle rejected on a feature-flag citation).
Further tuning against supabase/metamask would overfit them; the next measurement should be
a blind benchmark on repos and labels that never informed the gate.
