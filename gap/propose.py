#!/usr/bin/env python3
"""
Propose journey candidates from source. The missing producer for `discovered:code`.

`discovered:code` has been a first-class provenance source in the intake schema
since the start, and nothing produced it — `ce init` emitted a template with
placeholder steps (`started`, `submitted`, `confirmed`) and the human hand-authored
the rest. That put the single most time-consuming part of an engagement, reading
someone else's architecture, back on the SE. This closes it.

Code is the right seed, for three reasons established in the design:
  - **complete** — contains the rare-but-critical flows that sampling never shows
  - **semantically rich** — route and handler names carry vocabulary traces lack,
    and a payment-provider dependency is a stronger criticality signal than any
    span count
  - **non-circular** — traces only reveal what someone already instrumented, and
    instrumentation is the thing being sold

What it derives, and what it refuses to:

  DERIVED (roles 1–3, structural)   journey candidates from domain modules,
                                    ordered steps from routes and handlers,
                                    correlation-key candidates from identifiers
  PROPOSED (role 4)                 outcome values, when a state machine or a
                                    string-literal union is found
  CANDIDATES (roles 6–7)            magnitude and segment attributes, from field
                                    names that look like money and tenancy
  NEVER                             `business_impact`. Nothing in source says
                                    which flow earns revenue, and frequency is
                                    actively misleading as a proxy. A human
                                    assigns it or it stays unset.

Output is a journeys.yaml valid against intake/schema/journey-candidate.schema.json
with `source: discovered:code`, so `ce intake --discovered` consumes it directly and
the resolver ranks it below anything the customer actually declared.

Usage:
    ce propose --repo /path/to/service --out journeys.yaml
    ce propose --repo . --out journeys.yaml --report proposal.md --max-journeys 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

JS_SUFFIX = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
PY_SUFFIX = {".py"}
CODE = JS_SUFFIX | PY_SUFFIX
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "venv",
             ".venv", "__pycache__", "site-packages", ".tox", "target", "vendor",
             "migrations", "public", "static", "assets", "generated",
             ".aws", ".ssh", ".gnupg", ".secrets", ".env"}
SKIP_FILE = re.compile(r"\.(test|spec|stories|d)\.[tj]sx?$|^(test_|conftest\.py$)")
SECRET_FILES = {".env", "credentials.json", "credentials.yml", "credentials.yaml",
                "id_rsa", "id_ed25519", "service-account.json"}
SECRET_SUFFIX = {".pem", ".p12", ".pfx", ".key"}

# Directory names that are infrastructure, not business domains.
NOT_A_DOMAIN = {
    "src", "app", "lib", "libs", "utils", "util", "helpers", "common", "shared",
    "core", "config", "configs", "types", "typings", "models", "model", "schema",
    "schemas", "db", "database", "middleware", "middlewares", "components", "hooks",
    "context", "contexts", "styles", "assets", "pages", "api", "routes", "router",
    "controllers", "services", "repositories", "server", "client", "internal",
    "infra", "infrastructure", "scripts", "bin", "cmd", "test", "tests", "spec",
    "e2e", "fixtures", "mocks", "constants", "errors", "logger", "logging",
    "telemetry", "worker", "workers", "queue", "jobs", "tasks", "main", "index",
}

# --- routes ---------------------------------------------------------------
JS_ROUTE = re.compile(
    r"""\b(?:app|router|server|fastify|api)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.I)
PY_ROUTE = re.compile(
    r"""@\s*(?:\w+)\s*\.\s*(get|post|put|patch|delete|route)\s*\(\s*['"]([^'"]+)['"]""",
    re.I)

# --- exported / public functions ------------------------------------------
JS_EXPORT = re.compile(r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_]\w*)")
JS_EXPORT_CONST = re.compile(r"\bexport\s+const\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(")
PY_DEF = re.compile(r"^(?:async\s+)?def\s+([a-z_]\w*)\s*\(", re.M)

# --- state machines → outcome candidates ----------------------------------
# `export type CheckoutStatus = 'pending' | 'authorized' | ...`
# The optional-`type` alternation in the first version never matched the common
# `export type` form, so every TS state machine was missed and every journey came
# back with "no outcome values proposed" — the most valuable thing this can derive.
TS_UNION = re.compile(
    r"""(?:export\s+)?type\s+(\w*(?:Status|State|Outcome|Result)\w*)\s*=\s*"""
    r"""((?:\s*['"][a-z_][a-z0-9_]*['"]\s*\|)+\s*['"][a-z_][a-z0-9_]*['"])""")
PY_ENUM = re.compile(
    r"class\s+(\w*(?:Status|State|Outcome|Result))\s*\([^)]*\)\s*:((?:\s*\n\s+[A-Z_]+\s*=\s*['\"][a-z_]+['\"])+)")
PY_ENUM_MEMBER = re.compile(r"[A-Z_]+\s*=\s*['\"]([a-z_]+)['\"]")

# --- attribute candidates -------------------------------------------------
MONEY = re.compile(
    r"\b([a-z_]*(?:total|amount|subtotal|price|value|revenue|cents|minor|balance|fee|tax|discount)[a-z_]*)\b",
    re.I)
TENANT = re.compile(
    r"\b(org_?id|organization_?id|tenant_?id|account_?id|plan_?tier|plan|tier|"
    r"customer_?id|workspace_?id|role|segment|region|locale)\b", re.I)
ID_FIELD = re.compile(r"\b([a-z][a-z_]*?_?id)\b", re.I)

# --- dependency signals ---------------------------------------------------
PROVIDERS = {
    "stripe": "payments", "adyen": "payments", "braintree": "payments",
    "paypal": "payments", "square": "payments", "checkout.com": "payments",
    "razorpay": "payments", "mollie": "payments", "klarna": "payments",
    "shippo": "fulfillment", "easypost": "fulfillment", "twilio": "notifications",
    "sendgrid": "notifications", "postmark": "notifications", "resend": "notifications",
    "auth0": "identity", "clerk": "identity", "okta": "identity",
}

STEP_ORDER_HINTS = [
    # Rough lifecycle ordering, applied ONLY to function-derived steps (routes keep
    # declaration order). A heuristic, and it will be wrong on ambiguous verbs:
    # "review" is early in a checkout (cart review) and mid-flow in a refund
    # (approval). Ordering is always flagged as unverified in the output.
    "view", "open", "select", "add", "enter", "request", "invite",
    "start", "begin", "create", "init", "validate", "revalidate", "check",
    "submit", "review", "approve", "accept", "reserve", "authorize", "charge",
    "capture", "pay", "process", "settle", "persist", "save", "commit",
    "enqueue", "queue", "send", "notify", "complete", "confirm", "finish", "done",
]


def is_secret_path(p: Path) -> bool:
    """Skip credential files even if a suffix would otherwise match. A scan
    that opens `.env` or a PEM is how a customer loses trust in the tool."""
    if p.name in SECRET_FILES or p.name.startswith(".env."):
        return True
    return p.suffix.lower() in SECRET_SUFFIX


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in CODE:
            continue
        if any(part in SKIP_DIRS for part in p.parts) or SKIP_FILE.search(p.name):
            continue
        if is_secret_path(p):
            continue
        yield p


def has_supported_source(root: Path) -> bool:
    """JS/TS or Python files exist. Go/Ruby/Java produce nothing useful from
    propose; fail loud and point at `ce init` instead of emitting junk."""
    for _ in iter_files(root):
        return True
    return False


def slugify(name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return re.sub(r"_+", "_", s)


def titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("_"))


def singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses") or word.endswith("shes") or word.endswith("ches"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def outcome_key(type_name: str) -> str:
    """`CheckoutStatus` → `checkout`, the domain the state machine belongs to.

    Order matters and cost a debugging round: singularising first turned
    `checkout_status` into `checkout_statu`, so the later `_status` strip found
    nothing and every detected state machine failed to match its journey. Strip
    the suffix, then singularise.
    """
    slug = slugify(type_name)
    slug = re.sub(r"_(status|state|outcome|result)$", "", slug)
    return singular(slug)


def step_rank(name: str) -> int:
    """Lifecycle position of a step id, by whole-token match.

    Token-based, not substring: naive `hint in name` matched "view" inside
    "review", so `review_refund` sorted to position 0 and the refund journey came
    out as review → request → settle. Tokens are compared with equality or prefix,
    so "payment" still matches "pay" while "review" no longer matches "view".
    """
    best = len(STEP_ORDER_HINTS)
    for token in slugify(name).split("_"):
        for i, hint in enumerate(STEP_ORDER_HINTS):
            if token == hint or token.startswith(hint):
                best = min(best, i)
                break
    return best


# --------------------------------------------------------------------------


class Evidence:
    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self.funcs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.files: dict[str, set[str]] = defaultdict(set)
        self.outcomes: dict[str, list[str]] = {}
        self.money: set[str] = set()
        self.tenant: set[str] = set()
        self.ids: dict[str, int] = defaultdict(int)
        self.providers: dict[str, str] = {}
        self.surfaces: dict[str, str] = {}


def domain_of(path: Path, root: Path) -> str | None:
    """Business domain a file belongs to, from its directory path."""
    parts = [p for p in path.relative_to(root).parts[:-1]]
    for part in reversed(parts):
        slug = slugify(part)
        if slug and slug not in NOT_A_DOMAIN and not slug.isdigit():
            return singular(slug) if slug.endswith("s") else slug
    return None


def surface_of(path: Path, text: str) -> str:
    rel = str(path).lower()
    if path.suffix in {".tsx", ".jsx"} or "/components/" in rel or "/pages/" in rel:
        return "browser"
    if "worker" in rel or "job" in rel or "queue" in rel or "task" in rel:
        return "worker"
    if path.suffix in PY_SUFFIX:
        return "node" if False else "python"
    return "node"


def collect(root: Path) -> Evidence:
    ev = Evidence()

    # dependency manifests first — the strongest criticality signal available
    for man in ("package.json", "requirements.txt", "pyproject.toml", "Pipfile",
                "go.mod", "Gemfile"):
        p = root / man
        if p.exists():
            body = p.read_text(errors="replace").lower()
            for dep, kind in PROVIDERS.items():
                if dep in body:
                    ev.providers[dep] = kind

    for path in iter_files(root):
        text = path.read_text(errors="replace")
        dom = domain_of(path, root)
        surface = surface_of(path, text)
        rel = str(path.relative_to(root))

        # Next.js App Router: app/api/<domain>/<step>/route.ts
        parts = path.relative_to(root).parts
        if path.name in ("route.ts", "route.js", "handler.ts") and "api" in parts:
            i = parts.index("api")
            seg = [s for s in parts[i + 1:-1] if not s.startswith(("[", "("))]
            if seg:
                dom = singular(slugify(seg[0]))
                if len(seg) > 1:
                    ev.routes[dom].append(("POST", "/".join(seg), rel))
                    ev.surfaces.setdefault(dom, "node")

        for rx in (JS_ROUTE, PY_ROUTE):
            for m in rx.finditer(text):
                verb, route = m.group(1).upper(), m.group(2)
                segs = [s for s in route.strip("/").split("/")
                        if s and not s.startswith((":", "{", "<")) and s != "api"]
                if not segs:
                    continue
                rdom = singular(slugify(segs[0]))
                if rdom in NOT_A_DOMAIN:
                    continue
                ev.routes[rdom].append((verb, route, rel))
                ev.surfaces.setdefault(rdom, surface)

        if dom:
            ev.files[dom].add(rel)
            ev.surfaces.setdefault(dom, surface)
            for rx in (JS_EXPORT, JS_EXPORT_CONST):
                for m in rx.finditer(text):
                    ev.funcs[dom].append((m.group(1), rel))
            if path.suffix in PY_SUFFIX:
                for m in PY_DEF.finditer(text):
                    if not m.group(1).startswith("_"):
                        ev.funcs[dom].append((m.group(1), rel))

        # state machines → outcome candidates
        for m in TS_UNION.finditer(text):
            vals = re.findall(r"['\"]([a-z_]+)['\"]", m.group(2))
            key = outcome_key(m.group(1))
            if len(vals) >= 2:
                # Index under BOTH the type's own name and the file's domain. A
                # `InviteStatus` enum living in `src/onboarding/` belongs to the
                # onboarding journey, and keying only on the type name missed it.
                ev.outcomes.setdefault(key, vals)
                if dom:
                    ev.outcomes.setdefault(dom, vals)
        for m in PY_ENUM.finditer(text):
            vals = PY_ENUM_MEMBER.findall(m.group(2))
            key = outcome_key(m.group(1))
            if len(vals) >= 2:
                # Index under BOTH the type's own name and the file's domain. A
                # `InviteStatus` enum living in `src/onboarding/` belongs to the
                # onboarding journey, and keying only on the type name missed it.
                ev.outcomes.setdefault(key, vals)
                if dom:
                    ev.outcomes.setdefault(dom, vals)

        for m in MONEY.finditer(text):
            tok = m.group(1)
            if 3 < len(tok) < 40 and not tok.lower().startswith(("get", "set", "is")):
                ev.money.add(tok)
        for m in TENANT.finditer(text):
            ev.tenant.add(m.group(1))
        for m in ID_FIELD.finditer(text):
            ev.ids[m.group(1)] += 1

    return ev


# --------------------------------------------------------------------------


def build_journeys(ev: Evidence, max_journeys: int) -> tuple[list[dict], list[dict]]:
    domains = set(ev.routes) | set(ev.files)
    domains = {d for d in domains if d and d not in NOT_A_DOMAIN}

    scored = []
    for d in domains:
        score = (len(ev.routes.get(d, [])) * 3
                 + min(len(ev.funcs.get(d, [])), 8)
                 + len(ev.files.get(d, set())))
        if any(k == d or d in k for k in ev.providers.values()):
            score += 6
        scored.append((score, d))
    scored.sort(key=lambda x: (-x[0], x[1]))

    journeys, report = [], []
    for score, dom in scored[:max_journeys]:
        steps, seen = [], set()
        from_routes = bool(ev.routes.get(dom))

        for verb, route, rel in ev.routes.get(dom, []):
            segs = [s for s in route.strip("/").split("/")
                    if s and not s.startswith((":", "{", "<")) and s not in ("api", dom)]
            sid = slugify("_".join(segs)) or slugify(verb.lower() + "_" + dom)
            if sid in seen:
                continue
            seen.add(sid)
            steps.append({"id": sid, "surface": ev.surfaces.get(dom, "node"),
                          "evidence": f"{verb} {route} ({rel})"})

        if len(steps) < 2:
            for fname, rel in ev.funcs.get(dom, [])[:12]:
                sid = slugify(fname)
                if sid in seen or len(sid) < 3:
                    continue
                seen.add(sid)
                steps.append({"id": sid, "surface": ev.surfaces.get(dom, "node"),
                              "evidence": f"function {fname}() ({rel})"})

        if len(steps) < 2:
            report.append({"journey": dom, "skipped": "fewer than 2 identifiable steps"})
            continue

        # Routes keep the order they were declared in; only function-derived steps
        # get reordered by lifecycle hints.
        #
        # Hint-sorting routes actively produced a WRONG journey: `shipping` has no
        # lifecycle keyword so it ranked last, landing after `payment` and
        # `confirm`. Declaration order at least reflects how the developer wrote the
        # flow. A visibly wrong order costs more trust than an unordered list, and
        # either way the order is flagged as unverified below.
        if not from_routes:
            for idx, s in enumerate(steps):
                s["_i"] = idx
            steps.sort(key=lambda s: (step_rank(s["id"]), s["_i"]))
            for s in steps:
                s.pop("_i", None)
        steps = steps[:9]

        outcome_vals = ev.outcomes.get(dom) or ev.outcomes.get(singular(dom))
        corr = f"{dom}.id"
        for cand in (f"{dom}_id", f"{dom}Id"):
            if ev.ids.get(cand):
                corr = f"{dom}.id"
                break

        # Prefer money fields that name this domain; fall back to generic ones.
        own = sorted(m for m in ev.money if dom in slugify(m))
        money = (own or sorted(m for m in ev.money if any(
            k in slugify(m) for k in ("total", "amount", "value", "minor"))))[:2]

        # Normalise and dedupe segment candidates: `orgId` and `org_id` are the same
        # attribute written twice, and emitting both makes the proposal look careless.
        seg_norm: list[str] = []
        for raw in sorted(ev.tenant):
            attr = slugify(raw).replace("_", ".")
            attr = re.sub(r"\.id$", ".id", attr)
            if attr not in seg_norm:
                seg_norm.append(attr)
        seg = seg_norm[:2]

        j: dict = {
            "id": dom,
            "name": titleize(dom),
            "source": "discovered:code",
            "confidence": "high" if score >= 10 else ("medium" if score >= 5 else "low"),
            "correlation_key": {"attribute": corr,
                                "persists_across": ["service"]},
            "steps": steps,
            "needs_clarification": [
                "business_impact is unset — a human must assign it. Nothing in "
                "source says which flow earns revenue, and traffic volume is "
                "actively misleading as a proxy.",
                (f"Confirm the step order for `{dom}` — it is route DECLARATION "
                 "order, which is not necessarily execution order."
                 if from_routes else
                 f"Confirm the step order for `{dom}` — it was guessed from function "
                 "names (verbs like validate/authorize/confirm), not from execution."),
            ],
            "notes": (f"Proposed from {len(ev.files.get(dom, set()))} file(s), "
                      f"{len(ev.routes.get(dom, []))} route(s), "
                      f"{len(ev.funcs.get(dom, []))} exported function(s)."),
        }

        if outcome_vals:
            j["outcome"] = {"attribute": f"{dom}.outcome", "values": outcome_vals}
            j["needs_clarification"].append(
                f"Outcome values {outcome_vals} came from a state machine in the "
                "source. Confirm which count as success — that decision is not in "
                "the code.")
        else:
            j["needs_clarification"].append(
                "No state machine found, so no outcome values are proposed. Ask "
                "the customer how this flow terminates, including partial success.")

        if money:
            mag = []
            for m in money[:1]:
                slug = slugify(m)
                # `*_minor` / `*_cents` means integer minor units, which is the
                # right way to carry money and rules out the stringified-float
                # defect. Calling it a double would misdescribe the field.
                minor = bool(re.search(r"(minor|cents)$", slug))
                entry = {"attribute": f"{dom}.{slug}",
                         "type": "int" if minor else "double"}
                if minor:
                    entry["unit"] = "minor_currency_unit"
                mag.append(entry)
            j["magnitude"] = mag
            j["needs_clarification"].append(
                f"Magnitude candidate(s) from field names: {money}. Confirm the unit"
                + (" — inferred as integer minor units from the field name."
                   if mag[0]["type"] == "int"
                   else " and whether the value is stored in minor units."))
        if seg:
            j["actor_segment"] = [{"attribute": s} for s in seg]

        journeys.append(j)
        report.append({"journey": dom, "score": score, "steps": len(steps),
                       "confidence": j["confidence"],
                       "routes": ev.routes.get(dom, [])[:4],
                       "outcome_from_source": outcome_vals,
                       "money_candidates": money, "segment_candidates": seg})

    return journeys, report


def to_yaml(journeys: list[dict]) -> str:
    def q(s: str) -> str:
        return '"' + str(s).replace('"', '\\"') + '"'

    L = [
        "# PROPOSED by `ce propose` from source. A DRAFT, not a decision.",
        "#",
        "# Structural roles (journey, correlation key, steps) are derived from routes,",
        "# handlers, and directory layout. Semantic roles are not, and cannot be:",
        "#   - business_impact is deliberately UNSET. Nothing in source says which",
        "#     flow earns revenue, and traffic volume is a misleading proxy.",
        "#   - outcome values are proposed only where a state machine was found, and",
        "#     which of them count as success is a human decision.",
        "#",
        "# Review every entry, delete the journeys that do not matter, then feed this",
        "# to `ce intake --discovered`. Change `source: declared` on anything the",
        "# customer actually confirmed — declared journeys outrank proposals.",
        "",
        "version: 1",
        "",
        "journeys:",
    ]
    for j in journeys:
        L.append(f"  - id: {j['id']}")
        L.append(f"    name: {q(j['name'])}")
        L.append(f"    source: {j['source']}")
        L.append(f"    confidence: {j['confidence']}")
        L.append("    # business_impact: critical    # <- UNCOMMENT AND SET (human only)")
        if j.get("notes"):
            L.append(f"    notes: {q(j['notes'])}")
        ck = j["correlation_key"]
        L.append("    correlation_key:")
        L.append(f"      attribute: {ck['attribute']}")
        L.append(f"      persists_across: [{', '.join(ck['persists_across'])}]")
        L.append("    steps:")
        for s in j["steps"]:
            L.append(f"      - id: {s['id']}")
            L.append(f"        surface: {s['surface']}")
            L.append(f"        evidence: {q(s['evidence'])}")
        if j.get("outcome"):
            o = j["outcome"]
            L.append("    outcome:")
            L.append(f"      attribute: {o['attribute']}")
            L.append(f"      values: [{', '.join(o['values'])}]")
            L.append("      # success_values: [...]   # <- SET THIS (human only)")
        if j.get("magnitude"):
            L.append("    magnitude:")
            for m in j["magnitude"]:
                L.append(f"      - attribute: {m['attribute']}")
                L.append(f"        type: {m['type']}")
                if m.get("unit"):
                    L.append(f"        unit: {m['unit']}")
        if j.get("actor_segment"):
            L.append("    actor_segment:")
            for s in j["actor_segment"]:
                L.append(f"      - attribute: {s['attribute']}")
        if j.get("needs_clarification"):
            L.append("    needs_clarification:")
            for q_ in j["needs_clarification"]:
                L.append(f"      - {q(q_)}")
        L.append("")
    return "\n".join(L) + "\n"


def render_report(ev: Evidence, report: list[dict], root: Path) -> str:
    L = [f"# Journey proposal — `{root}`\n"]
    kept = [r for r in report if "score" in r]
    L.append(f"**{len(kept)} candidate journey(s)** proposed from source.\n")
    if ev.providers:
        L.append("## Dependency signals\n")
        L.append("A third-party provider in the manifest is a stronger criticality "
                 "signal than any span count.\n")
        for dep, kind in sorted(ev.providers.items()):
            L.append(f"- `{dep}` → {kind}")
        L.append("")
    L.append("## Candidates\n")
    L.append("| Journey | Confidence | Steps | Outcome from source | Money candidates |")
    L.append("| --- | --- | --- | --- | --- |")
    for r in kept:
        L.append(f"| `{r['journey']}` | {r['confidence']} | {r['steps']} | "
                 f"{', '.join(r['outcome_from_source'] or []) or '—'} | "
                 f"{', '.join(r['money_candidates']) or '—'} |")
    L.append("")
    skipped = [r for r in report if "skipped" in r]
    if skipped:
        L.append("## Not proposed\n")
        for r in skipped:
            L.append(f"- `{r['journey']}` — {r['skipped']}")
        L.append("")
    L.append("## What a human still owns\n")
    L.append("- **business_impact** on every journey. Source cannot say which flow "
             "earns revenue, and volume is a misleading proxy — health checks "
             "dominate traffic while refunds are rare and expensive.")
    L.append("- **Which outcome values count as success**, including partial "
             "success. A charge captured with fulfillment failed is not a success "
             "and no state machine says so.")
    L.append("- **Deleting the journeys that do not matter.** Over-instrumenting "
             "crowds out the flows that do; 2–3 per engagement is the working rule.")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Propose journey candidates from a codebase.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True, help="journeys.yaml to write")
    ap.add_argument("--report", help="Markdown explaining the evidence per candidate")
    ap.add_argument("--max-journeys", type=int, default=6)
    ap.add_argument("--json", help="Also write the raw evidence as JSON")
    args = ap.parse_args(argv)

    root = Path(args.repo)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    ev = collect(root)
    journeys, report = build_journeys(ev, args.max_journeys)
    if not journeys:
        print("error: no journey candidates found. Either the layout is flat "
              "(no domain directories, no routes) or the source is not JS/TS or "
              "Python. Fall back to `ce init` and author the journey by hand.",
              file=sys.stderr)
        return 2

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(to_yaml(journeys))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(render_report(ev, report, root))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"providers": ev.providers, "candidates": report}, indent=2, default=list) + "\n")

    print(f"proposed {len(journeys)} journey(s) → {args.out}", file=sys.stderr)
    for r in report:
        if "score" in r:
            print(f"  {r['journey']:<20} {r['confidence']:<7} {r['steps']} steps"
                  + (f" · outcome {r['outcome_from_source']}"
                     if r["outcome_from_source"] else ""), file=sys.stderr)
    print("\nbusiness_impact is unset on every candidate — assign it before "
          "running `ce intake`. Nothing in source can decide it.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
