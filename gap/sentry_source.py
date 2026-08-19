#!/usr/bin/env python3
"""
Builds an `observed.json` for the gap analyzer from a customer's Sentry org.

Two data needs, two very different levels of API support. This is the single
most important thing to understand before relying on this file.

  1. ATTRIBUTE PRESENCE — public, documented, stable.

         GET /api/0/organizations/{org}/trace-items/attributes/
             ?dataset=spans&statsPeriod=30d[&substringMatch=][&attributeType=]

     Scopes: org:read (or org:admin / org:write). Returns per attribute `key`,
     `name`, `attributeType` (array|boolean|number|string) and — the part that
     matters most — `attributeSource.source_type`, which is `sentry` for
     SDK-provided attributes and `user` for customer-defined ones. That single
     field separates "what their SDK gives them" from "what they instrumented
     themselves", which is the whole gap analysis.
     `itemType` is a deprecated alias for `dataset`; use `dataset`.
     Docs: https://docs.sentry.io/api/discover/list-trace-item-attributes/

  2. SPAN NAMES AND COUNTS — NOT publicly documented.

     The Discover & Performance API section contains no span/event query
     endpoint. `/api/0/organizations/{org}/events/?dataset=spans` is what Trace
     Explorer and the Sentry MCP actually call, but its absence from the public
     reference makes it an unstable contract that can change without notice.

     `fetch_span_names()` below implements it anyway, because it works and the
     analyzer needs it — but it is fenced off, off by default, and prints a
     warning. The supported path is `--from-mcp`: run the Sentry MCP
     `search_events` tool (dataset='spans', fields=['span.description',
     'count()'], sort='-count()'), paste the JSON, and this module converts it.

Offline fixtures exist so the analyzer is fully testable with no credentials.

Usage:
    # Supported: attributes live, span names from an MCP search_events result
    ./sentry_source.py --org acme --token $SENTRY_TOKEN \\
        --from-mcp mcp-spans.json --out observed.json

    # Attributes only (no span names -> every span rule will report missing)
    ./sentry_source.py --org acme --token $SENTRY_TOKEN --out observed.json

    # Undocumented endpoint, opt-in and at your own risk
    ./sentry_source.py --org acme --token $SENTRY_TOKEN \\
        --unsafe-span-query --out observed.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_HOST = "https://sentry.io"
ATTRIBUTES_PATH = "/api/0/organizations/{org}/trace-items/attributes/"
# Undocumented. See module docstring.
UNSAFE_EVENTS_PATH = "/api/0/organizations/{org}/events/"


def _get(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:400]
        sys.exit(f"error: HTTP {exc.code} for {url}\n{body}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: cannot reach {url}: {exc.reason}")


def list_projects(org: str, token: str, *, host: str = DEFAULT_HOST) -> list[dict]:
    """Documented endpoint, for scoping a run to specific projects."""
    url = host + f"/api/0/organizations/{org}/projects/"
    data = _get(url, token)
    return [{"id": str(p.get("id")), "slug": p.get("slug"), "platform": p.get("platform")}
            for p in (data if isinstance(data, list) else [])]


def fetch_attributes(
    org: str, token: str, *, host: str = DEFAULT_HOST,
    stats_period: str = "30d", dataset: str = "spans",
    substring: str | None = None, project_ids: list[str] | None = None,
) -> list[dict]:
    """Public, documented endpoint. Safe to depend on."""
    params: list[tuple[str, str]] = [("dataset", dataset), ("statsPeriod", stats_period)]
    if substring:
        params.append(("substringMatch", substring))
    # Sentry scopes org endpoints with repeated numeric `project` params.
    for pid in project_ids or []:
        params.append(("project", pid))
    url = host + ATTRIBUTES_PATH.format(org=org) + "?" + urllib.parse.urlencode(params)
    data = _get(url, token)
    if not isinstance(data, list):
        sys.exit(f"error: unexpected attributes response shape: {type(data).__name__}")
    return [
        {
            "key": a.get("key"),
            "name": a.get("name"),
            "attributeType": a.get("attributeType"),
            "attributeSource": a.get("attributeSource") or {},
        }
        for a in data
        if a.get("key")
    ]


def fetch_span_names(
    org: str, token: str, *, host: str = DEFAULT_HOST,
    stats_period: str = "30d", limit: int = 500,
) -> list[dict]:
    """UNDOCUMENTED endpoint. Opt-in only.

    Prefer `from_mcp_search_events()`. If this stops working, that is expected —
    it is not in the public API reference and carries no stability guarantee.
    """
    print(
        "warning: querying an undocumented endpoint "
        "(/organizations/{org}/events/?dataset=spans). No stability guarantee. "
        "Prefer --from-mcp.",
        file=sys.stderr,
    )
    params = [
        ("dataset", "spans"),
        ("field", "span.description"),
        ("field", "count()"),
        ("sort", "-count()"),
        ("statsPeriod", stats_period),
        ("per_page", str(limit)),
    ]
    url = host + UNSAFE_EVENTS_PATH.format(org=org) + "?" + urllib.parse.urlencode(params)
    data = _get(url, token)
    return _rows_to_span_names(data.get("data") or [])


def _rows_to_span_names(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        name = r.get("span.description") or r.get("span.name") or r.get("description")
        if not name:
            continue
        out.append({"name": name, "count": int(r.get("count()") or r.get("count") or 0)})
    return out


def _rows_to_span_ops(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if "span.op" not in r:
            continue
        out.append({"op": r.get("span.op") or "",
                    "count": int(r.get("count()") or r.get("count") or 0)})
    return out


def from_mcp_search_events(path: Path) -> tuple[list[dict], list[dict]]:
    """Convert Sentry MCP `search_events` results into (span_names, span_ops).

    Accepts the aggregate JSON array the tool returns, or an object wrapping it
    under `data` / `results`, or a list of several such results concatenated.
    Run both queries and concatenate them into one file:

        # names — what the journey steps are matched against
        search_events(organizationSlug='acme', dataset='spans',
                      fields=['span.description','count()'],
                      sort='-count()', period='30d', limit=100)

        # ops — what the automatic-vs-custom classification is built on
        search_events(organizationSlug='acme', dataset='spans',
                      fields=['span.op','count()'],
                      sort='-count()', period='30d', limit=50)
    """
    doc = json.loads(path.read_text())
    if isinstance(doc, list):
        rows = doc
    else:
        rows = doc.get("data") or doc.get("results") or []
        for key in ("span_names", "span_ops", "names", "ops"):
            extra = doc.get(key)
            if isinstance(extra, list):
                rows = rows + extra
    names, ops = _rows_to_span_names(rows), _rows_to_span_ops(rows)
    if not names and not ops:
        sys.exit(f"error: nothing parsed from {path}. Expected rows with "
                 "'span.description' and/or 'span.op', plus 'count()'.")
    return names, ops


def build(
    org: str,
    attributes: list[dict],
    span_names: list[dict],
    *,
    span_ops: list[dict] | None = None,
    projects: list[str] | None = None,
    stats_period: str = "30d",
    traces_sample_rate: float | None = None,
    example_traces: dict[str, str] | None = None,
) -> dict:
    return {
        "org": org,
        "projects": projects or [],
        "dataset": "spans",
        "stats_period": stats_period,
        "traces_sample_rate": traces_sample_rate,
        "span_names": span_names,
        "span_ops": span_ops or [],
        "attributes": attributes,
        "example_traces": example_traces or {},
        "_provenance": {
            "attributes": "GET /api/0/organizations/{org}/trace-items/attributes/ (public, documented)",
            "span_names": "MCP search_events or undocumented /events/?dataset=spans",
            "span_ops": "MCP search_events fields=['span.op','count()']",
        },
    }


def merge_scan_into_live(scan: dict, live: dict) -> dict:
    """Live snapshot has attributes and span names; the scan has SDK presence
    and source_languages. Spec generation needs both. Overlay, do not replace
    provenance — a merged file must still say which half is telemetry."""
    out = dict(live)
    if scan.get("sdk"):
        out["sdk"] = scan["sdk"]
    if scan.get("source_languages"):
        out["source_languages"] = scan["source_languages"]
    prov = dict(live.get("_provenance") or {})
    scan_prov = scan.get("_provenance") or {}
    if scan_prov:
        prov["sdk"] = scan_prov.get("source") or "static code scan"
        prov["source_languages"] = scan_prov.get("source_languages")
        prov["merged"] = "scan (sdk + languages) + live Sentry (spans + attributes)"
    out["_provenance"] = prov
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Snapshot a Sentry org into observed.json.")
    p.add_argument("--org", required=True)
    p.add_argument("--token", help="Auth token with org:read. Or set SENTRY_AUTH_TOKEN.")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="Region URL, e.g. https://us.sentry.io or https://de.sentry.io")
    p.add_argument("--stats-period", default="30d",
                   help="Plan-gated: Developer 7d, Team 14d, Business 30d.")
    p.add_argument("--traces-sample-rate", type=float,
                   help="Record it. Below 5%% every finding degrades to low confidence.")
    p.add_argument("--substring", help="Narrow attributes to keys containing this substring.")
    p.add_argument("--from-mcp", type=Path,
                   help="JSON from the Sentry MCP search_events tool (preferred).")
    p.add_argument("--unsafe-span-query", action="store_true",
                   help="Query the undocumented span endpoint directly.")
    p.add_argument("--project", action="append", default=[], metavar="ID_OR_SLUG",
                   help="Scope to a project (repeatable). Slugs are resolved to ids.")
    p.add_argument("--list-projects", action="store_true",
                   help="Print the org's projects and exit. Use this to choose a scope.")
    p.add_argument("--out", type=Path, help="Required unless --list-projects.")
    args = p.parse_args(argv)

    import os
    token = args.token or os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        print("error: pass --token or set SENTRY_AUTH_TOKEN", file=sys.stderr)
        return 1

    if args.list_projects:
        for pr in list_projects(args.org, token, host=args.host):
            print(f"{pr['id']}\t{pr['slug']}\t{pr.get('platform') or ''}")
        return 0

    if not args.out:
        print("error: --out is required", file=sys.stderr)
        return 1

    project_ids: list[str] = []
    if args.project:
        known = {pr["slug"]: pr["id"] for pr in list_projects(args.org, token, host=args.host)}
        for want in args.project:
            if want.isdigit():
                project_ids.append(want)
            elif want in known:
                project_ids.append(known[want])
            else:
                print(f"error: no project '{want}' in {args.org}. "
                      f"Known: {', '.join(sorted(known))}", file=sys.stderr)
                return 1

    attributes = fetch_attributes(
        args.org, token, host=args.host, stats_period=args.stats_period,
        substring=args.substring, project_ids=project_ids,
    )

    span_ops: list[dict] = []
    if args.from_mcp:
        span_names, span_ops = from_mcp_search_events(args.from_mcp)
    elif args.unsafe_span_query:
        span_names = fetch_span_names(
            args.org, token, host=args.host, stats_period=args.stats_period)
    else:
        span_names = []
        print("warning: no span-name source given, so every span-level rule will "
              "report missing. Pass --from-mcp or --unsafe-span-query.", file=sys.stderr)
    if not span_ops:
        print("note: no span ops present, so the automatic-vs-custom profile will fall "
              "back to name-based classification. Include a "
              "fields=['span.op','count()'] query in --from-mcp for the stronger signal.",
              file=sys.stderr)

    doc = build(
        args.org, attributes, span_names,
        span_ops=span_ops, projects=args.project,
        stats_period=args.stats_period,
        traces_sample_rate=args.traces_sample_rate,
    )
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    user = sum(1 for a in attributes
               if (a.get("attributeSource") or {}).get("source_type") == "user")
    print(f"wrote {args.out}: {len(attributes)} attributes ({user} customer-defined), "
          f"{len(span_names)} span names", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
