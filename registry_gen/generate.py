#!/usr/bin/env python3
"""
Weaver registry generator. Instantiates the grammar as OpenTelemetry semantic
conventions, one registry per customer.

This closes the last gap between the plan and the code: BUILD-PLAN said Layer 1
emits "a Weaver registry per customer journey", and until now the checkout
registry was hand-written.

The registry is GENERATED, not curated. It is the *implementation* layer in the
SLI-specification-vs-implementation split: `GRAMMAR.md` is the specification and
does not rot; this output is disposable and regenerated whenever the customer's
journey definition or stack changes.

Verified syntax constraints (weaver_semconv source + schemas/semconv.schema.json):
  - manifest file MUST be named `manifest.yaml`; `registry_manifest.yaml` is
    legacy and warns.
  - manifest `schema_url` is REQUIRED and of the form http[s]://host/path/<version>;
    name and version are DERIVED from it. Top-level `name`, `semconv_version` and
    `schema_base_url` are deprecated.
  - dependency sub-fields are `schema_url` (required) + `registry_path`. A
    dependency carrying only `name:` hard-fails.
  - attributes may only be DEFINED in `attribute_group`s whose id starts with
    `registry.` (prose convention in semantic-conventions/model/README.md).
  - the old `prefix:` field is REJECTED — write full attribute ids.
  - `stability` is required on every group type except `attribute_group`.
  - non-metric/event groups require `attributes` or `extends`.
  - span groups require `span_kind`.
  - a `ref` MUST NOT carry `id`, `type`, `stability`, or `deprecated`; it MAY
    override brief/note/examples/requirement_level/sampling_relevant/tag.
  - `requirement_level` conditional forms are MAPS, not strings.

Journey metadata (`journey`, `journey_step`, `impact`, …) is emitted inside each
span group's `note` rather than as top-level keys, because unknown top-level keys
fail validation. That is also how the hand-written exemplar carried it, so the
spec generator and coverage checker parse it the same way.

Usage:
    ./generate.py --resolved ../intake/example-resolved.json \\
                  --schema-url https://acme.example.com/schemas/checkout/0.1.0 \\
                  --out-dir out/

Exit codes:
    0  generated
    1  input error
    2  nothing to generate
    3  placeholders remain and --strict-examples was passed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

STABILITY = "development"
OTEL_SCHEMA_URL = "https://opentelemetry.io/schemas/1.44.0"
OTEL_REGISTRY_PATH = (
    "https://github.com/open-telemetry/semantic-conventions@v1.44.0[model]"
)

# Placeholder emitted when we have no real example value. `weaver registry check`
# requires `examples` on string attributes, so omitting them is not an option;
# inventing plausible-looking customer data is worse. Flag and report instead.
PLACEHOLDER = "REPLACE_WITH_A_REAL_EXAMPLE"

# surface -> span_kind. A default to review, not a derivation: nothing in the
# journey definition distinguishes an outbound call (`client`) from handling an
# inbound one (`server`).
SPAN_KIND = {
    "browser": "internal",
    "node": "server",
    "python": "server",
    "worker": "consumer",
    "queue": "consumer",
    "mobile": "internal",
    "ios": "internal",
    "android": "internal",
}

# surface -> default Sentry span op, from the documented op vocabulary.
DEFAULT_OP = {"browser": "ui.action.click", "worker": "queue.task", "queue": "queue.task"}
ROOT_OP = {"browser": "ui.action"}


def yaml_str(s: str) -> str:
    """Double-quote and escape a scalar. Avoids a PyYAML dependency in output."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def conditionally_required(text: str, indent: int) -> list[str]:
    """Emit a `requirement_level: {conditionally_required: ...}` map safely.

    The condition text contains backticks and punctuation, so it MUST go into a
    folded scalar. Emitting it as a bare multi-line mapping value produced
    invalid YAML — `found character '`' that cannot start any token`. Caught by
    registry_gen/validate.py, which is exactly what it is for.
    """
    pad = " " * indent
    return [f"{pad}conditionally_required: >-"] + block(text, indent + 2)


def block(text: str, indent: int) -> list[str]:
    pad = " " * indent
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 76:
            lines.append(pad + cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(pad + cur)
    return lines


def slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*", s))


# --------------------------------------------------------------------------
# Attribute definitions (the `registry.` attribute_group)
# --------------------------------------------------------------------------


def attribute_defs(j: dict) -> tuple[list[dict], list[str]]:
    """Grammar roles -> attribute definitions. Returns (attrs, placeholders)."""
    roles = j["roles"]
    attrs: list[dict] = []
    placeholders: list[str] = []

    def add(key: str, typ: Any, brief: str, note: str = "",
            examples: list[Any] | None = None) -> None:
        a: dict[str, Any] = {"id": key, "type": typ, "brief": brief}
        if note:
            a["note"] = note
        if examples is not None:
            a["examples"] = examples
        attrs.append(a)

    corr = roles.get("correlation_key") or {}
    if corr.get("attribute"):
        persists = ", ".join(corr.get("persists_across") or []) or "the journey"
        placeholders.append(corr["attribute"])
        add(corr["attribute"], "string",
            "Unique identifier for one journey instance. Carried on every span in "
            "the journey.",
            f"Journey correlation key. MUST persist across: {persists}. Not the "
            "trace ID — a browser navigation starts a new trace, so the trace ID "
            "cannot stitch a multi-page journey.",
            [PLACEHOLDER])

    steps = roles.get("steps") or []
    if steps:
        add(f"{j['id']}.step",
            {"members": [
                {"id": s["id"], "value": s["id"], "stability": STABILITY,
                 "brief": f"Step {i + 1} of the journey."}
                for i, s in enumerate(steps)
            ]},
            "The journey step this span represents.",
            "Enumerable and ordered — this enum is what makes drop-off computable.")

    oc = roles.get("outcome") or {}
    if oc.get("attribute") and oc.get("values"):
        succ = set(oc.get("success_values") or [])
        add(oc["attribute"],
            {"members": [
                {"id": v, "value": v, "stability": STABILITY,
                 "brief": ("Success." if v in succ else "Non-success terminal state.")}
                for v in oc["values"]
            ]},
            "Terminal outcome of the journey instance.",
            "A string enum, deliberately not a boolean: Sentry surfaces boolean "
            "attributes as the strings 'true'/'false', and a binary collapses "
            "distinct non-success states that route to different teams."
            + (f" Initialise to `{oc['default_value']}` at journey start and "
               "overwrite on a terminal event, so an abandoned journey is "
               "distinguishable from an uninstrumented one."
               if oc.get("default_value") else ""))

    fr = roles.get("failure_reason") or {}
    if fr.get("attribute"):
        known = fr.get("known_values") or []
        if not known:
            placeholders.append(fr["attribute"])
        add(fr["attribute"], "string",
            "Coded reason for a non-success outcome.",
            "MUST be a coded reason, never a free-text message and never a raw "
            "upstream payload. Keep cardinality low enough to be useful in a "
            "Group By.",
            known or [PLACEHOLDER])

    for m in roles.get("magnitude") or []:
        typ = m.get("type") if m.get("type") in ("int", "double") else "double"
        unit = f" Unit: {m['unit']}." if m.get("unit") else ""
        add(m["attribute"], typ,
            "Business measure attached at the value-bearing step.",
            "Numeric so it is chartable in Trace Explorer with no setup "
            f"(`p50()`, `sum()`). A stringified value cannot be aggregated.{unit}",
            [1, 2] if typ == "int" else [1.0, 2.5])

    for s in roles.get("actor_segment") or []:
        placeholders.append(s["attribute"])
        add(s["attribute"], "string",
            "Segmentation axis for slicing the journey.",
            "Turns 'the journey is slow' into 'the journey is slow for this "
            "segment'."
            + (" Already available in the auth or tenancy layer."
               if s.get("already_available") else ""),
            [PLACEHOLDER])

    return attrs, placeholders


def render_attribute_group(j: dict, attrs: list[dict]) -> str:
    L = [
        "# GENERATED by registry_gen/generate.py — do not edit by hand.",
        f"# Journey: {j['name']} ({j['id']})",
        "#",
        "# Attributes may only be DEFINED in an `attribute_group` whose id starts",
        "# with `registry.`. The old `prefix:` field is rejected, so ids are full.",
        "# `stability` is not required on an attribute_group.",
        "",
        "groups:",
        f"  - id: registry.{j['id']}",
        "    type: attribute_group",
        f"    brief: Attributes describing the {j['name']} critical experience.",
        "    attributes:",
    ]
    for a in attrs:
        L.append(f"      - id: {a['id']}")
        if isinstance(a["type"], dict):
            L.append("        type:")
            L.append("          members:")
            for m in a["type"]["members"]:
                L.append(f"            - id: {m['id']}")
                L.append(f"              value: {yaml_str(str(m['value']))}")
                L.append(f"              stability: {m['stability']}")
                L.append(f"              brief: {yaml_str(m['brief'])}")
        else:
            L.append(f"        type: {a['type']}")
        L.append(f"        stability: {STABILITY}")
        L.append("        brief: >-")
        L += block(a["brief"], 10)
        if a.get("note"):
            L.append("        note: >-")
            L += block(a["note"], 10)
        if a.get("examples") is not None:
            vals = ", ".join(
                yaml_str(v) if isinstance(v, str) else str(v) for v in a["examples"]
            )
            L.append(f"        examples: [{vals}]")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# Span groups
# --------------------------------------------------------------------------


def expected_span_name(journey_id: str, step: dict, is_first: bool) -> str:
    if step.get("span_name"):
        return step["span_name"]
    return journey_id if is_first else f"{journey_id}.{step['id']}"


def render_span_groups(j: dict) -> str:
    roles = j["roles"]
    steps = roles.get("steps") or []
    corr = (roles.get("correlation_key") or {}).get("attribute")
    oc = roles.get("outcome") or {}
    fr = (roles.get("failure_reason") or {}).get("attribute")
    mags = roles.get("magnitude") or []
    segs = roles.get("actor_segment") or []
    succ = set(oc.get("success_values") or [])
    non_success = [v for v in (oc.get("values") or []) if v not in succ]

    L = [
        "# GENERATED by registry_gen/generate.py — do not edit by hand.",
        f"# Journey: {j['name']} ({j['id']})",
        "#",
        "# IMPORTANT: `requirement_level` on a SPAN group is documentation only.",
        "# `weaver registry live-check` is sample-driven and has no span lookup —",
        "# it will never report a missing span or a missing span attribute. Journey",
        "# completeness is enforced by gap/analyze.py against Sentry. See",
        "# BUILD-PLAN.md §0.",
        "#",
        "# span_kind and the `sentry_op` hints below are DEFAULTS derived from each",
        "# step's `surface`. Nothing in a journey definition distinguishes an",
        "# outbound call (client) from handling an inbound one (server) — review.",
        "",
        "groups:",
    ]

    for i, s in enumerate(steps):
        first, last = i == 0, i == len(steps) - 1
        surface = (s.get("surface") or "").lower()
        name = expected_span_name(j["id"], s, first)
        kind = SPAN_KIND.get(surface, "internal")
        op = (ROOT_OP.get(surface) if first else None) or DEFAULT_OP.get(surface, "function")
        impact = s.get("impact", "normal")
        role = "root" if first else ("terminal" if last else "step")

        L.append("")
        L.append(f"  - id: span.{j['id']}.{s['id']}")
        L.append("    type: span")
        L.append(f"    span_kind: {kind}")
        L.append(f"    stability: {STABILITY}")
        L.append("    brief: >-")
        L += block(f"Step {i + 1} of the {j['name']} journey: {s['id']}.", 6)
        L.append("    note: |")
        L.append(f"      journey: {j['id']}")
        L.append(f"      journey_step: {i + 1}")
        L.append(f"      journey_role: {role}")
        if surface:
            L.append(f"      surface: {surface}")
        L.append(f"      sentry_op: {op}")
        L.append(f"      sentry_span_name: {yaml_str(name)}")
        L.append(f"      impact: {impact}")
        if s.get("evidence"):
            L.append(f"      evidence: {yaml_str(s['evidence'])}")
        L.append("    attributes:")

        if corr:
            L.append(f"      - ref: {corr}")
            L.append("        requirement_level: required")
        L.append(f"      - ref: {j['id']}.step")
        L.append("        requirement_level: required")
        L.append(f"        note: MUST be `{s['id']}` on this span.")

        if first:
            if oc.get("attribute"):
                L.append(f"      - ref: {oc['attribute']}")
                L.append("        requirement_level:")
                L += conditionally_required(
                    "Required before this span ends."
                    + (f" Initialise to `{oc['default_value']}` and overwrite on a "
                       "terminal event." if oc.get("default_value") else ""), 10)
            for m in mags:
                if not m.get("step") or m.get("step") == s["id"]:
                    L.append(f"      - ref: {m['attribute']}")
                    L.append("        requirement_level: required")
            for sg in segs:
                L.append(f"      - ref: {sg['attribute']}")
                L.append("        requirement_level: recommended")
        else:
            for m in mags:
                if m.get("step") == s["id"]:
                    L.append(f"      - ref: {m['attribute']}")
                    L.append("        requirement_level: required")

        if fr and non_success:
            L.append(f"      - ref: {fr}")
            L.append("        requirement_level:")
            L += conditionally_required(
                "If the journey outcome is one of: "
                + ", ".join(sorted(non_success)) + ".", 10)

    return "\n".join(L) + "\n"


def render_manifest(schema_url: str, journeys: list[dict]) -> str:
    names = ", ".join(f"{j['name']} ({j['id']})" for j in journeys)
    L = [
        "# GENERATED by registry_gen/generate.py — do not edit by hand.",
        "#",
        "# File MUST be named `manifest.yaml` (`registry_manifest.yaml` is legacy",
        "# and warns). `schema_url` is required; registry name and version are",
        "# DERIVED from it. Top-level `name`, `semconv_version` and",
        "# `schema_base_url` are deprecated. A dependency needs `schema_url` —",
        "# one carrying only `name:` hard-fails.",
        "#",
        "# Validate:  weaver registry check --future -r .",
        "# Generate:  weaver registry generate --future -r . markdown ./docs",
        "# Coverage:  weaver registry stats --future -r . --format json",
        "",
        f"schema_url: {schema_url}",
        "",
        "description: >-",
    ]
    L += block(f"Critical Experience registry. Journeys: {names}. Generated from the "
               "grammar in GRAMMAR.md — regenerate rather than edit.", 2)
    L += [
        "",
        f"stability: {STABILITY}",
        "",
        "dependencies:",
        f"  - schema_url: {OTEL_SCHEMA_URL}",
        f"    registry_path: {OTEL_REGISTRY_PATH}",
    ]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate a Weaver registry per journey.")
    p.add_argument("--resolved", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--schema-url",
                   default="https://sentry-se.example.com/schemas/critical-experience/0.1.0")
    p.add_argument("--journey", action="append", default=[])
    p.add_argument("--strict-examples", action="store_true",
                   help="Exit 3 if any attribute still carries a placeholder example. "
                        "`weaver registry check` wants real examples on string "
                        "attributes, and inventing customer data is worse than "
                        "flagging it.")
    args = p.parse_args(argv)

    try:
        resolved = json.loads(Path(args.resolved).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out_dir)
    (out / "registry").mkdir(parents=True, exist_ok=True)
    (out / "spans").mkdir(parents=True, exist_ok=True)

    emitted: list[dict] = []
    all_placeholders: list[tuple[str, str]] = []

    for j in resolved.get("journeys", []):
        if j.get("excluded"):
            continue
        if args.journey and j["id"] not in args.journey:
            continue
        if not (j.get("roles") or {}).get("steps"):
            print(f"skip {j['id']}: no steps", file=sys.stderr)
            continue
        if not slug_ok(j["id"]):
            print(f"skip {j['id']}: id is not a valid attribute namespace", file=sys.stderr)
            continue

        attrs, placeholders = attribute_defs(j)
        if not attrs:
            print(f"skip {j['id']}: no roles filled", file=sys.stderr)
            continue

        (out / "registry" / f"{j['id']}.yaml").write_text(render_attribute_group(j, attrs))
        (out / "spans" / f"{j['id']}.yaml").write_text(render_span_groups(j))
        emitted.append(j)
        all_placeholders += [(j["id"], k) for k in placeholders]
        print(f"wrote registry/{j['id']}.yaml ({len(attrs)} attributes) and "
              f"spans/{j['id']}.yaml ({len(j['roles']['steps'])} span groups)",
              file=sys.stderr)

    if not emitted:
        print("error: nothing to generate.", file=sys.stderr)
        return 2

    (out / "manifest.yaml").write_text(render_manifest(args.schema_url, emitted))
    print(f"wrote manifest.yaml for {len(emitted)} journey(s)", file=sys.stderr)

    if all_placeholders:
        print(f"\n{len(all_placeholders)} attribute(s) need a real `examples` value "
              "before `weaver registry check` will be happy:", file=sys.stderr)
        for jid, key in all_placeholders:
            print(f"  {jid}: {key}", file=sys.stderr)
        if args.strict_examples:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
