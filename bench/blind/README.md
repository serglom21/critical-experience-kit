# Blind benchmark

Supabase and MetaMask were used to tune the explorer and its gate, so their scores show the
mechanism works, not that it generalises. This benchmark measures generalisation.

## Mode: post-run review (chosen 26 Nov 2026)

Labels were not written in advance. Instead each repo is run once, then a human judges every
proposed journey with `bench/review.py` (critical / minor / wrong, and whether its steps are
right) and lists any critical journey they notice is missing. This gives precision and step
quality honestly. It does not give true recall: the reviewer sees the output first, so
`found_of_known_critical` is an upper bound. The pre-labelled protocol below is kept for a
later, stricter round; the ground-truth stubs stay empty.

## Protocol (pre-labelled; not used in this round)
1. **Freeze the kit.** All blind runs use kit commit `5510825` (discovery-v2). No changes to
   `gap/`, `bench/score.py` or the prompts until every blind repo is scored.
2. **Pin the repos.** Commits are listed in each `bench/ground_truth/blind/<repo>.yaml`.
3. **Label first.** Fill each ground-truth file before running the explorer on that repo.
   The labeller must not have seen explorer output for it. Commit the labels (with
   `labeled_at`) before the run so the order is auditable.
4. **Run once, defaults only.** `ce explore --mode openrouter` with default turns and token
   ceiling. One run per repo; a crash may be re-run, a bad score may not.
5. **Score and record.** `bench/score.py --truth bench/ground_truth/blind/<repo>.yaml ...`.
   Record results here even if they are bad. Fixes come after, and are re-measured on a
   fresh repo, not on these.

## Repos
| Repo | Lang | Sentry account | Notes |
|---|---|---|---|
| supabase/auth | Go | Supabase, $25k+ | Same company as a tuning repo, different codebase and language |
| mozilla/fx-private-relay | Python | Mozilla, $25k+ | Playwright e2e, subscriptions |
| indico/indico | Python | CERN, $25k+ | CERN also self-hosts Sentry; paid org may not cover Indico |
| chatwoot/chatwoot | Ruby | not $25k | Ruby coverage |
| killbill/killbill | Java | Prospect | No UI: tests discovery with almost no e2e seeds |
| cli/cli | Go | Microsoft - GitHub, $25k+ | Optional, CLI-shaped |

## Known issues, recorded before any run (not fixed, per the freeze)
- `gap/signals.py` E2E_DIR does not match `e2e-tests/` or a bare `integration/` segment, so
  fx-private-relay (`e2e-tests/`) and killbill (`beatrix/.../integration/`) get 0 e2e seeds
  and the coverage gate never fires on them. Found from directory listings during setup, not
  from any score. Those two runs measure discovery when a repo's test naming is not recognised.
  Fix after scoring; verify on a fresh repo.

## Results
(fill in after runs)
