#!/usr/bin/env python3
"""
Journey candidate intake resolver.

Merges provenance-tagged journey candidates from any number of sources, computes
the declared/discovered 2x2 status for each, reports which of the seven grammar
roles are filled, and emits a ranked worklist.

Design constraints, from GRAMMAR.md:
  - `declared` outranks anything discovery produced. The customer naming a
    journey has already answered the business-criticality question that no
    inference can answer.
  - Discovery never gates. A declared journey that no scan corroborates is a
    FINDING worth raising, not a validation error.
  - Missing roles are reported, never inferred. Roles 4-7 are human-owned.
  - Ranking never uses volume as a primary signal. Health checks and polling
    dominate traffic; refunds and disputes are rare and expensive.

Usage:
    ./resolve.py --declared candidates/declared.yaml \
                 --discovered candidates/discovered-code.json \
                 --out-md report.md --out-json resolved.json

Exit codes:
    0  resolved (findings and blockers may still be present — those are output,
       not failure; a declared_unconfirmed journey is the point, not an error)
    1  input parse or validation error
    2  no candidates found in any input
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Grammar roles. Structural roles describe the journey's shape and are often
# inferrable. Semantic roles carry business meaning and are human-owned.
# --------------------------------------------------------------------------

STRUCTURAL_ROLES = ["name", "correlation_key", "steps"]
SEMANTIC_ROLES = ["outcome", "failure_reason", "magnitude", "actor_segment"]
ALL_ROLES = STRUCTURAL_ROLES + SEMANTIC_ROLES

ROLE_LABELS = {
    "name": "1 journey",
    "correlation_key": "2 correlation key",
    "steps": "3 step marker",
    "outcome": "4 outcome",
    "failure_reason": "5 failure reason",
    "magnitude": "6 magnitude",
    "actor_segment": "7 actor segment",
}

# Roles that must be present and well-formed before a spec can be generated.
# Deliberately excludes magnitude and actor_segment: a journey is specifiable
# without them, just less useful.
SPEC_REQUIRED_ROLES = ["name", "correlation_key", "steps", "outcome"]

IMPACT_RANK = {"critical": 0, "important": 1, "normal": 2}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

DECLARED = "declared"
DISCOVERED_PREFIX = "discovered:"

# 2x2 statuses
CORROBORATED = "corroborated"
DECLARED_UNCONFIRMED = "declared_unconfirmed"
PROPOSED = "proposed"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit(
            "error: PyYAML is required to read .yaml inputs.\n"
            "       pip install pyyaml --break-system-packages\n"
            "       (or convert the input to JSON, which needs no dependency)"
        )
    return yaml.safe_load(text)


def load_file(path: Path) -> list[dict]:
    """Read one candidate file. Accepts YAML or JSON, wrapped or bare list."""
    try:
        text = path.read_text()
    except OSError as exc:
        sys.exit(f"error: cannot read {path}: {exc}")

    try:
        data = _load_yaml(text) if path.suffix in (".yaml", ".yml") else json.loads(text)
    except Exception as exc:  # noqa: BLE001 - surface any parse failure verbatim
        sys.exit(f"error: cannot parse {path}: {exc}")

    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "journeys" in data:
        return data["journeys"] or []
    sys.exit(f"error: {path} must be a list, or an object with a 'journeys' key")


def validate(raw: dict, path: Path) -> None:
    """Minimal structural checks. The schema is permissive on purpose — the
    resolver's job is to REPORT incompleteness, not reject it."""
    for required in ("id", "name", "source"):
        if not raw.get(required):
            sys.exit(f"error: {path}: journey missing required field '{required}': {raw!r}")

    src = raw["source"]
    if src != DECLARED and not src.startswith(DISCOVERED_PREFIX):
        sys.exit(
            f"error: {path}: journey '{raw['id']}' has unknown source '{src}'. "
            f"Expected '{DECLARED}' or '{DISCOVERED_PREFIX}<kind>'."
        )

    outcome = raw.get("outcome")
    if isinstance(outcome, dict):
        vals = outcome.get("values") or []
        if len(vals) == 1:
            sys.exit(
                f"error: {path}: journey '{raw['id']}' outcome has a single value. "
                "Outcome must be a multi-value string enum — a binary hides the "
                "distinction between failed, abandoned, and rejected."
            )
        if isinstance(vals, list) and any(isinstance(v, bool) for v in vals):
            sys.exit(
                f"error: {path}: journey '{raw['id']}' outcome uses booleans. "
                "Sentry renders boolean attributes as the strings 'true'/'false'; "
                "use a string enum."
            )


# --------------------------------------------------------------------------
# Matching across sources
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Fold a name to a comparable key. 'Checkout Flow' -> 'checkout flow'."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_keys(raw: dict) -> set[str]:
    """Every token by which this candidate may be matched to another source.

    Conservative: id, normalized name, explicit aliases, and the correlation
    key attribute. We do NOT fuzzy-match on step overlap — a false merge is
    worse than a missed one, because it silently hides a declared journey.
    """
    keys = {f"id:{raw['id']}", f"name:{normalize(raw['name'])}"}
    for alias in raw.get("aliases") or []:
        keys.add(f"name:{normalize(alias)}")
    corr = raw.get("correlation_key") or {}
    if corr.get("attribute"):
        keys.add(f"corr:{corr['attribute']}")
    return keys


@dataclass
class Journey:
    """A resolved journey: one business process, evidence from >=1 source."""

    id: str
    name: str
    contributions: list[dict] = field(default_factory=list)

    # ---- provenance -----------------------------------------------------

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for c in self.contributions:
            if c["source"] not in seen:
                seen.append(c["source"])
        return seen

    @property
    def is_declared(self) -> bool:
        return DECLARED in self.sources

    @property
    def is_discovered(self) -> bool:
        return any(s.startswith(DISCOVERED_PREFIX) for s in self.sources)

    @property
    def status(self) -> str:
        """The 2x2. Note there is no fourth cell: not-declared-and-not-
        discovered cannot arrive here by definition."""
        if self.is_declared and self.is_discovered:
            return CORROBORATED
        if self.is_declared:
            return DECLARED_UNCONFIRMED
        return PROPOSED

    @property
    def confidence(self) -> str:
        """Best confidence any source asserted."""
        ranked = sorted(
            (c.get("confidence", "medium") for c in self.contributions),
            key=lambda c: CONFIDENCE_RANK.get(c, 1),
        )
        return ranked[0] if ranked else "medium"

    @property
    def business_impact(self) -> str | None:
        """Human-assigned only. Prefer a declared assertion over a discovered one."""
        for c in sorted(self.contributions, key=lambda c: 0 if c["source"] == DECLARED else 1):
            if c.get("business_impact"):
                return c["business_impact"]
        return None

    @property
    def observed_volume(self) -> int | None:
        vols = [c["observed_volume"] for c in self.contributions if c.get("observed_volume") is not None]
        return max(vols) if vols else None

    @property
    def owner(self) -> str | None:
        for c in self.contributions:
            if c.get("owner"):
                return c["owner"]
        return None

    @property
    def excluded(self) -> bool:
        """A human marked this candidate as noise.

        Exemptions are first-class here for the same reason every service-
        scorecard vendor converged on them: rules and worklist items a team
        can never action destroy the artifact's credibility. Discovery will
        surface health probes, static-asset loads, and polling loops. The
        answer is a one-line human exclusion, not a cleverer heuristic —
        nothing in telemetry distinguishes a probe from a purchase.
        """
        return any(c.get("excluded") for c in self.contributions)

    @property
    def excluded_reason(self) -> str | None:
        for c in self.contributions:
            if c.get("excluded") and c.get("excluded_reason"):
                return c["excluded_reason"]
        return None

    # ---- roles ----------------------------------------------------------

    def role_value(self, role: str) -> Any:
        """Merged value for a role. Declared contributions win on conflict."""
        ordered = sorted(self.contributions, key=lambda c: 0 if c["source"] == DECLARED else 1)
        if role == "name":
            return self.name
        for c in ordered:
            val = c.get(role)
            if val:
                return val
        return None

    def role_filled(self, role: str) -> bool:
        val = self.role_value(role)
        if not val:
            return False
        # Structural minimums: a one-step journey is not a journey, and an
        # outcome needs at least two states to be a discriminator.
        if role == "steps" and len(val) < 2:
            return False
        if role == "outcome" and len(val.get("values") or []) < 2:
            return False
        return True

    @property
    def filled_roles(self) -> list[str]:
        return [r for r in ALL_ROLES if self.role_filled(r)]

    @property
    def missing_roles(self) -> list[str]:
        return [r for r in ALL_ROLES if not self.role_filled(r)]

    # ---- readiness ------------------------------------------------------

    @property
    def blockers(self) -> list[str]:
        """What stands between this journey and a generated spec."""
        out = [
            f"role {ROLE_LABELS[r]} not defined"
            for r in SPEC_REQUIRED_ROLES
            if not self.role_filled(r)
        ]

        # Conditional: a non-success outcome value demands a coded reason,
        # otherwise every failure collapses into one undiagnosable bucket.
        outcome = self.role_value("outcome")
        if outcome and self.role_filled("outcome"):
            successes = set(outcome.get("success_values") or [])
            non_success = [v for v in outcome["values"] if v not in successes]
            if non_success and not self.role_filled("failure_reason"):
                out.append(
                    f"outcome defines non-success values ({', '.join(sorted(non_success))}) "
                    "but no failure reason attribute"
                )
            if not successes:
                out.append("outcome does not declare which values count as success")
        return out

    @property
    def spec_ready(self) -> bool:
        return not self.blockers

    @property
    def undeclared_steps(self) -> list[str]:
        """Steps a discovery pass found that the declared definition omits.

        `role_value` resolves `steps` to the declared list wholesale, which is
        correct — the customer owns the journey shape. But silently discarding
        discovered steps loses a real finding: *your code has a step you didn't
        mention.* Surfaced rather than merged, so the human decides.
        """
        if not self.is_declared:
            return []
        declared_ids = {
            s["id"]
            for c in self.contributions if c["source"] == DECLARED
            for s in (c.get("steps") or [])
        }
        out: list[str] = []
        for c in self.contributions:
            if c["source"] == DECLARED:
                continue
            for s in c.get("steps") or []:
                if s["id"] not in declared_ids and s["id"] not in out:
                    out.append(s["id"])
        return out

    @property
    def clarifications(self) -> list[str]:
        """Explicit questions plus derived ones for missing semantic roles."""
        out: list[str] = []
        for c in self.contributions:
            for q in c.get("needs_clarification") or []:
                if q not in out:
                    out.append(q)
        for sid in self.undeclared_steps:
            out.append(
                f"discovery found a step `{sid}` that is not in the declared journey "
                "— is it part of this flow?"
            )
        for role in SEMANTIC_ROLES:
            if not self.role_filled(role):
                out.append(f"[{ROLE_LABELS[role]}] not defined — ask the customer")
        if not self.business_impact:
            out.append("[business impact] unassigned — must be set by a human, never derived from volume")
        return out


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve(candidates: list[dict]) -> list[Journey]:
    """Group candidates into journeys by transitive match-key overlap."""
    journeys: list[Journey] = []
    index: dict[str, Journey] = {}

    for raw in candidates:
        keys = match_keys(raw)
        hits: list[Journey] = []
        for k in keys:
            j = index.get(k)
            if j is not None and j not in hits:
                hits.append(j)

        if not hits:
            journey = Journey(id=raw["id"], name=raw["name"])
            journeys.append(journey)
        else:
            # Transitive merge: this candidate bridges previously separate groups.
            journey = hits[0]
            for other in hits[1:]:
                journey.contributions.extend(other.contributions)
                journeys.remove(other)
                for k, v in list(index.items()):
                    if v is other:
                        index[k] = journey

        journey.contributions.append(raw)

        # A declared name is the business name; prefer it over an inferred one.
        if raw["source"] == DECLARED:
            journey.name = raw["name"]
            journey.id = raw["id"]

        for k in keys | match_keys({"id": journey.id, "name": journey.name}):
            index[k] = journey

    return journeys


def rank(journeys: list[Journey]) -> list[Journey]:
    """declared first, then human-assigned impact, then spec-readiness, then
    volume, then name for stability.

    Volume is deliberately gated on business_impact being assigned. Frequency
    only becomes informative *after* a human has said the journey matters;
    before that it is actively misleading. Without this gate a 9.2M-instance
    /healthz probe sorts above a refund flow that runs a few hundred times a
    month — which is the exact failure the ranking rule exists to prevent,
    displaced one level down. Caught by test_volume_does_not_lift_unassigned.
    """
    return sorted(
        journeys,
        key=lambda j: (
            0 if j.is_declared else 1,
            IMPACT_RANK.get(j.business_impact or "", 3),
            0 if j.spec_ready else 1,
            -(j.observed_volume or 0) if j.business_impact else 0,
            normalize(j.name),
        ),
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _roles_cell(j: Journey) -> str:
    return " ".join(
        ("+" if j.role_filled(r) else "-") + ROLE_LABELS[r].split(" ")[0] for r in ALL_ROLES
    )


def render_markdown(journeys: list[Journey]) -> str:
    excluded = [j for j in rank(journeys) if j.excluded]
    ranked = [j for j in rank(journeys) if not j.excluded]
    by_status = {CORROBORATED: [], DECLARED_UNCONFIRMED: [], PROPOSED: []}
    for j in ranked:
        by_status[j.status].append(j)

    ready = [j for j in ranked if j.spec_ready]
    L: list[str] = []
    A = L.append

    A("# Journey intake — resolved candidates\n")
    A(f"**{len(ranked)}** candidate journeys · **{len(ready)}** spec-ready"
      + (f" · {len(excluded)} excluded\n" if excluded else "\n"))
    A("| Status | Count | Meaning |")
    A("| --- | --- | --- |")
    A(f"| corroborated | {len(by_status[CORROBORATED])} | Declared *and* found by discovery — highest confidence |")
    A(f"| declared_unconfirmed | {len(by_status[DECLARED_UNCONFIRMED])} | Customer says it matters, no corroborating evidence. **A finding, not an error** |")
    A(f"| proposed | {len(by_status[PROPOSED])} | Discovery found it, nobody declared it. Propose; do not assume it matters |")
    A("")

    A("## Worklist\n")
    A(
        "Ranked: declared first, then human-assigned impact, then spec-readiness. "
        "Volume tiebreaks *only* once a human has assigned impact — before that it "
        "is inert, because frequency is not importance.\n"
    )
    A("| # | Journey | Status | Impact | Sources | Ready | Roles |")
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for i, j in enumerate(ranked, 1):
        A(
            f"| {i} | {j.name} | {j.status} | {j.business_impact or '—'} | "
            f"{', '.join(j.sources)} | {'yes' if j.spec_ready else 'no'} | `{_roles_cell(j)}` |"
        )
    A("")

    if by_status[DECLARED_UNCONFIRMED]:
        A("## Findings — declared but not corroborated\n")
        A(
            "Raise each of these with the customer. Wording that works: *you told me "
            "this matters and I can find no evidence of it in your code or telemetry.* "
            "Three explanations, all worth knowing: it lives in a service you can't "
            "see, it's aspirational, or it's completely dark today.\n"
        )
        for j in by_status[DECLARED_UNCONFIRMED]:
            A(f"- **{j.name}** ({j.business_impact or 'impact unassigned'})"
              + (f" — owner {j.owner}" if j.owner else ""))
        A("")

    if by_status[PROPOSED]:
        A("## Proposed — discovered, not declared\n")
        A(
            "Discovery's real value: journeys nobody named on the call. Refunds, admin "
            "paths, retry-after-failure, plan downgrade. Each needs a human to confirm "
            "it matters, name it, and assign outcome semantics.\n"
        )
        for j in by_status[PROPOSED]:
            vol = f" · {j.observed_volume:,} observed" if j.observed_volume else ""
            A(f"- **{j.name}** ({', '.join(j.sources)}, confidence {j.confidence}{vol})")
        A("")
        A(
            "> Volume shown for context only. It does not affect ranking for "
            "candidates with no human-assigned business impact — a high-volume "
            "probe must not outrank a low-volume refund flow. Mark noise with "
            "`excluded: true` rather than hoping a heuristic will catch it.\n"
        )

    if excluded:
        A("## Excluded\n")
        A("Human-marked noise. Reported for auditability, dropped from the worklist.\n")
        for j in excluded:
            vol = f" · {j.observed_volume:,} observed" if j.observed_volume else ""
            A(f"- **{j.name}**{vol} — {j.excluded_reason or 'no reason given'}")
        A("")

    A("## Per-journey detail\n")
    for j in ranked:
        A(f"### {j.name}  ·  `{j.id}`\n")
        A(f"- Status: **{j.status}** · sources: {', '.join(j.sources)} · confidence: {j.confidence}")
        A(f"- Business impact: {j.business_impact or '**unassigned**'}"
          + (f" · owner: {j.owner}" if j.owner else ""))
        if j.observed_volume is not None:
            A(f"- Observed volume: {j.observed_volume:,}")
        A("")
        A("| Role | State | Value |")
        A("| --- | --- | --- |")
        for r in ALL_ROLES:
            val = j.role_value(r)
            if r == "steps" and val:
                shown = " → ".join(s["id"] for s in val)
            elif r in ("magnitude", "actor_segment") and val:
                shown = ", ".join(x["attribute"] for x in val)
            elif r == "outcome" and val:
                # Comma-joined, not pipe-joined: a raw `|` breaks the table cell.
                succ = set(val.get("success_values") or [])
                vals = ", ".join(
                    f"**{v}**" if v in succ else v for v in val["values"]
                )
                shown = f"`{val['attribute']}` = {vals}"
            elif r in ("correlation_key", "failure_reason") and val:
                shown = f"`{val['attribute']}`"
            else:
                shown = str(val) if val else "—"
            A(f"| {ROLE_LABELS[r]} | {'filled' if j.role_filled(r) else 'MISSING'} | {shown} |")
        A("")
        if j.blockers:
            A("**Blockers before spec generation**\n")
            for b in j.blockers:
                A(f"- {b}")
            A("")
        if j.clarifications:
            A("**Carry into the spec as `[NEEDS CLARIFICATION]`**\n")
            for q in j.clarifications:
                A(f"- {q}")
            A("")

    A("---\n")
    A(
        "Generated by `intake/resolve.py`. Roles are the seven in `GRAMMAR.md`. "
        "Missing semantic roles (4–7) are expected on discovered journeys — a human "
        "owns those, and guessing them is what produces instrumentation nobody asked for."
    )
    return "\n".join(L) + "\n"


def to_json(journeys: list[Journey]) -> dict:
    active = [j for j in journeys if not j.excluded]
    return {
        "version": 1,
        "summary": {
            "total": len(active),
            "excluded": sum(1 for j in journeys if j.excluded),
            "spec_ready": sum(1 for j in active if j.spec_ready),
            "by_status": {
                s: sum(1 for j in active if j.status == s)
                for s in (CORROBORATED, DECLARED_UNCONFIRMED, PROPOSED)
            },
        },
        "journeys": [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status,
                "excluded": j.excluded,
                "excluded_reason": j.excluded_reason,
                "sources": j.sources,
                "confidence": j.confidence,
                "business_impact": j.business_impact,
                "observed_volume": j.observed_volume,
                "owner": j.owner,
                "spec_ready": j.spec_ready,
                "blockers": j.blockers,
                "undeclared_steps": j.undeclared_steps,
                "filled_roles": j.filled_roles,
                "missing_roles": j.missing_roles,
                "needs_clarification": j.clarifications,
                "roles": {r: j.role_value(r) for r in ALL_ROLES},
            }
            for j in rank(journeys)
        ],
    }


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Resolve provenance-tagged journey candidates into a ranked worklist."
    )
    p.add_argument("--declared", action="append", default=[], metavar="PATH",
                   help="Customer-declared candidates (repeatable). Optional.")
    p.add_argument("--discovered", action="append", default=[], metavar="PATH",
                   help="Discovery output (repeatable). Optional — discovery never gates.")
    p.add_argument("--out-md", metavar="PATH", help="Write the markdown report here.")
    p.add_argument("--out-json", metavar="PATH", help="Write the resolved set here.")
    args = p.parse_args(argv)

    candidates: list[dict] = []
    for path in [*args.declared, *args.discovered]:
        pth = Path(path)
        for raw in load_file(pth):
            validate(raw, pth)
            candidates.append(raw)

    if not candidates:
        print("error: no candidates found. Supply --declared and/or --discovered.", file=sys.stderr)
        return 2

    journeys = resolve(candidates)
    report = render_markdown(journeys)
    payload = to_json(journeys)

    if args.out_md:
        Path(args.out_md).write_text(report)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(payload, indent=2) + "\n")
    if not args.out_md and not args.out_json:
        print(report)

    s = payload["summary"]
    print(
        f"resolved {s['total']} journeys · {s['spec_ready']} spec-ready · "
        f"{s['by_status'][CORROBORATED]} corroborated, "
        f"{s['by_status'][DECLARED_UNCONFIRMED]} declared-unconfirmed, "
        f"{s['by_status'][PROPOSED]} proposed",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
