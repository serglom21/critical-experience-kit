#!/usr/bin/env python3
"""
Before/after visibility diff. Takes two gap.json snapshots and reports what
changed.

This is the artifact that closes the loop. Without it, "we recommended
instrumentation" is a claim; with it, it's a measurement — and it gives the next
conversation an agenda instead of a check-in.

Design priorities, in order:

  1. **Regressions first.** A rule that used to pass and now fails is the single
     most important line in the report. Instrumentation rots: a refactor drops a
     span, an SDK upgrade renames an op, someone "cleans up" an attribute. Burying
     that under a celebratory score delta is how a tool loses trust.
  2. **Comparability before comparison.** Two snapshots taken over different
     windows, or at materially different sample rates, are not comparable. Say so
     loudly rather than printing a confident delta that means nothing.
  3. **Coverage transitions over score deltas.** `absent → partial` and
     `partial → complete` are the meaningful state changes. A score moving 53.3 →
     71.2 is a summary; "the payment step now emits" is the finding.

Usage:
    ./diff.py --baseline gap-2026-08.json --current gap-2026-10.json \\
              --out-md visibility-diff.md --out-json diff.json

Exit codes:
    0  compared
    1  input error
    2  no journeys in common
    3  regressions present and --fail-on-regression was passed
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Sample rates differing by more than this factor make score comparison
# unreliable — a "new" span may simply have become visible.
SAMPLE_RATE_TOLERANCE = 1.5

COVERAGE_ORDER = {"absent": 0, "partial": 1, "complete": 2}


@dataclass
class RuleChange:
    rule: str
    entity: str | None
    description: str
    rationale: str
    impact: str
    detail_before: str
    detail_after: str


@dataclass
class JourneyDiff:
    id: str
    name: str
    score_before: float
    score_after: float
    grade_before: str
    grade_after: str
    coverage_before: str
    coverage_after: str
    steps_before: int
    steps_after: int
    steps_total: int
    resolved: list[RuleChange] = field(default_factory=list)
    regressed: list[RuleChange] = field(default_factory=list)
    still_failing: list[RuleChange] = field(default_factory=list)
    newly_measured: list[str] = field(default_factory=list)
    no_longer_measured: list[str] = field(default_factory=list)
    dark_before: list[list[str]] = field(default_factory=list)
    dark_after: list[list[str]] = field(default_factory=list)

    @property
    def score_delta(self) -> float:
        return round(self.score_after - self.score_before, 1)

    @property
    def coverage_direction(self) -> str:
        a, b = COVERAGE_ORDER[self.coverage_before], COVERAGE_ORDER[self.coverage_after]
        return "improved" if b > a else ("regressed" if b < a else "unchanged")

    @property
    def headline(self) -> str:
        if self.regressed:
            return f"{len(self.regressed)} regression(s)"
        if self.coverage_direction == "improved":
            return f"{self.coverage_before} → {self.coverage_after}"
        if self.resolved:
            return f"{len(self.resolved)} finding(s) resolved"
        return "no change"

    @property
    def changed(self) -> bool:
        return bool(self.resolved or self.regressed or self.newly_measured
                    or self.no_longer_measured or self.score_delta
                    or self.coverage_direction != "unchanged")


def _fail_index(journey: dict) -> dict[tuple[str, str | None], dict]:
    """Findings keyed by (rule, entity) so per-step and per-attribute rules that
    share a rule id don't collide."""
    return {(f["rule"], f.get("entity")): f for f in journey["findings"]}


def diff_journey(before: dict, after: dict) -> JourneyDiff:
    bi, ai = _fail_index(before), _fail_index(after)
    d = JourneyDiff(
        id=after["id"], name=after["name"],
        score_before=before["score"], score_after=after["score"],
        grade_before=before["grade"], grade_after=after["grade"],
        coverage_before=before.get("coverage_state", "absent"),
        coverage_after=after.get("coverage_state", "absent"),
        steps_before=before.get("steps_instrumented", 0),
        steps_after=after.get("steps_instrumented", 0),
        steps_total=after.get("steps_total", 0),
        dark_before=before.get("dark_segments") or [],
        dark_after=after.get("dark_segments") or [],
    )

    for key, af in ai.items():
        bf = bi.get(key)
        if bf is None:
            d.newly_measured.append(f"{key[0]}" + (f" ({key[1]})" if key[1] else ""))
            continue
        change = RuleChange(
            rule=af["rule"], entity=af.get("entity"),
            description=af["description"], rationale=af["rationale"],
            impact=af["impact"],
            detail_before=bf.get("detail") or "", detail_after=af.get("detail") or "",
        )
        if bf["passed"] and not af["passed"]:
            d.regressed.append(change)
        elif not bf["passed"] and af["passed"]:
            d.resolved.append(change)
        elif not af["passed"]:
            d.still_failing.append(change)

    for key in bi:
        if key not in ai:
            d.no_longer_measured.append(f"{key[0]}" + (f" ({key[1]})" if key[1] else ""))

    weight = {"critical": 0, "important": 1, "normal": 2, "low": 3}
    for lst in (d.regressed, d.resolved, d.still_failing):
        lst.sort(key=lambda c: (weight[c.impact], c.rule))
    return d


# --------------------------------------------------------------------------


def comparability(base: dict, cur: dict) -> list[str]:
    """Reasons the two snapshots may not be directly comparable."""
    out: list[str] = []
    if base.get("org") != cur.get("org"):
        out.append(f"different orgs (`{base.get('org')}` vs `{cur.get('org')}`) — "
                   "these are not two views of the same system")
    if base.get("stats_period") != cur.get("stats_period"):
        out.append(f"different windows ({base.get('stats_period')} vs "
                   f"{cur.get('stats_period')}) — volume-derived extent and any "
                   "count comparison are not meaningful")
    rb, rc = base.get("traces_sample_rate"), cur.get("traces_sample_rate")
    if rb and rc:
        ratio = max(rb, rc) / min(rb, rc)
        if ratio > SAMPLE_RATE_TOLERANCE:
            out.append(f"sample rate changed {rb} → {rc} ({ratio:.1f}×) — a span that "
                       "appears 'new' may simply have become visible")
    elif (rb is None) != (rc is None):
        out.append("sample rate known on only one side — treat score movement as "
                   "directional, not quantitative")
    if base.get("low_confidence") or cur.get("low_confidence"):
        out.append("at least one snapshot was taken below a 5% sample rate and is "
                   "flagged low-confidence")
    return out


def render_markdown(diffs: list[JourneyDiff], base: dict, cur: dict,
                    added: list[dict], dropped: list[dict]) -> str:
    L: list[str] = []
    A = L.append
    warn = comparability(base, cur)

    total_resolved = sum(len(d.resolved) for d in diffs)
    total_regressed = sum(len(d.regressed) for d in diffs)
    improved = [d for d in diffs if d.coverage_direction == "improved"]

    A(f"# Visibility diff — `{cur.get('org')}`\n")
    A(f"Baseline: {base.get('stats_period')} window · "
      f"Current: {cur.get('stats_period')} window\n")

    if total_regressed:
        A(f"> ## {total_regressed} regression(s)\n")
        A("> A rule that used to pass and now fails. Instrumentation rots — a "
          "refactor drops a span, an SDK upgrade renames an op, an attribute gets "
          "'cleaned up'. Read this section first.\n")

    A(f"**{total_resolved} finding(s) resolved** · **{total_regressed} regression(s)** · "
      f"**{len(improved)} journey(s) improved coverage**\n")

    if warn:
        A("## Comparability caveats\n")
        for w in warn:
            A(f"- {w}")
        A("")

    # ---- regressions, unconditionally first ----
    if total_regressed:
        A("## Regressions\n")
        A("| Journey | Rule | Impact | Was | Now |")
        A("| --- | --- | --- | --- | --- |")
        for d in diffs:
            for c in d.regressed:
                A(f"| {d.name} | {c.rule}"
                  + (f" `{c.entity}`" if c.entity else "")
                  + f" | {c.impact} | {c.detail_before or 'passing'} | "
                    f"**{c.detail_after}** |")
        A("")
        for d in diffs:
            for c in d.regressed:
                A(f"**{d.name} · {c.rule} {c.description}** — {c.rationale}\n")

    # ---- scoreboard ----
    A("## Journeys\n")
    A("| Journey | Coverage | Score | Grade | Resolved | Regressed | Headline |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for d in sorted(diffs, key=lambda x: (-len(x.regressed), -x.score_delta)):
        cov = (f"{d.steps_before}/{d.steps_total} → {d.steps_after}/{d.steps_total}"
               if d.steps_before != d.steps_after else f"{d.steps_after}/{d.steps_total}")
        delta = f"{d.score_before} → {d.score_after} ({d.score_delta:+})" \
            if d.score_delta else f"{d.score_after} (—)"
        grade = f"{d.grade_before} → {d.grade_after}" \
            if d.grade_before != d.grade_after else d.grade_after
        A(f"| {d.name} | {cov} | {delta} | {grade} | {len(d.resolved)} | "
          f"{len(d.regressed)} | {d.headline} |")
    A("")

    if added:
        A("### Newly measured journeys\n")
        for j in added:
            A(f"- **{j['name']}** — {j.get('coverage_state')}, score {j['score']}")
        A("")
    if dropped:
        A("### No longer measured\n")
        A("Present in the baseline and absent now. Either descoped, or the journey "
          "definition changed — confirm which.\n")
        for j in dropped:
            A(f"- **{j['name']}** (was {j.get('coverage_state')}, score {j['score']})")
        A("")

    # ---- per journey ----
    for d in sorted(diffs, key=lambda x: (-len(x.regressed), -x.score_delta)):
        if not d.changed:
            continue
        A(f"## {d.name}\n")
        if d.coverage_direction != "unchanged":
            A(f"Coverage **{d.coverage_before} → {d.coverage_after}** "
              f"({d.steps_before} → {d.steps_after} of {d.steps_total} steps).\n")

        fixed_dark = [s for s in d.dark_before if s not in d.dark_after]
        new_dark = [s for s in d.dark_after if s not in d.dark_before]
        for seg in fixed_dark:
            A(f"- Dark segment **closed**: {' → '.join(seg)} now emits.")
        for seg in new_dark:
            A(f"- **New dark segment**: {' → '.join(seg)}.")
        if fixed_dark or new_dark:
            A("")

        if d.resolved:
            A("**Resolved**\n")
            for c in d.resolved:
                A(f"- {c.rule}"
                  + (f" `{c.entity}`" if c.entity else "")
                  + f" — {c.description}. Now: {c.detail_after}")
            A("")
        if d.still_failing:
            A("**Still open**\n")
            for c in d.still_failing:
                A(f"- {c.rule}"
                  + (f" `{c.entity}`" if c.entity else "")
                  + f" ({c.impact}) — {c.detail_after}")
            A("")
        if d.newly_measured:
            A("**Newly measured** (rule evaluated now, absent from the baseline — "
              "usually a journey definition change, not a code change)\n")
            for r in d.newly_measured:
                A(f"- {r}")
            A("")
        if d.no_longer_measured:
            A("**No longer measured**\n")
            for r in d.no_longer_measured:
                A(f"- {r}")
            A("")

    A("---\n")
    A("Scores are per journey and never ANDed. A score delta is a summary; the "
      "coverage transition and the resolved-rule list are the findings. Rule "
      "definitions in `rules.md`.")
    return "\n".join(L) + "\n"


def to_json(diffs: list[JourneyDiff], base: dict, cur: dict,
            added: list[dict], dropped: list[dict]) -> dict:
    return {
        "version": 1,
        "org": cur.get("org"),
        "baseline": {"stats_period": base.get("stats_period"),
                     "traces_sample_rate": base.get("traces_sample_rate")},
        "current": {"stats_period": cur.get("stats_period"),
                    "traces_sample_rate": cur.get("traces_sample_rate")},
        "comparability_warnings": comparability(base, cur),
        "summary": {
            "resolved": sum(len(d.resolved) for d in diffs),
            "regressed": sum(len(d.regressed) for d in diffs),
            "coverage_improved": sum(1 for d in diffs if d.coverage_direction == "improved"),
            "coverage_regressed": sum(1 for d in diffs if d.coverage_direction == "regressed"),
            "journeys_added": len(added),
            "journeys_dropped": len(dropped),
        },
        "journeys": [
            {
                "id": d.id, "name": d.name,
                "score_before": d.score_before, "score_after": d.score_after,
                "score_delta": d.score_delta,
                "grade_before": d.grade_before, "grade_after": d.grade_after,
                "coverage_before": d.coverage_before, "coverage_after": d.coverage_after,
                "coverage_direction": d.coverage_direction,
                "steps_before": d.steps_before, "steps_after": d.steps_after,
                "steps_total": d.steps_total,
                "headline": d.headline,
                "resolved": [c.__dict__ for c in d.resolved],
                "regressed": [c.__dict__ for c in d.regressed],
                "still_failing": [c.__dict__ for c in d.still_failing],
                "newly_measured": d.newly_measured,
                "no_longer_measured": d.no_longer_measured,
                "dark_segments_before": d.dark_before,
                "dark_segments_after": d.dark_after,
            }
            for d in sorted(diffs, key=lambda x: (-len(x.regressed), -x.score_delta))
        ],
        "journeys_added": [{"id": j["id"], "name": j["name"]} for j in added],
        "journeys_dropped": [{"id": j["id"], "name": j["name"]} for j in dropped],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diff two gap.json snapshots.")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--current", required=True)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--fail-on-regression", action="store_true",
                    help="Exit 3 if any rule regressed. For scheduled monitoring.")
    args = ap.parse_args(argv)

    try:
        base = json.loads(Path(args.baseline).read_text())
        cur = json.loads(Path(args.current).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    bj = {j["id"]: j for j in base.get("journeys", [])}
    cj = {j["id"]: j for j in cur.get("journeys", [])}
    common = sorted(set(bj) & set(cj))
    if not common:
        print("error: no journeys in common between the two snapshots.", file=sys.stderr)
        return 2

    diffs = [diff_journey(bj[i], cj[i]) for i in common]
    added = [cj[i] for i in sorted(set(cj) - set(bj))]
    dropped = [bj[i] for i in sorted(set(bj) - set(cj))]

    report = render_markdown(diffs, base, cur, added, dropped)
    payload = to_json(diffs, base, cur, added, dropped)

    if args.out_md:
        Path(args.out_md).write_text(report)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(report)

    s = payload["summary"]
    print(f"{s['resolved']} resolved · {s['regressed']} regressed · "
          f"{s['coverage_improved']} improved coverage · "
          f"{s['coverage_regressed']} lost coverage", file=sys.stderr)
    for w in payload["comparability_warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    if s["regressed"] and args.fail_on_regression:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
