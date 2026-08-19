#!/usr/bin/env python3
"""
Static code scan → `observed.json`. The path for services with no live telemetry.

Three starting states have to work equally well, and only one of them has spans
to read:

  A. Sentry installed, no journey coverage    → telemetry works; use `ce local`
  B. **Sentry not installed at all**          → no telemetry exists. This.
  C. Some custom instrumentation, improvable  → either; this shows intent even
                                                where a path never got exercised

For B and C, the source is the only evidence available. This produces the same
`observed.json` shape the rest of the pipeline consumes, so `ce gap`,
`ce profile`, and `ce spec` all work unchanged — with provenance stamped so
nobody mistakes intent for behaviour.

**What this can and cannot tell you.** It sees instrumentation that was *written*.
It cannot see whether it *runs*, how often, or what a value's runtime type is —
`cart.value: cart.total` could be either. Counts are synthetic, so
`_synthetic_counts` is set and the gap analyzer suppresses extent rather than
printing a fabricated percentage.

Languages: JS/TS (`@sentry/*`) and Python (`sentry_sdk`). Both SDK families are
detected separately, so "no SDK anywhere" is reported as a finding rather than as
zero coverage — those need different conversations.

Usage:
    ce scan --repo /path/to/service --out observed.json
    ce scan --repo . --out observed.json --org acme --journey-prefix checkout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "eval"))

JS_SUFFIX = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
PY_SUFFIX = {".py"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage", "venv",
             ".venv", "__pycache__", "site-packages", ".tox", "target", "vendor",
             ".aws", ".ssh", ".gnupg", ".secrets", ".env"}
SKIP_NAME = re.compile(r"\.(test|spec|stories|d)\.[tj]sx?$|^(test_|conftest\.py$)")
SECRET_FILES = {".env", "credentials.json", "credentials.yml", "credentials.yaml",
                "id_rsa", "id_ed25519", "service-account.json"}
SECRET_SUFFIX = {".pem", ".p12", ".pfx", ".key"}

# --- SDK presence -----------------------------------------------------------
JS_SDK = re.compile(r"""["']@sentry/(node|browser|react|nextjs|vue|angular|svelte|"""
                    r"""remix|sveltekit|astro|bun|deno|aws-serverless|google-cloud"""
                    r"""-serverless|nestjs|solid|solidstart|ember|gatsby|electron|"""
                    r"""react-native|capacitor|cloudflare|core)["']""")
# Any mention of the module counts. Matching only `import sentry_sdk` missed
# `import os, sentry_sdk` — a comma-separated import — and reported a fully
# instrumented service as having no SDK at all.
PY_SDK = re.compile(r"\bsentry_sdk\b")
JS_INIT = re.compile(r"Sentry\.init\s*\(")
PY_INIT = re.compile(r"sentry_sdk\.init\s*\(")

# --- Python instrumentation -------------------------------------------------
PY_SPAN_START = re.compile(r"sentry_sdk\.start_(transaction|span)\s*\(")
PY_NAME_KW = re.compile(r"""\b(?:name|description)\s*=\s*['"]([^'"]+)['"]""")
PY_OP_KW = re.compile(r"""\bop\s*=\s*['"]([^'"]+)['"]""")
PY_SET_ATTR = re.compile(r"""\.set_(?:attribute|data|tag)\s*\(\s*['"]([^'"]+)['"]""")


def _is_secret_path(p: Path) -> bool:
    if p.name in SECRET_FILES or p.name.startswith(".env."):
        return True
    return p.suffix.lower() in SECRET_SUFFIX


def _iter_files(root: Path, suffixes: set[str]):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in suffixes:
            continue
        if any(part in SKIP_DIRS for part in p.parts) or SKIP_NAME.search(p.name):
            continue
        if _is_secret_path(p):
            continue
        yield p


def scan_python(root: Path) -> tuple[dict[str, str], dict[str, list[str]], bool, bool]:
    """Returns (name→op, attribute→[value exprs], sdk_imported, sdk_initialised)."""
    from grade import _balanced, _split_top_level, strip_comments  # reuse the JS helpers

    names: dict[str, str] = {}
    attrs: dict[str, list[str]] = {}
    imported = initialised = False

    for path in _iter_files(root, PY_SUFFIX):
        raw = path.read_text(errors="replace")
        # Python comments are `#`; strip_comments handles // and /* */. Do a cheap
        # line-level pass instead, preserving string literals well enough.
        text = "\n".join(re.sub(r"(?<!['\"])#.*$", "", ln) for ln in raw.splitlines())
        if PY_SDK.search(raw):
            imported = True
        if PY_INIT.search(raw):
            initialised = True

        for m in PY_SPAN_START.finditer(text):
            paren = text.find("(", m.end() - 1)
            body, _ = _balanced(text, paren, "(", ")")
            nm = PY_NAME_KW.search(body)
            if nm:
                op = PY_OP_KW.search(body)
                names[nm.group(1)] = op.group(1) if op else ""

        for m in PY_SET_ATTR.finditer(text):
            paren = text.rfind("(", 0, m.end())
            body, _ = _balanced(text, paren, "(", ")")
            parts = _split_top_level(body)
            if len(parts) >= 2:
                attrs.setdefault(m.group(1), []).append(parts[1].strip())
    return names, attrs, imported, initialised


def scan_js(root: Path) -> tuple[dict[str, str], dict[str, list[str]], bool, bool]:
    from grade import NAME_KEY, SPAN_START, _balanced, scan_repo, strip_comments

    s = scan_repo(root)                       # reuses the tested grader scanner
    names: dict[str, str] = {n: "" for n in s.span_names}

    # scan_repo does not retain the op, and the profile needs it.
    op_key = re.compile(r"""\bop\s*:\s*['"`]([^'"`]+)['"`]""")
    imported = initialised = False
    for path in _iter_files(root, JS_SUFFIX):
        raw = path.read_text(errors="replace")
        if JS_SDK.search(raw):
            imported = True
        if JS_INIT.search(raw):
            initialised = True
        text = strip_comments(raw)
        for m in SPAN_START.finditer(text):
            paren = text.find("(", m.end() - 1)
            body, _ = _balanced(text, paren, "(", ")")
            head = body[:400]
            nm, op = NAME_KEY.search(head), op_key.search(head)
            if nm and op:
                names[nm.group(1)] = op.group(1)
    return names, dict(s.attributes), imported, initialised


SDK_PREFIXES = ("sentry.", "http.", "db.", "server.", "client.", "url.", "otel.",
                "user_agent.", "network.", "messaging.", "gen_ai.", "browser.",
                "device.", "os.", "process.", "thread.", "code.", "span.", "trace.",
                "resource.", "faas.", "cloud.", "k8s.", "telemetry.", "service.")

STRINGY = ("String(", ".toString(", ".toFixed(", "JSON.stringify(", "str(", "f\"", "f'")


def guess_type(values: list[str]) -> str:
    """Source can rarely prove a type. Only claim one when the literal says so."""
    for v in values:
        v = v.strip()
        if re.fullmatch(r"""['"`].*['"`]""", v, re.S) or any(t in v for t in STRINGY):
            return "string"
        if v in ("True", "False", "true", "false"):
            return "boolean"
    for v in values:
        v = v.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", v) or re.search(
                r"\b(Number|parseFloat|parseInt|float|int)\s*\(", v):
            return "number"
    return "string"      # unprovable → the conservative choice, flagged below


def build(root: Path, org: str) -> dict:
    js_names, js_attrs, js_imp, js_init = scan_js(root)
    py_names, py_attrs, py_imp, py_init = scan_python(root)

    names = {**js_names, **py_names}
    attrs: dict[str, list[str]] = {}
    for d in (js_attrs, py_attrs):
        for k, v in d.items():
            attrs.setdefault(k, []).extend(v)

    unprovable = []
    attributes = []
    for key in sorted(attrs):
        t = guess_type(attrs[key])
        literal = any(
            re.fullmatch(r"""['"`].*['"`]|-?\d+(\.\d+)?""", v.strip(), re.S)
            or v.strip() in ("True", "False", "true", "false")
            for v in attrs[key])
        if not literal:
            unprovable.append(key)
        attributes.append({
            "key": key, "name": key, "attributeType": t,
            "attributeSource": {"source_type": "sentry" if key.startswith(SDK_PREFIXES)
                                else "user"},
            "type_from_source": "literal" if literal else "unprovable",
            "source_expressions": attrs[key][:3],
        })

    langs: set[str] = set()
    for p in _iter_files(root, JS_SUFFIX | PY_SUFFIX):
        if p.suffix in JS_SUFFIX:
            langs.add("javascript")
        elif p.suffix in PY_SUFFIX:
            langs.add("python")

    return {
        "org": org,
        "dataset": "spans",
        "stats_period": "static-code-scan",
        "traces_sample_rate": None,
        # Counts are synthetic — every span "seen" once, because source has no
        # frequency. `_synthetic_counts` tells the analyzer to suppress extent
        # instead of printing a fabricated percentage.
        "span_names": [{"name": n, "count": 1} for n in sorted(names)],
        "span_ops": [{"op": o, "count": 1} for o in sorted({v for v in names.values() if v})],
        "span_pairs": [{"name": n, "op": o, "count": 1} for n, o in sorted(names.items())],
        "attributes": attributes,
        "example_traces": {},
        "_synthetic_counts": True,
        "sdk": {
            "javascript": {"imported": js_imp, "initialised": js_init},
            "python": {"imported": py_imp, "initialised": py_init},
            # `init(...)` counts as presence on its own: an import can be missed by
            # a regex (aliased, re-exported, comma-separated), but an init call is
            # unambiguous proof the SDK is in the tree.
            "any_sdk_present": js_imp or py_imp or js_init or py_init,
            "any_sdk_initialised": js_init or py_init,
        },
        "source_languages": sorted(langs),
        "_provenance": {
            "source": f"static code scan of {root} (gap/code_scan.py)",
            "means": "instrumentation that was WRITTEN. It does not show whether the "
                     "code runs, how often, or what a value's runtime type is.",
            "counts": "SYNTHETIC (1 per span). Extent is suppressed downstream.",
            "attributeType": "inferred from literals where possible; otherwise "
                             "defaulted to string and listed in unprovable_types",
            "attributeSource": "HEURISTIC (namespace prefix)",
            "source_languages": "file suffixes walked, excluding SKIP_DIRS — used by "
                                "spec generation when no SDK is installed (State B)",
            "next": "run `ce local` once the service emits, to confirm these spans "
                    "actually execute and to resolve value types",
        },
        "unprovable_types": unprovable,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Derive an observed.json from source, for services with no telemetry.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--org", default="static-scan")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.repo)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    doc = build(root, args.org)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n")

    sdk = doc["sdk"]
    n_spans, n_user = len(doc["span_names"]), sum(
        1 for a in doc["attributes"] if a["attributeSource"]["source_type"] == "user")
    if not args.quiet:
        if not sdk["any_sdk_present"]:
            print("No Sentry SDK found in this repo.\n"
                  "  This is state B: nothing to validate against telemetry. The journey "
                  "spec will need to include SDK install and init.\n"
                  "  `ce spec --include-absent` generates it anyway.", file=sys.stderr)
        elif not sdk["any_sdk_initialised"]:
            print("Sentry SDK is imported but no `init(...)` call was found — "
                  "check whether initialisation lives in config or a framework hook.",
                  file=sys.stderr)
        langs = [k for k in ("javascript", "python")
                 if sdk[k]["imported"] or sdk[k]["initialised"]]
        print(f"scanned {root} · SDK: {', '.join(langs) or 'none'} · "
              f"{n_spans} span name(s) · {n_user} customer-defined attribute(s) → {args.out}",
              file=sys.stderr)
        if doc["unprovable_types"]:
            print(f"note: {len(doc['unprovable_types'])} attribute type(s) not provable "
                  "from source — confirm with `ce local`: "
                  + ", ".join(doc["unprovable_types"][:5]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
