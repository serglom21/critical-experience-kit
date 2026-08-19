#!/usr/bin/env python3
"""
Instrumentation profile: classify what a service is actually sending as
AUTOMATIC vs CUSTOM, and turn the split into recommendations.

Two signals do the work, and both come from data we already collect:

  1. `attributeSource.source_type` from GET /trace-items/attributes/ —
     `sentry` means the SDK produced it, `user` means a human wrote it. This is
     a first-class, documented field, not a heuristic.

  2. Span `op` families. Sentry's documented op vocabulary is almost entirely
     auto-instrumentation territory (`http.server`, `db.query`, `resource.*`,
     `ui.react.mount`, `queue.task.*`). A custom business span is either `op:
     function` / `ui.action` with a domain-specific name, or carries no op.

Why this matters more than a score: it locates the customer on the layer they
have and the layer they're missing. Grafana states the boundary plainly —
"auto-instrumentation can only capture technical insights, like status codes or
durations; it cannot determine the intent of the instrumented services." A
service can be richly auto-instrumented and completely blind to its own funnel.

Usage:
    ./instrumentation_profile.py --observed fixtures/observed-customer.example.json
    ./instrumentation_profile.py --observed observed.json --gap gap.json --out-md profile.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Documented Sentry span operations, grouped by the SDK integration that emits
# them. Presence of a family is evidence that integration is active.
# Source: https://develop.sentry.dev/sdk/telemetry/traces/span-operations/
AUTO_FAMILIES: dict[str, tuple[str, ...]] = {
    "HTTP server (inbound requests)": ("http.server",),
    "HTTP client (outbound calls)": ("http.client",),
    "Database": ("db", "db.query", "db.sql.query", "db.redis"),
    "Cache": ("cache.get_item", "cache.put_item", "cache.remove_item", "cache.flush"),
    "Browser page lifecycle": ("pageload", "navigation"),
    "Browser resources": ("resource",),
    "Browser timing": ("browser", "measure", "mark", "paint"),
    "Web vitals": ("ui.webvital",),
    "UI rendering": ("ui.react", "ui.render", "ui.update", "ui.vue", "ui.svelte",
                     "ui.angular", "ui.long-animation-frame"),
    "Code-level tracing": ("code.block",),
    "Unset / default": ("default",),
    "Framework middleware": ("middleware", "middleware.nextjs", "middleware.express",
                             "middleware.django"),
    "Template rendering": ("template.render",),
    "Queues / background jobs": ("queue.task", "queue.task.celery", "queue.publish",
                                 "queue.process", "topic.send", "topic.receive"),
    "GraphQL": ("graphql.execute", "graphql.parse", "graphql.resolve", "graphql.validate"),
    "File I/O": ("file.read", "file.write", "file.copy", "file.delete"),
    "Serverless": ("function.aws", "function.gcp", "function.azure", "function.nextjs"),
    "Serialization": ("serialize",),
    "AI / LLM": ("gen_ai.chat", "gen_ai.invoke_agent", "ai.run", "ai.pipeline"),
}

# Ops a human typically chooses for business instrumentation. Their presence is
# necessary-but-not-sufficient evidence of custom work: `op: function` also shows
# up from some auto-instrumentation, so we cross-check against span naming.
CUSTOM_OPS = ("function", "ui.action", "ui.action.click", "ui.action.swipe",
              "ui.action.scroll", "ui.load", "")

# Attribute namespaces Sentry owns. A `user`-sourced attribute inside one of
# these is a collision risk worth flagging.
SENTRY_NAMESPACES = ("sentry.", "http.", "db.", "server.", "client.", "url.",
                     "user_agent.", "network.", "cache.", "messaging.", "gen_ai.",
                     "browser.", "device.", "os.", "process.", "thread.", "code.",
                     "span.", "trace.", "resource.", "faas.", "cloud.", "k8s.")


def family_for_op(op: str) -> str | None:
    """Longest-prefix match against the op vocabulary.

    Prefix rather than exact: real orgs emit `browser.DNS`, `browser.TLS/SSL`,
    `ui.webvital.cls`, `ui.react.update`, `db.redis` — dozens of leaf ops per
    family. Exact matching silently dumped all of them into "unclassified",
    which then read as a finding when it was just an incomplete table.
    """
    best: tuple[int, str] | None = None
    for fam, prefixes in AUTO_FAMILIES.items():
        for pre in prefixes:
            if op == pre or op.startswith(pre + "."):
                if best is None or len(pre) > best[0]:
                    best = (len(pre), fam)
    return best[1] if best else None


# A span whose description looks like a code location is SDK-derived function
# tracing or a manual code-level span — real instrumentation, but NOT business
# instrumentation. `src.db.get_products` and
# `UIKit.NavigationBarContentView.__backButtonAction` are not journey steps;
# `items_added_to_cart` and `processCheckout` are.
#
# This one IS a heuristic, unlike source_type and op families. Labelled as such
# everywhere it surfaces, and it only ever downgrades confidence — never hides a
# span.
def looks_like_code_location(name: str) -> bool:
    if not name or name in ("<unknown>",):
        return True
    if "::" in name or "(" in name or "/" in name:
        return True
    parts = name.split(".")
    if len(parts) >= 3:
        return True                      # src.db.get_products
    if len(parts) == 2 and parts[0] in ("src", "app", "lib", "main", "api", "internal"):
        return True
    if name.startswith("_") or "__" in name:
        return True
    if len(parts) == 2 and parts[0][:1].isupper() and parts[1][:1].islower():
        return True                      # UIKit.invalidateAssistant
    return False


@dataclass
class Profile:
    org: str
    stats_period: str
    projects: list[str] = field(default_factory=list)

    auto_families: dict[str, int] = field(default_factory=dict)   # family -> span count
    auto_attributes: list[str] = field(default_factory=list)
    custom_attributes: list[str] = field(default_factory=list)
    custom_spans: list[tuple[str, int]] = field(default_factory=list)  # (name, count)
    code_level_spans: list[tuple[str, int]] = field(default_factory=list)
    unclassified_ops: dict[str, int] = field(default_factory=dict)
    namespace_collisions: list[str] = field(default_factory=list)
    total_spans: int = 0

    # ---- derived ---------------------------------------------------------

    @property
    def custom_span_volume(self) -> int:
        return sum(c for _, c in self.custom_spans)

    @property
    def custom_share(self) -> float:
        """Share of span volume that is custom business instrumentation.
        Expected to be tiny even in healthy orgs — Heap reports roughly 10% of
        the events in their own reports are manually tagged. Low is normal; ZERO
        is the finding."""
        return (self.custom_span_volume / self.total_spans) if self.total_spans else 0.0

    @property
    def tier(self) -> str:
        """Where the customer sits on the automatic → custom ladder."""
        if not self.auto_families and not self.custom_spans:
            return "none"
        if not self.custom_spans and not self.custom_attributes:
            return "automatic only"
        if self.custom_attributes and not self.custom_spans:
            return "attributes without journey spans"
        if self.custom_spans and not self.custom_attributes:
            return "spans without business attributes"
        return "custom instrumentation present"

    @property
    def headline(self) -> str:
        return {
            "none": "No recognisable instrumentation in the window. Confirm the SDK is "
                    "installed and sending, and that the window and projects are right.",
            "automatic only": "Fully on the syntactic layer. You can see status codes and "
                              "durations, and nothing about business intent. Auto-"
                              "instrumentation cannot determine what a request meant.",
            "attributes without journey spans": "Business attributes are already being "
                                                "sent, but there are no journey spans to "
                                                "hang them on. The plumbing exists — this "
                                                "is the cheapest possible starting point.",
            "spans without business attributes": "Custom spans exist but carry no business "
                                                 "values, so the funnel is measured in "
                                                 "requests rather than outcomes.",
            "custom instrumentation present": "Both layers are in place. The work is "
                                              "completeness and correctness, not adoption.",
        }[self.tier]


def classify(observed: dict) -> Profile:
    p = Profile(
        org=observed.get("org", "unknown"),
        stats_period=observed.get("stats_period", "unknown"),
        projects=observed.get("projects") or [],
    )

    # --- span ops (preferred signal when present) ---
    for row in observed.get("span_ops") or []:
        op, count = row.get("op") or "", int(row.get("count") or 0)
        p.total_spans += count
        fam = family_for_op(op)
        if fam:
            p.auto_families[fam] = p.auto_families.get(fam, 0) + count
        elif op in CUSTOM_OPS:
            pass  # attributed via span names below
        else:
            p.unclassified_ops[op] = p.unclassified_ops.get(op, 0) + count

    # --- span names ---
    # `span_pairs` (name + its op) is the strong signal and is preferred when
    # present. Without it, `SELECT * FROM carts` and `POST /cart` had to be judged
    # on their names alone — and the SQL string read as a custom business span,
    # which flipped an "automatic only" org into "spans without business
    # attributes". That is the single most load-bearing line in the report for an
    # uninstrumented service, so the op decides it when we know the op.
    pairs = observed.get("span_pairs") or []
    name_op = {row.get("name"): (row.get("op") or "") for row in pairs}
    have_ops = bool(observed.get("span_ops"))

    for row in observed.get("span_names") or []:
        name, count = row.get("name") or "", int(row.get("count") or 0)
        if not have_ops:
            p.total_spans += count

        op = name_op.get(name)
        fam = family_for_op(op) if op else None      # op first, it is authoritative
        if fam is None:
            fam = family_for_op(name)                # then the name as a fallback
        if fam:
            if not have_ops:
                p.auto_families[fam] = p.auto_families.get(fam, 0) + count
            continue

        if looks_like_code_location(name):
            p.code_level_spans.append((name, count))
        else:
            p.custom_spans.append((name, count))
    p.custom_spans.sort(key=lambda x: -x[1])
    p.code_level_spans.sort(key=lambda x: -x[1])

    # --- attributes ---
    for a in observed.get("attributes") or []:
        key = a.get("key") or ""
        src = (a.get("attributeSource") or {}).get("source_type")
        if src == "user":
            p.custom_attributes.append(key)
            if key.startswith(SENTRY_NAMESPACES):
                p.namespace_collisions.append(key)
        else:
            p.auto_attributes.append(key)
    p.custom_attributes.sort()
    p.auto_attributes.sort()
    return p


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------


def recommend(p: Profile, gapdoc: dict | None = None) -> list[dict]:
    """Profile plus gaps into a ranked, specific set of recommendations.

    Ordered by what to raise first on a call, not by severity.
    """
    recs: list[dict] = []

    def add(title: str, why: str, ask: str, priority: str = "normal") -> None:
        recs.append({"priority": priority, "title": title, "why": why, "ask": ask})

    if p.tier == "automatic only":
        fams = ", ".join(sorted(p.auto_families)[:4])
        add("Start with one journey, five attributes",
            f"Auto-instrumentation is healthy — {len(p.auto_families)} integration "
            f"families are firing ({fams}). None of it says what a request meant. "
            f"{len(p.auto_attributes)} attributes are SDK-provided and 0 are yours.",
            "Pick the single highest-value journey and add a correlation key, a step "
            "marker, an outcome, a coded failure reason, and one numeric magnitude. "
            "Five attributes, not a rewrite.",
            "critical")

    if p.tier == "attributes without journey spans":
        add("The plumbing already exists",
            f"{len(p.custom_attributes)} customer-defined attributes are being sent "
            f"({', '.join(p.custom_attributes[:4])}…) but no custom spans carry them. "
            "Somebody already did the hard part — deciding what matters.",
            "Add the journey spans and attach the attributes you already emit. No new "
            "domain modelling required.",
            "critical")

    if p.tier == "spans without business attributes":
        add("Custom spans with no business payload",
            f"{len(p.custom_spans)} custom span names are firing with no customer-defined "
            "attributes, so the funnel is counted in requests rather than outcomes.",
            "Attach an outcome enum and one numeric magnitude to the spans you already "
            "create. Numeric span attributes chart in Trace Explorer with no setup.",
            "critical")

    if p.namespace_collisions:
        add("Customer attributes inside Sentry-owned namespaces",
            "These keys sit in namespaces Sentry owns, so product semantics can diverge "
            f"from yours without warning: {', '.join(p.namespace_collisions[:5])}.",
            "Move them into a namespace you control. Sentry's convention requires "
            "namespacing, and its registry grows.",
            "important")

    if p.unclassified_ops:
        top = sorted(p.unclassified_ops.items(), key=lambda x: -x[1])[:3]
        add("Unrecognised span operations",
            "These ops are not in Sentry's documented vocabulary: "
            + ", ".join(f"`{o}` ({c:,})" for o, c in top)
            + ". Unknown ops default to `default` and lose product features.",
            "Map each to a documented op, or confirm it is deliberate.",
            "normal")

    if gapdoc:
        js = gapdoc.get("journeys") or []
        partial = [j for j in js if j.get("coverage_state") == "partial"]
        absent = [j for j in js if j.get("coverage_state") == "absent"]

        for j in partial:
            drift = [f for f in j["findings"] if f["rule"] == "CE-013" and not f["passed"]]
            if drift:
                add(f"{j['name']}: one rename, not new instrumentation",
                    drift[0]["detail"] + ". The step is already emitting; every query "
                    "against the bound name returns empty.",
                    "Rename the span. Cheapest fix in the whole engagement.",
                    "critical")
            if j.get("dark_segments"):
                for seg in j["dark_segments"]:
                    add(f"{j['name']}: funnel goes dark at {' → '.join(seg)}",
                        "Drop-off from these steps is being attributed to the last "
                        "instrumented step, so the owning team never sees it.",
                        "Instrument the missing steps before adding any new journey.",
                        "critical")
            typed = [f for f in j["findings"]
                     if f["rule"] in ("CE-007", "CE-010") and not f["passed"]]
            for f in typed:
                add(f"{j['name']}: {f['description'].lower()}",
                    f["rationale"], f["detail"] + " — fix the type.", "important")

        cheap = [j for j in absent
                 if any(f["rule"] == "CE-004" and f["passed"] for f in j["findings"])]
        if cheap:
            add("Declared journeys whose correlation key already exists",
                "The correlation key is present for: "
                + ", ".join(j["name"] for j in cheap)
                + ". Only the spans are missing.",
                "Sequence these next — they are the shortest path to a working funnel.",
                "important")

    order = {"critical": 0, "important": 1, "normal": 2}
    return sorted(recs, key=lambda r: order[r["priority"]])


# --------------------------------------------------------------------------


def render_markdown(p: Profile, recs: list[dict]) -> str:
    L: list[str] = []
    A = L.append
    A(f"# Instrumentation profile — `{p.org}`\n")
    scope = ", ".join(p.projects) if p.projects else "all projects"
    A(f"Window: {p.stats_period} · scope: {scope}\n")
    A(f"## {p.tier.title()}\n")
    A(f"{p.headline}\n")

    A("| Layer | What we found |")
    A("| --- | --- |")
    A(f"| Automatic (SDK-provided) | {len(p.auto_families)} integration families · "
      f"{len(p.auto_attributes)} attributes |")
    A(f"| Custom business | {len(p.custom_spans)} span names · "
      f"{len(p.custom_attributes)} attributes |")
    if p.code_level_spans:
        A(f"| Code-level (not business) | {len(p.code_level_spans)} span names |")
    if p.total_spans:
        A(f"| Custom share of span volume | {p.custom_share:.2%} |")
    A("")
    if p.total_spans:
        A("> A low custom share is normal, not a failure — Heap reports roughly 10% of the "
          "events in their own reports are manually tagged. **Zero** is the finding.\n")

    if p.auto_families:
        A("### Automatic instrumentation detected\n")
        A("| Integration family | Span volume |")
        A("| --- | --- |")
        for fam, c in sorted(p.auto_families.items(), key=lambda x: -x[1]):
            A(f"| {fam} | {c:,} |")
        A("")
        A("This is the syntactic layer: status codes, durations, query shapes. It cannot "
          "tell you what a request *meant* — that boundary is the entire case for custom "
          "instrumentation.\n")

    if p.custom_spans:
        A("### Custom business spans\n")
        A("| Span name | Volume |")
        A("| --- | --- |")
        for name, c in p.custom_spans[:15]:
            A(f"| `{name}` | {c:,} |")
        A("")

    if p.code_level_spans:
        A("### Code-level spans (not business instrumentation)\n")
        A("Real spans, but they name a code location rather than a business step, so they "
          "answer *where time went* and not *what the user was trying to do*. Mostly "
          "SDK-derived function tracing.\n")
        A("| Span name | Volume |")
        A("| --- | --- |")
        for name, c in p.code_level_spans[:10]:
            A(f"| `{name}` | {c:,} |")
        A("")
        A("> This split is the one **heuristic** in the profile — pattern-matching on the "
          "span description. `source_type` and op families are authoritative; this is not. "
          "Check the table before quoting it.\n")

    if p.custom_attributes:
        A("### Customer-defined attributes\n")
        A(", ".join(f"`{k}`" for k in p.custom_attributes[:30]) + "\n")
        A("Identified by `attributeSource.source_type == \"user\"` — a documented field, "
          "not a guess.\n")
    else:
        A("### Customer-defined attributes\n")
        A("**None.** Every attribute in the window was SDK-provided.\n")

    if p.namespace_collisions:
        A("### Namespace collisions\n")
        A(", ".join(f"`{k}`" for k in p.namespace_collisions) + "\n")

    if recs:
        A("## Recommendations\n")
        for i, r in enumerate(recs, 1):
            A(f"### {i}. {r['title']}  ·  _{r['priority']}_\n")
            A(f"{r['why']}\n")
            A(f"**Ask:** {r['ask']}\n")

    A("---\n")
    A("Automatic vs custom is derived from two signals: `attributeSource.source_type` on "
      "`GET /trace-items/attributes/`, and span `op` families from Sentry's documented "
      "operation vocabulary. Neither is inferred from naming conventions alone.")
    return "\n".join(L) + "\n"


def to_json(p: Profile, recs: list[dict]) -> dict:
    return {
        "version": 1,
        "org": p.org,
        "projects": p.projects,
        "stats_period": p.stats_period,
        "tier": p.tier,
        "headline": p.headline,
        "automatic": {
            "families": p.auto_families,
            "attribute_count": len(p.auto_attributes),
        },
        "custom": {
            "spans": [{"name": n, "count": c} for n, c in p.custom_spans],
            "attributes": p.custom_attributes,
            "share_of_span_volume": round(p.custom_share, 4),
        },
        "unclassified_ops": p.unclassified_ops,
        "namespace_collisions": p.namespace_collisions,
        "recommendations": recs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify instrumentation as automatic vs custom.")
    ap.add_argument("--observed", required=True)
    ap.add_argument("--gap", help="Optional gap.json, to fold journey findings into the asks.")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    args = ap.parse_args(argv)

    try:
        observed = json.loads(Path(args.observed).read_text())
        gapdoc = json.loads(Path(args.gap).read_text()) if args.gap else None
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    p = classify(observed)
    recs = recommend(p, gapdoc)
    md = render_markdown(p, recs)

    if args.out_md:
        Path(args.out_md).write_text(md)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(to_json(p, recs), indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(md)

    print(f"tier: {p.tier} · {len(p.auto_families)} auto families · "
          f"{len(p.custom_spans)} custom spans · {len(p.custom_attributes)} custom attrs · "
          f"{len(recs)} recommendations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
