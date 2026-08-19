#!/usr/bin/env python3
"""
Gap analyzer. Diffs resolved journeys against what a customer's Sentry org
actually contains, and produces the report that makes the second call click.

Inputs:
    resolved.json   from intake/resolve.py
    observed.json   from gap/sentry_source.py (live) or a fixture (offline)

Rule catalog, weights, capping, and the reporting requirements are documented
in rules.md. Read that first — this file implements it, it doesn't define it.

Usage:
    ./analyze.py --resolved ../intake/example-resolved.json \
                 --observed fixtures/observed-customer.example.json \
                 --out-md gap-report.md --out-json gap.json

Exit codes:
    0  analyzed
    1  input error
    2  no analyzable journeys (all excluded or not spec-ready)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Impact weights, from the Instrumentation Score spec.
WEIGHTS = {"critical": 40, "important": 30, "normal": 20, "low": 10}

# Score bands, same source.
BANDS = [(90, "excellent"), (75, "good"), (50, "needs improvement"), (0, "poor")]

# Grade caps. Weighted averages alone let attribute hygiene mask total journey
# blindness, so a failed capping rule ceilings the score regardless. SSL Labs.
CAP_POOR = 49
CAP_NEEDS_IMPROVEMENT = 74

# Below this sample rate, Trace Explorer aggregates are unreliable enough that a
# "missing" span may simply never have been recorded.
LOW_CONFIDENCE_SAMPLE_RATE = 0.05


def band(score: float) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return "poor"


def normalize_span_name(name: str) -> str:
    """Fold separators and case so `checkout.payment_authorized` and
    `checkout.payment.authorize` are comparable for drift detection."""
    return re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")


def expected_span_name(journey_id: str, step: dict, is_first: bool) -> str:
    """Explicit binding wins; otherwise the convention.

    Convention: the first step is the journey root and carries the bare journey
    id; every other step is `<journey_id>.<step_id>`. See the `span_name` field
    in intake/schema/journey-candidate.schema.json.
    """
    if step.get("span_name"):
        return step["span_name"]
    return journey_id if is_first else f"{journey_id}.{step['id']}"


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    rule: str
    description: str
    rationale: str
    target: str  # "span" | "attribute"
    impact: str
    passed: bool
    detail: str = ""
    extent: str | None = None
    example: str | None = None
    entity: str | None = None  # which step or attribute

    @property
    def weight(self) -> int:
        return WEIGHTS[self.impact]


@dataclass
class JourneyGap:
    id: str
    name: str
    findings: list[Finding] = field(default_factory=list)
    dark_segments: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    low_confidence: bool = False

    # ---- scoring --------------------------------------------------------

    @property
    def raw_score(self) -> float:
        total = sum(f.weight for f in self.findings)
        if not total:
            return 0.0
        got = sum(f.weight for f in self.findings if f.passed)
        return round(got / total * 100, 1)

    @property
    def caps(self) -> list[str]:
        out = []
        for f in self.findings:
            if f.passed:
                continue
            if f.rule == "CE-001":
                out.append("CE-001 (no root span) caps the score at 49")
            elif f.rule == "CE-002":
                out.append("CE-002 (no terminal outcome span) caps the score at 74")
        return out

    @property
    def score(self) -> float:
        s = self.raw_score
        failed = {f.rule for f in self.findings if not f.passed}
        if "CE-001" in failed:
            s = min(s, CAP_POOR)
        if "CE-002" in failed:
            s = min(s, CAP_NEEDS_IMPROVEMENT)
        return s

    @property
    def grade(self) -> str:
        return band(self.score)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.passed]

    @property
    def missing_steps(self) -> list[str]:
        return [f.entity for f in self.failures if f.rule == "CE-003" and f.entity]

    # ---- coverage state --------------------------------------------------
    # A journey with zero instrumented steps is not a low score, it is a job
    # that hasn't started. Scoring it produces a meaningless number (every such
    # journey lands on the same ~15, since only the "not declared" rules vary)
    # and, worse, sorting worst-first then buries the PARTIALLY instrumented
    # journeys — which are the only actionable ones and the whole point of the
    # exercise. Classify first, score second.

    @property
    def step_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.rule == "CE-003"]

    @property
    def steps_total(self) -> int:
        return len(self.step_findings)

    @property
    def steps_instrumented(self) -> int:
        return sum(1 for f in self.step_findings if f.passed)

    @property
    def coverage_state(self) -> str:
        if not self.steps_total:
            return "absent"
        if self.steps_instrumented == 0:
            return "absent"
        if self.steps_instrumented == self.steps_total:
            return "complete"
        return "partial"

    @property
    def coverage_label(self) -> str:
        return f"{self.steps_instrumented}/{self.steps_total} steps"


# --------------------------------------------------------------------------
# Observation lookup
# --------------------------------------------------------------------------


class Observed:
    """What the org actually contains, indexed for lookup."""

    def __init__(self, doc: dict):
        self.org = doc.get("org", "unknown")
        self.stats_period = doc.get("stats_period", "unknown")
        self.traces_sample_rate = doc.get("traces_sample_rate")
        self.span_counts: dict[str, int] = {
            s["name"]: s.get("count", 0) for s in doc.get("span_names", [])
        }
        self._span_norm: dict[str, list[str]] = {}
        for name in self.span_counts:
            self._span_norm.setdefault(normalize_span_name(name), []).append(name)
        self.attributes: dict[str, dict] = {a["key"]: a for a in doc.get("attributes", [])}
        self.example_traces: dict[str, str] = doc.get("example_traces", {})
        # A static code scan has no frequency information, so it fabricates a count
        # of 1 per span. Reporting "~0% of instances lack this span" off that would
        # be inventing a statistic. Extent is suppressed instead.
        self.synthetic_counts: bool = bool(doc.get("_synthetic_counts"))
        self.sdk: dict = doc.get("sdk") or {}

    def has_span(self, name: str) -> bool:
        return name in self.span_counts

    def span_count(self, name: str) -> int:
        return self.span_counts.get(name, 0)

    def drifted_names(self, expected: str) -> list[str]:
        """Observed names that normalize to `expected` but differ literally."""
        return [n for n in self._span_norm.get(normalize_span_name(expected), []) if n != expected]

    def attribute(self, key: str) -> dict | None:
        return self.attributes.get(key)

    @property
    def low_confidence(self) -> bool:
        r = self.traces_sample_rate
        return r is not None and r < LOW_CONFIDENCE_SAMPLE_RATE


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def _attr_finding(
    rule: str,
    description: str,
    rationale: str,
    impact: str,
    key: str | None,
    obs: Observed,
    *,
    require_type: str | None = None,
    forbid_type: str | None = None,
) -> Finding:
    """Shared shape for the attribute-presence rules."""
    if not key:
        return Finding(rule, description, rationale, "attribute", impact, False,
                       detail="not declared in the journey definition")
    a = obs.attribute(key)
    if a is None:
        return Finding(rule, description, rationale, "attribute", impact, False,
                       detail=f"`{key}` not found in the org's span attributes", entity=key)
    t = a.get("attributeType")
    if forbid_type and t == forbid_type:
        return Finding(rule, description, rationale, "attribute", impact, False,
                       detail=f"`{key}` is type `{t}`", entity=key)
    if require_type and t != require_type:
        return Finding(rule, description, rationale, "attribute", impact, False,
                       detail=f"`{key}` is type `{t}`, expected `{require_type}`", entity=key)
    src = (a.get("attributeSource") or {}).get("source_type", "unknown")
    return Finding(rule, description, rationale, "attribute", impact, True,
                   detail=f"`{key}` present (type `{t}`, source `{src}`)", entity=key)


def analyze_journey(journey: dict, obs: Observed) -> JourneyGap:
    g = JourneyGap(id=journey["id"], name=journey["name"])
    g.low_confidence = obs.low_confidence
    roles = journey.get("roles") or {}
    steps = roles.get("steps") or []

    # ---- span-level rules ------------------------------------------------
    expected = [
        (s["id"], expected_span_name(journey["id"], s, i == 0), s.get("impact", "normal"))
        for i, s in enumerate(steps)
    ]
    present = {sid: obs.has_span(name) for sid, name, _ in expected}
    root_count = obs.span_count(expected[0][1]) if expected else 0

    if expected:
        sid, name, _ = expected[0]
        g.findings.append(Finding(
            "CE-001", "Journey root span present",
            "Without a root span there is no journey to attach outcomes to and no "
            "funnel is queryable. Every other finding is moot until this passes.",
            "span", "critical", present[sid],
            detail=f"expected span `{name}`"
            + (f" — {root_count:,} observed" if present[sid] else " — not found"),
            entity=name,
            example=obs.example_traces.get(name),
        ))

        sid, name, _ = expected[-1]
        g.findings.append(Finding(
            "CE-002", "Terminal outcome-bearing span present",
            "A journey that commits its business effect but never records reaching a "
            "terminal state cannot distinguish success from failure — the most "
            "expensive possible blind spot in a funnel.",
            "span", "critical", present[sid],
            detail=f"expected span `{name}`"
            + (f" — {obs.span_count(name):,} observed" if present[sid] else " — not found"),
            entity=name,
            example=obs.example_traces.get(name),
        ))

    # CE-003, one finding per step, weighted by the step's own impact.
    for i, (sid, name, impact) in enumerate(expected):
        ok = present[sid]
        count = obs.span_count(name)
        extent = None
        if obs.synthetic_counts:
            extent = None            # source scan: no frequency exists to report
        elif not ok and root_count:
            extent = f"100% of the {root_count:,} observed journey instances (approx.)"
        elif ok and root_count and count < root_count:
            share = 1 - (count / root_count)
            if share >= 0.02:
                extent = f"~{share:.0%} of journey instances lack this span (approx.)"
        g.findings.append(Finding(
            "CE-003", f"Step {i + 1} `{sid}` instrumented",
            "A missing step is a dark segment. Drop-off gets attributed to the step "
            "before it, so the owning team never sees the problem.",
            "span", impact, ok,
            detail=f"expected span `{name}`"
            + (f" — {count:,} observed" if ok else " — not found"),
            extent=extent, entity=name, example=obs.example_traces.get(name),
        ))

    # CE-012 dark segments: runs of missing steps BETWEEN instrumented ones.
    flags = [present[sid] for sid, _, _ in expected]
    run: list[str] = []
    for i, (sid, name, _) in enumerate(expected):
        if not flags[i]:
            run.append(sid)
            continue
        if run and any(flags[:i]):  # a gap bounded on both sides
            g.dark_segments.append(run)
        run = []
    g.findings.append(Finding(
        "CE-012", "No dark segment between instrumented steps",
        "A gap in the middle is worse than a gap at the end: latency and drop-off "
        "from the dark steps are silently attributed to the last instrumented step. "
        "This is the finding that produces the 'visible to payment, then goes dark' "
        "sentence.",
        "span", "important", not g.dark_segments,
        detail="; ".join(" → ".join(seg) for seg in g.dark_segments) or "none detected",
    ))

    # CE-013 name drift.
    drift = [(name, alt) for _, name, _ in expected for alt in obs.drifted_names(name)]
    for _, alt in drift:
        # The drifted name is the actionable artifact: it proves the step IS
        # instrumented and the fix is a rename, not new code.
        g.notes.append(
            f"`{alt}` is being sent {obs.span_count(alt):,} times — this is the "
            "drifted step, one rename from working"
        )
    g.findings.append(Finding(
        "CE-013", "No span name drift",
        "A near-miss name produces no error, no failing test, and no data — the team "
        "believes the step is instrumented while every query returns empty. Reported "
        "apart from 'missing' because the fix is a rename, not new instrumentation.",
        "span", "normal", not drift,
        detail="; ".join(f"expected `{e}`, found `{a}`" for e, a in drift) or "none detected",
        example=drift[0][1] if drift else None,
    ))

    # ---- attribute-level rules -------------------------------------------
    corr = (roles.get("correlation_key") or {}).get("attribute")
    g.findings.append(_attr_finding(
        "CE-004", "Correlation key attribute present",
        "The correlation key stitches steps into one journey instance. A browser "
        "navigation starts a new Sentry trace, so the trace ID cannot do this job. "
        "Without the key you have disconnected spans, not a journey.",
        "critical", corr, obs))

    corr_attr = obs.attribute(corr) if corr else None
    if corr_attr is not None:
        src = (corr_attr.get("attributeSource") or {}).get("source_type", "unknown")
        g.findings.append(Finding(
            "CE-005", "Correlation key is customer-defined",
            "A `sentry` source type means the product already owns that key with its "
            "own semantics — using it for a business correlation key risks a silent "
            "collision when Sentry's meaning diverges from yours.",
            "attribute", "low", src == "user",
            detail=f"`{corr}` source is `{src}`", entity=corr))

    outcome = roles.get("outcome") or {}
    g.findings.append(_attr_finding(
        "CE-006", "Outcome attribute present",
        "The outcome is the numerator of every funnel question. Without it you can "
        "measure latency and errors but not whether the business process succeeded.",
        "critical", outcome.get("attribute"), obs))
    if outcome.get("attribute") and obs.attribute(outcome["attribute"]):
        g.findings.append(_attr_finding(
            "CE-007", "Outcome is a string enum, not a boolean",
            "Sentry renders boolean attributes as the strings 'true'/'false', and a "
            "binary collapses failed, abandoned, and rejected into one bucket. Those "
            "three route to three different teams.",
            "important", outcome["attribute"], obs, forbid_type="boolean"))

    successes = set(outcome.get("success_values") or [])
    non_success = [v for v in (outcome.get("values") or []) if v not in successes]
    if non_success:
        g.findings.append(_attr_finding(
            "CE-008", "Failure reason present (outcome admits failure)",
            "Without a coded reason every failure collapses into one undiagnosable "
            "bucket. Usually the highest-value attribute for the customer, because it "
            "is the one that routes a problem to an owning team.",
            "important", (roles.get("failure_reason") or {}).get("attribute"), obs))
        g.notes.append(
            f"outcome admits non-success values: {', '.join(sorted(non_success))}"
        )

    # One finding PER declared magnitude, not one aggregate. An aggregate that
    # passes as soon as any magnitude exists hides the absent ones entirely —
    # `order.value` reported "present" in the generated spec while missing from
    # the org. Caught by test_ce009_reports_each_magnitude_separately.
    mags = roles.get("magnitude") or []
    mag_present = [m for m in mags if obs.attribute(m["attribute"])]
    if not mags:
        g.findings.append(Finding(
            "CE-009", "Magnitude present",
            "The magnitude is what turns a latency chart into a revenue chart. Numeric "
            "span attributes are chartable in Trace Explorer with no setup.",
            "attribute", "important", False, detail="none declared"))
    for m in mags:
        g.findings.append(_attr_finding(
            "CE-009", f"Magnitude `{m['attribute']}` present",
            "The magnitude is what turns a latency chart into a revenue chart. Numeric "
            "span attributes are chartable in Trace Explorer with no setup.",
            "important", m["attribute"], obs))
    for m in mag_present:
        g.findings.append(_attr_finding(
            "CE-010", f"Magnitude `{m['attribute']}` is numeric",
            "A value stringified as \"129.99\" cannot be aggregated — sum() and p50() "
            "silently return nothing useful. The instrumentation looks correct and "
            "produces no answer.",
            "important", m["attribute"], obs, require_type="number"))

    segs = roles.get("actor_segment") or []
    seg_present = [s for s in segs if obs.attribute(s["attribute"])]
    g.findings.append(Finding(
        "CE-011", "Actor segment present",
        "Segments turn 'checkout is slow' into 'checkout is slow for enterprise "
        "customers in the EU on mobile'. Usually the cheapest role to fill, since it "
        "often already exists in the auth or tenancy layer.",
        "attribute", "normal", bool(seg_present),
        detail=", ".join(f"`{s['attribute']}`" for s in seg_present)
        or (f"declared but absent: {', '.join('`' + s['attribute'] + '`' for s in segs)}"
            if segs else "none declared"),
    ))

    return g


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def order_for_report(gaps: list[JourneyGap]) -> list[JourneyGap]:
    """Partial coverage first — those are the only actionable journeys, and the
    ones that produce the 'visible to payment, then goes dark' finding. Then
    complete, then absent. Within each tier, worst score first.

    Sorting purely by score ascending (the first implementation) put four
    entirely-uninstrumented journeys at the top, all on the same meaningless
    ~15, and buried the one journey with a real diagnosable gap at the bottom.
    """
    tier = {"partial": 0, "complete": 1, "absent": 2}
    return sorted(gaps, key=lambda g: (tier[g.coverage_state], g.score, g.name))


def render_markdown(gaps: list[JourneyGap], obs: Observed, skipped: list[dict]) -> str:
    L: list[str] = []
    A = L.append
    ordered = order_for_report(gaps)
    partial = [g for g in ordered if g.coverage_state == "partial"]
    complete = [g for g in ordered if g.coverage_state == "complete"]
    absent = [g for g in ordered if g.coverage_state == "absent"]
    detailed = partial + complete

    A(f"# Instrumentation gap analysis — `{obs.org}`\n")
    A(f"Window: {obs.stats_period} · "
      f"sample rate: {obs.traces_sample_rate if obs.traces_sample_rate is not None else 'unknown'} · "
      f"{len(obs.span_counts)} distinct span names · {len(obs.attributes)} span attributes\n")

    if obs.synthetic_counts:
        A("> **Source-derived, not observed.** This reflects instrumentation that was "
          "*written*, from a static code scan. It cannot show whether the code runs, "
          "how often, or what a value's runtime type is — so extent is suppressed "
          "rather than fabricated. Run `ce local` once the service emits to confirm "
          "these spans execute.\n")
        if obs.sdk and not obs.sdk.get("any_sdk_present"):
            A("> **No Sentry SDK found in the scanned source.** Every journey below is "
              "necessarily absent. The spec needs to include SDK install and "
              "initialisation — generate it with `ce spec --include-absent`.\n")
        elif obs.sdk and not obs.sdk.get("any_sdk_initialised"):
            A("> **SDK imported but no `init(...)` found.** Initialisation may live in "
              "config or a framework hook — confirm before treating this as a gap.\n")

    if obs.low_confidence:
        A(f"> **Low confidence.** Sample rate is below {LOW_CONFIDENCE_SAMPLE_RATE:.0%}, so a "
          "span missing from this data may simply never have been recorded. Verify "
          "against an unsampled environment before telling the customer instrumentation "
          "is absent.\n")

    A(f"**{len(partial)} partially instrumented** (where visibility breaks) · "
      f"**{len(complete)} complete** · **{len(absent)} not instrumented**\n")

    if partial:
        A("## Where visibility breaks\n")
        A("The actionable tier. These journeys are instrumented enough to be trusted "
          "and incomplete enough to mislead — the worst combination, because a partial "
          "funnel looks like a working one.\n")
        A("| Journey | Coverage | Score | Grade | Goes dark at |")
        A("| --- | --- | --- | --- | --- |")
        for g in partial:
            dark = "; ".join(" → ".join(s) for s in g.dark_segments) or "—"
            raw = f" (raw {g.raw_score})" if g.caps and g.raw_score != g.score else ""
            A(f"| {g.name} | {g.coverage_label} | {g.score}{raw} | {g.grade} | {dark} |")
        A("")

    if complete:
        A("## Fully instrumented\n")
        A("| Journey | Coverage | Score | Grade | Failed rules |")
        A("| --- | --- | --- | --- | --- |")
        for g in complete:
            A(f"| {g.name} | {g.coverage_label} | {g.score} | {g.grade} | {len(g.failures)} |")
        A("")

    if absent:
        A("## Not instrumented\n")
        A("No spans found for any declared step. These are not low scores — they are "
          "work that hasn't started, so they carry no grade. Scoring them produces the "
          "same meaningless number for every one and drowns out the tier above.\n")
        A("| Journey | Steps | Correlation key | Notes |")
        A("| --- | --- | --- | --- |")
        for g in absent:
            corr = next((f for f in g.findings if f.rule == "CE-004"), None)
            corr_txt = ("present" if corr and corr.passed else "absent") if corr else "—"
            other = [f.rule for f in g.failures
                     if f.rule not in ("CE-001", "CE-002", "CE-003", "CE-012", "CE-013")]
            A(f"| {g.name} | {g.steps_total} declared, 0 found | {corr_txt} | "
              f"{', '.join(sorted(set(other))) or '—'} |")
        A("")
        A("> An absent journey whose **correlation key is already present** is the "
          "cheapest possible win: the plumbing exists, only the spans are missing.\n")

    A("Scored per journey and reported as a distribution — never ANDed across journeys. "
      "That aggregation bug is why the OTel Demo scores 35 overall while every "
      "individual service scores higher.\n")

    if skipped:
        A("## Not analyzed\n")
        for s in skipped:
            A(f"- **{s['name']}** — {s['reason']}")
        A("")

    if not detailed:
        A("_No partially or fully instrumented journey to detail._\n")

    for g in detailed:
        if g is detailed[0]:
            A("## Detail\n")
        A(f"### {g.name} — {g.score} ({g.grade}) · {g.coverage_label}\n")
        for c in g.caps:
            A(f"> **Capped.** {c}\n")

        if g.dark_segments:
            for seg in g.dark_segments:
                A(f"**Goes dark at:** {' → '.join(seg)}. Latency and drop-off from these "
                  "steps are being attributed to the last instrumented step.\n")

        if g.missing_steps:
            A(f"**Missing spans:** {', '.join('`' + s + '`' for s in g.missing_steps)}\n")

        for n in g.notes:
            A(f"- {n}")
        if g.notes:
            A("")

        # rules.md requires extent + entity + example on every finding. The
        # example column is what turns a score into something an SE can open.
        A("| Rule | Impact | Result | Detail | Extent | Example |")
        A("| --- | --- | --- | --- | --- | --- |")
        for f in g.findings:
            ex = f"`{f.example}`" if f.example else "—"
            A(f"| {f.rule} | {f.impact} | {'pass' if f.passed else '**FAIL**'} | "
              f"{f.detail or '—'} | {f.extent or '—'} | {ex} |")
        A("")

        traceable = [f for f in g.findings if f.example and len(f.example) == 32]
        if traceable:
            A("**Open one of these traces to see it:**\n")
            for f in traceable[:3]:
                A(f"- `{f.entity}` → trace `{f.example}`")
            A("")

        if g.failures:
            A("**Why these matter** — use this language with the customer:\n")
            seen: set[str] = set()
            for f in g.failures:
                if f.rule in seen:
                    continue
                seen.add(f.rule)
                A(f"- **{f.rule} {f.description}.** {f.rationale}")
            A("")

    A("---\n")
    A("Rule definitions, weights, capping, and the extent/entity/example requirement "
      "are in `rules.md`. Extent is count-derived and approximate: it compares "
      "per-span-name totals rather than joining on the correlation key, so a step "
      "firing twice per journey understates the gap.")
    return "\n".join(L) + "\n"


def to_json(gaps: list[JourneyGap], obs: Observed, skipped: list[dict]) -> dict:
    return {
        "version": 1,
        "org": obs.org,
        "stats_period": obs.stats_period,
        "traces_sample_rate": obs.traces_sample_rate,
        "low_confidence": obs.low_confidence,
        "skipped": skipped,
        "journeys": [
            {
                "id": g.id,
                "name": g.name,
                "coverage_state": g.coverage_state,
                "steps_instrumented": g.steps_instrumented,
                "steps_total": g.steps_total,
                "score": g.score,
                "raw_score": g.raw_score,
                "grade": g.grade,
                "caps": g.caps,
                "low_confidence": g.low_confidence,
                "dark_segments": g.dark_segments,
                "missing_steps": g.missing_steps,
                "notes": g.notes,
                "findings": [
                    {
                        "rule": f.rule, "description": f.description,
                        "rationale": f.rationale, "target": f.target,
                        "impact": f.impact, "weight": f.weight, "passed": f.passed,
                        "detail": f.detail, "extent": f.extent,
                        "entity": f.entity, "example": f.example,
                    }
                    for f in g.findings
                ],
            }
            for g in order_for_report(gaps)
        ],
    }


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Diff resolved journeys against observed telemetry.")
    p.add_argument("--resolved", required=True, help="Output of intake/resolve.py")
    p.add_argument("--observed", required=True, help="Output of gap/sentry_source.py, or a fixture")
    p.add_argument("--out-md")
    p.add_argument("--out-json")
    p.add_argument("--include-unready", action="store_true",
                   help="Also analyze journeys that are not spec-ready. Useful early: a "
                        "journey missing its outcome role still has steps worth diffing.")
    args = p.parse_args(argv)

    try:
        resolved = json.loads(Path(args.resolved).read_text())
        observed_doc = json.loads(Path(args.observed).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    obs = Observed(observed_doc)
    gaps: list[JourneyGap] = []
    skipped: list[dict] = []

    for j in resolved.get("journeys", []):
        if j.get("excluded"):
            skipped.append({"name": j["name"], "reason": "excluded during intake"})
            continue
        if not (j.get("roles") or {}).get("steps"):
            skipped.append({"name": j["name"], "reason": "no steps defined — nothing to diff"})
            continue
        if not j.get("spec_ready") and not args.include_unready:
            skipped.append({
                "name": j["name"],
                "reason": "not spec-ready: " + "; ".join(j.get("blockers") or []),
            })
            continue
        gaps.append(analyze_journey(j, obs))

    if not gaps:
        print("error: no analyzable journeys. Try --include-unready.", file=sys.stderr)
        return 2

    report = render_markdown(gaps, obs, skipped)
    payload = to_json(gaps, obs, skipped)

    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(report)
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(report)

    for g in order_for_report(gaps):
        if g.coverage_state == "absent":
            print(f"{g.name}: not instrumented (0/{g.steps_total} steps)", file=sys.stderr)
        else:
            print(f"{g.name}: {g.score} ({g.grade}) {g.coverage_label}, "
                  f"{len(g.failures)} failed" + (" [CAPPED]" if g.caps else ""),
                  file=sys.stderr)
    if skipped:
        print(f"skipped {len(skipped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
