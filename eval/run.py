#!/usr/bin/env python3
"""
Eval runner. Turns the spec from a craft artifact into a tunable one.

Convex published a ~20% lift in AI success rate writing their code, and it was
only credible because they had an open eval harness and tuned their guidelines
against the categories that failed. This is that loop for instrumentation specs:

    task repo (uninstrumented)  +  SPEC.md  →  agent  →  grade.py  →  score

The number that matters is not any single score. It is the **per-check-kind
failure rate across runs** — "the spec fails `attribute_numeric` 60% of the time"
tells you which paragraph of the spec to rewrite. That's the tuning signal, and
it's why `--repeat` exists: a single run of a stochastic agent tells you nothing.

Agent-agnostic by design. `--agent` is a shell command template; the harness
never assumes which model or tool. Two modes need no agent at all:

    --solution NAME   grade the shipped golden solutions. Regression-tests the
                      HARNESS, and lets you use it before any agent is wired up.
    --dry-run         copy and grade the untouched `before/` repo, establishing
                      the floor a real run must beat.

Usage:
    ./run.py --solution correct
    ./run.py --solution all
    ./run.py --agent 'claude -p "Implement {spec}" --cwd {repo}' --repeat 3
    ./run.py --dry-run --out-md eval-report.md

Exit codes:
    0  ran
    1  input error
    2  mean score below --fail-under, or a regression with --fail-on-regression
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from grade import grade, scan_repo  # noqa: E402


def discover_tasks(tasks_dir: Path) -> list[dict]:
    out = []
    for d in sorted(tasks_dir.iterdir()):
        if not d.is_dir() or not (d / "before").is_dir():
            continue
        out.append({
            "name": d.name,
            "dir": d,
            "before": d / "before",
            "solutions": {s.name: s for s in sorted((d / "solutions").iterdir())}
            if (d / "solutions").is_dir() else {},
        })
    return out


def run_one(task: dict, rubric: dict, spec: Path | None, agent: str | None,
            source: Path, keep: Path | None) -> dict:
    """Copy `source` into a scratch dir, optionally run the agent, then grade."""
    tmp = Path(tempfile.mkdtemp(prefix="ce-eval-"))
    repo = tmp / "repo"
    shutil.copytree(source, repo)
    agent_out = None
    if agent:
        spec_dst = repo / "SENTRY_INSTRUMENTATION_SPEC.md"
        if spec:
            shutil.copy(spec, spec_dst)
        cmd = agent.format(repo=str(repo), spec=str(spec_dst))
        proc = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True,
                              text=True, timeout=1800)
        agent_out = {"returncode": proc.returncode,
                     "stdout_tail": proc.stdout[-2000:],
                     "stderr_tail": proc.stderr[-2000:]}
        # The spec is a deliverable, not part of the codebase — don't grade it.
        spec_dst.unlink(missing_ok=True)

    g = grade(rubric, scan_repo(repo))
    g["task"] = task["name"]
    g["source"] = source.name
    if agent_out:
        g["agent"] = agent_out
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copytree(repo, keep / f"{task['name']}-{source.name}",
                        dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return g


def aggregate(runs: list[dict]) -> dict:
    scores = [r["score"] for r in runs]
    clean = [r for r in runs if r["clean"]]

    # The tuning signal: which check kinds fail, and how often.
    by_kind: dict[str, dict[str, int]] = {}
    for r in runs:
        for res in r["results"]:
            kind = res.get("check")
            if not kind:
                continue
            b = by_kind.setdefault(kind, {"pass": 0, "fail": 0, "indeterminate": 0})
            if res["status"] in b:
                b[res["status"]] += 1
    for kind, b in by_kind.items():
        total = b["pass"] + b["fail"] + b["indeterminate"]
        b["fail_rate"] = round(b["fail"] / total, 3) if total else 0.0

    guard_fails: dict[str, int] = {}
    for r in runs:
        for gd in r.get("guards", []):
            if gd["status"] == "fail":
                guard_fails[gd["target"]] = guard_fails.get(gd["target"], 0) + 1

    return {
        "runs": len(runs),
        "clean_rate": round(len(clean) / len(runs), 3) if runs else 0.0,
        "mean_score": round(statistics.mean(scores), 1) if scores else 0.0,
        "median_score": round(statistics.median(scores), 1) if scores else 0.0,
        "stdev_score": round(statistics.stdev(scores), 1) if len(scores) > 1 else 0.0,
        "min_score": min(scores) if scores else 0.0,
        "max_score": max(scores) if scores else 0.0,
        "total_regressions": sum(r["guard_failures"] for r in runs),
        "by_check_kind": dict(sorted(by_kind.items(),
                                     key=lambda kv: -kv[1]["fail_rate"])),
        "guard_failures_by_target": dict(sorted(guard_fails.items(),
                                                key=lambda kv: -kv[1])),
    }


def render_markdown(runs: list[dict], agg: dict, label: str) -> str:
    L: list[str] = []
    A = L.append
    A(f"# Eval report — {label}\n")
    A(f"**{agg['runs']} run(s)** · clean rate **{agg['clean_rate']:.0%}** · "
      f"mean **{agg['mean_score']}%** (median {agg['median_score']}, "
      f"σ {agg['stdev_score']}, range {agg['min_score']}–{agg['max_score']}) · "
      f"{agg['total_regressions']} regression(s)\n")

    A("## Runs\n")
    A("| Task | Source | Verdict | Score | Passed | Failed | Regressions |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for r in runs:
        A(f"| {r['task']} | {r['source']} | "
          f"{'CLEAN' if r['clean'] else '**NOT CLEAN**'} | {r['score']}% | "
          f"{r['passed']}/{r['requirements_total']} | {r['failed']} | "
          f"{r['guard_failures']} |")
    A("")

    if agg["by_check_kind"]:
        A("## Where the spec fails — the tuning signal\n")
        A("Sorted by failure rate. A high rate here means the corresponding section "
          "of the spec is unclear, not that the agent is bad. Rewrite that paragraph "
          "and re-run.\n")
        A("| Check kind | Fail rate | pass | fail | indeterminate |")
        A("| --- | --- | --- | --- | --- |")
        for kind, b in agg["by_check_kind"].items():
            A(f"| `{kind}` | {b['fail_rate']:.0%} | {b['pass']} | {b['fail']} | "
              f"{b['indeterminate']} |")
        A("")

    if agg["guard_failures_by_target"]:
        A("## Existing instrumentation broken\n")
        A("Each of these worked before the task. Requirement passes do not offset "
          "them.\n")
        for target, n in agg["guard_failures_by_target"].items():
            A(f"- `{target}` — broken in {n} run(s)")
        A("")

    for r in runs:
        fails = [x for x in r["results"] if x["status"] == "fail"]
        gfails = [x for x in r.get("guards", []) if x["status"] == "fail"]
        if not fails and not gfails:
            continue
        A(f"### {r['task']} / {r['source']}\n")
        for x in fails:
            A(f"- **{x['id']}** ({x['impact']}, `{x.get('check')}`) — {x['detail']}")
        for x in gfails:
            A(f"- **{x['id']}** REGRESSION `{x['target']}` — {x['detail']}")
        A("")

    A("---\n")
    A("Static grading only — it proves the call sites exist with the right literal "
      "names and plausible types. Pair with `gap/analyze.py` against telemetry from "
      "the instrumented app for the runtime half.")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the instrumentation spec eval.")
    ap.add_argument("--tasks", default=str(HERE / "tasks"))
    ap.add_argument("--rubric", default=str(HERE.parent / "spec/out/checkout-RUBRIC.json"))
    ap.add_argument("--spec", default=str(HERE.parent / "spec/out/checkout-SPEC.md"))
    ap.add_argument("--task", action="append", default=[], help="Limit to task name(s).")
    ap.add_argument("--agent", help="Shell command template. {repo} and {spec} are "
                                    "substituted. Omit for --solution/--dry-run.")
    ap.add_argument("--solution", help="Grade a golden solution by name, or 'all'.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Grade the untouched `before/` repo — the floor to beat.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Runs per task. Agents are stochastic; one run is noise.")
    ap.add_argument("--keep", help="Directory to keep the resulting repos in.")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--fail-under", type=float)
    ap.add_argument("--fail-on-regression", action="store_true")
    args = ap.parse_args(argv)

    if not (args.agent or args.solution or args.dry_run):
        print("error: pass one of --agent, --solution, or --dry-run", file=sys.stderr)
        return 1
    try:
        rubric = json.loads(Path(args.rubric).read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    tasks = discover_tasks(Path(args.tasks))
    if args.task:
        tasks = [t for t in tasks if t["name"] in args.task]
    if not tasks:
        print(f"error: no tasks found under {args.tasks}", file=sys.stderr)
        return 1

    spec = Path(args.spec) if args.spec and Path(args.spec).exists() else None
    keep = Path(args.keep) if args.keep else None
    runs: list[dict] = []

    for t in tasks:
        sources: list[Path] = []
        if args.dry_run:
            sources = [t["before"]]
        elif args.solution:
            if args.solution == "all":
                sources = list(t["solutions"].values())
            elif args.solution in t["solutions"]:
                sources = [t["solutions"][args.solution]]
            else:
                print(f"error: {t['name']} has no solution "
                      f"'{args.solution}'. Available: "
                      f"{', '.join(sorted(t['solutions'])) or 'none'}", file=sys.stderr)
                return 1
        else:
            sources = [t["before"]]

        for src in sources:
            for _ in range(args.repeat):
                runs.append(run_one(t, rubric, spec,
                                    args.agent if not (args.solution or args.dry_run)
                                    else None, src, keep))

    agg = aggregate(runs)
    label = ("golden solutions" if args.solution else
             "dry run (uninstrumented floor)" if args.dry_run else "agent")
    report = render_markdown(runs, agg, label)
    payload = {"version": 1, "label": label, "rubric": args.rubric,
               "aggregate": agg, "runs": runs}

    if args.out_md:
        Path(args.out_md).write_text(report)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(report)

    print(f"{agg['runs']} run(s) · clean {agg['clean_rate']:.0%} · "
          f"mean {agg['mean_score']}% (σ {agg['stdev_score']}) · "
          f"{agg['total_regressions']} regression(s)", file=sys.stderr)
    worst = next(iter(agg["by_check_kind"].items()), None)
    if worst and worst[1]["fail_rate"] > 0:
        print(f"weakest spec section: `{worst[0]}` fails "
              f"{worst[1]['fail_rate']:.0%} of the time", file=sys.stderr)

    if args.fail_under is not None and agg["mean_score"] < args.fail_under:
        return 2
    if args.fail_on_regression and agg["total_regressions"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
