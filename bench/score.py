#!/usr/bin/env python3
"""
Score discovered journeys against hand-labeled ground truth.

Why this exists: `eval/` grades whether an agent *implemented* a spec. Nothing graded
whether discovery found the *right journeys*, so changes to discovery were guesses.
This is the missing measurement. Run it before and after every change.

Matching (deliberately language-agnostic and name-independent):
  A proposed journey matches a ground-truth journey when
    (a) at least one file it cites lies under one of the truth journey's `anchors`
        (prefix match on repo-relative paths) — the PRIMARY key, or
    (b) its id contains one of the truth journey's `aliases` as a whole token.
  (b) is weak on purpose-built names ("transaction" matches a confirmations alias),
  so it flatters the baseline slightly. That is the conservative direction.

Metrics (reported overall and per tier):
  recall@k      share of truth journeys matched by any of the first k proposals
  precision@k   share of the first k proposals that match any truth journey
  evidence_ok   share of cited paths that exist on disk (needs --repo) — a
                hallucination check; a proposal citing files that do not exist is
                worse than one citing nothing.

Usage:
    python3 bench/score.py --truth bench/ground_truth/supabase.yaml \
        --proposed ce-work/journeys.yaml --repo /path/to/supabase --k 5 10
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

PATH_RE = re.compile(r"[A-Za-z0-9_@.\-\[\]()]+(?:/[A-Za-z0-9_@.\-\[\]()]+)+")


def paths_in(text: str) -> list[str]:
    """Repo-relative path-like tokens in free text. Trailing `:line` and punctuation
    are stripped. Requires a `/` so bare words are never mistaken for paths."""
    out = []
    for m in PATH_RE.finditer(text or ""):
        p = m.group(0).strip("()[].,;:")
        # Require a file extension on the last segment. Without it, prose like
        # "connect/disconnect events" in a step description was scored as a cited
        # path that "does not exist", which made evidence_ok look worse than it was.
        if "/" in p and not p.startswith(("http", "//")) and re.search(r"\.[A-Za-z0-9]{1,6}$", p):
            out.append(p)
    return out


def journey_paths(j: dict) -> list[str]:
    found = []
    for s in j.get("steps") or []:
        ev = s.get("evidence")
        if isinstance(ev, list):
            ev = " ; ".join(map(str, ev))
        found.extend(paths_in(str(ev or "")))
    found.extend(paths_in(j.get("notes", "")))
    seen, uniq = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def id_tokens(jid: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", jid.lower()) if t}


def matches(truth: dict, j: dict, paths: list[str]) -> str | None:
    """Return 'anchor', 'alias', or None."""
    for p in paths:
        for a in truth.get("anchors", []):
            if p == a or p.startswith(a.rstrip("/") + "/") or p.startswith(a):
                return "anchor"
    toks = id_tokens(j.get("id", "")) | id_tokens(j.get("name", ""))
    for al in truth.get("aliases", []):
        al_toks = id_tokens(al)
        if al_toks and al_toks <= toks:
            return "alias"
    return None


def score(truth_doc: dict, proposed_doc: dict, repo: Path | None, ks: list[int]) -> dict:
    truth = truth_doc["journeys"]
    props = proposed_doc.get("journeys") or []
    pp = [journey_paths(j) for j in props]

    # matrix[i][t] = how proposal i matches truth t
    mat = [[matches(t, j, pp[i]) for t in truth] for i, j in enumerate(props)]

    def at(k: int) -> dict:
        sub = mat[:k]
        covered = {t for row in sub for t, how in enumerate(row) if how}
        valid = sum(1 for row in sub if any(row))
        tiers = {}
        for tier in sorted({t.get("tier", "e2e") for t in truth}):
            idx = [i for i, t in enumerate(truth) if t.get("tier", "e2e") == tier]
            tiers[tier] = round(sum(1 for i in idx if i in covered) / len(idx), 3)
        return {
            "k": k,
            "proposed": len(sub),
            "recall": round(len(covered) / len(truth), 3) if truth else 0.0,
            "precision": round(valid / len(sub), 3) if sub else 0.0,
            "recall_by_tier": tiers,
            "truth_covered": sorted(truth[i]["id"] for i in covered),
        }

    result = {
        "truth_total": len(truth),
        "proposed_total": len(props),
        "at": [at(k) for k in ks] + [at(len(props))],
        "unmatched_proposals": [props[i].get("id") for i, row in enumerate(mat) if not any(row)],
        "missed_truth": sorted(
            truth[t]["id"] for t in range(len(truth)) if not any(row[t] for row in mat)),
    }

    if repo is not None:
        cited = [p for plist in pp for p in plist]
        exist = [p for p in cited if (repo / p).exists()]
        result["evidence_ok"] = round(len(exist) / len(cited), 3) if cited else 0.0
        result["evidence_cited"] = len(cited)
        result["evidence_missing_sample"] = [p for p in cited if p not in set(exist)][:8]
    return result


def render(r: dict, label: str) -> str:
    lines = [f"## {label}", "",
             f"truth journeys: {r['truth_total']} · proposed: {r['proposed_total']}", "",
             "| k | recall | precision | recall by tier |", "|---|---|---|---|"]
    for a in r["at"]:
        tiers = ", ".join(f"{k} {v}" for k, v in a["recall_by_tier"].items())
        lines.append(f"| {a['k']} | {a['recall']} | {a['precision']} | {tiers} |")
    if "evidence_ok" in r:
        lines += ["", f"evidence paths that exist: {r['evidence_ok']} of {r['evidence_cited']} cited"]
        if r["evidence_missing_sample"]:
            lines.append("missing sample: " + ", ".join(r["evidence_missing_sample"]))
    lines += ["", "missed: " + (", ".join(r["missed_truth"]) or "none"),
              "proposals matching nothing: " + (", ".join(map(str, r["unmatched_proposals"])) or "none")]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--truth", required=True)
    ap.add_argument("--proposed", required=True)
    ap.add_argument("--repo", help="repo root, to check cited paths exist")
    ap.add_argument("--k", type=int, nargs="*", default=[5, 10])
    ap.add_argument("--label", default=None)
    ap.add_argument("--json", dest="out_json")
    args = ap.parse_args(argv)

    truth = yaml.safe_load(Path(args.truth).read_text())
    proposed = yaml.safe_load(Path(args.proposed).read_text())
    r = score(truth, proposed, Path(args.repo) if args.repo else None, args.k)
    print(render(r, args.label or Path(args.proposed).stem))
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
