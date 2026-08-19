#!/usr/bin/env python3
"""
Publish the agent-facing contract out of ce-work/ into tracked paths.

ce-work/ is gitignored working storage (YAML, gap, WHY). Warden and the
customer's coding agent need a tracked spec or they no-op / never see it.

Writes (next to source, deliberately):
  .agents/journeys/<id>-SPEC.md
  .agents/skills/sentry-critical-experience/   (vendored Warden skill)
  AGENTS.md pointer (create or append)
  .cursor/rules/sentry-journeys.mdc
  warden.toml                                  (failOn=off, paths from kept journeys)
  ce-work/EXPLORE.md                           (copy-paste Trace Explorer queries)

Never copies *-WHY.md into .agents/.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
AGENTS_MARKER = "<!-- ce:sentry-journeys -->"
AGENTS_SECTION = f"""{AGENTS_MARKER}
## Sentry journey specs

Implement only the files in `.agents/journeys/`. Those specs are the contract:
exact span names, attribute keys, and the only Sentry APIs allowed.

- Implement the numbered requirements in §4. Do not invent names.
- Do not read or implement from `ce-work/` or `*-WHY.md`.
- One journey per session.
"""

CURSOR_RULE = """---
description: Implement Sentry journey specs from .agents/journeys only
globs: .agents/journeys/**/*.md
alwaysApply: true
---

Implement Sentry instrumentation only from `.agents/journeys/<id>-SPEC.md`.

- Read the whole spec first. Implement only the numbered requirements in §4.
- Use exactly the span names and attribute keys in the spec. Do not invent names.
- Use only the Sentry APIs in §5. Never the APIs in §6.
- Do not read or implement from `ce-work/` or `*-WHY.md`.
- If a required span has no clear location in this repo, stop and ask.
"""

MCP_PROMPT = """# Fetch live Sentry data for `ce`

`ce` cannot speak MCP itself — MCP lives in Cursor. Authenticate the Sentry
MCP server, then save the tool results as JSON for `ce snapshot --from-mcp`.

1. Authenticate the Sentry MCP (the `mcp_auth` tool on the Sentry server).
2. Call `search_events` with dataset `spans`, fields
   `['span.description', 'count()']`, sort `-count()`, stats period `30d`.
   Save the JSON to `ce-work/mcp-spans.json`.
3. Prefer a second `search_events` with fields `['span.op', 'count()']` in the
   same file (or a sibling `ce-work/mcp-ops.json` merged in).
4. Optional, for a real waterfall on `ce review`: fetch one example trace
   (MCP `get_trace` / equivalent) and save the spans as
   `ce-work/mcp-trace.json` (`{ "spans": [ { "span_id", "parent_span_id",
   "name" or "description", "op", "data" } ] }`). Without this file, the
   review page sketches structure from span names or from source routes —
   it never invents latency.
5. Then, with an org slug and `SENTRY_AUTH_TOKEN` (org:read) for the
   **documented** attributes API:

```bash
export SENTRY_AUTH_TOKEN=...
ce snapshot --org YOUR_ORG --from-mcp ce-work/mcp-spans.json --out ce-work/observed.json
ce report
```

Do not use undocumented span query endpoints. Span names come from MCP;
attribute presence comes from `trace-items/attributes/` (`source_type`
sentry vs user).
"""


def _skill_src() -> Path | None:
    candidates = [
        KIT / "warden-skill" / "sentry-critical-experience",
    ]
    try:
        import warden_skill  # type: ignore
        pkg = Path(warden_skill.__file__).resolve().parent / "sentry-critical-experience"
        candidates.insert(0, pkg)
    except ImportError:
        pass
    for p in candidates:
        if (p / "SKILL.md").is_file():
            return p
    return None


WARDEN_BEGIN = "# --- begin ce:sentry-critical-experience ---"
WARDEN_END = "# --- end ce:sentry-critical-experience ---"


def _glob_from_step(step: dict) -> str | None:
    """Turn a discovered step's evidence path into a tight Warden glob.

    Hardcoding `src/checkout/**` was how a scaffold reviewed the wrong tree
    on every non-checkout service.
    """
    evidence = step.get("evidence") or ""
    m = re.search(r"\(([^)]+)\)\s*$", evidence)
    if not m:
        return None
    rel = m.group(1).replace("\\", "/").strip()
    if not rel or rel in (".", "/"):
        return None
    parent = str(Path(rel).parent).replace("\\", "/")
    if parent in (".", ""):
        parent = rel.rsplit("/", 1)[0] if "/" in rel else rel
    suffix = Path(rel).suffix.lower()
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return f"{parent}/**/*.{{ts,tsx,js,jsx}}"
    if suffix == ".py":
        return f"{parent}/**/*.py"
    return f"{parent}/**/*"


def _warden_paths(work: Path, journey_ids: list[str]) -> list[str]:
    journeys: list[dict] = []
    resolved = work / "resolved.json"
    if resolved.is_file():
        try:
            journeys = json.loads(resolved.read_text()).get("journeys") or []
        except (OSError, json.JSONDecodeError):
            journeys = []
    by_id = {j.get("id"): j for j in journeys}
    paths: list[str] = []
    seen: set[str] = set()
    for jid in journey_ids:
        j = by_id.get(jid) or {}
        roles = j.get("roles") or {}
        steps = roles.get("steps") or j.get("steps") or []
        found = False
        for s in steps:
            g = _glob_from_step(s)
            if g and g not in seen:
                seen.add(g)
                paths.append(g)
                found = True
        if not found:
            g = f"**/{jid}/**/*.{{ts,tsx,js,jsx,py}}"
            if g not in seen:
                seen.add(g)
                paths.append(g)
    if not paths:
        # Last resort: still journey-id-derived, never checkout-hardcoded.
        paths = [f"**/{jid}/**/*" for jid in journey_ids] or ["src/**/*"]
    return paths


def _skill_block(paths: list[str]) -> str:
    listed = "\n".join(f'  "{p}",' for p in paths)
    return f"""[[skills]]
name = "./.agents/skills/sentry-critical-experience"
paths = [
{listed}
]
ignorePaths = [
  "**/*.test.*",
  "**/*.spec.*",
  "**/__tests__/**",
  "**/__fixtures__/**",
  "**/__mocks__/**",
  "**/*.stories.*",
  "**/generated/**",
  "**/*.d.ts",
]
minConfidence = "high"
failOn = "off"
reportOn = "low"
maxFindings = 10

[[skills.triggers]]
type = "pull_request"
actions = ["opened", "synchronize", "reopened", "labeled"]
draft = false
"""


def _warden_toml(paths: list[str]) -> str:
    return f"""# Warden — Sentry critical experience (scaffolded by `ce report`)
#
# Advisory on day one: comments on the implementation PR, never blocks merge.
# After the first green PR, raise failOn to "high" if you want CI to gate.
#
# Skill is vendored at .agents/skills/sentry-critical-experience so this repo
# does not depend on an external git remote.

version = 1

[defaults]
runtime = "pi"
failOn = "off"
reportOn = "medium"

{WARDEN_BEGIN}
{_skill_block(paths)}{WARDEN_END}
"""


def _upsert_warden_toml(repo: Path, paths: list[str]) -> None:
    path = repo / "warden.toml"
    block = f"{WARDEN_BEGIN}\n{_skill_block(paths)}{WARDEN_END}\n"
    if not path.exists():
        path.write_text(_warden_toml(paths))
        print(f"wrote {path} (failOn=off, advisory)", file=sys.stderr)
        return
    text = path.read_text()
    if WARDEN_BEGIN in text and WARDEN_END in text:
        pre, rest = text.split(WARDEN_BEGIN, 1)
        _, post = rest.split(WARDEN_END, 1)
        path.write_text(pre + block + post)
        print(f"updated {path} skill paths (failOn=off)", file=sys.stderr)
        return
    path.write_text(text.rstrip() + "\n\n" + block)
    print(f"appended {path} skill block (failOn=off, advisory)", file=sys.stderr)


def _append_agents_md(repo: Path) -> None:
    path = repo / "AGENTS.md"
    existing = path.read_text() if path.exists() else ""
    if AGENTS_MARKER in existing:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + prefix + "\n" + AGENTS_SECTION)
    print(f"wrote {path} (journey pointer)", file=sys.stderr)


def _write_cursor_rule(repo: Path) -> None:
    path = repo / ".cursor" / "rules" / "sentry-journeys.mdc"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CURSOR_RULE)
    print(f"wrote {path}", file=sys.stderr)


def _copy_skill(repo: Path) -> None:
    src = _skill_src()
    dest = repo / ".agents" / "skills" / "sentry-critical-experience"
    if dest.exists():
        return
    if src is None:
        print("warning: warden skill not shipped with this install; "
              "skip vendoring. Copy warden-skill/sentry-critical-experience "
              "into .agents/skills/ by hand.", file=sys.stderr)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    print(f"vendored Warden skill → {dest}", file=sys.stderr)


def _copy_specs(work: Path, repo: Path) -> list[str]:
    src_dir = work / "specs"
    dest = repo / ".agents" / "journeys"
    dest.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    if not src_dir.is_dir():
        return ids
    for p in sorted(src_dir.glob("*-SPEC.md")):
        shutil.copy2(p, dest / p.name)
        ids.append(p.name[: -len("-SPEC.md")])
        print(f"wrote {dest / p.name}", file=sys.stderr)
    # Never copy WHY.md — Warden and the agent must not implement from it.
    return ids


def _explore_md(work: Path, ids: list[str]) -> None:
    L = [
        "# Trace Explorer queries\n",
        "Copy-paste after the instrumentation PR merges. `ce` does not create "
        "dashboards or alerts (that needs org:write and is surprising).\n",
    ]
    spec_dir = work / "specs"
    for jid in ids:
        spec = spec_dir / f"{jid}-SPEC.md"
        if not spec.is_file():
            continue
        text = spec.read_text()
        if "## 7. Acceptance criteria" not in text:
            continue
        section = text.split("## 7. Acceptance criteria", 1)[1]
        rest = section.split("\n## ", 1)[0]
        L.append(f"## {jid}\n")
        L.append(rest.strip() + "\n")
    (work / "EXPLORE.md").write_text("\n".join(L))
    print(f"wrote {work / 'EXPLORE.md'}", file=sys.stderr)


def write_mcp_prompt(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "SENTRY-MCP.md").write_text(MCP_PROMPT)
    print(f"wrote {work / 'SENTRY-MCP.md'} — authenticate Sentry MCP, then snapshot",
          file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ce publish",
        description="Copy specs into tracked .agents/journeys and scaffold Warden.")
    ap.add_argument("--work", default="ce-work")
    ap.add_argument("--repo", default=".",
                    help="Service root — where .agents/ and warden.toml land.")
    ap.add_argument("--skip-warden", action="store_true")
    args = ap.parse_args(argv)

    work = Path(args.work)
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 1

    ids = _copy_specs(work, repo)
    if not ids:
        print("note: no *-SPEC.md under specs/; nothing published to .agents/",
              file=sys.stderr)
        return 0

    _append_agents_md(repo)
    _write_cursor_rule(repo)
    if not args.skip_warden:
        _copy_skill(repo)
        _upsert_warden_toml(repo, _warden_paths(work, ids))
    _explore_md(work, ids)

    # resolved.json ids with impact, for a one-line next-step
    print("Next: open a branch, point your agent at "
          ".agents/journeys/<id>-SPEC.md (see CUSTOMER.md). "
          "Install Warden so the implementation PR is reviewed.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
