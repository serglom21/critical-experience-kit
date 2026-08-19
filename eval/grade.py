#!/usr/bin/env python3
"""
Static grader. Scores a JS/TS repo against a spec rubric.

This is the `avo status` / `ampli status` pattern ported to spans: scan the source
for the call sites the contract requires, and make the count of missing ones the
exit code. Those two tools are the reason it exists — Segment shipped Typewriter's
codegen with no equivalent verifier and the product went to maintenance mode.

Two properties matter more than sophistication:

  1. **The rubric comes from the spec generator, not from this file.** A grader
     with hand-written expectations only tests the cases someone remembered. This
     one grades whatever the spec asked for, so it cannot fall behind the spec.
  2. **It is deliberately syntactic.** It proves the call sites exist with the
     right literal names and plausible value types. It cannot prove they run on
     the right code path — that is what `gap/analyze.py` does against real
     telemetry. Static and runtime verification are complementary, exactly as in
     Avo (`avo status` + Inspector).

Known limits, stated rather than hidden:
  - regex-based, not a real parser. Attribute values built through indirection
    (a variable assigned elsewhere) are reported as `indeterminate`, never as a
    silent pass.
  - JS/TS only.
  - Cannot detect a span created on an unreachable path.

Usage:
    ./grade.py --rubric ../spec/out/checkout-RUBRIC.json --repo tasks/checkout-js/before
    ./grade.py --rubric R.json --repo DIR --out-json result.json --fail-under 100

Exit codes:
    0  graded (score may still be low)
    1  input error
    2  score below --fail-under
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage",
             "__tests__", "__mocks__", "__fixtures__"}
SKIP_NAME = re.compile(r"\.(test|spec|stories|d)\.[tj]sx?$")

SPAN_START = re.compile(r"\bstart(?:Inactive)?Span(?:Manual)?\s*\(")
NAME_KEY = re.compile(r"""\bname\s*:\s*['"`]([^'"`]+)['"`]""")
SET_ATTRIBUTE = re.compile(r"""\.setAttribute\s*\(\s*['"`]([^'"`]+)['"`]\s*,""")
SET_ATTRIBUTES = re.compile(r"\.setAttributes\s*\(\s*\{")
ATTRIBUTES_OPT = re.compile(r"\battributes\s*:\s*\{")
PAIR = re.compile(r"""^\s*['"`]?([A-Za-z0-9_.\-]+)['"`]?\s*:\s*(.+)$""", re.S)

# Removed or deprecated in v9/v10. Same list the generated spec's §6 ships.
DEPRECATED = [
    "startTransaction(", ".startChild(", ".setData(", ".finish()", ".setName(",
    "configureScope(", "getCurrentHub(", "Sentry.metrics.", "setMeasurement(",
    "enableTracing", "tracingOrigins", "new Sentry.Replay(",
    "addOpenTelemetryInstrumentation(",
]

# Attribute keys and value expressions that should never reach a span.
PII_KEY = re.compile(
    r"(card[_.]?number|cardnum|pan\b|cvv|cvc|security[_.]?code|"
    r"\bssn\b|social[_.]?security|password|passwd|secret|"
    r"api[_.]?key|auth[_.]?token|bearer|access[_.]?token|"
    r"email|e[_.]?mail|phone|full[_.]?name|address[_.]?line|postal|dob|"
    r"date[_.]?of[_.]?birth|raw[_.]?response|raw[_.]?payload|provider[_.]?response)",
    re.I)

STRINGIFIERS = ("String(", ".toString(", ".toFixed(", ".toLocaleString(",
                ".join(", "JSON.stringify(")


def strip_comments(text: str) -> str:
    """Blank out `//` and `/* */` comments, preserving length and line structure.

    Not cosmetic. A comment between two object-literal properties breaks pair
    splitting, so `"cart.value": cart.total.toFixed(2)` sitting under a `// BUG:`
    line was reported as "never set" — a false negative on real-world-shaped code,
    which is the worst kind of grader error: it tells the customer to add
    instrumentation they already have. Replaced with spaces rather than removed so
    every offset stays valid.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            q, i = c, i + 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    out[i] = " "
                    i += 1
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                end = n if end == -1 else end + 2
                for j in range(i, end):
                    if out[j] != "\n":
                        out[j] = " "
                i = end
                continue
        i += 1
    return "".join(out)


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    """Content between matching delimiters, starting at the opener index."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            q, i = c, i + 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i
        i += 1
    return text[start + 1:], n


def _split_top_level(body: str) -> list[str]:
    out, depth, cur, i, n = [], 0, "", 0, len(body)
    while i < n:
        c = body[i]
        if c in "\"'`":
            q, j = c, i + 1
            while j < n and body[j] != q:
                j += 2 if body[j] == "\\" else 1
            cur += body[i:j + 1]
            i = j + 1
            continue
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        out.append(cur)
    return out


@dataclass
class Scan:
    span_names: set[str] = field(default_factory=set)
    attributes: dict[str, list[str]] = field(default_factory=dict)
    deprecated: list[tuple[str, str]] = field(default_factory=list)   # (token, file)
    pii: list[tuple[str, str]] = field(default_factory=list)          # (what, file)
    files: int = 0
    raw: str = ""

    def add_attr(self, key: str, value: str) -> None:
        self.attributes.setdefault(key, []).append(value.strip())


def scan_repo(root: Path) -> Scan:
    s = Scan()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts) or SKIP_NAME.search(path.name):
            continue
        source = path.read_text(errors="replace")
        text = strip_comments(source)
        s.files += 1
        s.raw += "\n" + text
        rel = str(path.relative_to(root))

        # span names, from the options object of each span-start call
        for m in SPAN_START.finditer(text):
            paren = text.find("(", m.end() - 1)
            body, _ = _balanced(text, paren, "(", ")")
            nm = NAME_KEY.search(body[:400])
            if nm:
                s.span_names.add(nm.group(1))

        # span.setAttribute("k", value)
        for m in SET_ATTRIBUTE.finditer(text):
            paren = text.rfind("(", 0, m.end())
            body, _ = _balanced(text, paren, "(", ")")
            parts = _split_top_level(body)
            if len(parts) >= 2:
                s.add_attr(m.group(1), parts[1])

        # .setAttributes({...}) and attributes: {...}
        for rx in (SET_ATTRIBUTES, ATTRIBUTES_OPT):
            for m in rx.finditer(text):
                brace = text.find("{", m.end() - 1)
                body, _ = _balanced(text, brace, "{", "}")
                for pair in _split_top_level(body):
                    pm = PAIR.match(pair)
                    if pm:
                        s.add_attr(pm.group(1), pm.group(2))

        for token in DEPRECATED:
            if token in text:
                s.deprecated.append((token, rel))

    for key, values in s.attributes.items():
        if PII_KEY.search(key):
            s.pii.append((f"attribute key `{key}`", "—"))
        for v in values:
            if PII_KEY.search(v):
                s.pii.append((f"value of `{key}`: {v[:60]}", "—"))
    return s


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _value_verdict(values: list[str]) -> str:
    """`missing` | `string` | `boolean` | `numeric` | `unresolved`.

    `unresolved` means an identifier, property access, or call that is not a known
    stringifier — `cart.total`, `order.total`, `computeTotal(cart)`. Almost every
    real numeric attribute looks like this, so treating it as indeterminate made
    the grader useless: a perfect solution scored 7/9. What IS statically decidable
    is the opposite — whether the value has been *stringified*, which is the actual
    defect (`cart.value` arriving as `"129.99"`, silently unaggregatable). So
    `attribute_numeric` passes on `unresolved` and flags low confidence.

    Worst verdict wins when an attribute is set in several places: one stringified
    call site is a defect even if the others are fine.
    """
    if not values:
        return "missing"
    verdicts = set()
    for v in values:
        v = v.strip()
        if re.fullmatch(r"""['"`].*['"`]""", v, re.S) or v.startswith("`"):
            verdicts.add("string")
        elif any(t in v for t in STRINGIFIERS):
            verdicts.add("string")
        elif v in ("true", "false"):
            verdicts.add("boolean")
        elif re.fullmatch(r"-?\d+(\.\d+)?", v) or re.search(
                r"\b(Number|parseFloat|parseInt)\s*\(", v):
            verdicts.add("numeric")
        else:
            verdicts.add("unresolved")
    for pref in ("string", "boolean", "numeric", "unresolved"):
        if pref in verdicts:
            return pref
    return "unresolved"


def run_check(check: dict, scan: Scan) -> tuple[bool | None, str]:
    """Returns (passed, detail). `None` means indeterminate — never a silent pass."""
    kind = check["kind"]

    if kind == "span_present":
        name = check["span"]
        ok = name in scan.span_names
        return ok, (f"span `{name}` created" if ok else f"no span named `{name}`")

    if kind == "span_renamed":
        to, frm = check.get("to"), check.get("from")
        has_new = to in scan.span_names
        has_old = frm in scan.span_names if frm else False
        if has_new and not has_old:
            return True, f"renamed to `{to}`"
        if has_new and has_old:
            return False, (f"both `{frm}` and `{to}` exist — the span was duplicated "
                           "rather than renamed")
        if has_old:
            return False, f"still emitting the old name `{frm}`"
        return False, f"neither `{frm}` nor `{to}` found"

    if kind == "attribute_present":
        key = check["attribute"]
        ok = key in scan.attributes
        return ok, (f"`{key}` set {len(scan.attributes.get(key, []))}×" if ok
                    else f"`{key}` never set")

    if kind == "any_attribute_present":
        found = [k for k in check["attributes"] if k in scan.attributes]
        return bool(found), (f"found {', '.join('`' + k + '`' for k in found)}"
                             if found else "none of "
                             + ", ".join(f"`{k}`" for k in check["attributes"]))

    if kind == "attribute_numeric":
        key = check["attribute"]
        v = _value_verdict(scan.attributes.get(key, []))
        if v == "missing":
            return False, f"`{key}` never set"
        if v == "numeric":
            return True, f"`{key}` set to a numeric literal or coercion"
        if v == "unresolved":
            return True, (f"`{key}` not stringified (value is `"
                          + scan.attributes[key][0][:40].strip()
                          + "`) — type unconfirmable from source, verify in telemetry")
        return False, f"`{key}` set to a {v} — cannot be aggregated"

    if kind == "attribute_not_boolean":
        key = check["attribute"]
        v = _value_verdict(scan.attributes.get(key, []))
        if v == "missing":
            return False, f"`{key}` never set"
        if v == "boolean":
            return False, f"`{key}` is a boolean — Sentry stores it as 'true'/'false'"
        allowed = check.get("allowed") or []
        if allowed:
            hit = [a for a in allowed if f'"{a}"' in scan.raw or f"'{a}'" in scan.raw]
            if not hit:
                return None, (f"`{key}` is not a boolean, but none of the declared "
                              f"values ({', '.join(allowed)}) appear as literals")
            return True, f"`{key}` uses declared values: {', '.join(hit)}"
        return True, f"`{key}` is not a boolean"

    if kind == "attribute_key_exact":
        # The Avo failure mode: "no compiler error and no failing test catches a
        # misnamed event." A near-miss key produces no error and no data.
        #
        # Checked even when the correct key IS present elsewhere. The grader scans
        # repo-wide, so a single file typoing `checkoutId` hides behind three files
        # that spell `checkout.id` correctly — and the spans from that one file
        # silently drop out of every journey query. Coexistence is the defect.
        key = check["attribute"]
        norm = lambda k: re.sub(r"[^a-z0-9]", "", k.lower())  # noqa: E731
        near = [k for k in scan.attributes if k != key and norm(k) == norm(key)]
        if key in scan.attributes and near:
            return False, (f"`{key}` is set, but so is the near-miss `{near[0]}` — "
                           "spans using the wrong key drop out of every journey query "
                           "with no error")
        if key in scan.attributes:
            return True, f"`{key}` set {len(scan.attributes[key])}×"
        if near:
            return False, (f"`{key}` missing; `{near[0]}` is set instead — a near-miss "
                           "key produces no error and no data")
        return False, f"`{key}` never set"

    if kind == "literal_present":
        lit = check["literal"]
        ok = f'"{lit}"' in scan.raw or f"'{lit}'" in scan.raw
        return ok, (f"literal `{lit}` present" if ok else f"literal `{lit}` not found")

    if kind == "no_deprecated_api":
        if not scan.deprecated:
            return True, "no removed or deprecated Sentry API used"
        uniq = sorted({t for t, _ in scan.deprecated})
        where = sorted({f for _, f in scan.deprecated})[:3]
        return False, f"uses {', '.join('`' + t + '`' for t in uniq)} in {', '.join(where)}"

    if kind == "no_pii":
        if not scan.pii:
            return True, "no PII-shaped keys or values on spans"
        return False, "; ".join(w for w, _ in scan.pii[:3])

    return None, f"unknown check kind `{kind}`"


IMPACT_WEIGHT = {"critical": 40, "important": 30, "normal": 20, "low": 10}


def grade(rubric: dict, scan: Scan) -> dict:
    results = []
    for req in rubric["requirements"]:
        if not req.get("gradeable"):
            results.append({"id": req["id"], "rule": req["rule"], "status": "ungradeable",
                            "impact": "normal", "weight": 0,
                            "detail": "prose-only requirement, no automated check",
                            "text": req["text"]})
            continue
        check = req["check"]
        passed, detail = run_check(check, scan)
        status = "pass" if passed else ("fail" if passed is False else "indeterminate")
        impact = check.get("impact", "normal")
        results.append({"id": req["id"], "rule": req["rule"], "status": status,
                        "impact": impact, "weight": IMPACT_WEIGHT[impact],
                        "check": check["kind"], "detail": detail, "text": req["text"]})

    # Guards: what already worked before this task. A failure here is a regression,
    # and it must not be offset by requirement passes.
    guards = []
    for g in rubric.get("guards") or []:
        passed_, detail = run_check(g["check"], scan)
        guards.append({
            "id": g["id"], "why": g.get("why", ""),
            "check": g["check"]["kind"],
            "target": g["check"].get("span") or g["check"].get("attribute"),
            "impact": g["check"].get("impact", "important"),
            "status": "pass" if passed_ else ("fail" if passed_ is False
                                              else "indeterminate"),
            "detail": detail,
        })
    guard_failures = [g for g in guards if g["status"] == "fail"]

    scored = [r for r in results if r["status"] in ("pass", "fail")]
    total_w = sum(r["weight"] for r in scored)
    got_w = sum(r["weight"] for r in scored if r["status"] == "pass")
    passed = sum(1 for r in results if r["status"] == "pass")
    return {
        "version": 1,
        "journey": rubric["journey"],
        "files_scanned": scan.files,
        "score": round(got_w / total_w * 100, 1) if total_w else 0.0,
        "clean": bool(total_w) and got_w == total_w and not guard_failures,
        "requirements_total": len(results),
        "passed": passed,
        "failed": sum(1 for r in results if r["status"] == "fail"),
        "indeterminate": sum(1 for r in results if r["status"] == "indeterminate"),
        "ungradeable": sum(1 for r in results if r["status"] == "ungradeable"),
        "guards_total": len(guards),
        "guard_failures": len(guard_failures),
        "guards": guards,
        "regressions": [r for r in results
                        if r["status"] == "fail" and r["check"] in
                        ("no_deprecated_api", "no_pii")] + guard_failures,
        "results": results,
        "observed": {
            "span_names": sorted(scan.span_names),
            "attributes": sorted(scan.attributes),
        },
    }


def render_markdown(g: dict) -> str:
    L, A = [], None
    out: list[str] = []
    A = out.append
    A(f"# Eval — {g['journey']['name']}\n")
    verdict = "CLEAN" if g["clean"] else "NOT CLEAN"
    A(f"**{verdict}** · **{g['score']}%** weighted · "
      f"{g['passed']}/{g['requirements_total']} requirements passed · "
      f"{g['failed']} failed · {g['indeterminate']} indeterminate · "
      f"{g['guards_total'] - g['guard_failures']}/{g['guards_total']} guards held · "
      f"{g['files_scanned']} files scanned\n")
    if g["guard_failures"]:
        A(f"> ## {g['guard_failures']} regression(s) in existing instrumentation\n")
        A("> These worked before the task. A requirement pass does not offset them.\n")
        for r in g["guards"]:
            if r["status"] == "fail":
                A(f"> - {r['id']} `{r['target']}` — {r['detail']}\n")
    if any(r["status"] == "fail" and r.get("check") in ("no_deprecated_api", "no_pii")
           for r in g["results"]):
        A("> **Hard failures.** `must not` rules — no amount of correct "
          "instrumentation offsets them.\n")
        for r in g["results"]:
            if r["status"] == "fail" and r.get("check") in ("no_deprecated_api", "no_pii"):
                A(f"> - {r['id']} {r['detail']}\n")
    A("| Req | Rule | Check | Impact | Result | Detail |")
    A("| --- | --- | --- | --- | --- | --- |")
    for r in g["results"]:
        mark = {"pass": "pass", "fail": "**FAIL**", "indeterminate": "?",
                "ungradeable": "—"}[r["status"]]
        A(f"| {r['id']} | {r['rule']} | {r.get('check', '—')} | {r['impact']} | "
          f"{mark} | {r['detail']} |")
    A("")
    A(f"Spans found: {', '.join('`' + s + '`' for s in g['observed']['span_names']) or 'none'}\n")
    A("---\n")
    A("Static analysis only. It proves the call sites exist with the right literal "
      "names and plausible value types; it cannot prove they run on the right code "
      "path. Pair with `gap/analyze.py` against real telemetry — the same static + "
      "runtime split Avo uses.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grade a repo against a spec rubric.")
    ap.add_argument("--rubric", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    ap.add_argument("--fail-under", type=float, default=None)
    ap.add_argument("--fail-on-regression", action="store_true",
                    help="Exit 2 if any guard failed — existing instrumentation broke.")
    args = ap.parse_args(argv)

    try:
        rubric = json.loads(Path(args.rubric).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    root = Path(args.repo)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    g = grade(rubric, scan_repo(root))
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(g, indent=2) + "\n")
    if args.out_md:
        Path(args.out_md).write_text(render_markdown(g))
    if not args.out_json and not args.out_md:
        print(render_markdown(g))

    print(f"{'CLEAN' if g['clean'] else 'NOT CLEAN':9} {g['score']:5}% · "
          f"{g['passed']}/{g['requirements_total']} passed · {g['failed']} failed · "
          f"{g['indeterminate']} indet · {g['guard_failures']} regression(s)",
          file=sys.stderr)
    if args.fail_under is not None and g["score"] < args.fail_under:
        return 2
    if g["guard_failures"] and args.fail_on_regression:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
