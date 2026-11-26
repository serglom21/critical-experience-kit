#!/usr/bin/env python3
"""
Post-run review: a human judges each journey the explorer proposed.

The inverse of bench/score.py. Instead of labelling the right answer first, run the
explorer, then mark every proposed journey. This measures precision (are the proposals
worth instrumenting?) and step quality. It does not measure recall: the reviewer only
lists journeys they notice are missing, after seeing the output.

    python3 bench/review.py make  --out ce-work/<repo> --sheet bench/blind/review/<repo>.yaml
    # edit the sheet: fill `verdict` for each journey, add anything missing under `missing`
    python3 bench/review.py score bench/blind/review/*.yaml

Verdicts:
    critical  - worth instrumenting; a business owner would page someone if it broke
    minor     - a real flow, but low consequence (nice to have)
    wrong     - not a journey, or the steps/citations do not describe it
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

VERDICTS = ("critical", "minor", "wrong")
STEPS = ("right", "partly", "wrong")


def make(out: Path, sheet: Path) -> int:
    src = out / "discovery.validated.json"
    if not src.exists():
        print(f"error: {src} not found; run `ce explore` first", file=sys.stderr)
        return 2
    data = json.loads(src.read_text())
    if sheet.exists():
        print(f"error: {sheet} exists; refusing to overwrite a review in progress", file=sys.stderr)
        return 2
    rows = []
    for j in data["journeys"]:
        rows.append({
            "id": j["id"],
            "name": j["name"],
            "why": j.get("why") or "",
            "confidence": j.get("confidence"),
            "steps": [
                f"{s['id']}: {s.get('description', '')} — " + "; ".join(
                    f"{e['file']}" + (f":{e['line']}" if e.get("line") else "")
                    for e in s["evidence"] if e["status"] in ("verified", "file_only"))
                for s in j["steps"]],
            "verdict": "",   # critical | minor | wrong
            "steps_ok": "",  # right | partly | wrong
            "note": "",
        })
    acct = (data.get("report") or {}).get("accounting") or {}
    doc = {
        "repo": out.name,
        "reviewed_by": "",
        "reviewed_at": "",
        "e2e_coverage": f"{acct.get('ok', 0)} of {acct.get('total', 0)} test names" if acct.get("total") else "no e2e seeds",
        "journeys": rows,
        "missing": [],   # journeys you expected and did not see: [{name: ..., where: path, severity: critical|minor}]
    }
    sheet.parent.mkdir(parents=True, exist_ok=True)
    header = (__doc__.split("Verdicts:")[1].strip() + "\n").replace("\n", "\n# ")
    sheet.write_text("# Review sheet. Fill `verdict` and `steps_ok` for every journey; list misses under `missing`.\n"
                     "# Verdicts:\n# " + header + "\n"
                     + yaml.safe_dump(doc, sort_keys=False, width=110, allow_unicode=True))
    print(f"wrote {sheet} ({len(rows)} journeys to review)")
    return 0


def score_one(doc: dict) -> dict:
    js = doc.get("journeys") or []
    v = [str(j.get("verdict") or "").strip().lower() for j in js]
    s = [str(j.get("steps_ok") or "").strip().lower() for j in js]
    unfilled = sum(1 for x in v if x not in VERDICTS)
    n = len(js)
    crit, minor, wrong = (v.count(k) for k in VERDICTS)
    missing = doc.get("missing") or []
    miss_crit = sum(1 for m in missing if str((m or {}).get("severity", "critical")).lower() == "critical")
    return {
        "repo": doc.get("repo"), "proposed": n, "unfilled": unfilled,
        "critical": crit, "minor": minor, "wrong": wrong,
        "precision_strict": round(crit / n, 3) if n else None,       # critical only
        "precision_lenient": round((crit + minor) / n, 3) if n else None,
        "steps_right": s.count("right"), "steps_partly": s.count("partly"), "steps_wrong": s.count("wrong"),
        "missing_critical": miss_crit, "missing_total": len(missing),
        # share of the critical journeys (found + noticed missing) that the explorer found
        "found_of_known_critical": round(crit / (crit + miss_crit), 3) if crit + miss_crit else None,
    }


def score(paths: list) -> int:
    rows = [score_one(yaml.safe_load(Path(p).read_text())) for p in paths]
    cols = ["repo", "proposed", "critical", "minor", "wrong", "precision_strict", "precision_lenient",
            "steps_right", "steps_partly", "steps_wrong", "missing_critical", "found_of_known_critical", "unfilled"]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |")
    if any(r["unfilled"] for r in rows):
        print("\nwarning: some journeys have no verdict yet (see `unfilled`)", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="create a review sheet from an explore output dir")
    m.add_argument("--out", required=True, type=Path)
    m.add_argument("--sheet", required=True, type=Path)
    s = sub.add_parser("score", help="summarise filled review sheets")
    s.add_argument("sheets", nargs="+")
    a = ap.parse_args(argv)
    return make(a.out, a.sheet) if a.cmd == "make" else score(a.sheets)


if __name__ == "__main__":
    sys.exit(main())
