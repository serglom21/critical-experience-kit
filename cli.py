#!/usr/bin/env python3
"""
`ce` — one entry point for the Critical Experience kit.

Before this, running the kit meant invoking seven scripts from seven directories
with hand-wired relative paths. Every stage still works standalone; this only
dispatches, so there is no second copy of the logic to drift.

The customer-facing path, run from the service root. Working files land in
`ce-work/` (gitignored). Specs the agent and Warden need are copied to
tracked `.agents/journeys/` by `ce report`.

    ce discover                    # propose + scan + intake → ce-work/
    ce review                      # keep/drop + business_impact (browser)
    ce report                      # gap + profile + spec → ce-work/ and .agents/

Stages still work standalone; this file only dispatches, so there is no second
copy of the logic to drift.

    ce doctor                      # what's installed, what's missing
    ce init                        # scaffold ce-work/ (hand-authored journeys)
    ce propose --repo . --out ce-work/journeys.yaml
    ce intake  --discovered ce-work/journeys.yaml --out-json ce-work/resolved.json
    ce local   --resolved ce-work/resolved.json --drive 'npm run e2e'
    ce snapshot --org acme --token $SENTRY_AUTH_TOKEN --out ce-work/observed.json
    ce gap     --resolved ce-work/resolved.json --observed ce-work/observed.json
    ce profile --observed ce-work/observed.json
    ce spec    --resolved ce-work/resolved.json --gap ce-work/gap.json --out-dir ce-work/specs --rubric
    ce registry --resolved ce-work/resolved.json --out-dir ce-work/registry
    ce diff    --baseline before.json --current after.json
    ce grade   --rubric ce-work/specs/checkout-RUBRIC.json --repo .
    ce eval    --solution all
    ce runtime --variant all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
# 3.9 is the real floor. PEP 604 unions are used widely but every module has
# `from __future__ import annotations`, so they are never evaluated at runtime.
MIN_PYTHON = (3, 9)
# Customer-run default. The previous invariant forbade writing into the service
# repo at all (a `.ce-observed.json` in cwd showed up in git status). The new
# contract: write only under this directory, and gitignore it. Never next to src/.
WORK_DIRNAME = "ce-work"

STAGES = {
    "intake": KIT / "intake" / "resolve.py",
    "gap": KIT / "gap" / "analyze.py",
    "profile": KIT / "gap" / "instrumentation_profile.py",
    "diff": KIT / "gap" / "diff.py",
    "snapshot": KIT / "gap" / "sentry_source.py",
    "scan": KIT / "gap" / "code_scan.py",
    "propose": KIT / "gap" / "propose.py",
    "review": KIT / "intake" / "review.py",
    "spec": KIT / "spec" / "generate.py",
    "publish": KIT / "spec" / "publish.py",
    "registry": KIT / "registry_gen" / "generate.py",
    "validate-registry": KIT / "registry_gen" / "validate.py",
    "grade": KIT / "eval" / "grade.py",
    "eval": KIT / "eval" / "run.py",
    "runtime": KIT / "eval" / "runtime" / "run_runtime.py",
}


def delegate(script: Path, argv: list[str]) -> int:
    """Run a stage in-process so tracebacks and exit codes pass straight through."""
    if not script.exists():
        print(f"error: {script} not found — is the kit intact?", file=sys.stderr)
        return 1
    sys.argv = [str(script), *argv]
    sys.path.insert(0, str(script.parent))
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def cwd_or_die() -> Path:
    """Relative --out paths need a live cwd. A deleted-and-recreated directory
    leaves the shell on a ghost inode; pathlib then reports FileNotFoundError
    on `journeys.yaml` as if the filename were wrong."""
    try:
        return Path.cwd()
    except FileNotFoundError:
        print("error: current directory no longer exists (it was deleted or "
              "replaced). cd to an absolute path and retry.", file=sys.stderr)
        sys.exit(1)


def resolve_path(p: str | Path) -> Path:
    path = Path(p)
    if not path.is_absolute():
        path = cwd_or_die() / path
    return path


def ensure_workdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def gitignore_workdir(repo: Path, work: Path) -> None:
    """Ignore the workdir in the service repo. Skip if work is not inside repo
    — a `--out /tmp/foo` must not edit someone else's .gitignore."""
    try:
        rel = work.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    if rel == Path("."):
        return
    entry = str(rel).replace("\\", "/") + "/"
    gi = repo / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if re.search(r"^" + re.escape(entry.rstrip("/")) + r"/?$", existing, re.M):
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    gi.write_text(existing + prefix + f"# ce artifacts — generated, do not commit\n{entry}\n")


def write_review(work: Path) -> None:
    (work / "REVIEW.md").write_text(
        "# Review before `ce report`\n\n"
        "`ce discover` proposed journeys from source. This is a **draft**.\n\n"
        "Do not hand-edit YAML unless you prefer it. Run:\n\n"
        "```bash\nce review\n```\n\n"
        "That opens a local page: keep 2–3 journeys, set `business_impact` "
        "(critical / important / normal), optionally fill outcome values. "
        "Nothing in source can decide impact — health checks dominate traffic; "
        "refunds are rare and expensive.\n\n"
        "A name like `web` is usually a directory, not a flow; the page defaults "
        "those to drop.\n\n"
        "If you already edited `journeys.yaml` by hand, `ce review --stamp` "
        "instead.\n\n"
        "Then:\n\n```bash\nce report\n```\n\n"
        "`ce report` copies `*-SPEC.md` to `.agents/journeys/` for your coding "
        "agent and for Warden. Do not implement from WHY.md.\n"
    )


def prompt_yes(question: str) -> bool:
    """TTY only. Tests and piped stdin skip live-Sentry so discover stays
    non-interactive. Never default to fetching an org."""
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------



def cmd_doctor(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ce doctor",
                                 description="Report what is and isn't ready.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str, needed_for: str,
            fix: str = "") -> None:
        checks.append({"name": name, "status": "ok" if ok else
                       ("warn" if ok is None else "missing"),
                       "detail": detail, "needed_for": needed_for, "fix": fix})

    v = sys.version_info
    add("python", v >= MIN_PYTHON, f"{v.major}.{v.minor}.{v.micro}", "everything",
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
        f"(3.9 works: annotations are lazy via `from __future__ import annotations`)")

    try:
        import yaml  # noqa: F401
        add("PyYAML", True, "importable", "YAML journey files")
    except ImportError:
        add("PyYAML", False, "not importable", "YAML journey files (JSON works without it)",
            "pip install pyyaml")

    try:
        import jsonschema  # noqa: F401
        add("jsonschema", True, "importable", "registry validation")
    except ImportError:
        add("jsonschema", None, "not importable",
            "full registry validation (structural checks still run)",
            "pip install jsonschema")

    node = shutil.which("node")
    if node:
        ver = subprocess.run([node, "--version"], capture_output=True, text=True)
        add("node", True, ver.stdout.strip(), "the JS runtime eval fixture")
    else:
        add("node", None, "not found", "the JS runtime eval fixture only — "
            "`ce local` works with any language", "install Node 18+")

    fixture = KIT / "eval" / "runtime" / "tasks" / "checkout-js"
    installed = (fixture / "node_modules").exists()
    # `warn`, not `missing`: this is only the shipped demo fixture. `ce local`
    # grades a real service without it. A fresh clone reporting a blocking item
    # for a demo is how a tool teaches people to ignore its own output.
    add("runtime fixture deps", True if installed else None,
        "installed" if installed else "not installed",
        "`ce runtime` (the bundled demo only — `ce local` does not need it)",
        f"npm install --prefix {fixture}")

    tok = os.environ.get("SENTRY_AUTH_TOKEN")
    add("SENTRY_AUTH_TOKEN", None if not tok else True,
        "set" if tok else "not set",
        "`ce snapshot` against a live org (needs org:read)",
        "export SENTRY_AUTH_TOKEN=...")

    weaver = shutil.which("weaver")
    add("weaver", None if not weaver else True,
        "found" if weaver else "not found",
        "`weaver registry check` — `ce validate-registry` substitutes offline",
        "cargo install weaver-forge, or grab a release binary")

    present = sum(1 for p in STAGES.values() if p.exists())
    add("stage scripts", present == len(STAGES),
        f"{present}/{len(STAGES)} next to cli.py",
        "every command other than doctor",
        "reinstall the kit (a non-editable pip install used to ship only cli.py)")

    ce_bin = shutil.which("ce")
    add("ce on PATH", True if ce_bin else None,
        ce_bin or "not found",
        "running `ce` after pip install",
        "pip install writes the `ce` script into Python's bin/scripts dir. "
        "Add that dir to PATH and open a new shell. "
        "Install Python 3.9+ from python.org — not Docker.")

    add("egress (default path)", True,
        "none — discover/review/report are local; snapshot talks to Sentry only if asked",
        "customer trust: source does not leave the machine")

    missing = [c for c in checks if c["status"] == "missing"]
    if args.json:
        print(json.dumps({"checks": checks, "ready": not missing}, indent=2))
        return 0 if not missing else 1

    width = max(len(c["name"]) for c in checks)
    for c in checks:
        mark = {"ok": "ok  ", "warn": "warn", "missing": "MISS"}[c["status"]]
        print(f"[{mark}] {c['name']:<{width}}  {c['detail']}")
        if c["status"] != "ok":
            print(f"{'':8}{' ' * width}  needed for: {c['needed_for']}")
            if c["fix"]:
                print(f"{'':8}{' ' * width}  fix: {c['fix']}")
    print()
    if missing:
        print(f"{len(missing)} blocking item(s). Everything else is optional and "
              "scoped to one command.")
        return 1
    print("Core pipeline ready. Optional items above affect only the commands named.")
    return 0


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

DECLARED_TEMPLATE = """# Declared journeys — the only required input.
#
# Fill in what you know and delete what you don't. A journey with nothing but
# id/name/source is valid: the resolver reports the missing roles and carries them
# into the spec as [NEEDS CLARIFICATION]. Guessing is worse than an open question.
#
# The seven roles are documented in GRAMMAR.md.

version: 1

journeys:
  - id: {jid}
    name: {jname}
    source: declared
    confidence: high
    business_impact: critical      # human-assigned; never derived from volume
    owner: you@example.com

    correlation_key:
      # NOT the trace ID — a browser navigation starts a new trace.
      attribute: {jid}.id
      persists_across: [page_load, service]

    steps:
      # `span_name` is the implementation binding. Omit it and the convention
      # applies: `{jid}` for step 1, `{jid}.<step_id>` after.
      - id: started
        span_name: {jid}
        surface: browser
        impact: normal
      - id: submitted
        span_name: {jid}.submitted
        surface: node
        impact: critical
      - id: confirmed
        span_name: {jid}.confirmed
        surface: browser
        impact: important

    outcome:
      attribute: {jid}.outcome
      values: [completed, failed, abandoned, rejected]
      success_values: [completed]
      default_value: abandoned

    failure_reason:
      attribute: {jid}.failure_reason
      bounded: true
      known_values: []

    magnitude:
      - attribute: {jid}.value
        type: double
        step: started

    actor_segment:
      - attribute: user.plan_tier
        already_available: true
"""

RUNBOOK = """# Run this against your service

Generated by `ce init`. Every path below is relative to this directory.

The usual customer path from the **service root** (not this folder) is:

```bash
ce discover
ce review
ce report
```

See `CUSTOMER.md` at the kit root: what we read, what we write, what leaves
the machine (nothing on that path).

## 1. Check the environment

```bash
ce doctor
```

## 2. Describe the journey

Edit `journeys.yaml`. Only `id`, `name`, and `source` are required.

```bash
ce intake --declared journeys.yaml --out-json resolved.json --out-md intake.md
```

## 3. Grade what your service emits — locally, no Sentry account

`ce local` starts a Sentry envelope collector, exports `SENTRY_DSN` pointing at
it, runs your command, then scores the telemetry that arrived.

```bash
ce local --resolved resolved.json --journey {jid} \\
  --drive 'npm run e2e' --out-md gap.md --out-json gap.json
```

Your app must read the DSN from the environment. Anything that speaks the Sentry
envelope protocol works — Python, Go, Ruby, JS, mobile.

`--drive` has to actually exercise the journey. A path never driven looks exactly
like a path never instrumented.

## 4. Or measure a live org instead

```bash
export SENTRY_AUTH_TOKEN=...      # needs org:read
ce snapshot --org YOUR_ORG --host https://us.sentry.io --project YOUR_PROJECT \\
  --stats-period 30d --traces-sample-rate 1.0 --out observed.json
ce gap --resolved resolved.json --observed observed.json --out-md gap.md --out-json gap.json
```

## 5. See what's automatic vs custom

```bash
ce profile --observed observed.json --gap gap.json --out-md profile.md
```

## 6. Generate the deliverable

```bash
ce spec --resolved resolved.json --gap gap.json --out-dir specs --rubric
```

`specs/{jid}-SPEC.md` goes to the coding agent, `-WHY.md` to the human.
`-RUBRIC.json` feeds `ce grade`.

## 7. Prove it worked

Keep `gap.json` as a baseline. After the work ships, re-measure and diff:

```bash
ce diff --baseline gap.json --current gap-after.json --out-md visibility-diff.md
```

Regressions lead that report, above the score.
"""


def cmd_init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ce init",
                                 description="Scaffold a working directory.")
    ap.add_argument("--out", default=WORK_DIRNAME)
    ap.add_argument("--journey-id", default="checkout")
    ap.add_argument("--journey-name", default="Checkout")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    out = resolve_path(args.out)
    ensure_workdir(out)
    gitignore_workdir(cwd_or_die(), out)
    files = {
        "journeys.yaml": DECLARED_TEMPLATE.format(jid=args.journey_id,
                                                  jname=args.journey_name),
        "RUNBOOK.md": RUNBOOK.format(jid=args.journey_id),
    }
    for name, body in files.items():
        p = out / name
        if p.exists() and not args.force:
            print(f"skip {p} (exists; --force to overwrite)", file=sys.stderr)
            continue
        p.write_text(body)
        print(f"wrote {p}", file=sys.stderr)
    # Human-authored template already has business_impact. Stamp so `ce report`
    # does not demand a second review of a file the engineer just filled in.
    (out / ".reviewed").write_text(json.dumps(
        {"kept": [args.journey_id], "excluded": [], "source": "ce init"},
        indent=2) + "\n")
    print(f"\nNext: edit {out}/journeys.yaml, then follow {out}/RUNBOOK.md",
          file=sys.stderr)
    print("Or, to derive candidates from this repo instead: `ce discover`",
          file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# local — the "run it against my service" command
# --------------------------------------------------------------------------


def cmd_local(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="ce local",
        description="Collect telemetry from a locally running service and grade it.")
    ap.add_argument("--resolved", required=True, help="Output of `ce intake`.")
    ap.add_argument("--drive", required=True,
                    help="Command that exercises the journey. SENTRY_DSN is exported "
                         "into its environment.")
    ap.add_argument("--journey", help="Journey id to report on. Default: all.")
    ap.add_argument("--cwd", default=".", help="Working directory for --drive.")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="Seconds to wait after the command exits, for async flush.")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--dsn-env", default="SENTRY_DSN",
                    help="Env var to put the DSN in. Some stacks use SENTRY_DSN_URL.")
    ap.add_argument("--sample-rate", type=float, default=1.0,
                    help="Recorded in the snapshot. Set it to what your app uses — "
                         "below 5%% every finding degrades to low confidence.")
    ap.add_argument("--out-observed", help="Keep the captured observed.json.")
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    ap.add_argument("--include-unready", action="store_true", default=True)
    ap.add_argument("--allow-empty", action="store_true",
                    help="Treat zero spans as a legitimate zero-coverage baseline "
                         "instead of an error. For a service with no Sentry SDK, "
                         "'nothing arrived' IS the finding — but prefer `ce scan`, "
                         "which reads the source and can still see intent.")
    ap.add_argument("--work", default=WORK_DIRNAME,
                    help="Directory for artifacts when --out-* is omitted. "
                         "Gitignored. Never the service root.")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(KIT / "eval" / "runtime"))
    try:
        from collector import EnvelopeCollector
    except ImportError as exc:
        print(f"error: cannot load the collector: {exc}", file=sys.stderr)
        return 1

    resolved = Path(args.resolved)
    if not resolved.exists():
        print(f"error: {resolved} not found. Run `ce intake` first.", file=sys.stderr)
        return 1

    with EnvelopeCollector(port=args.port) as c:
        env = dict(os.environ)
        env[args.dsn_env] = c.dsn
        print(f"collector on {c.host}:{c.port} · {args.dsn_env}={c.dsn}", file=sys.stderr)
        print(f"driving: {args.drive}", file=sys.stderr)
        try:
            proc = subprocess.run(args.drive, shell=True, cwd=args.cwd, env=env,
                                  timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f"error: --drive exceeded {args.timeout}s", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            print(f"warning: --drive exited {proc.returncode}; grading whatever "
                  "arrived anyway", file=sys.stderr)
        time.sleep(args.settle)
        observed = c.observed(org="local", stats_period="local-run",
                              traces_sample_rate=args.sample_rate)
        n_env = len(c.envelopes)

    if not observed["span_names"] and args.allow_empty:
        print(f"note: no spans captured, continuing because --allow-empty was passed. "
              f"({n_env} envelope(s) reached the collector.) Every journey will report "
              "zero coverage, which is the honest baseline for a service with no "
              "instrumentation.", file=sys.stderr)
    elif not observed["span_names"]:
        # These two cases have completely different causes, so they get different
        # advice. "Ran fine, sent nothing" is the more confusing one.
        if n_env == 0:
            print("error: no envelopes reached the collector at all.\n"
                  f"  - does the app read {args.dsn_env}? Some stacks use a config "
                  "file or a different variable — try --dsn-env\n"
                  "  - is tracing enabled (tracesSampleRate / traces_sample_rate > 0)?\n"
                  "  - did it flush before exiting (Sentry.flush / sentry_sdk.flush)?\n"
                  "  - PYTHON SDK: a bare `start_span` with no active transaction is an\n"
                  "    orphan and is DROPPED silently. The journey root must be\n"
                  "    `sentry_sdk.start_transaction(...)`; `start_span` is for children.\n"
                  "    The app exits 0 and sends nothing, which looks like a\n"
                  "    collector problem and isn't.", file=sys.stderr)
        else:
            print(f"error: {n_env} envelope(s) captured, but none carried spans.\n"
                  "  - envelopes with only error events mean tracing is off or the\n"
                  "    journey code path never ran\n"
                  "  - is tracing enabled (tracesSampleRate > 0)?\n"
                  "  - did --drive actually exercise the journey?", file=sys.stderr)
        print("\n  If the service genuinely has no instrumentation yet, that is not an\n"
              "  error — use `ce scan --repo . --out observed.json` to read the source\n"
              "  instead, or re-run with --allow-empty for a zero-coverage baseline.",
              file=sys.stderr)
        return 1

    # Write only under a chosen output or ce-work/. Dropping `.ce-observed.json`
    # in cwd showed up in git status — a tool leaving litter next to src/ is
    # not acceptable. Prefer, in order: --out-observed, next to --out-json /
    # --out-md, then ce-work/ (gitignored), then a temp file.
    work = resolve_path(args.work)
    if args.out_observed:
        obs_path = resolve_path(args.out_observed)
    elif args.out_json:
        obs_path = resolve_path(args.out_json).with_name(
            Path(args.out_json).stem + "-observed.json")
    elif args.out_md:
        obs_path = resolve_path(args.out_md).with_name(
            Path(args.out_md).stem + "-observed.json")
    else:
        ensure_workdir(work)
        gitignore_workdir(cwd_or_die(), work)
        obs_path = work / "observed.json"
        args.out_json = str(work / "gap.json")
        args.out_md = str(work / "gap.md")
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    obs_path.write_text(json.dumps(observed, indent=2) + "\n")
    print(f"captured {n_env} envelope(s) · {len(observed['span_names'])} distinct "
          f"span name(s) · {len(observed['attributes'])} attribute(s) → {obs_path}",
          file=sys.stderr)

    gap_argv = ["--resolved", str(resolved), "--observed", str(obs_path)]
    if args.include_unready:
        gap_argv.append("--include-unready")
    if args.out_md:
        gap_argv += ["--out-md", args.out_md]
    if args.out_json:
        gap_argv += ["--out-json", args.out_json]
    return delegate(STAGES["gap"], gap_argv)


# --------------------------------------------------------------------------
# discover / review / report — the customer-run path
# --------------------------------------------------------------------------


def _maybe_live_sentry(args: argparse.Namespace, work: Path, observed: Path) -> int:
    """Optional overlay of live org data onto the scan's observed.json.

    Span names: MCP `--from-mcp` only (undocumented /events query stays fenced).
    Attributes: documented API, needs a token. No token → write the MCP prompt
    and keep the scan. Never enables --unsafe-span-query.
    """
    want = args.sentry
    if args.no_sentry:
        want = False
    elif not args.sentry:
        want = prompt_yes("Fetch live telemetry from Sentry?")
    if not want:
        return 0

    sys.path.insert(0, str(KIT / "spec"))
    from publish import write_mcp_prompt  # noqa: WPS433
    write_mcp_prompt(work)

    token = args.token or os.environ.get("SENTRY_AUTH_TOKEN")
    org = args.org
    project = getattr(args, "project", None)
    mcp = args.from_mcp
    if mcp is None:
        candidate = work / "mcp-spans.json"
        mcp = candidate if candidate.is_file() else None

    if not token:
        print("No SENTRY_AUTH_TOKEN. Authenticate the Sentry MCP in Cursor and "
              f"follow {work / 'SENTRY-MCP.md'}. Continuing with the source scan.",
              file=sys.stderr)
        return 0
    if not org:
        if sys.stdin.isatty():
            try:
                org = input("Sentry org slug: ").strip()
            except EOFError:
                org = ""
        if not org:
            print("error: --org is required to fetch live Sentry data "
                  "(or skip with --no-sentry).", file=sys.stderr)
            return 1
    if not project and sys.stdin.isatty():
        try:
            project = input("Sentry project slug (Enter to skip): ").strip() or None
        except EOFError:
            project = None

    # Token stays in the environment — never argv, never a file, never git.
    if args.token:
        os.environ["SENTRY_AUTH_TOKEN"] = args.token
    snap_argv = [
        "--org", org,
        "--out", str(work / "observed-live.json"),
    ]
    if project:
        snap_argv += ["--project", project]
    if mcp:
        snap_argv += ["--from-mcp", str(mcp)]
    else:
        print("note: no MCP span JSON yet, so this snapshot is attributes-only. "
              f"See {work / 'SENTRY-MCP.md'} for search_events → --from-mcp.",
              file=sys.stderr)
    rc = delegate(STAGES["snapshot"], snap_argv)
    if rc != 0:
        print("warning: live snapshot failed; keeping the source scan.",
              file=sys.stderr)
        return 0

    sys.path.insert(0, str(KIT / "gap"))
    from sentry_source import merge_scan_into_live  # noqa: WPS433
    try:
        scan_doc = json.loads(observed.read_text())
        live_doc = json.loads((work / "observed-live.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not merge live snapshot: {exc}", file=sys.stderr)
        return 0
    observed.write_text(json.dumps(merge_scan_into_live(scan_doc, live_doc),
                                   indent=2) + "\n")
    print(f"merged live Sentry data → {observed}", file=sys.stderr)
    return 0


def cmd_discover(argv: list[str]) -> int:
    """propose + scan + intake into ce-work/. Stops for the human review."""
    ap = argparse.ArgumentParser(
        prog="ce discover",
        description="Propose journeys from this repo and scan for existing "
                    "instrumentation. Writes only under --out (default ce-work/).")
    ap.add_argument("--repo", default=".", help="Service root. Default: cwd.")
    ap.add_argument("--out", default=WORK_DIRNAME,
                    help="Work directory for artifacts. Gitignored.")
    ap.add_argument("--max-journeys", type=int, default=6)
    ap.add_argument("--sentry", action="store_true",
                    help="Fetch live Sentry data (token + optional --from-mcp).")
    ap.add_argument("--no-sentry", action="store_true",
                    help="Skip the live-Sentry prompt (default in non-TTY).")
    ap.add_argument("--org", help="Sentry org slug, used with --sentry.")
    ap.add_argument("--project", help="Sentry project slug or id, used with --sentry.")
    ap.add_argument("--token", help="Or set SENTRY_AUTH_TOKEN. Never written to disk.")
    ap.add_argument("--from-mcp", type=Path,
                    help="JSON from Sentry MCP search_events (span names).")
    args = ap.parse_args(argv)

    repo = resolve_path(args.repo)
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 1

    sys.path.insert(0, str(KIT / "gap"))
    from propose import has_supported_source  # noqa: WPS433
    if not has_supported_source(repo):
        print("error: no JavaScript/TypeScript or Python source in this tree. "
              "`ce propose` only reads those languages. Hand-author journeys "
              "with `ce init` instead of generating an empty or junk proposal.",
              file=sys.stderr)
        return 2

    work = ensure_workdir(resolve_path(args.out))
    gitignore_workdir(repo, work)
    # A leftover stamp from `ce init` must not skip review of newly proposed
    # journeys — that is how a `web` directory used to reach `ce report`.
    stamp = work / ".reviewed"
    if stamp.exists():
        stamp.unlink()

    journeys = work / "journeys.yaml"
    proposal = work / "proposal.md"
    observed = work / "observed.json"
    resolved = work / "resolved.json"
    intake_md = work / "intake.md"

    rc = delegate(STAGES["propose"], [
        "--repo", str(repo), "--out", str(journeys),
        "--report", str(proposal), "--max-journeys", str(args.max_journeys),
    ])
    if rc not in (0, 2):
        return rc
    if rc == 2:
        print("note: no journey candidates from source. Hand-author "
              f"{journeys} (`ce init --out {work}`) or add routes/handlers "
              "and re-run.", file=sys.stderr)

    scan_rc = delegate(STAGES["scan"], [
        "--repo", str(repo), "--out", str(observed),
    ])
    if scan_rc != 0:
        return scan_rc

    live_rc = _maybe_live_sentry(args, work, observed)
    if live_rc != 0:
        return live_rc

    if rc == 0 and journeys.exists():
        intake_rc = delegate(STAGES["intake"], [
            "--discovered", str(journeys),
            "--out-json", str(resolved), "--out-md", str(intake_md),
        ])
        if intake_rc != 0:
            return intake_rc

    write_review(work)
    print(f"\nWrote {work}/. Next: `ce review` (see REVIEW.md), then `ce report`. "
          "Nothing in source can set business_impact.",
          file=sys.stderr)
    return 0


def cmd_review(argv: list[str]) -> int:
    return delegate(STAGES["review"], argv)


def cmd_report(argv: list[str]) -> int:
    """gap + profile + spec from ce-work/. Needs discover + review first."""
    ap = argparse.ArgumentParser(
        prog="ce report",
        description="Gap, profile, and spec from a ce-work/ directory. "
                    "Copies *-SPEC.md to .agents/journeys/ for the agent and Warden.")
    ap.add_argument("--work", default=WORK_DIRNAME)
    ap.add_argument("--repo", default=".",
                    help="Service root for .agents/, AGENTS.md, warden.toml.")
    ap.add_argument("--include-absent", action="store_true", default=None,
                    help="Force spec generation for fully uninstrumented journeys. "
                         "Default: on when the scan found no SDK.")
    args = ap.parse_args(argv)

    work = resolve_path(args.work)
    repo = resolve_path(args.repo)
    journeys = work / "journeys.yaml"
    resolved = work / "resolved.json"
    observed = work / "observed.json"
    if not (work / ".reviewed").is_file():
        print(f"error: {work / '.reviewed'} missing. Run `ce review` "
              "(browser) or `ce review --stamp` after editing journeys.yaml. "
              "`ce report` will not infer business_impact.",
              file=sys.stderr)
        return 1
    if not observed.exists():
        print(f"error: {observed} not found. Run `ce discover` or "
              f"`ce scan --repo . --out {observed}`.", file=sys.stderr)
        return 1
    if journeys.exists():
        # Review rewrites yaml (keep/drop/impact). Re-resolve so report
        # does not spec journeys the engineer dropped.
        intake_rc = delegate(STAGES["intake"], [
            "--discovered", str(journeys),
            "--out-json", str(resolved), "--out-md", str(work / "intake.md"),
        ])
        if intake_rc != 0:
            return intake_rc
    if not resolved.exists():
        print(f"error: {resolved} not found. Run `ce discover` first "
              "(or `ce intake` into this directory).", file=sys.stderr)
        return 1

    try:
        doc = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {resolved}: {exc}", file=sys.stderr)
        return 1
    keepers = [j for j in doc.get("journeys") or []
               if not j.get("excluded") and j.get("business_impact")]
    if not keepers:
        print("error: no kept journey has business_impact. Re-run `ce review` "
              "and set impact on at least one keeper.", file=sys.stderr)
        return 1

    gap_json = work / "gap.json"
    gap_md = work / "gap.md"
    rc = delegate(STAGES["gap"], [
        "--resolved", str(resolved), "--observed", str(observed),
        "--include-unready", "--out-json", str(gap_json), "--out-md", str(gap_md),
    ])
    if rc != 0:
        return rc

    profile_md = work / "profile.md"
    profile_json = work / "profile.json"
    pr = delegate(STAGES["profile"], [
        "--observed", str(observed), "--gap", str(gap_json),
        "--out-md", str(profile_md), "--out-json", str(profile_json),
    ])
    if pr != 0:
        return pr

    include_absent = args.include_absent
    if include_absent is None:
        try:
            sdk = json.loads(observed.read_text()).get("sdk") or {}
            include_absent = not sdk.get("any_sdk_present", True)
        except (OSError, json.JSONDecodeError):
            include_absent = False

    spec_argv = [
        "--resolved", str(resolved), "--gap", str(gap_json),
        "--out-dir", str(work / "specs"), "--rubric",
        "--observed", str(observed), "--sdk", "auto",
    ]
    if include_absent:
        spec_argv.append("--include-absent")
    sr = delegate(STAGES["spec"], spec_argv)
    if sr != 0:
        return sr

    pub = delegate(STAGES["publish"], [
        "--work", str(work), "--repo", str(repo),
    ])
    if pub != 0:
        return pub
    print(f"report → {work}/gap.md · profile.md · specs/ · {repo}/.agents/journeys/",
          file=sys.stderr)
    print("Next: point your coding agent at .agents/journeys/<id>-SPEC.md "
          "(see CUSTOMER.md). Do not implement from WHY.md.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------


BUILTINS = {
    "doctor": cmd_doctor,
    "init": cmd_init,
    "local": cmd_local,
    "discover": cmd_discover,
    "review": cmd_review,
    "report": cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.version_info < MIN_PYTHON:
        print(f"error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
              f"got {sys.version.split()[0]}", file=sys.stderr)
        return 1
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nstages: " + ", ".join(sorted({*BUILTINS, *STAGES})))
        print("\n`ce <stage> --help` for a stage's own options.")
        return 0
    if argv[0] in ("--version", "-V"):
        print("critical-experience-kit 0.1.0")
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd in BUILTINS:
        return BUILTINS[cmd](rest)
    if cmd in STAGES:
        return delegate(STAGES[cmd], rest)
    print(f"error: unknown command `{cmd}`\n"
          f"available: {', '.join(sorted({*BUILTINS, *STAGES}))}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
