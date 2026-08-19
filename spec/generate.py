#!/usr/bin/env python3
"""
Spec generator. Turns resolved journeys + gap findings into the two files the
customer actually receives.

Per journey:
    <id>-SPEC.md   for their AI coding agent. Terse, imperative, only the gaps.
    <id>-WHY.md    for the human. What they'll be able to see once it lands.

The design rule that shapes everything here: **only ask for what is missing.**
Handing a customer a seven-span spec when five spans already exist wastes their
engineers' time and destroys the credibility of the exercise. Every requirement
is derived from a FAILED gap rule.

Second rule, from the ETH Zurich study (Feb 2026) measuring agent instruction
files decreasing task success ~3% while raising cost >20%: include only
non-discoverable content. No "what Sentry is" prose, no repo overviews. The
agent has the repo; it needs the contract and the footguns.

Usage:
    ./generate.py --resolved ../intake/example-resolved.json \\
                  --gap ../gap/example-gap.json --out-dir out/

Exit codes:
    0  generated
    1  input error
    2  nothing to generate (no journey has an actionable gap)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Rules that translate into a customer-facing requirement, and how.
# Rules absent from this map are diagnostic only and never generate an ask.
ACTIONABLE = {
    "CE-001", "CE-002", "CE-003", "CE-004", "CE-006",
    "CE-007", "CE-008", "CE-009", "CE-010", "CE-011", "CE-013",
}

DEPRECATED_TABLE_JS = [
    ("Sentry.startTransaction()", "Sentry.startSpan() — removed in v8"),
    ("transaction.startChild() / span.startChild()",
     "a separate Sentry.startSpan(), or the parentSpan option"),
    ("span.setData(...)", "span.setAttribute(...)"),
    ("span.setTag(...)", "span.setAttribute(...)"),
    ("span.finish()", "span.end()"),
    ("span.setName(...)", "Sentry.updateSpanName(span, name)"),
    ("Sentry.configureScope(cb)", "Sentry.getCurrentScope() / Sentry.withScope()"),
    ("Sentry.getCurrentHub(), Hub", "scope APIs — fully removed in v9"),
    ("Sentry.metrics.increment / legacy Sentry.metrics (pre–Application Metrics)",
     "span attributes; if §4 asks, Sentry.metrics.count|gauge|distribution "
     "(SDK ≥ 10.25)"),
    ("Sentry.setMeasurement(...)", "span attributes"),
    ("enableTracing, tracingOrigins", "tracesSampleRate, tracePropagationTargets"),
    ("new Sentry.Replay() and other class integrations", "Sentry.replayIntegration()"),
    ("span.traceId / span.spanId / span.status",
     "span.spanContext().traceId / .spanId, spanToJSON(span).status"),
]

DEPRECATED_TABLE_PY = [
    ("sentry_sdk.start_span(...) as the journey root",
     "sentry_sdk.start_transaction(...) — a bare start_span with no active "
     "transaction is dropped silently, exit 0, nothing on the wire"),
    ("span.set_tag(...)", "span.set_data(...) — tags are not custom attributes"),
    ("span.setAttribute(...) / span.setAttributes(...)",
     "span.set_data(key, value) — setAttribute is the JavaScript SDK"),
    ("Hub / sentry_sdk.Hub", "sentry_sdk.get_current_scope() / isolation scope APIs"),
    ("sentry_sdk.configure_scope", "get_current_scope() / new_scope()"),
]

# Back-compat alias used by tests that imported the old name.
DEPRECATED_TABLE = DEPRECATED_TABLE_JS


def fail_map(gap_journey: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in gap_journey["findings"]:
        if not f["passed"]:
            out.setdefault(f["rule"], []).append(f)
    return out


def steps_of(resolved_journey: dict) -> list[dict]:
    return resolved_journey["roles"].get("steps") or []


def expected_name(journey_id: str, step: dict, is_first: bool) -> str:
    if step.get("span_name"):
        return step["span_name"]
    return journey_id if is_first else f"{journey_id}.{step['id']}"


def detect_sdks(observed: dict | None, explicit: list[str] | None) -> list[str]:
    """Which SDK families §5/§6 should document.

    Explicit `--sdk javascript` / `--sdk python` wins. `auto` (the default) reads
    observed.json: installed SDK families first, then `source_languages` from a
    static scan (State B: no SDK yet). Last resort is javascript — that was the
    previous hardcoded output, so existing fixtures keep generating the same spec.
    """
    wanted = [s.lower() for s in (explicit or ["auto"])]
    if wanted == ["auto"] or (len(wanted) == 1 and wanted[0] == "auto") or "auto" in wanted:
        if not observed:
            return ["javascript"]
        sdk = observed.get("sdk") or {}
        found = [k for k in ("javascript", "python")
                 if (sdk.get(k) or {}).get("imported")
                 or (sdk.get(k) or {}).get("initialised")]
        if found:
            return found
        langs = [s for s in (observed.get("source_languages") or [])
                 if s in ("javascript", "python")]
        return langs or ["javascript"]
    out = []
    for s in wanted:
        if s == "auto":
            continue
        if s not in ("javascript", "python"):
            continue
        if s not in out:
            out.append(s)
    return out or ["javascript"]


# Same 5% guard as gap/analyze.py::LOW_CONFIDENCE_SAMPLE_RATE / gap/rules.md.
# Below it, span aggregates lie; SIGNAL.md uses it to decide log/metric companions.
COMPANION_SAMPLE_RATE = 0.05

# SIGNAL.md "abandoned-only": these are non-success but Issues should not triage
# them as coded failures. Not the same as inferring success_values.
_ABANDONED_LIKE = frozenset({"abandoned", "cancelled", "canceled"})
# Used only to peel success-shaped values off outcome.values so the remainder
# can be tested for abandoned-only. Never written into a spec as success_values.
_SUCCESS_SHAPED = frozenset({
    "completed", "success", "succeeded", "ok", "authorized", "approved",
})


def traces_sample_rate(observed: dict | None) -> float | None:
    """Unknown stays unknown — SIGNAL.md forbids inventing a rate so a companion fires."""
    if not observed:
        return None
    raw = observed.get("traces_sample_rate")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def merge_observed(observed: dict | None, gapdoc: dict | None) -> dict | None:
    """Scan/snapshot wins; gap.json carries the same rate when --observed was omitted."""
    out = dict(observed or {})
    if out.get("traces_sample_rate") is None and gapdoc is not None:
        if gapdoc.get("traces_sample_rate") is not None:
            out["traces_sample_rate"] = gapdoc["traces_sample_rate"]
    return out or None


def _has_triageable_failure(values: list | None) -> bool:
    """True iff outcome.values has a non-success that is not only abandoned."""
    vals = {str(v).strip().lower() for v in (values or []) if v is not None and str(v).strip()}
    if not vals:
        return False
    return bool((vals - _SUCCESS_SHAPED) - _ABANDONED_LIKE)


def _persists_across_surfaces(rj: dict) -> bool:
    persists = ((rj.get("roles") or {}).get("correlation_key") or {}).get("persists_across") or []
    return len({str(s).strip() for s in persists if s}) > 1


def companion_signals(rj: dict, gj: dict, observed: dict | None = None) -> list[dict]:
    """SIGNAL.md policy. Companions on failed actionable rules only. No LLM.

    Each item: kind (error|log|metric), rule to hang on, why (WHY.md one-liner),
    text (appended to that FR-*).
    """
    fails = fail_map(gj)
    roles = rj.get("roles") or {}
    rate = traces_sample_rate(observed)
    out: list[dict] = []

    corr = (roles.get("correlation_key") or {}).get("attribute")
    fr = (roles.get("failure_reason") or {}).get("attribute")
    outcome = roles.get("outcome") or {}
    oc_attr = outcome.get("attribute")
    segs = [s["attribute"] for s in (roles.get("actor_segment") or []) if s.get("attribute")]
    mags = [m["attribute"] for m in (roles.get("magnitude") or []) if m.get("attribute")]

    if "CE-008" in fails and _has_triageable_failure(outcome.get("values")):
        fp = f"`{fr}`" if fr else "the coded failure reason"
        key = f"`{corr}`" if corr else "the correlation key"
        out.append({
            "kind": "error",
            "rule": "CE-008",
            "why": "Issues will group by coded reason.",
            "text": (
                f"Also call `Sentry.captureException` / `sentry_sdk.capture_exception` "
                f"so Seer and Issues can triage the failure. Fingerprint and tag with "
                f"the **coded** {fp} — the exception message is not the grouper. Attach "
                f"{key} on the scope. This does **not** replace the span attribute."
            ),
        })

    sampled_out = rate is not None and rate < COMPANION_SAMPLE_RATE
    if ("CE-004" in fails or "CE-008" in fails) and (sampled_out or _persists_across_surfaces(rj)):
        log_rule = "CE-004" if "CE-004" in fails else "CE-008"
        attrs = []
        if corr:
            attrs.append(f"`{corr}`")
        attrs.append("the step id")
        if fr:
            attrs.append(f"the coded `{fr}`")
        attr_list = ", ".join(attrs)
        out.append({
            "kind": "log",
            "rule": log_rule,
            "why": "Logs keep the instance when traces are sampled.",
            "text": (
                "Also emit one structured log (`Sentry.logger.*` / `sentry_sdk.logger.*`; "
                "JS requires `enableLogs: true`) with a parameterized, low-cardinality "
                f"body. Attributes: {attr_list}. No PII, no raw provider payload. Do not "
                "log every happy-path info line. The log is not the only place the "
                "correlation key or outcome may live."
            ),
        })

    impact = (rj.get("business_impact") or "").strip().lower()
    critical_undersampled = impact == "critical" and rate is not None and rate < 1
    mag_fail = "CE-009" in fails or "CE-010" in fails
    if mag_fail and mags and (sampled_out or critical_undersampled):
        metric_rule = "CE-009" if "CE-009" in fails else "CE-010"
        mag_list = ", ".join(f"`{m}`" for m in mags)
        metric_attrs = []
        if oc_attr:
            metric_attrs.append(f"`{oc_attr}`")
        metric_attrs.extend(f"`{s}`" for s in segs)
        extra = ""
        if metric_attrs:
            extra = (" Put " + ", ".join(metric_attrs)
                     + " on the metric if declared.")
        no_id = (f" Do **not** put `{corr}` on the metric — a journey id is an "
                 "instance id and explodes cardinality.") if corr else ""
        out.append({
            "kind": "metric",
            "rule": metric_rule,
            "why": "Metrics keep magnitude when traces are sampled.",
            "text": (
                f"Also emit an Application Metric so `{mags[0]}` survives sampling: "
                "`Sentry.metrics.count` / `distribution` (JS SDK ≥ 10.25) or "
                "`sentry_sdk.metrics.count` / `distribution` (Python ≥ 2.44). "
                f"Name the metric from the attribute ({mag_list}).{extra}{no_id} "
                "This does **not** replace the span attribute. Do **not** stringify "
                "the number."
            ),
        })
    return out


def _attach_companions(reqs: list[dict], companions: list[dict]) -> None:
    """Hang each companion on the first FR-* for its failed rule."""
    by_rule: dict[str, list[dict]] = {}
    for r in reqs:
        by_rule.setdefault(r["rule"], []).append(r)
    for c in companions:
        targets = by_rule.get(c["rule"]) or []
        if not targets:
            continue
        targets[0]["text"] = targets[0]["text"] + " " + c["text"]
        targets[0]["companion"] = c["kind"]


# --------------------------------------------------------------------------
# Requirement construction
# --------------------------------------------------------------------------


def build_requirements(rj: dict, gj: dict, observed: dict | None = None) -> list[dict]:
    """One numbered requirement per failed, actionable rule. Nothing else."""
    fails = fail_map(gj)
    steps = steps_of(rj)
    roles = rj["roles"]
    reqs: list[dict] = []

    def add(text: str, rule: str, note: str = "", check: dict | None = None) -> None:
        """`check` is the machine-checkable form of the requirement.

        It exists so the eval harness grades against the SAME source the spec was
        generated from, rather than parsing the markdown back out. A rubric
        derived from prose can only test the cases someone remembered to write a
        grader for; this one cannot drift from the spec because both come from
        here. Requirements with `check: None` are prose-only and are reported as
        ungradeable rather than silently passing.
        """
        reqs.append({"id": f"FR-{len(reqs) + 1:03d}", "text": text,
                     "rule": rule, "note": note, "check": check})

    # Drift first — it is the cheapest fix in the whole spec, and doing it in the
    # wrong order means an engineer writes a span that already exists.
    #
    # A drifted step ALSO shows up as a missing step (queries on the bound name
    # return nothing). Emitting both a "rename it" and a "create it" requirement
    # for the same span is contradictory: an agent either duplicates the span or
    # stalls. Drift supersedes.
    drift_covers: set[str] = set()
    for f in fails.get("CE-013", []):
        parts = f["detail"].split("`")
        bound = parts[1] if len(parts) > 1 else None
        found = parts[3] if len(parts) > 3 else None
        if bound:
            drift_covers.add(bound)
        add(f"Rename the existing span so it matches the contract exactly. {f['detail']}. "
            "Do **not** add a new span — the instrumentation already exists and is "
            "emitting; only the name is wrong. This single change also satisfies the "
            "step requirement for that span.",
            "CE-013", "rename, not new code",
            check={"kind": "span_renamed", "to": bound, "from": found,
                   "impact": "critical"})

    missing_steps = {f["entity"] for f in fails.get("CE-003", [])} - drift_covers
    for i, s in enumerate(steps):
        name = expected_name(rj["id"], s, i == 0)
        if name not in missing_steps:
            continue
        surface = s.get("surface")
        where = f" in the {surface} runtime" if surface else ""
        role = ("This is the journey root span; open it when the journey starts and end "
                "it on a terminal event." if i == 0 else
                "Create it as a child of the surrounding span." )
        add(f"A span named `{name}` MUST be created{where} for step `{s['id']}`. {role}",
            "CE-003",
            check={"kind": "span_present", "span": name,
                   "impact": s.get("impact", "normal")})

    corr = (roles.get("correlation_key") or {}).get("attribute")
    if "CE-004" in fails and corr:
        persists = ", ".join((roles["correlation_key"].get("persists_across") or []))
        extra = f" It MUST survive across: {persists}." if persists else ""
        add(f"Every span in this journey MUST carry the attribute `{corr}`, generated once "
            f"when the journey starts.{extra} Do not use the trace ID for this — a browser "
            "navigation starts a new trace.", "CE-004",
            check={"kind": "attribute_key_exact", "attribute": corr,
                   "impact": "critical"})

    outcome = roles.get("outcome") or {}
    if "CE-006" in fails and outcome.get("attribute"):
        vals = " | ".join(outcome.get("values") or [])
        add(f"The journey root span MUST carry `{outcome['attribute']}` as a string enum "
            f"with one of: {vals}.", "CE-006",
            check={"kind": "attribute_present", "attribute": outcome["attribute"],
                   "impact": "critical"})
    if "CE-007" in fails and outcome.get("attribute"):
        vals = " | ".join(outcome.get("values") or [])
        add(f"`{outcome['attribute']}` MUST be a string enum ({vals}), not a boolean. "
            "Sentry renders boolean attributes as the strings 'true'/'false', so the "
            "current value cannot express more than two states.", "CE-007",
            check={"kind": "attribute_not_boolean", "attribute": outcome["attribute"],
                   "allowed": outcome.get("values") or [], "impact": "important"})
    if outcome.get("default_value"):
        # Not rule-driven, but worthless to omit: without it an abandoned journey
        # is indistinguishable from an uninstrumented one.
        if "CE-006" in fails or "CE-007" in fails:
            add(f"`{outcome['attribute']}` MUST be initialised to "
                f"`{outcome['default_value']}` when the journey starts and overwritten "
                "on a terminal event.", "CE-006",
                check={"kind": "literal_present", "literal": outcome["default_value"],
                       "impact": "normal"})

    if "CE-008" in fails:
        fr = (roles.get("failure_reason") or {}).get("attribute")
        if fr:
            known = roles["failure_reason"].get("known_values") or []
            ex = f" Known values: {', '.join(known)}." if known else ""
            add(f"When the outcome is not a success value, the span MUST carry `{fr}` "
                f"with the provider's or system's **coded** reason.{ex} Never a free-text "
                "message, never a raw upstream payload.", "CE-008",
                check={"kind": "attribute_present", "attribute": fr,
                       "impact": "important"})

    # CE-009 is per-attribute, so ask only for the ones actually absent.
    absent_mags = {f.get("entity") for f in fails.get("CE-009", [])}
    for m in roles.get("magnitude") or []:
        if m["attribute"] not in absent_mags:
            continue
        add(f"The span for step `{m.get('step') or 'the value-bearing step'}` MUST carry "
            f"`{m['attribute']}` as a **{m['type']}** (a JavaScript number, not a string).",
            "CE-009",
            check={"kind": "attribute_numeric", "attribute": m["attribute"],
                   "impact": "important"})
    for f in fails.get("CE-010", []):
        add(f"{f['detail']}. It MUST be emitted as a number. A stringified value cannot be "
            "aggregated — `sum()` and `p50()` return nothing useful while the code looks "
            "correct.", "CE-010",
            check={"kind": "attribute_numeric", "attribute": f.get("entity"),
                   "impact": "important"} if f.get("entity") else None)

    if "CE-011" in fails:
        segs = [s["attribute"] for s in (roles.get("actor_segment") or [])]
        if segs:
            add("The journey root span SHOULD carry at least one segmentation attribute: "
                + ", ".join(f"`{s}`" for s in segs)
                + ". These usually already exist in the auth or tenancy layer.", "CE-011",
                check={"kind": "any_attribute_present", "attributes": segs,
                       "impact": "normal"})

    companions = companion_signals(rj, gj, observed)
    _attach_companions(reqs, companions)
    kinds = {c["kind"] for c in companions}

    # Always last, always present. Both are negative checks — "must not" rules that
    # no amount of correct instrumentation can satisfy, only violate.
    init = ("Existing `Sentry.init()` options, sampling rates, and unrelated "
            "instrumentation MUST NOT be modified.")
    if "log" in kinds:
        init += (" Exception: JS `enableLogs: true` is required when this spec asks "
                 "for structured logs — do not change `tracesSampleRate`.")
    add(init, "-", check={"kind": "no_deprecated_api", "impact": "important"})
    pii_where = "span name, `op`, or attribute"
    if "log" in kinds or "metric" in kinds:
        pii_where = ("span name, `op`, attribute, log body, log attribute, "
                     "or metric attribute")
    add(f"No PII, card data, token, or raw provider payload may appear in any {pii_where}.",
        "-", check={"kind": "no_pii", "impact": "critical"})
    return reqs


def build_rubric(rj: dict, gj: dict, gapdoc: dict, observed: dict | None = None) -> dict:
    """The machine-checkable form of the spec, for eval/grade.py.

    Emitted from the same requirement list the markdown is rendered from, so the
    two cannot drift. Requirements with no `check` are carried as `gradeable:
    false` rather than dropped — an ungradeable requirement is a known blind spot,
    not a pass.
    """
    reqs = build_requirements(rj, gj, observed)
    roles = rj["roles"]
    steps = steps_of(rj)
    fails = fail_map(gj)
    missing_spans = {f["entity"] for f in fails.get("CE-003", [])}

    already_spans = [
        expected_name(rj["id"], s, i == 0) for i, s in enumerate(steps)
        if expected_name(rj["id"], s, i == 0) not in missing_spans
    ]

    # Guards protect what already works. The spec deliberately asks only for what
    # is missing — but that leaves everything already correct ungraded, so a
    # regression in existing instrumentation would score a clean 100%. An eval
    # that can't detect "you broke the thing that worked" is not a verification.
    # These are the static-analysis counterpart of gap/diff.py's regressions.
    guards: list[dict] = [
        {"id": f"GD-{i + 1:03d}", "why": "already instrumented before this work",
         "check": {"kind": "span_present", "span": name, "impact": "critical"}}
        for i, name in enumerate(already_spans)
    ]
    passing_attrs: list[str] = []
    for rule in ("CE-004", "CE-006", "CE-008", "CE-009", "CE-010", "CE-011"):
        for f in gj["findings"]:
            if f["rule"] == rule and f["passed"] and f.get("entity"):
                if f["entity"] not in passing_attrs:
                    passing_attrs.append(f["entity"])
    for a in passing_attrs:
        guards.append({
            "id": f"GD-{len(guards) + 1:03d}",
            "why": "attribute already present in the org before this work",
            "check": {"kind": "attribute_key_exact", "attribute": a,
                      "impact": "important"},
        })

    return {
        "version": 1,
        "journey": {"id": rj["id"], "name": rj["name"]},
        "measured_against": {"org": gapdoc.get("org"),
                             "stats_period": gapdoc.get("stats_period")},
        "baseline": {"score": gj["score"], "grade": gj["grade"],
                     "steps_instrumented": gj["steps_instrumented"],
                     "steps_total": gj["steps_total"]},
        "spans_already_present": already_spans,
        "requirements": [
            {"id": r["id"], "rule": r["rule"], "text": r["text"],
             "gradeable": r["check"] is not None, "check": r["check"]}
            for r in reqs
        ],
        "guards": guards,
        "acceptance_criteria": build_acceptance(rj, gj),
        "correlation_key": (roles.get("correlation_key") or {}).get("attribute"),
    }


def build_acceptance(rj: dict, gj: dict) -> list[dict]:
    roles = rj["roles"]
    corr = (roles.get("correlation_key") or {}).get("attribute")
    outcome = (roles.get("outcome") or {}).get("attribute")
    mags = [m["attribute"] for m in (roles.get("magnitude") or [])]
    steps = steps_of(rj)
    out: list[dict] = []

    def add(text: str) -> None:
        out.append({"id": f"SC-{len(out) + 1:03d}", "text": text})

    if corr and steps:
        add(f"Given one completed journey, when I search spans for `{corr}` matching that "
            f"instance, then all {len(steps)} spans in §2 are present.")
    if outcome:
        add(f"Given any completed journey, when I filter on `{outcome}`, then every "
            "instance resolves to exactly one value.")
    for f in gj["findings"]:
        if f["rule"] == "CE-013" and not f["passed"]:
            add("Given the rename is complete, when I search for the old span name, then "
                "no new spans appear under it.")
            break
    if mags:
        add(f"Given completed journeys, when I query `sum({mags[0]})`, then a numeric "
            "result renders — proving the attribute was stored as a number.")
    fr = (roles.get("failure_reason") or {}).get("attribute")
    if fr:
        add(f"Given a failed journey, when I group spans by `{fr}`, then every result has "
            "a non-empty coded reason.")
    surfaces = {s.get("surface") for s in steps if s.get("surface")}
    if len(surfaces) > 1:
        add("Given the journey crosses runtimes, when I open a trace for the hand-off "
            "step, then spans from both runtimes appear in the same trace.")
    add("Given any span in this journey, when I inspect all attributes, then no PII, card "
        "data, or raw provider payload is present.")
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _append_api(L: list[str], sdks: list[str], steps: list[dict],
                companions: list[dict] | None = None) -> None:
    """§5 and §6, parameterised by SDK family. JS-only was the biggest blocker
    to using a generated spec outside a JS shop (setAttribute vs set_data).

    Companion APIs (Issues / logs / Application Metrics) are listed only when
    SIGNAL.md selected them for this spec — otherwise §5 stays span-only.
    """
    A = L.append
    surfaces = {s.get("surface") for s in steps if s.get("surface")}
    both = len(sdks) > 1

    if "javascript" in sdks:
        if both:
            A("### JavaScript — `@sentry/*` v10\n")
        else:
            A("Everything you need. Verified against `@sentry/*` v10.\n")
        A("#### `Sentry.startSpan` — the default choice\n" if both else
          "### `Sentry.startSpan` — the default choice\n")
        A("Wraps a block, auto-ends the span, returns the callback's value. Works with sync "
          "or async callbacks; for async the span ends when the promise settles, and a throw "
          "or rejection marks the span errored.\n")
        A("```ts")
        A("const order = await Sentry.startSpan(")
        A("  { name: \"<span name from §2>\", op: \"function\", attributes: { \"<key>\": value } },")
        A("  async (span) => {")
        A("    const created = await doWork();")
        A("    span.setAttribute(\"<key>\", created.id);")
        A("    return created;")
        A("  },")
        A(");")
        A("```\n")
        A("Options: `name` (only required field), `op`, `attributes`, `startTime`, "
          "`parentSpan` (pass `null` to force a root span), `onlyIfParent`, `forceTransaction`.\n")
        A("#### Other span starters\n" if both else "### Other span starters\n")
        A("- `Sentry.startSpanManual(options, cb)` — same signature, but **you** call "
          "`span.end()`. Use when the end is triggered by an event.")
        A("- `Sentry.startInactiveSpan(options)` — returns a span that never becomes a parent. "
          "You call `span.end()`.")
        A("- **Long-lived browser spans** (a journey root that outlives one callback): pair "
          "`startInactiveSpan` with `Sentry.setActiveSpanInBrowser(span)` so later spans nest "
          "under it. Requires SDK ≥ 10.15.0, browser only. Ending the span clears it.\n")
        A("#### Attributes\n" if both else "### Attributes\n")
        A("```ts")
        A("span.setAttribute(\"cart.value\", 129.99);          // number, not \"129.99\"")
        A("span.setAttributes({ \"a\": \"x\", \"b\": true });")
        A("")
        A("// set an attribute on the journey root from a nested context")
        A("const active = Sentry.getActiveSpan();")
        A("const root = active ? Sentry.getRootSpan(active) : undefined;")
        A("root?.setAttribute(\"<key>\", value);")
        A("```\n")
        A("Allowed value types: `string`, `number`, `boolean`, or a non-mixed array of those. "
          "**Nested objects are not allowed** — flatten to dotted keys. Passing `undefined` "
          "removes the attribute.\n")
        A("Also available: `Sentry.updateSpanName(span, name)`, "
          "`Sentry.setHttpStatus(span, code)`, `span.setStatus({ code: 2 })` "
          "(0 unknown, 1 ok, 2 error), `Sentry.withActiveSpan(span, cb)`, "
          "`Sentry.getTraceData()`, `Sentry.continueTrace({ sentryTrace, baggage }, cb)`.\n")
        if len(surfaces) > 1:
            A("#### Distributed tracing\n" if both else "### Distributed tracing\n")
            A(f"This journey crosses {len(surfaces)} runtimes "
              f"({', '.join(sorted(x for x in surfaces if x))}). "
              "Two headers propagate the trace: `sentry-trace` and `baggage`. Since v8, with "
              "`tracePropagationTargets` unset, headers attach to **same-origin requests "
              "only** — a different origin *or a different port* gets none, and the server "
              "must allow both headers through CORS.\n")
            A("```ts")
            A("Sentry.init({")
            A("  dsn: process.env.SENTRY_DSN,")
            A("  tracePropagationTargets: [\"localhost:3000\", /^https:\\/\\/api\\.example\\.com/],")
            A("});")
            A("```\n")

    if "python" in sdks:
        if both:
            A("### Python — `sentry-sdk` 2.x\n")
        else:
            A("Everything you need. Verified against `sentry-sdk` 2.x.\n")
        A("#### `start_transaction` — journey roots\n" if both else
          "### `start_transaction` — journey roots\n")
        A("A bare `sentry_sdk.start_span(...)` with **no active transaction** is an "
          "orphan: the SDK drops it silently, the process exits 0, and nothing reaches "
          "Sentry. Journey roots must be `start_transaction`. `start_span` is for children.\n")
        A("```python")
        A("import sentry_sdk")
        A("")
        A("with sentry_sdk.start_transaction(")
        A("    op=\"ui.action\", name=\"<span name from §2>\"")
        A(") as tx:")
        A("    tx.set_data(\"<key>\", value)")
        A("    with sentry_sdk.start_span(op=\"function\", name=\"<child span from §2>\") as span:")
        A("        span.set_data(\"<key>\", created.id)")
        A("```\n")
        A("#### Attributes\n" if both else "### Attributes\n")
        A("```python")
        A("span.set_data(\"cart.value\", 129.99)   # number, not \"129.99\"")
        A("```\n")
        A("Use `set_data`, not `setAttribute` (that is the JavaScript SDK). Both land in "
          "the envelope `data` field. Allowed value types: `str`, `int`, `float`, `bool`. "
          "**Nested objects are not allowed** — flatten to dotted keys.\n")
        if len(surfaces) > 1:
            A("#### Distributed tracing\n" if both else "### Distributed tracing\n")
            A(f"This journey crosses {len(surfaces)} runtimes "
              f"({', '.join(sorted(x for x in surfaces if x))}). "
              "`sentry-trace` and `baggage` propagate the trace.\n")
            A("```python")
            A("sentry_sdk.init(")
            A("    dsn=os.environ[\"SENTRY_DSN\"],")
            A("    traces_sample_rate=1.0,")
            A("    trace_propagation_targets=[\"localhost:3000\", r\"https://api\\.example\\.com\"],")
            A(")")
            A("```\n")

    kinds = {c["kind"] for c in (companions or [])}
    if "error" in kinds or "log" in kinds or "metric" in kinds:
        A("### Companion signals\n")
        A("Selected by `SIGNAL.md` for failed rules in this spec. They do **not** replace "
          "the span attribute. Do not emit these APIs for other journeys.\n")
    if "javascript" in sdks:
        if "error" in kinds:
            A("```ts")
            A("Sentry.withScope((scope) => {")
            A("  scope.setFingerprint([\"<coded failure_reason>\"]);")
            A("  scope.setTag(\"<failure_reason key>\", codedReason);")
            A("  scope.setAttribute(\"<correlation key>\", value);")
            A("  Sentry.captureException(err);  // message is not the grouper")
            A("});")
            A("```\n")
        if "log" in kinds:
            A("`enableLogs: true` in `Sentry.init` is required. Parameterized body, no PII.\n")
            A("```ts")
            A("Sentry.logger.error(Sentry.logger.fmt`journey step failed`, {")
            A("  \"<correlation key>\": value,")
            A("  step: \"<step id>\",")
            A("  \"<failure_reason key>\": codedReason,")
            A("});")
            A("```\n")
        if "metric" in kinds:
            A("Application Metrics, SDK ≥ 10.25. Enabled by default. "
              "Do **not** put the journey instance id on the metric.\n")
            A("```ts")
            A("Sentry.metrics.distribution(\"<magnitude key>\", amount, {")
            A("  attributes: { \"<outcome key>\": outcome },")
            A("});")
            A("```\n")
    if "python" in sdks:
        if "error" in kinds:
            A("```python")
            A("with sentry_sdk.new_scope() as scope:")
            A("    scope.fingerprint = [\"<coded failure_reason>\"]")
            A("    scope.set_tag(\"<failure_reason key>\", coded_reason)")
            A("    scope.set_tag(\"<correlation key>\", value)")
            A("    sentry_sdk.capture_exception(err)  # message is not the grouper")
            A("```\n")
        if "log" in kinds:
            A("`sentry_sdk.logger` — SDK ≥ 2.35, enabled by default. "
              "Parameterized body, no PII.\n")
            A("```python")
            A("sentry_sdk.logger.error(")
            A("    \"journey step failed\",")
            A("    attributes={")
            A("        \"<correlation key>\": value,")
            A("        \"step\": \"<step id>\",")
            A("        \"<failure_reason key>\": coded_reason,")
            A("    },")
            A(")")
            A("```\n")
        if "metric" in kinds:
            A("`sentry_sdk.metrics` — SDK ≥ 2.44. Do **not** put the journey "
              "instance id on the metric.\n")
            A("```python")
            A("sentry_sdk.metrics.distribution(")
            A("    \"<magnitude key>\", amount,")
            A("    attributes={\"<outcome key>\": outcome},")
            A(")")
            A("```\n")

    A("### Constraints\n")
    A("- A transaction holds a maximum of **1,000 spans**.")
    A("- Span `name` and `op` must stay low-cardinality — never interpolate an ID or URL.")
    A("- Attribute values have no documented length cap; scope tags cap at 200 characters.")
    A("- Use the documented `op` vocabulary: `function`, `ui.action`, `ui.action.click`, "
      "`http.client`, `db`, `db.query`, `cache.get_item`, `queue.task.*`, "
      "`template.render`, `serialize`, `middleware.*`.\n")

    A("## 6. Do not use — removed or deprecated\n")
    A("Your training data likely contains these. Emitting them is a defect.\n")
    A("| Never emit | Use instead |")
    A("| --- | --- |")
    rows = []
    if "javascript" in sdks:
        rows.extend(DEPRECATED_TABLE_JS)
    if "python" in sdks:
        rows.extend(DEPRECATED_TABLE_PY)
    for bad, good in rows:
        A(f"| `{bad}` | {good} |")
    A("")
    if "javascript" in sdks:
        A("The Node SDK is built on OpenTelemetry, so OTel-instrumented spans are picked up "
          "automatically and OTel APIs are usable there. **Use `Sentry.startSpan()` anyway** — "
          "it is the documented recommendation, and mixing the two makes the §7 verification "
          "queries unreliable. The browser SDK is not OTel-based; do not use OTel APIs there.\n")


def render_spec(rj: dict, gj: dict, gapdoc: dict, sdks: list[str] | None = None,
                observed: dict | None = None) -> str:
    observed = merge_observed(observed, gapdoc)
    reqs = build_requirements(rj, gj, observed)
    companions = companion_signals(rj, gj, observed)
    kinds = {c["kind"] for c in companions}
    crit = build_acceptance(rj, gj)
    steps = steps_of(rj)
    roles = rj["roles"]
    fails = fail_map(gj)
    L: list[str] = []
    A = L.append

    A(f"# Sentry Instrumentation Spec — {rj['name']}\n")
    A("| | |")
    A("| --- | --- |")
    A(f"| **Journey** | {rj['name']} (`{rj['id']}`) |")
    A(f"| **Current coverage** | {gj['steps_instrumented']}/{gj['steps_total']} steps "
      f"instrumented · score {gj['score']} ({gj['grade']}) |")
    A(f"| **Measured against** | org `{gapdoc['org']}`, window {gapdoc['stats_period']} |")
    sdk_label = ", ".join(
        "`@sentry/*` v10" if s == "javascript" else "`sentry-sdk` 2.x" for s in (sdks or ["javascript"])
    )
    A(f"| **Target SDK** | {sdk_label} |")
    A("| **Minimum SDK** | JS `9.x` (`10.15.0+` for `setActiveSpanInBrowser`"
      + ("; `10.25.0+` for Application Metrics" if "metric" in kinds else "")
      + "); Python `2.x`"
      + (" (`2.35+` logs, `2.44+` metrics)" if kinds & {"log", "metric"} else "")
      + " |")
    A(f"| **Requirements** | {len(reqs)} |")
    A("| **Status** | Draft — review before implementing |")
    A("")
    if gapdoc.get("low_confidence"):
        A("> **Verify before implementing.** The sample rate in the measured window was "
          "below 5%, so a span reported as missing may simply not have been recorded.\n")

    A("## 0. Instructions for AI coding agents\n")
    A("You are adding **only the missing pieces** of one business journey's Sentry "
      "instrumentation. Read this whole file first.\n")
    A("- Everything in §2 that is marked *present* already works. **Do not touch it.**")
    A("- Implement only the numbered requirements in §4.")
    A("- Use **exactly** the span names and attribute keys given. They are a contract: "
      "the verification queries in §7 match on these literal strings. A misnamed "
      "attribute produces no error, no failing test, and no data.")
    A("- Every API you need is in §5. **Do not use any Sentry API not listed there.** "
      "§6 lists APIs that will be in your training data and are removed or deprecated.")
    A("- Never put an ID, URL, email, or free-text message in a span `name` or `op`.")
    A("- If the codebase has no clear location for a required span, stop and ask. Do not "
      "invent one.")
    A("- When done, output the §4 list with each item marked and the file:line where you "
      "implemented it.\n")

    A("## 1. Why this journey\n")
    for n in gj.get("notes") or []:
        A(f"- {n}")
    if gj.get("dark_segments"):
        for seg in gj["dark_segments"]:
            A(f"- The journey currently goes dark at **{' → '.join(seg)}**. Latency and "
              "drop-off from those steps are being attributed to the last instrumented "
              "step, so the owning team never sees the problem.")
    A("")

    A("## 2. Span contract — current state\n")
    A("| # | Span name | Surface | Step | State |")
    A("| --- | --- | --- | --- | --- |")
    missing = {f["entity"] for f in fails.get("CE-003", [])}
    for i, s in enumerate(steps):
        name = expected_name(rj["id"], s, i == 0)
        state = "**MISSING — add it**" if name in missing else "present, leave alone"
        A(f"| {i + 1} | `{name}` | {s.get('surface') or '—'} | `{s['id']}` | {state} |")
    A("")

    A("## 3. Attribute contract\n")
    A("Sentry surfaces boolean attributes as the strings `'true'`/`'false'`, so no "
      "attribute here is a boolean.\n")
    A("| Key | Type | Role | State |")
    A("| --- | --- | --- | --- |")

    def attr_state(rule: str, key: str) -> str:
        for f in fails.get(rule, []):
            if f.get("entity") == key or key in (f.get("detail") or ""):
                return f"**FIX — {f['detail']}**"
        return "present"

    corr = (roles.get("correlation_key") or {}).get("attribute")
    if corr:
        A(f"| `{corr}` | string | correlation key (every span) | {attr_state('CE-004', corr)} |")
    oc = roles.get("outcome") or {}
    if oc.get("attribute"):
        vals = ", ".join(oc.get("values") or [])
        st = attr_state("CE-006", oc["attribute"])
        if "CE-007" in fails:
            st = f"**FIX — {fails['CE-007'][0]['detail']}**"
        A(f"| `{oc['attribute']}` | string enum ({vals}) | outcome (root span) | {st} |")
    fr = roles.get("failure_reason") or {}
    if fr.get("attribute"):
        A(f"| `{fr['attribute']}` | string, coded | failure reason | "
          f"{attr_state('CE-008', fr['attribute'])} |")
    for m in roles.get("magnitude") or []:
        st = "present"
        for f in fails.get("CE-010", []) + fails.get("CE-009", []):
            if m["attribute"] in (f.get("detail") or "") or f.get("entity") == m["attribute"]:
                st = f"**FIX — {f['detail']}**"
        A(f"| `{m['attribute']}` | {m['type']} (number) | magnitude | {st} |")
    for s in roles.get("actor_segment") or []:
        A(f"| `{s['attribute']}` | string | actor segment | "
          f"{attr_state('CE-011', s['attribute'])} |")
    A("")

    A("## 4. Requirements\n")
    for r in reqs:
        tag = f" *({r['note']})*" if r["note"] else ""
        A(f"**{r['id']}** — {r['text']}{tag}\n")

    A("## 5. Sentry SDK API reference\n")
    _append_api(L, sdks or ["javascript"], steps, companions)

    A("## 7. Acceptance criteria\n")
    A("Each is checkable in Trace Explorer with no setup — custom span attributes are "
      "searchable, groupable, and (when numeric) chartable with no declaration step.\n")
    for c in crit:
        A(f"**{c['id']}** — {c['text']}\n")
    A("Query windows are plan-gated: Developer 7 days, Team 14, Business 30. Aggregates "
      "are sampling-extrapolated and warn below ~5%.\n")

    A("## 8. Out of scope\n")
    init_ok = "the propagation requirement"
    if "log" in kinds:
        init_ok += " and, if §4 asks, JS `enableLogs`"
    A("Do not, as part of this task: change sampling rates or `Sentry.init()` beyond "
      f"{init_ok}; instrument other journeys; create dashboards or alerts; "
      "modify or remove existing spans beyond the rename in §4; refactor unrelated code; "
      "add dependencies.\n")

    clar = rj.get("needs_clarification") or []
    if clar:
        A("## 9. Open questions\n")
        A("Resolve these before implementing. Do not guess.\n")
        for q in clar:
            A(f"- `[NEEDS CLARIFICATION]` {q}")
        A("")
    return "\n".join(L) + "\n"


def render_why(rj: dict, gj: dict, observed: dict | None = None) -> str:
    """The human-facing half. Rationales from the gap rules, verbatim — they were
    written to be said out loud."""
    L: list[str] = []
    A = L.append
    A(f"# Why this matters — {rj['name']}\n")
    A(f"Today this journey is **{gj['steps_instrumented']} of {gj['steps_total']} steps "
      f"instrumented**, scoring {gj['score']} ({gj['grade']}).\n")
    for cap in gj.get("caps") or []:
        A(f"> {cap}\n")

    if gj.get("dark_segments"):
        A("## Where you're blind right now\n")
        for seg in gj["dark_segments"]:
            A(f"The journey goes dark at **{' → '.join(seg)}**.\n")
        A("Everything downstream of that point is unattributable. Drop-off that actually "
          "happens in the dark segment gets recorded against the last instrumented step, "
          "so the team that owns the real problem never sees it.\n")

    for n in gj.get("notes") or []:
        A(f"- {n}")
    if gj.get("notes"):
        A("")

    A("## What each gap costs you\n")
    seen: set[str] = set()
    for f in gj["findings"]:
        if f["passed"] or f["rule"] in seen or f["rule"] not in ACTIONABLE:
            continue
        seen.add(f["rule"])
        A(f"**{f['description']}**\n")
        A(f"{f['rationale']}\n")
        if f.get("extent"):
            A(f"Extent: {f['extent']}\n")
        if f.get("example"):
            A(f"Example to open: `{f['example']}`\n")

    A("## What you'll be able to see once this lands\n")
    roles = rj["roles"]
    oc = (roles.get("outcome") or {}).get("attribute")
    mags = [m["attribute"] for m in (roles.get("magnitude") or [])]
    fr = (roles.get("failure_reason") or {}).get("attribute")
    segs = [s["attribute"] for s in (roles.get("actor_segment") or [])]
    if oc:
        A(f"- Completion rate for the whole journey, and drop-off per step, from `{oc}`.")
    if mags and fr:
        A(f"- Revenue at risk: `sum({mags[0]})` grouped by `{fr}` — how much money each "
          "failure mode is costing.")
    elif mags:
        A(f"- `sum({mags[0]})` and `p50({mags[0]})` per step, so the funnel is measured in "
          "money rather than requests.")
    if fr:
        A(f"- Which failure mode dominates, from `{fr}`, routed to the team that owns it.")
    if segs:
        A(f"- All of the above sliced by {', '.join('`' + s + '`' for s in segs)}.")
    A("- Alerts and dashboard widgets built directly from any of these queries via "
      "**Save As** in Trace Explorer.")
    companions = companion_signals(rj, gj, observed)
    for c in companions:
        A(f"- {c['why']}")
    A("")
    A("---\n")
    A("Every claim above is derived from measured gaps in your own Sentry org, not from a "
      "generic checklist. Rule definitions are in the gap analysis report.")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate customer-facing instrumentation specs.")
    p.add_argument("--resolved", required=True)
    p.add_argument("--gap", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--journey", action="append", default=[],
                   help="Limit to these journey ids (repeatable).")
    p.add_argument("--rubric", action="store_true",
                   help="Also emit <id>-RUBRIC.json, the machine-checkable form of the "
                        "spec, for eval/grade.py.")
    p.add_argument("--include-absent", action="store_true",
                   help="Also generate for journeys with zero instrumented steps. Off by "
                        "default: those need a kickoff conversation, not a gap-driven spec.")
    p.add_argument("--sdk", action="append", default=[],
                   choices=["auto", "javascript", "python"],
                   help="SDK family for §5/§6. Repeatable. Default: auto (from "
                        "--observed, else javascript).")
    p.add_argument("--observed",
                   help="observed.json from scan/local/snapshot. Used by --sdk auto "
                        "to pick javascript vs python, and by SIGNAL.md companions "
                        "for traces_sample_rate.")
    args = p.parse_args(argv)

    try:
        resolved = json.loads(Path(args.resolved).read_text())
        gapdoc = json.loads(Path(args.gap).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    observed = None
    if args.observed:
        try:
            observed = json.loads(Path(args.observed).read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"error: --observed: {exc}", file=sys.stderr)
            return 1
    sdks = detect_sdks(observed, args.sdk or ["auto"])

    by_id = {j["id"]: j for j in resolved["journeys"]}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for gj in gapdoc["journeys"]:
        rj = by_id.get(gj["id"])
        if rj is None:
            print(f"warning: {gj['id']} in gap report but not in resolved set", file=sys.stderr)
            continue
        if args.journey and gj["id"] not in args.journey:
            continue
        if gj["coverage_state"] == "absent" and not args.include_absent:
            print(f"skip {gj['id']}: not instrumented at all — needs a kickoff, not a "
                  "gap-driven spec (use --include-absent to override)", file=sys.stderr)
            continue
        actionable = [f for f in gj["findings"]
                      if not f["passed"] and f["rule"] in ACTIONABLE]
        if not actionable:
            print(f"skip {gj['id']}: no actionable gap — nothing to ask for", file=sys.stderr)
            continue

        spec_path = out_dir / f"{gj['id']}-SPEC.md"
        why_path = out_dir / f"{gj['id']}-WHY.md"
        obs = merge_observed(observed, gapdoc)
        spec_path.write_text(render_spec(rj, gj, gapdoc, sdks, obs))
        why_path.write_text(render_why(rj, gj, obs))
        written += [spec_path.name, why_path.name]
        reqs = build_requirements(rj, gj, obs)
        note = ""
        if args.rubric:
            rubric = build_rubric(rj, gj, gapdoc, obs)
            rubric_path = out_dir / f"{gj['id']}-RUBRIC.json"
            rubric_path.write_text(json.dumps(rubric, indent=2) + "\n")
            written.append(rubric_path.name)
            gradeable = sum(1 for r in rubric["requirements"] if r["gradeable"])
            note = (f", rubric with {gradeable}/{len(rubric['requirements'])} "
                    "gradeable")
        print(f"wrote {spec_path.name} ({len(reqs)} requirements) and "
              f"{why_path.name}{note}", file=sys.stderr)

    if not written:
        print("error: nothing to generate.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
