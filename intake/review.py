#!/usr/bin/env python3
"""
Interactive review of proposed journeys.

`ce discover` cannot set `business_impact` or decide which candidates matter —
those are human-owned. Editing YAML to express that was the friction: people
skipped it, then `ce report` spec'd junk like a `web` directory.

This stage writes the same `journeys.yaml` a hand-edit would, via a local HTML
page (stdlib http.server, bind 127.0.0.1 only) or `--apply` JSON for tests and
non-browser environments. The page asks an engineer to pick 2–3 customer
flows and set how painful an outage is. Evidence sits above a Today/After
sketch that shows why an error on a lone request is hard to debug, and what
the spec adds (one root, a correlation key, ordered steps). Illustrated
errors are labelled as such — never a fabricated incident, duration, or
frequency. It never infers impact and never emits application code.

Usage:
    ce review                      # open localhost, POST writes yaml + .reviewed
    ce review --apply decisions.json
    ce review --stamp              # yaml already edited by hand; just gate report
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# propose.py refuses these as domains, but a catch-all like `web` still slips
# through when `src/web/` has routes. Default-drop in the UI, never silently.
_GAP = Path(__file__).resolve().parent.parent / "gap"
if str(_GAP) not in sys.path:
    sys.path.insert(0, str(_GAP))
try:
    from propose import NOT_A_DOMAIN as _NOT_A_DOMAIN
except ImportError:
    _NOT_A_DOMAIN = set()

LIKELY_FALSE_POSITIVE = set(_NOT_A_DOMAIN) | {
    "web", "www", "frontend", "backend",
}

IMPACT = ("critical", "important", "normal")
REVIEWED_NAME = ".reviewed"


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("error: PyYAML is required to review journeys.yaml")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"error: cannot parse {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("journeys"), list):
        sys.exit(f"error: {path} must be an object with a 'journeys' list")
    return data


def suggest_drop(journey: dict) -> bool:
    jid = (journey.get("id") or "").lower()
    return jid in LIKELY_FALSE_POSITIVE


def dump_journeys(doc: dict) -> str:
    """Write journeys.yaml. Comments explain the human-owned fields; values
    themselves come only from review / hand-edit, never from volume."""
    def q(s: Any) -> str:
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

    L = [
        "# Reviewed. `business_impact` and keep/drop are human-assigned.",
        "# `ce report` will not run until `ce review` (or `ce review --stamp`).",
        "",
        "version: 1",
        "",
        "journeys:",
    ]
    for j in doc.get("journeys") or []:
        L.append(f"  - id: {j['id']}")
        L.append(f"    name: {q(j.get('name') or j['id'])}")
        L.append(f"    source: {j.get('source') or 'discovered:code'}")
        if j.get("confidence"):
            L.append(f"    confidence: {j['confidence']}")
        if j.get("excluded"):
            L.append("    excluded: true")
            if j.get("excluded_reason"):
                L.append(f"    excluded_reason: {q(j['excluded_reason'])}")
        if j.get("business_impact"):
            L.append(f"    business_impact: {j['business_impact']}")
        if j.get("notes"):
            L.append(f"    notes: {q(j['notes'])}")
        ck = j.get("correlation_key") or {}
        if ck.get("attribute"):
            L.append("    correlation_key:")
            L.append(f"      attribute: {ck['attribute']}")
            if ck.get("persists_across"):
                L.append("      persists_across: ["
                         + ", ".join(ck["persists_across"]) + "]")
        steps = j.get("steps") or []
        if steps:
            L.append("    steps:")
            for s in steps:
                L.append(f"      - id: {s['id']}")
                if s.get("span_name"):
                    L.append(f"        span_name: {s['span_name']}")
                if s.get("surface"):
                    L.append(f"        surface: {s['surface']}")
                if s.get("impact"):
                    L.append(f"        impact: {s['impact']}")
                if s.get("evidence"):
                    L.append(f"        evidence: {q(s['evidence'])}")
        oc = j.get("outcome")
        if isinstance(oc, dict) and oc.get("attribute") and oc.get("values"):
            L.append("    outcome:")
            L.append(f"      attribute: {oc['attribute']}")
            L.append("      values: [" + ", ".join(str(v) for v in oc["values"]) + "]")
            if oc.get("success_values"):
                L.append("      success_values: ["
                         + ", ".join(str(v) for v in oc["success_values"]) + "]")
            if oc.get("default_value"):
                L.append(f"      default_value: {oc['default_value']}")
        if j.get("magnitude"):
            L.append("    magnitude:")
            for m in j["magnitude"]:
                L.append(f"      - attribute: {m['attribute']}")
                L.append(f"        type: {m['type']}")
                if m.get("unit"):
                    L.append(f"        unit: {m['unit']}")
                if m.get("step"):
                    L.append(f"        step: {m['step']}")
        if j.get("actor_segment"):
            L.append("    actor_segment:")
            for s in j["actor_segment"]:
                L.append(f"      - attribute: {s['attribute']}")
                if s.get("already_available"):
                    L.append("        already_available: true")
        if j.get("needs_clarification"):
            L.append("    needs_clarification:")
            for q_ in j["needs_clarification"]:
                L.append(f"      - {q(q_)}")
        L.append("")
    return "\n".join(L) + "\n"


def apply_decisions(doc: dict, decisions: dict) -> dict:
    """Mutate journeys from a review payload. Does not invent impact."""
    by_id = {j["id"]: j for j in doc.get("journeys") or [] if j.get("id")}
    payload = decisions.get("journeys")
    if not isinstance(payload, list) or not payload:
        sys.exit("error: review payload needs a non-empty 'journeys' list")
    seen = set()
    for item in payload:
        jid = item.get("id")
        if jid not in by_id:
            sys.exit(f"error: unknown journey id '{jid}'")
        seen.add(jid)
        j = by_id[jid]
        if item.get("keep"):
            impact = item.get("business_impact")
            if impact not in IMPACT:
                sys.exit(
                    f"error: kept journey '{jid}' needs business_impact "
                    f"(critical|important|normal). Nothing in source can set this."
                )
            j["excluded"] = False
            j.pop("excluded_reason", None)
            j["business_impact"] = impact
            # A human keeping a candidate is a declaration.
            if str(j.get("source") or "").startswith("discovered:"):
                j["source"] = "declared"
            drop_steps = item.get("drop_steps") or []
            if isinstance(drop_steps, str):
                drop_steps = [s.strip() for s in drop_steps.split(",") if s.strip()]
            if drop_steps:
                drop_set = set(drop_steps)
                j["steps"] = [s for s in (j.get("steps") or [])
                              if s.get("id") not in drop_set]
            vals = item.get("outcome_values")
            if isinstance(vals, str):
                vals = [v.strip() for v in vals.split(",") if v.strip()]
            if isinstance(vals, list) and len(vals) >= 2:
                oc = dict(j.get("outcome") or {})
                oc.setdefault("attribute", f"{jid}.outcome")
                oc["values"] = vals
                succ = item.get("success_values")
                if isinstance(succ, str):
                    succ = [v.strip() for v in succ.split(",") if v.strip()]
                if isinstance(succ, list) and succ:
                    oc["success_values"] = succ
                j["outcome"] = oc
        else:
            j["excluded"] = True
            j["excluded_reason"] = item.get("excluded_reason") or "dropped in ce review"
            j.pop("business_impact", None)
    for jid, j in by_id.items():
        if jid not in seen:
            # Unmentioned candidates stay as they were; report still needs a stamp
            # covering keepers. Treat omission as drop so junk cannot sneak through.
            j["excluded"] = True
            j.setdefault("excluded_reason", "not reviewed — dropped")
            j.pop("business_impact", None)
    return doc


def kept_with_impact(doc: dict) -> list[dict]:
    out = []
    for j in doc.get("journeys") or []:
        if j.get("excluded"):
            continue
        if j.get("business_impact") in IMPACT:
            out.append(j)
    return out


def write_stamp(work: Path, doc: dict) -> None:
    kept = kept_with_impact(doc)
    payload = {
        "kept": [j["id"] for j in kept],
        "excluded": [j["id"] for j in doc.get("journeys") or [] if j.get("excluded")],
    }
    (work / REVIEWED_NAME).write_text(json.dumps(payload, indent=2) + "\n")


def write_review_md(work: Path, doc: dict) -> None:
    L = [
        "# Review before `ce report`\n",
        "Do not edit this file to set impact — run `ce review` (browser) or "
        "edit `journeys.yaml` and `ce review --stamp`.\n",
        "Nothing in source or telemetry can assign `business_impact`. "
        "Health checks dominate traffic; refunds are rare and expensive.\n",
        "## Candidates\n",
    ]
    for j in doc.get("journeys") or []:
        hint = " — likely a directory, not a flow" if suggest_drop(j) else ""
        L.append(f"- `{j.get('id')}` ({j.get('name')}){hint}")
    L.append("\nThen:\n\n```bash\nce review\nce report\n```\n")
    (work / "REVIEW.md").write_text("\n".join(L))


def expected_name(journey_id: str, step: dict, is_first: bool) -> str:
    """Must match spec/generate.py::expected_name. Copied so intake does not
    import spec — stages stay standalone."""
    if step.get("span_name"):
        return str(step["span_name"])
    sid = str(step.get("id") or "step")
    return journey_id if is_first else f"{journey_id}.{sid}"


def load_traces(work: Path) -> list[dict] | None:
    """Optional recorded trace from Sentry MCP. Not fetched via undocumented
    APIs — the customer (or Cursor) drops JSON next to observed.json."""
    for name in ("mcp-trace.json", "trace.json"):
        path = work / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        spans = _normalize_trace(raw)
        if spans:
            return spans
    return None


def _normalize_trace(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("spans") or (raw.get("data") or {}).get("spans") or []
    else:
        items = []
    out: list[dict] = []
    for s in items:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("span_id") or s.get("id") or s.get("spanId") or "")
        name = str(s.get("description") or s.get("name") or s.get("span.description") or "")
        if not name:
            continue
        parent = s.get("parent_span_id") or s.get("parentSpanId") or s.get("parent")
        data = s.get("data") or s.get("attributes") or {}
        if not isinstance(data, dict):
            data = {}
        out.append({
            "span_id": sid or f"anon-{len(out)}",
            "parent_span_id": str(parent) if parent else "",
            "name": name,
            "op": str(s.get("op") or s.get("span.op") or ""),
            "data": data,
        })
    return out


def load_observed(work: Path) -> dict | None:
    path = work / "observed.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def evidence_kind(evidence: str, source: str) -> str:
    ev = (evidence or "").strip().lower()
    src = (source or "")
    if ev.startswith("span:") or src.startswith("discovered:telemetry"):
        return "sentry"
    return "source"


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _chip(label: str, kind: str = "") -> str:
    return f'<span class="chip {kind}">{_e(label)}</span>'


def _observed_span_names(observed: dict | None) -> set[str]:
    names: set[str] = set()
    if not observed:
        return names
    for item in observed.get("span_names") or []:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
        elif isinstance(item, str):
            names.add(item)
    return names


def _span_seen(expected: str, names: set[str], journey_id: str) -> bool:
    if expected in names:
        return True
    # Prefix match on `{id}.` after an exact miss — `checkout.session` covers
    # `checkout.session.authorize` if the scan used a longer name. The root
    # span `checkout` is exact-only so every `checkout.*` does not light it up.
    prefix = expected + "."
    if expected != journey_id:
        return any(n.startswith(prefix) for n in names)
    return False


def _related_attributes(journey: dict, observed: dict | None) -> list[tuple[str, str]]:
    if not observed:
        return []
    jid = str(journey.get("id") or "")
    want: set[str] = set()
    ck = (journey.get("correlation_key") or {}).get("attribute")
    if ck:
        want.add(str(ck))
    oc = (journey.get("outcome") or {}).get("attribute")
    if oc:
        want.add(str(oc))
    for m in journey.get("magnitude") or []:
        if m.get("attribute"):
            want.add(str(m["attribute"]))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in observed.get("attributes") or []:
        if not isinstance(a, dict):
            continue
        key = str(a.get("key") or a.get("name") or "")
        if not key or key in seen:
            continue
        if key in want or (jid and (key == jid or key.startswith(jid + "."))):
            src = str((a.get("attributeSource") or {}).get("source_type") or "unknown")
            seen.add(key)
            out.append((key, src))
    return out


def _sdk_present(observed: dict | None) -> bool | None:
    if not observed:
        return None
    sdk = observed.get("sdk") or {}
    if "any_sdk_present" in sdk:
        return bool(sdk["any_sdk_present"])
    return None


def _is_live(observed: dict | None) -> bool:
    if not observed:
        return False
    prov = observed.get("_provenance") or {}
    blob = json.dumps(prov).lower()
    return "trace-items/attributes" in blob or "merged" in blob or "mcp" in blob


def provenance_banner(observed: dict | None) -> str:
    sdk = _sdk_present(observed)
    if observed is None:
        inner = (
            "<aside class='banner'>"
            "<strong>This pass used</strong> the journey file only — no "
            "<code>observed.json</code>. Recommendations are whatever "
            "<code>ce discover</code> wrote from source."
            "</aside>"
        )
    elif sdk is False:
        inner = (
            "<aside class='banner warn'>"
            "<strong>This pass used a source scan.</strong> No Sentry SDK in "
            "the tree. Recommendations come from routes and handlers, not from "
            "live traces. Nothing emits these spans yet."
            "</aside>"
        )
    elif _is_live(observed):
        inner = (
            "<aside class='banner live'>"
            "<strong>This pass used source plus live Sentry.</strong> "
            "Attributes from the documented API "
            "(<code>source_type</code> sentry vs user). Span names from MCP "
            "when present. Heuristics are labelled wherever they appear."
            "</aside>"
        )
    else:
        inner = (
            "<aside class='banner'>"
            "<strong>This pass used a static scan of this repo.</strong> "
            "Span names and attributes are what was written in source, not how "
            "often they run. Counts are not frequencies — we will not show a "
            "percentage."
            "</aside>"
        )
    return (
        "<details class='prov'><summary>Where these candidates came from"
        "</summary>" + inner + "</details>"
    )


def _why_lede(journey: dict) -> str:
    notes = (journey.get("notes") or "").strip()
    if notes:
        return notes
    n = len(journey.get("steps") or [])
    src = journey.get("source") or "discovered:code"
    if src.startswith("discovered:telemetry"):
        return (f"Proposed from Sentry telemetry: {n} step(s) whose span names "
                "look like a flow. Confirm this is a business journey, not noise.")
    if src == "declared":
        return "You (or a previous review) already named this journey."
    return (f"Proposed from source: {n} step(s) from routes or exported "
            "handlers. Confirm this is a business flow, not a directory.")


def _contract_block(journey: dict, observed: dict | None) -> str:
    jid = str(journey.get("id") or "")
    steps = journey.get("steps") or []
    names = _observed_span_names(observed)
    sdk = _sdk_present(observed)
    synthetic = bool(observed and observed.get("_synthetic_counts"))
    now_rows = []
    after_rows = []
    if sdk is False:
        now_rows.append(
            "<p class='empty'>Nothing emits these spans yet — no Sentry SDK "
            "in this repo.</p>"
        )
    elif observed is None:
        now_rows.append(
            "<p class='empty'>No observed.json, so we cannot show what is "
            "already instrumented.</p>"
        )
    for i, step in enumerate(steps):
        exp = expected_name(jid, step, i == 0)
        seen = _span_seen(exp, names, jid)
        sid = _e(step.get("id"))
        exp_e = _e(exp)
        if sdk is not False and observed is not None:
            if seen:
                now_rows.append(
                    f"<li><code>{exp_e}</code> — present"
                    f"{' (from source, not a frequency)' if synthetic else ''}"
                    f"</li>"
                )
            else:
                now_rows.append(
                    f"<li class='missing'><code>{exp_e}</code> — not observed "
                    f"(step <code>{sid}</code>)</li>"
                )
        after_rows.append(
            f"<li>Span <code>{exp_e}</code> on step <code>{sid}</code></li>"
        )
    ck = (journey.get("correlation_key") or {}).get("attribute")
    if ck:
        after_rows.append(
            f"<li>Correlation key <code>{_e(ck)}</code> "
            f"{_chip('Heuristic — confirm', 'heuristic')}</li>"
        )
    oc = journey.get("outcome") or {}
    if isinstance(oc, dict) and oc.get("attribute"):
        vals = oc.get("values") or []
        extra = f" values [{', '.join(_e(v) for v in vals)}]" if vals else ""
        after_rows.append(
            f"<li>Outcome <code>{_e(oc['attribute'])}</code>{extra}</li>"
        )
    for m in journey.get("magnitude") or []:
        after_rows.append(
            f"<li>Magnitude <code>{_e(m.get('attribute'))}</code> "
            f"{_chip('Heuristic — confirm', 'heuristic')} "
            f"(from a field name, not from traffic)</li>"
        )
    rel = _related_attributes(journey, observed)
    attr_now = ""
    if rel:
        bits = []
        for key, src in rel:
            bits.append(
                f"<li><code>{_e(key)}</code> "
                f"{_chip('sentry' if src == 'sentry' else 'user', 'attr')} "
                f"source_type={_e(src)}</li>"
            )
        attr_now = (
            "<p class='sub'>Related attributes already seen</p><ul>"
            + "".join(bits) + "</ul>"
        )
    now_html = "".join(now_rows) if now_rows else "<p class='empty'>None yet.</p>"
    if now_rows and now_rows[0].startswith("<p"):
        now_inner = now_html
    else:
        now_inner = f"<ul>{now_html}</ul>" if now_rows else now_html
    return f"""
<div class="diff">
  <div>
    <h3>Now (current)</h3>
    <p class="sub">What we can already see. Not a source-code diff. We do not
    invent how often this runs.</p>
    {now_inner}
    {attr_now}
  </div>
  <div>
    <h3>If you keep this</h3>
    <p class="sub">The spec will require this telemetry contract. Your coding
    agent implements it from <code>.agents/journeys/</code> after
    <code>ce report</code>. <strong>ce does not patch your app.</strong></p>
    <ul>{''.join(after_rows) or '<li>Named spans for each kept step.</li>'}</ul>
  </div>
</div>
"""


_ROUTE_EV = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(\S+)", re.I)


def _route_from_evidence(evidence: str) -> tuple[str, str] | None:
    m = _ROUTE_EV.search(evidence or "")
    if not m:
        return None
    return m.group(1).upper(), m.group(2)


def _op_for_step(step: dict) -> str:
    ev = str(step.get("evidence") or "")
    if _route_from_evidence(ev):
        return "http.client" if step.get("surface") == "browser" else "http.server"
    return {
        "browser": "ui.action",
        "node": "function",
        "worker": "queue.process",
    }.get(str(step.get("surface") or ""), "function")


def _journey_op(step: dict) -> str:
    """Custom journey spans are not HTTP. http.server is a child of the step."""
    return "ui.action" if step.get("surface") == "browser" else "function"


def _wf_node(name: str, op: str, *, kind: str, note: str = "",
             attrs: list[tuple[str, str, str]] | None = None,
             children: list[dict] | None = None, error: bool = False,
             debug: str = "", hint: str = "") -> dict:
    return {
        "name": name, "op": op, "kind": kind, "note": note,
        "attrs": attrs or [], "children": children or [],
        "error": error, "debug": debug, "hint": hint,
    }


def _span_is_failed(span: dict) -> bool:
    """True only when the payload itself says the span failed. Never inferred."""
    st = str(span.get("status") or span.get("span_status") or "").lower()
    if "error" in st or st in ("failed", "internal", "internal_error"):
        return True
    data = span.get("data") or {}
    if not isinstance(data, dict):
        return False
    raw = data.get("http.status_code") or data.get("http.response.status_code")
    try:
        return int(raw) >= 500
    except (TypeError, ValueError):
        return False


def _last_node(nodes: list[dict]) -> dict | None:
    if not nodes:
        return None
    node = nodes[-1]
    kids = node.get("children") or []
    return _last_node(kids) if kids else node


def _mark_illustrated_error(nodes: list[dict], *, debug: str, hint: str) -> None:
    """Paint a teaching error on the last row. Not a recorded incident."""
    node = _last_node(nodes)
    if not node:
        return
    node["error"] = True
    node["debug"] = debug
    node["hint"] = hint


_DEBUG_NOW = (
    "Error on this request (illustrated — not a recorded incident). Missing: "
    "which flow, which instance, whether earlier steps ran. The next search "
    "is who called this URL."
)
_DEBUG_AFTER = (
    "Same class of failure, now on this step of one journey instance. "
    "Prior steps are siblings. A correlation key is on every span. "
    "outcome=failed plus a coded failure_reason are what make this groupable. "
    "Illustrated — not a recorded incident."
)


def _tree_from_recorded(spans: list[dict], journey_id: str,
                        expected: list[str]) -> list[dict]:
    want = {n.lower() for n in expected}
    prefix = journey_id.lower() + "."
    matched_ids: set[str] = set()
    by_id = {s["span_id"]: s for s in spans}

    def relevant(s: dict) -> bool:
        n = s["name"].lower()
        return n in want or n == journey_id.lower() or n.startswith(prefix)

    for s in spans:
        if relevant(s):
            cur: dict | None = s
            while cur:
                matched_ids.add(cur["span_id"])
                parent = cur.get("parent_span_id") or ""
                cur = by_id.get(parent)
    use = [s for s in spans if s["span_id"] in matched_ids] or spans
    kids: dict[str, list[dict]] = {}
    roots_src: list[dict] = []
    for s in use:
        parent = s.get("parent_span_id") or ""
        if parent and parent in {x["span_id"] for x in use}:
            kids.setdefault(parent, []).append(s)
        else:
            roots_src.append(s)

    def lift(s: dict) -> dict:
        data = s.get("data") or {}
        attrs = []
        if isinstance(data, dict):
            for k, v in list(data.items())[:8]:
                if str(k).startswith("_"):
                    continue
                attrs.append((str(k), str(v), "sentry"))
        err = _span_is_failed(s)
        return _wf_node(
            s["name"], s.get("op") or "",
            kind="recorded",
            attrs=attrs,
            error=err,
            hint="error" if err else "",
            debug=(
                "Failed span from the recorded trace. Nesting is from the "
                "payload. No duration is shown."
                if err else ""
            ),
            children=[lift(c) for c in kids.get(s["span_id"], [])],
        )
    return [lift(s) for s in roots_src]


def waterfall_now(journey: dict, observed: dict | None,
                  traces: list[dict] | None) -> tuple[str, list[dict]]:
    """Current-state sketch. Never invents durations or frequencies."""
    jid = str(journey.get("id") or "")
    steps = journey.get("steps") or []
    expected = [expected_name(jid, s, i == 0) for i, s in enumerate(steps)]
    if traces:
        tree = _tree_from_recorded(traces, jid, expected)
        return (
            "Recorded Sentry trace. Nesting from the payload.",
            tree,
        )
    names = _observed_span_names(observed)
    sdk = _sdk_present(observed)
    live = _is_live(observed)
    if live and names:
        roots: list[dict] = []
        seen_any = False
        for i, (step, exp) in enumerate(zip(steps, expected)):
            hit = _span_seen(exp, names, jid)
            if hit:
                seen_any = True
            roots.append(_wf_node(
                exp if hit else ( _route_label(step) or exp),
                _op_for_step(step),
                kind="recorded" if hit else "missing",
                hint="own request",
                note=("Observed span name from Sentry. Parent/child is a sketch "
                      "— save mcp-trace.json for a real tree." if hit else
                      "Not in the Sentry span names we have."),
            ))
        if seen_any:
            _mark_illustrated_error(
                roots, debug=_DEBUG_NOW, hint="error · no parent")
            return (
                "Separate requests, no journey id.",
                roots,
            )
    # Code / SDK-absent: each route is its own request, not one nested trace.
    roots = []
    auto = "SDK automatic HTTP" if sdk else "uninstrumented HTTP request"
    for step in steps:
        label = _route_label(step) or str(step.get("id") or "step")
        roots.append(_wf_node(
            label, _op_for_step(step),
            kind="request",
            hint="own request",
            note=f"Separate {auto}. No journey span, no correlation key.",
        ))
    _mark_illustrated_error(roots, debug=_DEBUG_NOW, hint="error · no parent")
    return (
        "Separate requests, no journey id.",
        roots,
    )


def _route_label(step: dict) -> str | None:
    parsed = _route_from_evidence(str(step.get("evidence") or ""))
    if not parsed:
        return None
    method, route = parsed
    return f"{method} {route}"


def waterfall_after(journey: dict) -> tuple[str, list[dict]]:
    jid = str(journey.get("id") or "")
    steps = journey.get("steps") or []
    ck = (journey.get("correlation_key") or {}).get("attribute")
    oc = journey.get("outcome") if isinstance(journey.get("outcome"), dict) else {}
    mag = journey.get("magnitude") or []
    if not steps:
        return ("No steps to mock.", [])
    ck_attr = str(ck) if ck else f"{jid}.id"
    ck_src = "proposed" if ck else "heuristic"
    oc_attr = str(oc.get("attribute") or "outcome")

    def step_node(i: int, step: dict) -> dict:
        exp = expected_name(jid, step, i == 0)
        attrs: list[tuple[str, str, str]] = [
            (ck_attr, "<this instance>", ck_src),
        ]
        route = _route_label(step)
        if route:
            attrs.append(("http", route, "source"))
        if i == 0 and oc.get("attribute"):
            vals = oc.get("values") or []
            shown = " | ".join(str(v) for v in vals) if vals else "<string enum>"
            attrs.append((str(oc["attribute"]), shown, "proposed"))
        if i == len(steps) - 1:
            for m in mag:
                if m.get("attribute"):
                    attrs.append((str(m["attribute"]), "<number>", "heuristic"))
        return _wf_node(
            exp, _journey_op(step),
            kind="proposed",
            note=("Journey root — open when the flow starts, end on a "
                  "terminal event." if i == 0 else
                  f"Journey step `{step.get('id')}`."),
            attrs=attrs,
        )

    root = step_node(0, steps[0])
    for i, step in enumerate(steps[1:], start=1):
        root["children"].append(step_node(i, step))
    err_node = _last_node([root])
    if err_node is not None:
        err_node["error"] = True
        err_node["hint"] = "error"
        err_node["debug"] = _DEBUG_AFTER
        err_node["attrs"] = list(err_node.get("attrs") or []) + [
            (oc_attr, "failed", "illustrated"),
            ("failure_reason", "<coded reason>", "proposed"),
        ]
    n = len(steps)
    return (
        f"One root, one key, {n} step(s).",
        [root],
    )


def _layout_list(nodes: list[dict], start: float, width: float) -> None:
    if not nodes:
        return
    n = len(nodes)
    w = width / n
    for i, node in enumerate(nodes):
        node["_start"] = start + i * w
        node["_width"] = w
        _layout_list(node.get("children") or [], node["_start"], node["_width"])


def _layout_separate_roots(roots: list[dict]) -> None:
    """Each root is its own trace: full-width bar, children nested inside."""
    for node in roots:
        node["_start"] = 0.0
        node["_width"] = 1.0
        _layout_list(node.get("children") or [], 0.0, 1.0)


def _flatten_wf(nodes: list[dict], depth: int = 0, path: str = "") -> list[dict]:
    rows: list[dict] = []
    for i, node in enumerate(nodes):
        p = f"{path}/{i}" if path else str(i)
        kids = node.get("children") or []
        row = dict(node)
        row["_depth"] = depth
        row["_path"] = p
        row["_nkids"] = len(kids)
        rows.append(row)
        rows.extend(_flatten_wf(kids, depth + 1, p))
    return rows


def _op_slug(op: str) -> str:
    o = (op or "function").lower()
    if o.startswith("http"):
        return "http"
    if o.startswith("db"):
        return "db"
    if o.startswith("ui"):
        return "ui"
    return "fn"


def _render_wf_rows(roots: list[dict], *, separate: bool) -> str:
    if separate:
        _layout_separate_roots(roots)
    else:
        _layout_list(roots, 0.0, 1.0)
    rows = _flatten_wf(roots)
    out: list[str] = []
    for row in rows:
        depth = int(row.get("_depth") or 0)
        path = _e(row.get("_path"))
        nkid = int(row.get("_nkids") or 0)
        slug = _op_slug(str(row.get("op") or ""))
        chev = (
            f'<button type="button" class="wf-chev" aria-label="toggle children">'
            f"{'▾' if nkid else ''}</button>"
            if nkid else '<span class="wf-chev empty"></span>'
        )
        hint = str(row.get("hint") or ("error" if row.get("error") else ""))
        hint_h = f'<span class="wf-hint">{_e(hint)}</span>' if hint else ""
        err_cls = " error" if row.get("error") else ""
        payload = html.escape(json.dumps({
            "name": row.get("name"),
            "op": row.get("op"),
            "note": row.get("note") or "",
            "attrs": row.get("attrs") or [],
            "error": bool(row.get("error")),
            "debug": row.get("debug") or "",
        }), quote=True)
        out.append(
            f'<div class="wf-row{err_cls}" data-path="{path}" '
            f'data-depth="{depth}" data-span="{payload}">'
            f'<div class="wf-tree" style="--d:{depth}">{chev}'
            f'<span class="wf-dot op-{slug}"></span>'
            f'<span class="wf-opname">{_e(row.get("op") or "")}</span>'
            f'<code>{_e(row.get("name"))}</code>{hint_h}</div>'
            f"</div>"
        )
    return "".join(out) or "<p class='empty'>None</p>"


def _waterfall_block(journey: dict, observed: dict | None,
                     traces: list[dict] | None) -> str:
    cap_now, now_tree = waterfall_now(journey, observed, traces)
    cap_after, after_tree = waterfall_after(journey)
    now_rows = _render_wf_rows(now_tree, separate=not bool(traces))
    after_rows = _render_wf_rows(after_tree, separate=False)
    return f"""
<div class="wf-panel">
  <div class="wf-tabs" role="tablist">
    <button type="button" class="wf-tab on" data-tab="now">Today</button>
    <button type="button" class="wf-tab" data-tab="after">After this spec</button>
  </div>
  <div class="wf-view on" data-tab="now">
    <p class="sub">{_e(cap_now)}</p>
    <div class="wf-grid">{now_rows}</div>
  </div>
  <div class="wf-view" data-tab="after" hidden>
    <p class="sub">{_e(cap_after)}</p>
    <div class="wf-grid">{after_rows}</div>
  </div>
  <aside class="wf-detail" hidden>
    <div class="wf-detail-body"></div>
  </aside>
</div>
"""


def _journey_card(journey: dict, observed: dict | None,
                  traces: list[dict] | None = None, *,
                  expanded: bool = False) -> str:
    jid_raw = str(journey.get("id") or "")
    jid = _e(jid_raw)
    name = _e(journey.get("name") or jid_raw)
    drop = suggest_drop(journey)
    src = str(journey.get("source") or "discovered:code")
    if src.startswith("discovered:telemetry"):
        src_chip = _chip("From Sentry", "sentry")
    elif src == "declared":
        src_chip = _chip("Declared", "ok")
    else:
        src_chip = _chip("From source", "source")
    surfaces = sorted({str(s.get("surface") or "")
                       for s in (journey.get("steps") or []) if s.get("surface")})
    conf = journey.get("confidence")
    chips = [src_chip]
    chips += [_chip(s) for s in surfaces]
    if conf:
        chips.append(_chip(str(conf)))
    if drop:
        chips.append(_chip("likely drop", "warn"))
    ev_lis = []
    for s in journey.get("steps") or []:
        kind = evidence_kind(str(s.get("evidence") or ""), src)
        label = "From Sentry" if kind == "sentry" else "From source"
        ev_lis.append(
            f"<li>{_chip(label, kind)} "
            f"<code>{_e(s.get('id'))}</code> "
            f"{_e(s.get('evidence') or '(no evidence)')} "
            f"<label class='keep-step'><input type='checkbox' class='step' "
            f"data-step='{_e(s.get('id'))}' checked> keep step</label></li>"
        )
    questions = [f"<li>{_e(q_)}</li>"
                 for q_ in (journey.get("needs_clarification") or [])]
    q_html = (
        f"<h3>Open questions</h3><ul class='questions'>{''.join(questions)}</ul>"
        if questions else ""
    )
    oc = journey.get("outcome") or {}
    vals = ", ".join(str(v) for v in (oc.get("values") or []))
    keep_checked = "" if drop else "checked"
    drop_checked = "checked" if drop else ""
    hint = (
        "<p class='hint'>This name is usually a directory, not a business "
        "flow. Default is drop — keep it only if you mean it.</p>"
        if drop else ""
    )
    open_cls = " open" if expanded and not drop else ""
    return f"""
<section class="card{' skip' if drop else ' proposed'}{open_cls}" data-id="{jid}">
  <header class="card-head">
    <button type="button" class="card-toggle">
      <h2>{name} <code>{jid}</code></h2>
      <p class="chips">{''.join(chips)}</p>
    </button>
    <div class="card-decide">
      <fieldset class="decision">
        <label><input type="radio" name="keep-{jid}" value="keep" {keep_checked}> Keep</label>
        <label><input type="radio" name="keep-{jid}" value="drop" {drop_checked}> Drop</label>
      </fieldset>
      <fieldset class="impact-fields">
        <legend>Impact</legend>
        <label><input type="radio" name="impact-{jid}" value=""> unset</label>
        <label><input type="radio" name="impact-{jid}" value="critical"> critical</label>
        <label><input type="radio" name="impact-{jid}" value="important"> important</label>
        <label><input type="radio" name="impact-{jid}" value="normal"> normal</label>
        <p class="card-err" hidden>Set impact — we will not infer it.</p>
      </fieldset>
    </div>
  </header>
  <div class="card-body">
    {hint}
    <p class="lede">{_e(_why_lede(journey))}</p>
    {_waterfall_block(journey, observed, traces)}
    <details class="more">
      <summary>More</summary>
      <h3>Why we proposed this</h3>
      <p>{_e(_why_lede(journey))}</p>
      <ul class="evidence">{''.join(ev_lis) or '<li>(no steps)</li>'}</ul>
      <h3>Contract (spans and attributes)</h3>
      {_contract_block(journey, observed)}
      {q_html}
      <h3>How this flow ends (leave blank if you do not know)</h3>
      <p class="sub">Do not invent success. Outcome is a string enum
      (authorized, declined, abandoned) — never a boolean.</p>
      <label>Outcome values (comma-separated; leave blank if unknown)
        <input type="text" name="outcome-{jid}" value="{_e(vals)}">
      </label>
      <label>Success values (only if outcomes are set; do not guess)
        <input type="text" name="success-{jid}" value="">
      </label>
    </details>
  </div>
</section>
"""


_CSS = """
:root {
  --bg: #1f1633;
  --bg-deep: #150f23;
  --card: #241b38;
  --ink: #f4eefc;
  --muted: #c4b5d6;
  --line: #3d3158;
  --purple: #6a5fc1;
  --lime: #c2ef4e;
  --coral: #ffb287;
  --danger: #ff8a9b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 16px/1.5 Rubik, system-ui, sans-serif;
  background: var(--bg);
  color: var(--ink);
}
code { font-family: ui-monospace, Menlo, Monaco, monospace; font-size: .9em; }
.wrap { max-width: 72rem; margin: 0 auto; padding: 1.1rem 1.2rem 7rem; }
.hero h1 { font-size: 1.45rem; letter-spacing: -.02em; margin: 0 0 .25rem; }
.hero p, .how {
  color: var(--muted);
  font-size: .95rem;
  margin: 0 0 .7rem;
  max-width: 52rem;
}
.how b { color: var(--lime); }
.prov {
  color: var(--muted);
  font-size: .85rem;
  margin: 0 0 .8rem;
}
.prov summary { cursor: pointer; color: var(--ink); font-weight: 600; }
.lede { margin: 0 0 .4rem; color: var(--muted); font-size: .95rem; }
.banner {
  background: #2a2044;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: .55rem .9rem;
  margin: 0 0 1.2rem;
  color: var(--muted);
  font-size: .9rem;
}
.banner.live { border-color: var(--lime); }
.banner.warn { border-color: var(--coral); }
.banner strong { color: var(--ink); }
.list-title { margin: 1rem 0 .5rem; font-size: 1.05rem; }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: .55rem .85rem .7rem;
  margin: 0 0 .55rem;
}
.card.skip { opacity: .78; }
.card-head {
  display: grid;
  grid-template-columns: 1fr;
  gap: .6rem;
}
@media (min-width: 900px) {
  .card-head { grid-template-columns: 1fr auto; align-items: start; }
}
.card-toggle {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: 0;
  color: inherit;
  font: inherit;
  cursor: pointer;
  padding: 0;
}
.card-toggle h2 { margin: 0 0 .2rem; font-size: 1.1rem; }
.card:not(.open) .card-body { display: none; }
.card-decide { display: flex; flex-wrap: wrap; gap: .4rem; }
.card-decide fieldset {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: .15rem .55rem;
  margin: 0;
}
.card-decide label { display: inline; margin-right: .55rem; font-size: .9rem; }
.card h3 { margin: 1.1rem 0 .4rem; font-size: .95rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); font-weight: 600; }
.chips { display: flex; flex-wrap: wrap; gap: .4rem; margin: 0; }
.chip {
  display: inline-block;
  background: #362d59;
  color: var(--ink);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: .75rem;
  letter-spacing: .03em;
}
.chip.sentry, .chip.live { background: var(--lime); color: #150f23; }
.chip.source, .chip.ok { background: var(--purple); }
.chip.heuristic, .chip.warn { background: #5a3a20; color: var(--coral); }
.chip.attr { background: #362d59; }
.chip.proposed { background: #422082; }
.chip.illustrated { background: #5a3a20; color: var(--coral); }
.hint {
  background: #3a2a18;
  color: var(--coral);
  padding: .4rem .6rem;
  border-radius: 6px;
  font-size: .9rem;
}
.evidence, .questions, .diff ul { padding-left: 1.1rem; }
.keep-step { display: inline; margin-left: .5rem; color: var(--muted); font-size: .85rem; }
.diff {
  display: grid;
  grid-template-columns: 1fr;
  gap: .8rem;
  margin: .6rem 0;
}
@media (min-width: 720px) {
  .diff { grid-template-columns: 1fr 1fr; }
}
.diff > div {
  background: var(--bg-deep);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: .8rem 1rem;
}
.diff h3 { margin-top: 0; text-transform: none; letter-spacing: 0; color: var(--ink); }
.wf-panel {
  background: var(--bg-deep);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: .5rem .7rem .7rem;
  margin: .45rem 0 .35rem;
}
.wf-view[hidden] { display: none; }
.wf-tabs { display: flex; gap: .35rem; margin: 0 0 .35rem; }
.wf-tab {
  font: inherit;
  color: var(--muted);
  background: #2a2044;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: .25rem .7rem;
  cursor: pointer;
}
.wf-tab.on { color: #150f23; background: var(--lime); border-color: var(--lime); }
.wf-grid { display: flex; flex-direction: column; }
.wf-row {
  display: flex;
  align-items: center;
  min-height: 1.85rem;
  border-radius: 4px;
  cursor: pointer;
}
.wf-row:hover { background: #2a2044; }
.wf-row.on { box-shadow: inset 0 0 0 1px var(--purple); }
.wf-row.hide { display: none; }
.wf-row.error .wf-hint { color: var(--danger); }
.wf-row.error .wf-dot { background: var(--danger); }
.wf-view[data-tab="after"] .wf-row[data-depth="0"] .wf-tree code,
.wf-view[data-tab="after"] .wf-row[data-depth="0"] .wf-opname,
.wf-view[data-tab="after"] .wf-row[data-depth="0"] .wf-dot { opacity: .55; }
.wf-view[data-tab="after"] .wf-row[data-depth="1"] .wf-tree {
  border-left: 1px solid var(--line);
  margin-left: .4rem;
  padding-left: calc(var(--d, 1) * 1rem);
}
.wf-tree {
  display: flex;
  align-items: center;
  gap: .35rem;
  padding-left: calc(var(--d, 0) * 1rem);
  min-width: 0;
  font-size: .85rem;
}
.wf-tree code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-chev {
  width: 1.1rem;
  border: 0;
  background: none;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  padding: 0;
}
.wf-chev.empty { visibility: hidden; }
.wf-dot {
  width: .55rem; height: .55rem; border-radius: 50%; flex: 0 0 auto;
}
.wf-dot.op-http { background: #fa7faa; }
.wf-dot.op-fn { background: #c2ef4e; }
.wf-dot.op-ui { background: #6a5fc1; }
.wf-dot.op-db { background: #ffb287; }
.wf-opname { color: var(--muted); font-size: .75rem; flex: 0 0 auto; }
.wf-hint { color: var(--muted); font-size: .75rem; flex: 0 0 auto; }
.wf-detail {
  margin-top: .5rem;
  border-top: 1px solid var(--line);
  padding-top: .45rem;
  font-size: .9rem;
}
.wf-detail h4 { margin: 0 0 .3rem; }
.card-body details.more { margin: .35rem 0 0; }
.card-body summary { cursor: pointer; color: var(--muted); font-weight: 600; }
.more h3 {
  margin: .75rem 0 .3rem;
  font-size: .9rem;
  text-transform: none;
  letter-spacing: 0;
  color: var(--ink);
}
.sub { color: var(--muted); font-size: .85rem; margin: 0 0 .4rem; }
.missing { color: var(--danger); }
.empty { color: var(--muted); }
.card-decide fieldset.decision, .card-decide fieldset.impact-fields {
  padding: .15rem .55rem;
  margin: 0;
  border-radius: 6px;
}
.impact-fields:disabled { opacity: .45; }
label { display: block; margin: .35rem 0; }
input[type=text] {
  width: 100%;
  margin-top: .25rem;
  padding: .45rem .55rem;
  border: 0;
  border-radius: 8px;
  background: var(--bg-deep);
  color: var(--ink);
  box-shadow: inset 0 1px 2px rgba(0,0,0,.4);
}
.card-err { color: var(--danger); }
.skips {
  border: 1px dashed var(--line);
  border-radius: 12px;
  padding: .6rem 1rem 1rem;
  margin: 1rem 0 2rem;
  color: var(--muted);
}
.skips summary { cursor: pointer; color: var(--ink); font-weight: 600; padding: .4rem 0; }
.sticky {
  position: sticky;
  bottom: 0;
  background: var(--bg-deep);
  border-top: 1px solid var(--line);
  padding: .9rem 1.2rem;
  display: flex;
  flex-wrap: wrap;
  gap: .8rem;
  align-items: center;
  justify-content: space-between;
}
.sticky .meta { color: var(--muted); }
button[type=submit] {
  font: inherit;
  font-weight: 650;
  padding: .55rem 1.1rem;
  border: 0;
  border-radius: 8px;
  background: var(--lime);
  color: #150f23;
  box-shadow: inset 0 -2px 0 rgba(0,0,0,.25);
  cursor: pointer;
}
button[type=submit]:disabled { opacity: .45; cursor: not-allowed; }
#err { color: var(--danger); margin: 0; }
.saved { padding: 3rem 1.2rem; max-width: 40rem; margin: 0 auto; }
"""

_JS = """
function cards() { return [...document.querySelectorAll(".card")]; }
function isKeep(card) {
  return card.querySelector('input[value="keep"]:checked');
}
function impactOf(card) {
  const el = card.querySelector('input[name^="impact-"]:checked');
  return (el && el.value) || "";
}
function syncCard(card) {
  const keep = !!isKeep(card);
  const fs = card.querySelector(".impact-fields");
  if (fs) fs.disabled = !keep;
  if (!keep) {
    const err = card.querySelector(".card-err");
    if (err) err.hidden = true;
  }
}
function tally() {
  let kept = 0, missing = 0;
  cards().forEach((card) => {
    syncCard(card);
    if (!isKeep(card)) return;
    kept += 1;
    if (!impactOf(card)) missing += 1;
  });
  const meta = document.getElementById("tally");
  const btn = document.getElementById("save");
  if (meta) {
    meta.textContent = "Kept: " + kept + " (need 1–3 with impact)";
  }
  if (btn) btn.disabled = kept < 1 || missing > 0;
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
document.querySelectorAll(".card.proposed > .card-head .card-toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    const card = btn.closest(".card");
    const was = card.classList.contains("open");
    document.querySelectorAll(".card.proposed").forEach((c) => c.classList.remove("open"));
    if (!was) card.classList.add("open");
  });
});
document.querySelectorAll(".card.skip > .card-head .card-toggle").forEach((btn) => {
  btn.addEventListener("click", () => btn.closest(".card").classList.toggle("open"));
});
function syncTree(grid) {
  const collapsed = [...grid.querySelectorAll(".wf-row.collapsed")]
    .map((r) => r.dataset.path);
  grid.querySelectorAll(".wf-row").forEach((r) => {
    const p = r.dataset.path || "";
    r.classList.toggle("hide", collapsed.some((c) => p.startsWith(c + "/")));
  });
}
function showSpan(panel, row) {
  panel.querySelectorAll(".wf-row").forEach((r) => r.classList.remove("on"));
  row.classList.add("on");
  const aside = panel.querySelector(".wf-detail");
  const body = panel.querySelector(".wf-detail-body");
  let span = {};
  try { span = JSON.parse(row.dataset.span || "{}"); } catch (e) { span = {}; }
  aside.hidden = false;
  const debug = span.debug
    ? "<p class='hint'>" + esc(span.debug) + "</p>"
    : "";
  const errMark = span.error ? " <span class='chip warn'>error</span>" : "";
  const attrs = (span.attrs || []).map((a) => {
    const k = Array.isArray(a) ? a[0] : a.key;
    const v = Array.isArray(a) ? a[1] : a.value;
    const src = Array.isArray(a) ? a[2] : a.source;
    return "<li><code>" + esc(k) + "</code> " + esc(v) +
      " <span class='chip " + esc(src) + "'>" + esc(src) + "</span></li>";
  }).join("");
  body.innerHTML =
    "<h4><code>" + esc(span.op || "") + "</code> " + esc(span.name || "") +
    "</h4>" + errMark + debug +
    (span.note ? "<p class='sub'>" + esc(span.note) + "</p>" : "") +
    (attrs ? "<ul>" + attrs + "</ul>" : "<p class='empty'>No attributes on this span.</p>");
}
function revealError(panel, view) {
  if (!view) return;
  const row = view.querySelector(".wf-row.error") || view.querySelector(".wf-row");
  if (row) showSpan(panel, row);
}
document.querySelectorAll(".wf-panel").forEach((panel) => {
  panel.querySelectorAll(".wf-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const which = tab.dataset.tab;
      panel.querySelectorAll(".wf-tab").forEach((t) => t.classList.toggle("on", t === tab));
      let shown = null;
      panel.querySelectorAll(".wf-view").forEach((v) => {
        v.hidden = v.dataset.tab !== which;
        if (!v.hidden) shown = v;
      });
      revealError(panel, shown);
    });
  });
  panel.querySelectorAll(".wf-chev:not(.empty)").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = btn.closest(".wf-row");
      const closed = row.classList.toggle("collapsed");
      btn.textContent = closed ? "▸" : "▾";
      syncTree(row.parentElement);
    });
  });
  panel.querySelectorAll(".wf-row").forEach((row) => {
    row.addEventListener("click", () => showSpan(panel, row));
  });
  revealError(panel, panel.querySelector(".wf-view.on"));
});
document.querySelectorAll('input[type=radio]').forEach((el) => {
  el.addEventListener("change", tally);
});
tally();
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const journeys = [];
  let blocked = false;
  cards().forEach((card) => {
    const id = card.dataset.id;
    const keep = !!isKeep(card);
    const impact = impactOf(card);
    const cardErr = card.querySelector(".card-err");
    if (cardErr) cardErr.hidden = true;
    if (keep && !impact) {
      if (cardErr) cardErr.hidden = false;
      blocked = true;
    }
    const drop_steps = [...card.querySelectorAll("input.step")]
      .filter((el) => !el.checked).map((el) => el.dataset.step);
    journeys.push({
      id, keep,
      business_impact: keep ? impact : "",
      drop_steps,
      outcome_values: card.querySelector('input[name="outcome-'+id+'"]').value,
      success_values: card.querySelector('input[name="success-'+id+'"]').value,
    });
  });
  if (blocked) {
    err.textContent = "Set impact on every keeper — we will not infer it.";
    return;
  }
  const res = await fetch("/apply", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({journeys}),
  });
  const text = await res.text();
  if (!res.ok) { err.textContent = text; return; }
  document.body.innerHTML =
    '<div class="saved"><h1>Saved</h1><p>'+text+'</p>' +
    '<p>Next: <code>ce report</code>. That writes the spec to ' +
    '<code>.agents/journeys/</code> (tracked) and leaves WHY/gap in ' +
    '<code>ce-work/</code>. Point your coding agent at the SPEC, not at WHY.</p>' +
    '<p>You can close this tab.</p></div>';
});
"""


def _page(doc: dict, observed: dict | None = None,
          traces: list[dict] | None = None) -> bytes:
    proposed: list[str] = []
    skips: list[str] = []
    opened = False
    for j in doc.get("journeys") or []:
        drop = suggest_drop(j)
        expanded = (not drop) and (not opened)
        card = _journey_card(j, observed, traces, expanded=expanded)
        if drop:
            skips.append(card)
        else:
            proposed.append(card)
            if expanded:
                opened = True
    if proposed:
        proposed_html = (
            '<h2 class="list-title">Proposed journeys</h2>' + "".join(proposed)
        )
    else:
        proposed_html = (
            "<p>No journey candidates. Hand-author with <code>ce init</code>.</p>"
        )
    skips_html = ""
    if skips:
        skips_html = (
            f'<details class="skips"><summary>Probably not journeys '
            f"({len(skips)}) — usually directories, not flows</summary>"
            + "".join(skips)
            + "</details>"
        )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ce review</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>Which customer flows should Sentry treat as journeys?</h1>
    <p class="how">Keep 2–3, set impact
    (<b>critical</b> = we would page · <b>important</b> = same day ·
    <b>normal</b> = visible, not a pager), Save. Names like <code>web</code>
    default to drop. This page does not change application code.</p>
  </header>
  {provenance_banner(observed)}
  <form id="f">
    {proposed_html}
    {skips_html}
    <div class="sticky">
      <span class="meta" id="tally">Kept: 0 (need 1–3 with impact)</span>
      <button type="submit" id="save" disabled>Save and continue</button>
      <p id="err"></p>
    </div>
  </form>
</div>
<script>{_JS}</script>
</body></html>
"""
    return body.encode("utf-8")


def serve(work: Path, yaml_path: Path, *, port: int, open_browser: bool) -> int:
    doc = _load_yaml(yaml_path)
    page = _page(doc, load_observed(work), load_traces(work))
    done = threading.Event()
    result: dict[str, int] = {"code": 1}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path not in ("/", "/index.html", "/review.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/apply":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                decisions = json.loads(raw.decode("utf-8"))
                current = _load_yaml(yaml_path)
                apply_decisions(current, decisions)
                if not kept_with_impact(current):
                    raise ValueError(
                        "Keep at least one journey and set business_impact. "
                        "The tool will not infer it."
                    )
                yaml_path.write_text(dump_journeys(current))
                write_stamp(work, current)
                write_review_md(work, current)
            except SystemExit as exc:
                msg = str(exc)
                body = msg.encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except (ValueError, json.JSONDecodeError) as exc:
                body = str(exc).encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            msg = "Wrote journeys.yaml. Next: ce report"
            body = msg.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result["code"] = 0
            done.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    host, bound = httpd.server_address
    url = f"http://{host}:{bound}/"
    (work / "review.html").write_bytes(page)
    print(f"review at {url}  (Ctrl-C to abort without saving)", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # After the server is accepting — tests poll this file for the bound port.
    (work / "review.url").write_text(url)
    try:
        while not done.wait(timeout=0.4):
            pass
    except KeyboardInterrupt:
        print("\naborted — journeys.yaml unchanged", file=sys.stderr)
        httpd.shutdown()
        httpd.server_close()
        return 1
    httpd.shutdown()
    httpd.server_close()
    return result["code"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ce review",
        description="Keep/drop proposed journeys and assign business_impact.")
    ap.add_argument("--work", default="ce-work")
    ap.add_argument("--apply", type=Path,
                    help="JSON decisions {journeys:[{id,keep,business_impact,...}]}. "
                         "No browser. For tests and scripts.")
    ap.add_argument("--stamp", action="store_true",
                    help="YAML already edited by hand. Verify at least one keeper "
                         "has business_impact and allow `ce report`.")
    ap.add_argument("--port", type=int, default=0,
                    help="0 picks a free localhost port.")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    work = Path(args.work)
    yaml_path = work / "journeys.yaml"
    if not yaml_path.is_file():
        print(f"error: {yaml_path} not found. Run `ce discover` first.", file=sys.stderr)
        return 1

    if args.apply:
        doc = _load_yaml(yaml_path)
        try:
            decisions = json.loads(args.apply.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot read {args.apply}: {exc}", file=sys.stderr)
            return 1
        apply_decisions(doc, decisions)
        if not kept_with_impact(doc):
            print("error: keep at least one journey and set business_impact.",
                  file=sys.stderr)
            return 1
        yaml_path.write_text(dump_journeys(doc))
        write_stamp(work, doc)
        write_review_md(work, doc)
        print(f"reviewed → {yaml_path}. Next: ce report", file=sys.stderr)
        return 0

    if args.stamp:
        doc = _load_yaml(yaml_path)
        if not kept_with_impact(doc):
            print("error: no journey has business_impact set. Uncomment it on "
                  "keepers in journeys.yaml, or run `ce review`. The tool will "
                  "not infer impact from traffic or source.", file=sys.stderr)
            return 1
        write_stamp(work, doc)
        write_review_md(work, doc)
        print(f"stamped {work / REVIEWED_NAME}. Next: ce report", file=sys.stderr)
        return 0

    return serve(work, yaml_path, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
