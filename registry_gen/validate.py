#!/usr/bin/env python3
"""
Validate generated (and hand-written) registry YAML against the OFFICIAL
OpenTelemetry semconv JSON schema.

Why this exists: `weaver registry check` is the real validator, but the weaver
binary is a Rust artifact that may not be installable everywhere (no cargo, or
a proxy that blocks GitHub releases). This is the strongest substitute available
offline — `schemas/semconv.schema.json` from the weaver repo is the same schema
VS Code validates semconv files against.

It checks what the JSON schema can see, plus the constraints weaver enforces in
code that the JSON schema does NOT express:

  - `prefix:` is rejected outright (Error::InvalidGroupUsesPrefix)
  - attributes may only be DEFINED in `attribute_group`s whose id starts with
    `registry.` (prose in semantic-conventions/model/README.md)
  - a `ref` must not carry id/type/stability/deprecated
  - every `ref` must resolve to a locally defined attribute, or be a known
    upstream OTel attribute
  - `examples` required on string / string[] attribute definitions
  - manifest: `schema_url` required; `name`/`semconv_version`/`schema_base_url`
    deprecated; dependencies need `schema_url`

Usage:
    ./validate.py --registry out/
    ./validate.py --registry out/ --registry ../registry/   # several at once
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SCHEMA_URL = (
    "https://raw.githubusercontent.com/open-telemetry/weaver/main/"
    "schemas/semconv.schema.json"
)
SCHEMA_CACHE = Path(__file__).parent / "semconv.schema.json"

DEPRECATED_MANIFEST_KEYS = ("name", "semconv_version", "schema_base_url")
STRINGY = ("string", "string[]", "template[string]", "template[string[]]")

# Upstream OTel attributes a generated registry may legitimately reference. Kept
# short and explicit: a typo'd ref is a real bug and a permissive allowlist hides
# it. Extend deliberately.
KNOWN_UPSTREAM = {
    "http.request.method", "http.response.status_code", "http.route",
    "server.address", "server.port", "client.address", "url.full", "url.path",
    "db.system", "db.operation", "db.collection.name",
    "user.id", "session.id", "error.type",
}


def load_schema() -> dict | None:
    """Fetch or load the official semconv JSON schema. Returns None if
    unavailable.

    Degrades rather than hard-fails on purpose: the in-code constraint checks
    below (rejected `prefix:`, `registry.` definition scoping, ref hygiene, ref
    resolution, required examples) are the half the JSON schema does NOT express,
    and they are the half that catches real authoring bugs. A locked-down CI
    runner with no egress should still get them.
    """
    if SCHEMA_CACHE.exists():
        return json.loads(SCHEMA_CACHE.read_text())
    try:
        with urllib.request.urlopen(SCHEMA_URL, timeout=30) as r:
            data = r.read().decode()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN  semconv JSON schema unavailable ({type(exc).__name__}); running "
              "structural checks only.")
        print(f"WARN  to enable it, save {SCHEMA_URL}")
        print(f"WARN  to {SCHEMA_CACHE}")
        return None
    SCHEMA_CACHE.write_text(data)
    return json.loads(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a semconv registry offline.")
    ap.add_argument("--registry", action="append", required=True, type=Path)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        import yaml
        from jsonschema import Draft202012Validator
    except ImportError:
        sys.exit("error: pip install pyyaml jsonschema --break-system-packages")

    schema = load_schema()
    validator = Draft202012Validator(schema) if schema else None
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0

    for root in args.registry:
        if not root.is_dir():
            errors.append(f"{root}: not a directory")
            continue

        # ---- manifest ----
        manifest = root / "manifest.yaml"
        legacy = root / "registry_manifest.yaml"
        if legacy.exists() and not manifest.exists():
            warnings.append(f"{legacy}: legacy filename, rename to manifest.yaml")
            manifest = legacy
        if not manifest.exists():
            errors.append(f"{root}: no manifest.yaml")
        else:
            checked += 1
            m = yaml.safe_load(manifest.read_text()) or {}
            if not m.get("schema_url"):
                errors.append(f"{manifest}: missing required `schema_url`")
            for k in DEPRECATED_MANIFEST_KEYS:
                if k in m:
                    warnings.append(f"{manifest}: `{k}` is deprecated")
            deps = m.get("dependencies") or []
            if len(deps) > 1:
                warnings.append(f"{manifest}: {len(deps)} dependencies; weaver "
                                "currently supports at most one (weaver#604)")
            for d in deps:
                if not isinstance(d, dict) or not d.get("schema_url"):
                    errors.append(f"{manifest}: dependency without `schema_url` "
                                  "hard-fails in weaver")

        # ---- groups ----
        defined: set[str] = set()
        refs: list[tuple[str, str]] = []

        for path in sorted(root.rglob("*.yaml")):
            if path.name in ("manifest.yaml", "registry_manifest.yaml"):
                continue
            checked += 1
            doc = yaml.safe_load(path.read_text()) or {}

            if validator is not None:
                for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
                    errors.append(f"{path}: {list(e.path)}: {e.message[:200]}")

            for g in doc.get("groups") or []:
                gid, gtype = g.get("id", "?"), g.get("type")
                if "prefix" in g:
                    errors.append(f"{path}: group `{gid}` uses `prefix:`, which weaver "
                                  "rejects (InvalidGroupUsesPrefix)")
                if gtype != "attribute_group" and not g.get("stability"):
                    errors.append(f"{path}: group `{gid}` type `{gtype}` requires "
                                  "`stability`")
                if gtype == "span" and not g.get("span_kind"):
                    errors.append(f"{path}: span group `{gid}` requires `span_kind`")

                for a in g.get("attributes") or []:
                    if "ref" in a:
                        for banned in ("id", "type", "stability", "deprecated"):
                            if banned in a:
                                errors.append(f"{path}: ref `{a['ref']}` in `{gid}` "
                                              f"must not carry `{banned}`")
                        refs.append((str(path), a["ref"]))
                        continue
                    # a definition
                    if gtype != "attribute_group":
                        errors.append(f"{path}: attribute `{a.get('id')}` defined in "
                                      f"`{gid}` (type {gtype}); definitions belong in "
                                      "an attribute_group")
                    elif not gid.startswith("registry."):
                        errors.append(f"{path}: attribute `{a.get('id')}` defined in "
                                      f"`{gid}`, which does not start with `registry.`")
                    defined.add(a.get("id"))
                    if a.get("type") in STRINGY and not a.get("examples"):
                        errors.append(f"{path}: attribute `{a.get('id')}` is "
                                      f"`{a['type']}` and needs `examples`")
                    if isinstance(a.get("type"), dict):
                        for mem in a["type"].get("members") or []:
                            for req in ("id", "value", "stability"):
                                if req not in mem:
                                    errors.append(
                                        f"{path}: enum member in `{a.get('id')}` "
                                        f"missing `{req}`")

        for path, ref in refs:
            if ref not in defined and ref not in KNOWN_UPSTREAM:
                errors.append(f"{path}: ref `{ref}` resolves to neither a local "
                              "definition nor a known upstream OTel attribute")

    if not args.quiet:
        for w in warnings:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
    mode = "JSON schema + structural" if validator else "structural only"
    print(f"\n{checked} file(s) checked ({mode}) · {len(errors)} error(s) · "
          f"{len(warnings)} warning(s)")
    print("PASS" if not errors else "FAIL")
    print("\nNote: not a substitute for `weaver registry check --future`. Run that in "
          "CI where the binary is available; this covers the JSON schema plus the "
          "in-code constraints weaver enforces that the schema does not express.")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
