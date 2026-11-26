#!/usr/bin/env python3
"""
Explore a repo for critical journeys with an LLM — in any language — and verify it.

Why this exists. `propose.py` guessed journeys from directory names and exported
function names in two languages. On Supabase it proposed app folders and icon
components (0 of 14 labeled journeys in its top 5); on MetaMask, `ui` and a bucket of
mock builders. Static rules cannot adapt to a repo's layout or vocabulary; reading
code can. An LLM that reads code works on Go, Rust, Ruby or Solidity as well as TS.

The risk with an LLM is invention, so the design is: the model PROPOSES and a
mechanical validator DISPOSES.

    signals.py  →  brief (e2e names, analytics, docs, hot dirs)   language-agnostic
    explorer    →  discovery.json: journeys → steps → cited evidence
                   · `openrouter` mode: this module runs the tool loop (list/read/grep)
                   · `task` mode: emits a task file; the customer's OWN agent/model does
                     the reading, writes discovery.json (nothing leaves their machine)
    validator   →  every cited file must exist, symbol must be in it, line in range;
                   confidence is COMPUTED from grounding + independent corroboration,
                   never taken from the model; ungrounded journeys are dropped
    ce intake   →  journeys.yaml (source: discovered:code) feeds the existing pipeline

Invariants kept (AGENTS.md): never generate application code (this proposes journeys,
it writes none); never rank by volume; never infer `business_impact` or
`success_values`-as-fact (outcome values are proposals flagged for confirmation);
provenance on every artifact.

Usage:
    # emit a task for the customer's own agent (no network, no key)
    ce explore --repo . --mode task --out ce-work
    #   ...their agent writes ce-work/discovery.json, then:
    ce explore --repo . --ingest ce-work/discovery.json --out ce-work

    # run the exploration here via OpenRouter (key from OPENROUTER_API_KEY)
    ce explore --repo . --mode openrouter --out ce-work
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signals as sig  # noqa: E402

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Mid-tier, tool-calling, long-context: exploration is many cheap reads, so cost per
# customer matters more than peak reasoning. Override with --model or CE_EXPLORER_MODEL.
# NOTE: confirm this id against https://openrouter.ai/models before shipping; a wrong
# id fails fast with an actionable message (see `_http_error_hint`).
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

MAX_TOOL_CHARS = 9000
MAX_TURNS = 45
MAX_FILE_LINES = 220

TASK_TEMPLATE = """\
# Discover the critical journeys in `{repo}`

You are helping a Sentry Solutions Engineer decide which **business journeys** in this
codebase to instrument. You produce a JSON file. You do NOT write or change any code.

## What counts as a journey
A goal an actor (user, system, job) completes across **two or more steps or components**
with a business consequence: money moves, identity is established or changed, a resource
is provisioned or destroyed, data is created or lost, a message is delivered. Examples of
the *kind* of thing: sign-up, checkout, send funds, create a project, publish a plan.
Not a journey: a page, a component, a utility, a settings toggle, a design-system piece.

## How to work
1. Read the signals brief below. E2E spec names, analytics events and docs headings are
   things humans already wrote down about the product — start there, but do not stop there.
2. Find consequence-bearing flows the tests do NOT cover (billing/subscriptions,
   sign-in/up, org or team invites, provisioning, deletion/export, payments, signing).
   Revenue and identity flows are often untested and are the ones that matter most.
3. For each candidate, trace entry point → intermediate steps → effect by READING the code.
   Use directory listings, grep, and file reads. Do not guess from names alone.
4. Exclude: tests, mocks, stories, design systems, marketing/docs sites, examples/demo
   apps, benchmarks, CI and dev tooling. Include internal tools only if the product is one.

## Rules
- Every step cites evidence you actually read: `file` (repo-relative, must exist), `line`,
  and `symbol` (a function/handler/component name that is in that file at that line).
  Steps are listed in execution order. Unverifiable evidence is discarded automatically,
  and a journey with fewer than two grounded steps is dropped.
- Do NOT rank by traffic or guess `business_impact`. Order journeys by consequence
  (money, identity, data loss) — not by how often they run.
- Name journeys the way the business would ("Checkout", "Create project"), not by route or file.
- `correlation_key.attribute` must identify cases of THIS journey only (`checkout.id`,
  `onboarding.flow_id`) — not a shared entity id like `project.ref` or `organization.slug`
  that several journeys would reuse. Omit it if no such identifier exists.
- If you are unsure of something, say so in `open_questions` instead of inventing it.
  Outcome values and correlation keys are PROPOSALS a human will confirm.
- Return at most {max_journeys} journeys. Well-evidenced beats thin, but do not drop a real
  journey to stay short: several seeds may be one journey, and one seed may hold several.
- **Account for every E2E stem.** The brief lists numbered seeds (S1, S2, …), each with the
  spec/test names (stems) humans already wrote down. Every stem must end up in
  `seed_coverage`, either under the journey that covers it (`covers`) or in `rejected` with a
  category. A journey must plausibly be about its stems: a stem mapped to a journey that shares
  no vocabulary with it, or whose citations fail, is sent back. `"*"` (all stems) is allowed
  only for seeds with 3 stems or fewer. "I did not trace it" is not a rejection: trace it, or
  add the journey with what you could verify and put the rest in `open_questions`.
  A `not_consequence_bearing` citation must be code that relates to the stems you reject
  (shares their vocabulary), not any file you happened to read. A journey may cover at most
  {max_stems} stems: split bigger ones into more specific journeys (by asset, actor or channel).
  Rejection categories: `tooling`, `test_helper`, `docs_site`, `marketing_site`, `example_app`,
  `telemetry`, and `not_consequence_bearing` (this one needs an `evidence` citation to a file
  you read that shows why). Submissions that leave stems unaccounted are sent back.

## Output: a single JSON object
```json
{{
  "journeys": [
    {{
      "id": "snake_case_id",
      "name": "Business name",
      "why_it_matters": "one sentence: the consequence if this breaks",
      "signals": ["e2e:<spec or dir>", "analytics:<event>", "docs:<heading>", "code-only"],
      "steps": [
        {{"id": "step_id", "description": "what happens",
          "surface": "browser|node|worker|mobile|backend",
          "evidence": [{{"file": "path/in/repo.ext", "line": 42, "symbol": "functionName"}}]}}
      ],
      "correlation_key": {{"attribute": "checkout.id", "evidence": "where this id is created/passed"}},
      "outcome": {{"attribute": "checkout.outcome", "values": ["completed", "failed", "abandoned"],
                  "evidence": "where the terminal state is decided"}},
      "magnitude": [{{"attribute": "order.value", "type": "double", "unit": "USD"}}],
      "actor_segment": [{{"attribute": "plan_tier"}}],
      "open_questions": ["anything you could not verify"]
    }}
  ],
  "seed_coverage": [
    {{"seed": "S1",
      "covers": {{"snake_case_id": ["stem-a", "stem-b"], "other_journey_id": ["stem-c"]}},
      "rejected": [{{"stems": ["stem-d"], "category": "test_helper", "reason": "shared page helpers"}},
                  {{"stems": ["stem-e"], "category": "not_consequence_bearing",
                   "reason": "UI preference toggle, no data effect",
                   "evidence": {{"file": "path/in/repo.ext", "line": 10, "symbol": "toggleTheme"}}}}]}}
  ]
}}
```

{delivery}

---

{brief}
"""

DELIVERY_AGENT = """\
## Tools
You have `list_dir(path)`, `read_file(path, start, end)`, `grep(pattern, path)` and
`submit_discovery(discovery)`. Explore, then call `submit_discovery` once with the JSON above.
Budget: about {turns} tool calls. Spend them on tracing the flows, not on re-listing directories.
Older tool results are elided from the conversation to save budget. As you read, state in a
line what you learned (`file:line symbol → what it does`) so you never need to re-read it."""

DELIVERY_EMIT = """\
## Delivery
Write the JSON to `{out}/discovery.json`, then run:

    ce explore --repo {repo_arg} --ingest {out}/discovery.json --out {out}

That validates every citation against the repo and produces `journeys.yaml` for
`ce intake`. Use your normal tools to read the repo; do not modify any source files."""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s))
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return re.sub(r"_+", "_", s) or "journey"


def norm_evidence(ev) -> list:
    """Accept dicts, 'path:line' strings, or lists of either."""
    out = []
    if ev is None:
        return out
    if isinstance(ev, (str, dict)):
        ev = [ev]
    for e in ev:
        if isinstance(e, str):
            m = re.match(r"^\s*([^\s:]+?)(?::(\d+))?(?:\s+(\S+))?\s*$", e)
            if m:
                out.append({"file": m.group(1), "line": int(m.group(2)) if m.group(2) else None,
                            "symbol": m.group(3)})
        elif isinstance(e, dict) and e.get("file"):
            try:
                line = int(e["line"]) if e.get("line") not in (None, "") else None
            except (TypeError, ValueError):
                line = None
            out.append({"file": str(e["file"]), "line": line, "symbol": e.get("symbol") or None})
    return out


def check_evidence(ev: dict, root: Path, cache: dict) -> dict:
    """Return ev + `status` in {verified, file_only, noise_path, missing_file,
    outside_repo, secret, symbol_not_found, bad_line}. Only verified/file_only count."""
    rel = ev["file"].replace("\\", "/").lstrip("./") if ev["file"].startswith("./") else ev["file"].replace("\\", "/")
    p = Path(rel)
    if p.is_absolute():
        try:
            p = p.relative_to(root)
        except ValueError:
            return dict(ev, file=rel, status="outside_repo")
    full = (root / p).resolve()
    try:
        full.relative_to(root.resolve())
    except ValueError:
        return dict(ev, file=str(p), status="outside_repo")
    rel = str(p)
    base = dict(ev, file=rel)
    if sig.is_secret(full):
        return dict(base, status="secret")
    if not full.is_file():
        return dict(base, status="missing_file")
    if sig.NOISE_PATH.search(rel):
        return dict(base, status="noise_path")
    if rel not in cache:
        cache[rel] = sig.read(full).splitlines()
    lines = cache[rel]
    if ev.get("line") is not None and not (1 <= ev["line"] <= max(len(lines), 1)):
        return dict(base, status="bad_line")
    if ev.get("symbol"):
        if ev.get("line") is not None:
            lo, hi = max(ev["line"] - 40, 0), min(ev["line"] + 40, len(lines))
            window = "\n".join(lines[lo:hi])
            if ev["symbol"] not in window:
                return dict(base, status="symbol_not_found")
        elif ev["symbol"] not in "\n".join(lines):
            return dict(base, status="symbol_not_found")
        return dict(base, status="verified")
    return dict(base, status="verified" if ev.get("line") is not None else "file_only")


GENERIC = {"the", "a", "and", "or", "of", "to", "in", "new", "create", "view", "page", "flow", "edit",
           "manage", "update", "delete", "settings", "setting", "list", "get", "set", "add", "remove"}


def stem(t: str) -> str:
    """Light, dependency-free stemming: creation~create~creat, users~user, editor~edit."""
    for suf in ("ation", "ions", "ion", "ing", "ers", "er", "ors", "or", "ed", "es", "s", "e"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


GENERIC_STEMS = {"the", "a", "and", "or", "of", "to", "in", "new", "creat", "view", "page", "flow", "edit",
                 "manage", "update", "delet", "setting", "list", "get", "set", "add", "remov", "with"}


def _teq(a: str, b: str) -> bool:
    a, b = stem(a), stem(b)
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


def _overlaps(name_toks: set, other: set) -> bool:
    """A real match is two shared tokens, or a whole spec/event/heading name contained
    in the journey name. One shared common word (`access`, `table`) is not evidence —
    `api-access-toggle` must not corroborate "Create access tokens"."""
    other = {t for t in other if stem(t) not in GENERIC_STEMS}
    if not other or not name_toks:
        return False
    hit = {o for o in other if any(_teq(o, n) for n in name_toks)}
    return len(hit) >= 2 or (len(hit) == len(other) and len(other) <= 3)


def corroboration(j: dict, signals: dict) -> list:
    """Independent, MECHANICAL corroboration of a journey: do humans' own artifacts
    (e2e names, analytics events, docs headings) match its name? The model's own
    `signals` field is ignored — it is a claim, this is a check. Name matching only:
    an earlier version also credited "a track() call lives in a cited file", true of
    nearly every file in an instrumented app, so every journey came back corroborated."""
    if not signals:
        return []
    name_toks = {t for t in sig.norm_tokens(j["id"]) + sig.norm_tokens(j["name"])
                 if stem(t) not in GENERIC_STEMS}
    found = []
    for g in signals.get("e2e_groups", []):
        if sig.E2E_SUPPORT.search(g["dir"] + "/"):
            continue
        cands = [set(sig.norm_tokens(st)) for st in g["stems"]] + [set(sig.norm_tokens(g["dir"].split("/")[-1]))]
        if any(_overlaps(name_toks, c) for c in cands):
            found.append("e2e")
            break
    if any(_overlaps(name_toks, set(sig.norm_tokens(e["event"]))) for e in signals.get("analytics_events", [])):
        found.append("analytics")
    if any(_overlaps(name_toks, set(sig.norm_tokens(h))) for d in signals.get("docs", []) for h in d["headings"]):
        found.append("docs")
    return found


MAX_STEMS_PER_JOURNEY = 15
# A reason that admits the model did not look is not a reason (live run 3: Supabase
# `assistant` was rejected with "I did not trace it").
ADMISSION = re.compile(r"\b(did not|didn'?t|could ?n[o']t|unable to|never|not)\s+"
                       r"(trace|traced|find|found|read|look|looked|verify|verified|locate|inspect|"
                       r"investigate|investigated|examine|examined)\b|\buntraced\b|\bunverified\b", re.I)

REJECT_CATS = {"tooling", "test_helper", "docs_site", "marketing_site", "example_app",
               "telemetry", "not_consequence_bearing"}


def _stem_vocab(stem_name: str) -> set:
    return {t for t in sig.norm_tokens(stem_name) if stem(t) not in GENERIC_STEMS}


def _journey_vocab(j: dict) -> set:
    toks = set(sig.norm_tokens(j["id"]) + sig.norm_tokens(j["name"]))
    for s_ in j["steps"]:
        for e in s_["evidence"]:
            toks.update(sig.norm_tokens(e.get("file") or ""))
    return {t for t in toks if stem(t) not in GENERIC_STEMS}


def account(doc: dict, signals: dict | None, kept: list, root: Path, cache: dict | None = None) -> dict:
    """Mechanical accounting of every e2e stem in the brief.

    -> {"missing": [{seed, dir, stems}], "weak": [{seed, stem, journey}], "total": n, "ok": n}
    A stem counts when it sits under a journey that survived validation AND shares
    vocabulary with it (name or cited file paths), or is rejected with a known category
    (`not_consequence_bearing` needs a verifiable citation). Everything else is missing.
    The model cannot satisfy this by mapping a whole directory to one journey."""
    sds = sig.seeds(signals or {})
    if not sds:
        return {"missing": [], "weak": [], "total": 0, "ok": 0, "bad_rejections": [], "overloaded": []}
    cache = cache if cache is not None else {}
    by_id = {j["id"]: j for j in kept}
    cov = {str(c.get("seed")): c for c in (doc.get("seed_coverage") or []) if isinstance(c, dict)}
    missing, weak, total, ok = [], [], 0, 0
    bad_rej, per_journey = [], {}
    for sd in sds:
        c = cov.get(sd["id"], {})
        done, bad = set(), set()
        for jid, stems in (c.get("covers") or {}).items() if isinstance(c.get("covers"), dict) else []:
            j = by_id.get(snake(jid))
            if j is None:
                continue
            names = sd["stems"] if stems == "*" and len(sd["stems"]) <= 3 else \
                [x for x in (stems if isinstance(stems, list) else []) if x in sd["stems"]]
            jv = _journey_vocab(j)
            for st in names:
                sv = _stem_vocab(st)
                if not sv or any(_teq(a_, b_) for a_ in sv for b_ in jv):
                    done.add(st)
                    per_journey.setdefault(j["id"], set()).add((sd["id"], st))
                else:
                    bad.add(st)
                    weak.append({"seed": sd["id"], "stem": st, "journey": j["id"]})
        for r in c.get("rejected") or []:
            if not isinstance(r, dict) or r.get("category") not in REJECT_CATS:
                continue
            reason = str(r.get("reason") or "").strip()
            rstems = [x for x in (r.get("stems") or []) if x in sd["stems"]]
            if len(reason) < 8:
                continue
            if ADMISSION.search(reason):
                bad_rej.append({"seed": sd["id"], "stems": rstems, "why": "reason admits it was not read"})
                continue
            if r["category"] == "not_consequence_bearing":
                good = [e for e in (check_evidence(e, root, cache) for e in norm_evidence(r.get("evidence")))
                        if e["status"] in ("verified", "file_only")]
                if not good:
                    continue
                cite = set()
                for e in good:
                    cite.update(sig.norm_tokens(e.get("file") or "") + sig.norm_tokens(e.get("symbol") or ""))
                cite = {t for t in cite if stem(t) not in GENERIC_STEMS}
                want = set().union(*[_stem_vocab(x) for x in rstems]) if rstems else set()
                if want and not any(_teq(a_, b_) for a_ in want for b_ in cite):
                    bad_rej.append({"seed": sd["id"], "stems": rstems,
                                    "why": "citation shares no vocabulary with the rejected stems"})
                    continue
            done.update(rstems)
        lacking = [x for x in sd["stems"] if x not in done]
        total += len(sd["stems"])
        ok += len(sd["stems"]) - len(lacking)
        if lacking:
            missing.append({"seed": sd["id"], "dir": sd["dir"], "stems": lacking})
    over = [{"journey": jid, "stems": len(v)} for jid, v in sorted(per_journey.items())
            if len(v) > MAX_STEMS_PER_JOURNEY]
    return {"missing": missing, "weak": weak, "total": total, "ok": ok,
            "bad_rejections": bad_rej, "overloaded": over}


def validate(doc: dict, root: Path, signals: dict | None) -> tuple:
    """-> (validated journeys list, report dict). Drops ungrounded work, computes
    confidence, preserves the model's order within a confidence band."""
    cache: dict = {}
    kept, dropped, ids = [], [], set()
    stats = {"evidence_total": 0, "evidence_ok": 0, "by_status": {}}
    for rank, raw in enumerate(doc.get("journeys") or []):
        if not isinstance(raw, dict) or not raw.get("name"):
            dropped.append({"id": None, "reason": "no name"})
            continue
        jid = snake(raw.get("id") or raw["name"])
        base_id, n = jid, 2
        while jid in ids:
            jid, n = f"{base_id}_{n}", n + 1
        steps = []
        for si, rs in enumerate(raw.get("steps") or []):
            if not isinstance(rs, dict):
                continue
            evs = [check_evidence(e, root, cache) for e in norm_evidence(rs.get("evidence"))]
            for e in evs:
                stats["evidence_total"] += 1
                stats["by_status"][e["status"]] = stats["by_status"].get(e["status"], 0) + 1
                if e["status"] in ("verified", "file_only"):
                    stats["evidence_ok"] += 1
            steps.append({"id": snake(rs.get("id") or f"step_{si + 1}"),
                          "description": str(rs.get("description") or ""),
                          "surface": rs.get("surface"), "evidence": evs,
                          "grounded": any(e["status"] == "verified" for e in evs),
                          "valid": any(e["status"] in ("verified", "file_only") for e in evs)})
        good = [s for s in steps if s["valid"]]
        if len(good) < 2:
            dropped.append({"id": jid, "reason": f"fewer than 2 steps with verifiable evidence "
                                                 f"({len(good)} of {len(steps)})"})
            continue
        ids.add(jid)
        j = {"id": jid, "name": str(raw["name"]), "why": raw.get("why_it_matters", ""),
             "steps": good, "dropped_steps": len(steps) - len(good), "model_rank": rank,
             "correlation_key": raw.get("correlation_key"), "outcome": raw.get("outcome"),
             "magnitude": raw.get("magnitude") or [], "actor_segment": raw.get("actor_segment") or [],
             "open_questions": list(raw.get("open_questions") or [])}
        j["corroboration"] = corroboration(j, signals or {})
        ratio = sum(1 for s in good if s["grounded"]) / len(good)
        j["grounded_ratio"] = round(ratio, 2)
        if j["corroboration"] and ratio >= 0.6:
            j["confidence"] = "high"
        elif ratio >= 0.5 or j["corroboration"]:
            j["confidence"] = "medium"
        else:
            j["confidence"] = "low"
        kept.append(j)
    order = {"high": 0, "medium": 1, "low": 2}
    kept.sort(key=lambda j: (order[j["confidence"]], j["model_rank"]))
    acct = account(doc, signals, kept, root, cache)
    return kept, {"dropped": dropped, "accounting": acct, **stats}


def to_candidates(journeys: list, repo_name: str) -> dict:
    out = []
    seen_corr: dict = {}
    for j in journeys:
        steps = []
        for s in j["steps"]:
            cites = "; ".join(
                f"{e['file']}" + (f":{e['line']}" if e.get("line") else "") + (f" {e['symbol']}" if e.get("symbol") else "")
                for e in s["evidence"] if e["status"] in ("verified", "file_only"))
            ev = (s["description"] + " — " if s["description"] else "") + cites
            step = {"id": s["id"], "evidence": ev}
            if s.get("surface"):
                step["surface"] = str(s["surface"])
            steps.append(step)
        nc = [f"business_impact is unset — a human must assign it. Nothing in source says which flow earns revenue."]
        nc.append(f"Step order and boundaries were proposed by reading code, not observed at runtime — confirm.")
        nc += [f"Open question from explorer: {q}" for q in j["open_questions"]]
        if j["dropped_steps"]:
            nc.append(f"{j['dropped_steps']} proposed step(s) were dropped for unverifiable evidence.")
        c = {"id": j["id"], "name": j["name"], "source": "discovered:code",
             "confidence": j["confidence"],
             "notes": f"Explored from {repo_name}. {j['why']} Corroborated by: "
                      f"{', '.join(j['corroboration']) or 'nothing independent (code only)'}. "
                      f"Grounded steps: {int(j['grounded_ratio'] * 100)}%.",
             "steps": steps, "needs_clarification": nc}
        ck = j.get("correlation_key")
        if isinstance(ck, dict) and ck.get("attribute"):
            attr = str(ck["attribute"])
            # `ce intake` merges candidates that share a correlation key (same case
            # notion = same journey). Two journeys keyed on a shared entity id such as
            # `organization.slug` were silently merged into one, hiding a journey. A key
            # must identify ONE journey's cases, so a collision is namespaced.
            if attr in seen_corr and seen_corr[attr] != j["id"]:
                attr = f"{j['id']}.{attr.split('.')[-1]}"
                c["needs_clarification"].append(
                    f"Proposed correlation key collided with another journey's; renamed to `{attr}`. "
                    "Confirm what identifies one case of THIS journey.")
            seen_corr[str(ck["attribute"])] = seen_corr.get(str(ck["attribute"]), j["id"])
            c["correlation_key"] = {"attribute": attr}
        oc = j.get("outcome")
        if isinstance(oc, dict) and oc.get("attribute") and isinstance(oc.get("values"), list) \
                and len(oc["values"]) >= 2:
            c["outcome"] = {"attribute": str(oc["attribute"]), "values": [snake(v) for v in oc["values"]]}
            c["needs_clarification"].append(
                "Outcome values were proposed by the explorer — confirm them and set success_values.")
        mags = [{"attribute": str(m["attribute"]), "type": m.get("type") if m.get("type") in ("int", "double") else "double"}
                for m in j["magnitude"] if isinstance(m, dict) and m.get("attribute")]
        if mags:
            c["magnitude"] = mags
        segs = [{"attribute": str(a["attribute"])} for a in j["actor_segment"]
                if isinstance(a, dict) and a.get("attribute")]
        if segs:
            c["actor_segment"] = segs
        out.append(c)
    return {"version": 1, "journeys": out}


def render_report(journeys: list, report: dict, repo_name: str, mode: str) -> str:
    L = [f"# Journey exploration — {repo_name}", "",
         f"Explorer: {mode}. Evidence verified against the repo: "
         f"{report['evidence_ok']} of {report['evidence_total']} citations "
         f"({', '.join(f'{k} {v}' for k, v in sorted(report['by_status'].items())) or 'none'}).", "",
         "Confidence is computed (grounded steps + independent e2e/analytics/docs corroboration), "
         "never taken from the model. **business_impact is unset on every journey.**", "",
         "| # | Journey | Confidence | Steps | Corroborated by |", "|---|---|---|---|---|"]
    for i, j in enumerate(journeys, 1):
        L.append(f"| {i} | {j['name']} (`{j['id']}`) | {j['confidence']} | {len(j['steps'])} | "
                 f"{', '.join(j['corroboration']) or 'code only'} |")
    for j in journeys:
        L += ["", f"## {j['name']}", j["why"] or "", ""]
        for k, s in enumerate(j["steps"], 1):
            cite = ", ".join(f"`{e['file']}" + (f":{e['line']}" if e.get('line') else "") + "`"
                             for e in s["evidence"] if e["status"] in ("verified", "file_only"))
            L.append(f"{k}. **{s['id']}** — {s['description']} ({cite})")
        for q in j["open_questions"]:
            L.append(f"- ❓ {q}")
    acct = report.get("accounting") or {}
    if acct.get("total"):
        L += ["", f"## E2E coverage: {acct['ok']} of {acct['total']} test names accounted for", ""]
        if acct["missing"]:
            L += ["Mapped to no surviving journey and not rejected with a valid category — "
                  "treat as coverage gaps:", ""]
            L += [f"- {m['seed']} `{m['dir']}`: {', '.join(m['stems'])}" for m in acct["missing"]]
        if acct["weak"]:
            L += ["", "Mapped to a journey that shares no vocabulary with them:", ""]
            L += [f"- {w['seed']} `{w['stem']}` → `{w['journey']}`" for w in acct["weak"]]
        if acct.get("bad_rejections"):
            L += ["", "Rejections that did not hold (reason admits it was not read, or citation unrelated):", ""]
            L += [f"- {b['seed']} {', '.join(b['stems'][:8])}: {b['why']}" for b in acct["bad_rejections"]]
        if acct.get("overloaded"):
            L += ["", f"Journeys covering more than {MAX_STEMS_PER_JOURNEY} test names (likely several journeys in one):", ""]
            L += [f"- `{o['journey']}`: {o['stems']} stems" for o in acct["overloaded"]]
    if report["dropped"]:
        L += ["", "## Dropped", ""]
        L += [f"- `{d['id']}`: {d['reason']}" for d in report["dropped"]]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# the agent loop (OpenRouter, OpenAI-compatible tool calling)
# --------------------------------------------------------------------------

class RepoTools:
    """Read-only, repo-confined. Secrets and vendored trees are invisible."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _safe(self, rel: str):
        p = (self.root / (rel or ".")).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            return None
        return p

    def list_dir(self, path: str = ".") -> str:
        p = self._safe(path)
        if p is None or not p.is_dir():
            return f"error: not a directory inside the repo: {path}"
        rows = []
        for c in sorted(p.iterdir()):
            if c.name in sig.SKIP_DIRS or sig.is_secret(c) or c.is_symlink():
                continue
            rows.append(c.name + ("/" if c.is_dir() else ""))
        return "\n".join(rows[:300]) + ("\n…(truncated)" if len(rows) > 300 else "")

    def read_file(self, path: str, start: int = 1, end: int = 0) -> str:
        p = self._safe(path)
        if p is None or not p.is_file() or sig.is_secret(p):
            return f"error: cannot read {path}"
        lines = sig.read(p).splitlines()
        start = max(int(start or 1), 1)
        end = int(end) if end else min(start + MAX_FILE_LINES - 1, len(lines))
        end = min(end, start + MAX_FILE_LINES - 1, len(lines))
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} lines {start}-{end} of {len(lines)}\n{body}"

    def grep(self, pattern: str, path: str = ".") -> str:
        p = self._safe(path)
        if p is None:
            return f"error: path outside repo: {path}"
        cmd = ["grep", "-rInE", "--max-count=6", "--binary-files=without-match"]
        for d in sorted(sig.SKIP_DIRS):
            cmd.append(f"--exclude-dir={d}")
        for s in (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "credentials.*"):
            cmd.append(f"--exclude={s}")
        cmd += ["-e", pattern, str(p)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        except subprocess.TimeoutExpired:
            return "error: grep timed out; narrow the path"
        out = r.stdout.replace(str(self.root) + "/", "")
        lines = out.splitlines()
        return "\n".join(l[:240] for l in lines[:60]) + (f"\n…({len(lines) - 60} more)" if len(lines) > 60 else "") \
            or "(no matches)"


TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List a directory in the repo (repo-relative path).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read lines of a file (max ~220 lines per call).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep", "description": "Extended-regex search under a path; returns file:line:text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "submit_discovery", "description": "Submit the final journeys JSON. Call once, last.",
        "parameters": {"type": "object", "properties": {"discovery": {"type": "object"}},
                       "required": ["discovery"]}}},
]


def _http_error_hint(code: int, body: str) -> str:
    if code in (400, 404) and "model" in body.lower():
        return (f"OpenRouter rejected the model id ({code}). Pick a tool-calling model from "
                f"https://openrouter.ai/models and pass --model or set CE_EXPLORER_MODEL. {body[:200]}")
    if code in (401, 403):
        return "OpenRouter rejected the key. Check OPENROUTER_API_KEY."
    return f"OpenRouter error {code}: {body[:300]}"


def prepare_messages(messages: list, model: str) -> list:
    """Wire format. For Anthropic models the static prefix (system + task + brief, the
    biggest and never-changing part) is marked cacheable; other models get plain messages."""
    if not model.startswith("anthropic/") or len(messages) < 2:
        return messages
    first = messages[1]
    if not isinstance(first.get("content"), str):
        return messages
    out = list(messages)
    out[1] = dict(first, content=[{"type": "text", "text": first["content"],
                                   "cache_control": {"type": "ephemeral"}}])
    return out


def chat_openrouter(messages: list, model: str, key: str, tools: list) -> dict:
    payload = json.dumps({"model": model, "messages": prepare_messages(messages, model), "tools": tools,
                          "temperature": 0.2}).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Title": "ce explore"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt * 2)
                last = e
                continue
            raise RuntimeError(_http_error_hint(e.code, body))
        except urllib.error.URLError as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"could not reach OpenRouter: {last}")


MAX_GATE_ROUNDS = 2
KEEP_FULL_RESULTS = 10   # most recent tool results kept verbatim
ELIDE_BATCH = 6          # elide in batches so the history prefix stays stable between batches


def elide_old_results(messages: list, labels: dict) -> int:
    """Replace old tool results with a one-line stub (the tool_call pairing stays valid).
    Each request resends the whole conversation, so input tokens grow quadratically with
    turns; live run 3 spent 2.7M input tokens on MetaMask. Returns how many were elided."""
    live = [i for i, m in enumerate(messages)
            if m.get("role") == "tool" and m.get("tool_call_id") in labels
            and not str(m.get("content", "")).startswith("[elided")]
    if len(live) <= KEEP_FULL_RESULTS + ELIDE_BATCH:
        return 0
    old = live[:-KEEP_FULL_RESULTS]
    for i in old:
        m = messages[i]
        n = len(str(m["content"]))
        messages[i] = dict(m, content=f"[elided: {labels[m['tool_call_id']]}, {n} chars. "
                                      f"Re-read it only if you need the exact text.]")
    return len(old)


def run_agent(root: Path, brief: str, model: str, key: str | None, max_journeys: int,
              chat=None, turns: int = MAX_TURNS, log=print, signals: dict | None = None) -> dict:
    """Tool loop. `chat` is injectable so the loop is testable offline.

    A submission is accepted only when every E2E seed in the brief is accounted for
    (mapped to a journey it submitted, or rejected with a reason). That check is mechanical;
    the model gets at most MAX_GATE_ROUNDS bounces, then whatever it has is taken as is."""
    raw_chat = chat or (lambda msgs: chat_openrouter(msgs, model, key, TOOL_DEFS))
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}

    def chat(msgs):
        r = raw_chat(msgs)
        u = r.get("usage") or {}
        usage["requests"] += 1
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["cached"] = usage.get("cached", 0) + int(
            (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return r

    def done(doc):
        log(f"  tokens: {usage['prompt_tokens']} in / {usage['completion_tokens']} out "
            f"over {usage['requests']} request(s)"
            + (f" ({usage['cached']} cached)" if usage.get("cached") else ""))
        return doc

    gate = {"rounds": 0}
    labels: dict = {}

    def bounce(doc):
        """-> message to send back, or None to accept. Judged on the VALIDATED journeys, so
        a mapping to a journey the validator drops does not count."""
        if gate["rounds"] >= MAX_GATE_ROUNDS or not isinstance(doc, dict):
            return None
        _, rep = validate(doc, root, signals)
        acct = rep["accounting"]
        if not (acct["missing"] or acct["weak"] or acct["bad_rejections"] or acct["overloaded"]):
            return None
        gate["rounds"] += 1
        n = sum(len(m["stems"]) for m in acct["missing"])
        log(f"  gate: {n} stem(s) missing, {len(acct['weak'])} weak mapping(s), "
            f"{len(acct['bad_rejections'])} bad rejection(s), {len(acct['overloaded'])} overloaded journey(s) "
            f"(round {gate['rounds']})")
        parts = []
        if acct["missing"]:
            parts.append("Stems with no valid coverage: " + "; ".join(
                f"{m['seed']} `{m['dir']}`: {', '.join(m['stems'][:20])}" for m in acct["missing"][:12]))
        if acct["weak"]:
            parts.append("Mapped to a journey that shares no vocabulary with them: " + "; ".join(
                f"{w['seed']} {w['stem']}→{w['journey']}" for w in acct["weak"][:15]))
        if acct["bad_rejections"]:
            parts.append("Rejections that did not hold: " + "; ".join(
                f"{b['seed']} {', '.join(b['stems'][:6])} ({b['why']})" for b in acct["bad_rejections"][:10]))
        if acct["overloaded"]:
            parts.append(f"Journeys covering more than {MAX_STEMS_PER_JOURNEY} stems, split them: " + "; ".join(
                f"{o['journey']} ({o['stems']})" for o in acct["overloaded"]))
        if rep["dropped"]:
            parts.append("Journeys dropped for unverifiable citations: " + "; ".join(
                f"{d['id']} ({d['reason']})" for d in rep["dropped"][:6]))
        return ("Not accepted yet. " + " | ".join(parts) + ". Read the code behind them and add the "
                "journeys they represent (with real citations), map each stem to the journey it belongs "
                "to, or reject with a valid category (`not_consequence_bearing` needs an evidence "
                "citation). Then call submit_discovery again with the complete JSON.")

    tools = RepoTools(root)
    task = build_task(root.name, brief, max_journeys, agent=True, turns=turns)
    messages = [
        {"role": "system", "content": "You are a careful code-reading agent. You only cite files "
                                      "and symbols you have read. You never write or modify code."},
        {"role": "user", "content": task},
    ]
    for turn in range(turns):
        elide_old_results(messages, labels)
        resp = chat(messages)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} or
                        {"role": "assistant", "content": ""})
        calls = msg.get("tool_calls") or []
        if not calls:
            m = re.search(r"\{.*\}", msg.get("content") or "", re.S)
            if m:
                try:
                    doc = json.loads(m.group(0))
                    back = bounce(doc)
                    if back is None:
                        return done(doc)
                    messages.append({"role": "user", "content": back})
                    continue
                except ValueError:
                    pass
            messages.append({"role": "user", "content":
                             "Continue exploring or call submit_discovery with the final JSON."})
            continue
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if fn == "submit_discovery":
                d = args.get("discovery")
                doc = d if isinstance(d, dict) else {"journeys": []}
                back = bounce(doc)
                if back is None:
                    return done(doc)
                messages.append({"role": "tool", "tool_call_id": c.get("id", fn), "content": back})
                continue
            if fn in ("list_dir", "read_file", "grep"):
                try:
                    out = getattr(tools, fn)(**{k: v for k, v in args.items()
                                                if k in ("path", "start", "end", "pattern")})
                except TypeError as e:
                    out = f"error: bad arguments: {e}"
            else:
                out = f"error: unknown tool {fn}"
            log(f"  [{turn + 1}] {fn} {json.dumps(args)[:90]}")
            labels[c.get("id", fn)] = f"{fn} {json.dumps(args)[:70]}"
            messages.append({"role": "tool", "tool_call_id": c.get("id", fn), "content": out[:MAX_TOOL_CHARS]})
    messages.append({"role": "user", "content": "Out of budget. Call submit_discovery now with what you have."})
    resp = chat(messages)
    for c in ((resp.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []:
        if c["function"]["name"] == "submit_discovery":
            try:
                return done(json.loads(c["function"]["arguments"]).get("discovery") or {"journeys": []})
            except ValueError:
                pass
    return done({"journeys": []})


def build_task(repo_name: str, brief: str, max_journeys: int, agent: bool = False,
               turns: int = MAX_TURNS, out: str = "ce-work", repo_arg: str = ".") -> str:
    delivery = (DELIVERY_AGENT.format(turns=turns) if agent
                else DELIVERY_EMIT.format(out=out, repo_arg=repo_arg))
    return TASK_TEMPLATE.format(repo=repo_name, max_journeys=max_journeys, delivery=delivery, brief=brief,
                                max_stems=MAX_STEMS_PER_JOURNEY)


# --------------------------------------------------------------------------

def ingest(discovery: dict, root: Path, signals: dict | None, out: Path, mode: str) -> int:
    journeys, report = validate(discovery, root, signals)
    out.mkdir(parents=True, exist_ok=True)
    (out / "discovery.validated.json").write_text(json.dumps({"journeys": journeys, "report": report}, indent=2))
    (out / "journeys.yaml").write_text(yaml.safe_dump(to_candidates(journeys, root.name), sort_keys=False, width=100))
    (out / "discovery-report.md").write_text(render_report(journeys, report, root.name, mode))
    print(f"kept {len(journeys)} journey(s), dropped {len(report['dropped'])}; "
          f"evidence verified {report['evidence_ok']}/{report['evidence_total']} → {out / 'journeys.yaml'}")
    for j in journeys:
        print(f"  {j['confidence']:<6} {j['id']:<28} {len(j['steps'])} steps · "
              f"{','.join(j['corroboration']) or 'code only'}")
    print("business_impact is unset on every journey — assign it before `ce intake`.")
    return 0 if journeys else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default="ce-work", help="work directory (gitignored)")
    ap.add_argument("--mode", choices=["task", "openrouter"], default="task",
                    help="task: emit a task for your own agent. openrouter: run the exploration here.")
    ap.add_argument("--ingest", metavar="DISCOVERY_JSON",
                    help="validate a discovery.json and write journeys.yaml")
    ap.add_argument("--model", default=os.environ.get("CE_EXPLORER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--max-journeys", type=int, default=30)
    ap.add_argument("--turns", type=int, default=MAX_TURNS)
    args = ap.parse_args(argv)

    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    sig_path = out / "signals.json"
    if args.ingest:
        signals = json.loads(sig_path.read_text()) if sig_path.exists() else sig.collect(root)
        doc = json.loads(Path(args.ingest).read_text())
        return ingest(doc, root, signals, out, "agent-submitted discovery")

    signals = sig.collect(root)
    sig_path.write_text(json.dumps(signals, indent=2))
    brief = sig.render_brief(signals)
    (out / "signals.md").write_text(brief)

    if args.mode == "task":
        task = build_task(root.name, brief, args.max_journeys, agent=False, out=str(out),
                          repo_arg=str(args.repo))
        (out / "discovery-task.md").write_text(task)
        print(f"wrote {out / 'discovery-task.md'}\n"
              f"Give it to your coding agent (it reads this repo and writes {out / 'discovery.json'}), then:\n"
              f"  ce explore --repo {args.repo} --ingest {out / 'discovery.json'} --out {out}")
        return 0

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("error: set OPENROUTER_API_KEY, or use --mode task to hand the task to your own agent.",
              file=sys.stderr)
        return 2
    print(f"exploring {root.name} with {args.model} …")
    try:
        doc = run_agent(root, brief, args.model, key, args.max_journeys, turns=args.turns,
                        signals=signals)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    (out / "discovery.json").write_text(json.dumps(doc, indent=2))
    return ingest(doc, root, signals, out, f"openrouter · {args.model}")


if __name__ == "__main__":
    sys.exit(main())
