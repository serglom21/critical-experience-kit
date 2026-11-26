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
