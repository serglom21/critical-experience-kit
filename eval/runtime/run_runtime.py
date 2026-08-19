#!/usr/bin/env python3
"""
Runtime eval runner. Boots the instrumented app against a local envelope
collector, drives the journey, and grades the telemetry it actually produced.

    driver + DSN→collector  →  real envelopes  →  observed.json  →  gap/analyze.py

Why this exists: `eval/grade.py` reads source. It proves the call sites exist with
the right literal names, and it stops there. Two classes of defect are invisible
to it by construction —

  1. **A span that is written but never runs.** Static analysis sees the code and
     passes. Only telemetry knows whether the path executed.
  2. **A value's real type.** `cart.total` in source could be a number or a
     stringified one; the grader can only say "not obviously stringified". The
     wire says `129.99` or `"129.99"`, definitively.

Both are graded here, and neither needs a Sentry account.

Usage:
    ./run_runtime.py --variant correct
    ./run_runtime.py --variant all --out-md runtime-report.md
    ./run_runtime.py --variant correct --baseline ../../gap/example-gap.json

Exit codes:
    0  ran
    1  input / setup error
    2  a variant scored below --fail-under
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
KIT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from collector import EnvelopeCollector  # noqa: E402

DEFAULT_RESOLVED = KIT / "intake" / "example-resolved.json"
ANALYZE = KIT / "gap" / "analyze.py"
DIFF = KIT / "gap" / "diff.py"
VARIANTS = ("correct", "stringified", "skip-terminal")


def drive(task: Path, variant: str, runs: int, timeout: float) -> tuple[dict, dict]:
    """Run the driver with its DSN pointed at a local collector. Returns
    (observed, driver_result)."""
    with EnvelopeCollector() as c:
        env = dict(os.environ)
        env.update({
            "SENTRY_DSN": c.dsn,
            "CE_VARIANT": variant,
            "CE_RUNS": str(runs),
        })
        proc = subprocess.run(
            ["node", "drive.mjs"], cwd=task, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        # The SDK flushes asynchronously; give the last envelopes a moment to land.
        time.sleep(1.0)
        observed = c.observed(org=f"local-eval/{variant}", stats_period="runtime")
        envelopes = len(c.envelopes)

    return observed, {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip()[-500:],
        "stderr": proc.stderr.strip()[-1500:],
        "envelopes": envelopes,
    }


def analyze(observed: dict, resolved: Path, workdir: Path) -> dict:
    obs_path = workdir / "observed.json"
    gap_path = workdir / "gap.json"
    obs_path.write_text(json.dumps(observed, indent=2) + "\n")
    proc = subprocess.run(
        [sys.executable, str(ANALYZE), "--resolved", str(resolved),
         "--observed", str(obs_path), "--include-unready",
         "--out-json", str(gap_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"analyze.py failed: {proc.stderr[-800:]}")
    return json.loads(gap_path.read_text())


def journey_result(gap: dict, journey_id: str) -> dict | None:
    return next((j for j in gap["journeys"] if j["id"] == journey_id), None)


def type_findings(observed: dict) -> list[dict]:
    """Attribute type facts only the wire can establish."""
    out = []
    for a in observed["attributes"]:
        if (a.get("attributeSource") or {}).get("source_type") != "user":
            continue
        rec = {"key": a["key"], "type": a["attributeType"]}
        if a.get("type_conflict"):
            rec["conflict"] = a["type_conflict"]
        if a.get("observed_values"):
            rec["sample"] = a["observed_values"][:3]
        out.append(rec)
    return out


def render_markdown(rows: list[dict]) -> str:
    L: list[str] = []
    A = L.append
    A("# Runtime eval — telemetry-graded\n")
    A("Each variant boots the instrumented app against a local Sentry envelope "
      "collector, drives the journey, and scores the spans that actually arrived. "
      "No Sentry account involved.\n")

    A("| Variant | Envelopes | Spans seen | Coverage | Score | Grade |")
    A("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        j = r["journey"]
        A(f"| `{r['variant']}` | {r['driver']['envelopes']} | "
          f"{len(r['observed']['span_names'])} | "
          f"{j['steps_instrumented']}/{j['steps_total']} | {j['score']} | {j['grade']} |")
    A("")

    A("## What only the wire could tell us\n")
    for r in rows:
        j = r["journey"]
        A(f"### `{r['variant']}` — {j['score']} ({j['grade']})\n")
        if j.get("dark_segments"):
            for seg in j["dark_segments"]:
                A(f"- Goes dark at **{' → '.join(seg)}**")
        if j.get("missing_steps"):
            A(f"- Never emitted: {', '.join('`' + s + '`' for s in j['missing_steps'])}")
        types = {t["key"]: t for t in r["types"]}
        for key in ("cart.value", "order.value", "checkout.outcome"):
            t = types.get(key)
            if t:
                extra = f" · conflict {t['conflict']}" if t.get("conflict") else ""
                sample = f" · e.g. {t['sample'][0]!r}" if t.get("sample") else ""
                A(f"- `{key}` observed as **{t['type']}**{sample}{extra}")
        fails = [f for f in j["findings"] if not f["passed"]]
        if fails:
            A("")
            A("| Rule | Impact | Detail |")
            A("| --- | --- | --- |")
            for f in fails:
                A(f"| {f['rule']} | {f['impact']} | {f['detail']} |")
        A("")

    A("---\n")
    A("Static grading (`eval/grade.py`) and this are complementary, the same split "
      "Avo runs with `avo status` plus Inspector: static proves the call sites "
      "exist, runtime proves they executed and what types they carried. "
      "`attributeType` here comes from the real JSON value on the wire; "
      "`attributeSource` is a namespace heuristic, since the documented "
      "`source_type` field needs a live org.")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grade instrumentation from real telemetry.")
    ap.add_argument("--task", default=str(HERE / "tasks" / "checkout-js"))
    ap.add_argument("--resolved", default=str(DEFAULT_RESOLVED))
    ap.add_argument("--journey", default="checkout")
    ap.add_argument("--variant", default="correct",
                    help=f"one of {', '.join(VARIANTS)}, or 'all'")
    ap.add_argument("--runs", type=int, default=6, help="journey instances to drive")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--baseline", help="A gap.json to diff the runtime result against.")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--keep-observed", help="Write each variant's observed.json here.")
    ap.add_argument("--fail-under", type=float)
    args = ap.parse_args(argv)

    task = Path(args.task)
    if not (task / "drive.mjs").exists():
        print(f"error: {task}/drive.mjs not found", file=sys.stderr)
        return 1
    if not (task / "node_modules").exists():
        print(f"error: dependencies not installed. Run:\n"
              f"  npm install --prefix {task}", file=sys.stderr)
        return 1
    resolved = Path(args.resolved)
    if not resolved.exists():
        print(f"error: {resolved} not found — run intake/resolve.py first", file=sys.stderr)
        return 1

    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    rows: list[dict] = []

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for v in variants:
            observed, driver = drive(task, v, args.runs, args.timeout)
            if driver["returncode"] != 0:
                print(f"error: driver failed for `{v}`:\n{driver['stderr']}", file=sys.stderr)
                return 1
            if not observed["span_names"]:
                print(f"error: no spans captured for `{v}`. The SDK may not have "
                      "flushed, or the DSN was not picked up.", file=sys.stderr)
                return 1

            gap = analyze(observed, resolved, work)
            j = journey_result(gap, args.journey)
            if j is None:
                print(f"error: journey `{args.journey}` not in the resolved set",
                      file=sys.stderr)
                return 1

            row = {"variant": v, "driver": driver, "observed": observed,
                   "journey": j, "types": type_findings(observed)}

            if args.baseline:
                bpath, cpath = Path(args.baseline), work / f"gap-{v}.json"
                cpath.write_text(json.dumps(gap, indent=2) + "\n")
                d = subprocess.run(
                    [sys.executable, str(DIFF), "--baseline", str(bpath),
                     "--current", str(cpath), "--out-json", str(work / f"diff-{v}.json")],
                    capture_output=True, text=True)
                if d.returncode in (0, 3):
                    row["diff"] = json.loads((work / f"diff-{v}.json").read_text())

            if args.keep_observed:
                out = Path(args.keep_observed)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"observed-{v}.json").write_text(
                    json.dumps(observed, indent=2) + "\n")

            rows.append(row)
            print(f"{v:14} {driver['envelopes']:3} envelopes · "
                  f"{j['steps_instrumented']}/{j['steps_total']} steps · "
                  f"{j['score']} ({j['grade']})", file=sys.stderr)

    report = render_markdown(rows)
    payload = {"version": 1, "task": str(task), "journey": args.journey,
               "variants": [{k: r[k] for k in ("variant", "journey", "types", "driver")}
                            for r in rows]}
    if args.out_md:
        Path(args.out_md).write_text(report)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(report)

    if args.fail_under is not None:
        worst = min(r["journey"]["score"] for r in rows)
        if worst < args.fail_under:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
