#!/usr/bin/env python3
"""
Tests for `ce propose` — deriving journey candidates from source.

Five bugs got caught by running this against a realistic repo rather than a
minimal fixture, and each has a named regression test below. They all had the same
shape: output that looked plausible but described the wrong journey.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from propose import (  # noqa: E402
    PY_ENUM,
    TS_UNION,
    build_journeys,
    collect,
    is_secret_path,
    iter_files,
    outcome_key,
    step_rank,
    to_yaml,
)

CLI = HERE.parent / "cli.py"


def repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return d


CRATEWISE = {
    "package.json": '{"dependencies":{"express":"^4","stripe":"^14","pino":"^8"}}',
    "src/checkout/routes.ts": """
        import { Router } from "express";
        export const router = Router();
        router.post("/api/checkout/revalidate", revalidateCart);
        router.post("/api/checkout/shipping", submitShipping);
        router.post("/api/checkout/payment", authorizePayment);
        router.post("/api/checkout/confirm", createOrder);
    """,
    "src/checkout/service.ts": """
        export type CheckoutStatus = 'pending' | 'authorized' | 'declined' | 'rolled_back' | 'completed';
        export async function revalidateCart(id: string) { return { orderTotalMinor: 0 }; }
        export async function authorizePayment(id: string, orgId: string) {}
        export async function createOrder(id: string) {}
    """,
    "src/refunds/service.ts": """
        export type RefundStatus = 'requested' | 'auto_approved' | 'settled' | 'failed';
        export async function requestRefund(orderId: string, planTier: string) { return { refundAmountMinor: 0 }; }
        export async function reviewRefund(refundId: string) {}
        export async function settleRefund(refundId: string) {}
    """,
    "src/onboarding/service.py": """
        from enum import Enum
        class InviteStatus(str, Enum):
            SENT = "sent"
            ACCEPTED = "accepted"
            EXPIRED = "expired"
        def send_invite(org_id, email): ...
        def accept_invite(invite_id): ...
        def complete_profile(user_id): ...
    """,
    "src/utils/format.ts": "export function formatMoney(totalMinor: number) { return totalMinor; }",
}


def journeys_of(files: dict[str, str], max_j: int = 6) -> dict[str, dict]:
    ev = collect(repo(files))
    js, _ = build_journeys(ev, max_j)
    return {j["id"]: j for j in js}


class TestStateMachineDetection(unittest.TestCase):
    def test_export_type_union_matches(self):
        """The first regex had `(?:type|)` and never matched `export type`, so
        every TS state machine was missed — the most valuable thing derivable."""
        m = TS_UNION.search(
            "export type CheckoutStatus = 'pending' | 'authorized' | 'completed';")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "CheckoutStatus")

    def test_bare_type_union_also_matches(self):
        self.assertIsNotNone(TS_UNION.search("type OrderState = 'a' | 'b';"))

    def test_python_enum_matches(self):
        self.assertIsNotNone(PY_ENUM.search(
            'class InviteStatus(str, Enum):\n    SENT = "sent"\n    EXPIRED = "expired"\n'))

    def test_outcome_key_strips_suffix_before_singularising(self):
        """Singularising first turned `checkout_status` into `checkout_statu`, so
        the later `_status` strip found nothing and no journey matched its own
        state machine."""
        self.assertEqual(outcome_key("CheckoutStatus"), "checkout")
        self.assertEqual(outcome_key("RefundStatus"), "refund")
        self.assertEqual(outcome_key("OrderState"), "order")
        self.assertEqual(outcome_key("PaymentOutcome"), "payment")

    def test_outcome_indexed_by_domain_too(self):
        """`InviteStatus` lives in src/onboarding/, so it belongs to the
        onboarding journey. Keying only on the type name missed it."""
        j = journeys_of(CRATEWISE)
        self.assertEqual(j["onboarding"]["outcome"]["values"],
                         ["sent", "accepted", "expired"])

    def test_outcome_values_reach_the_journey(self):
        j = journeys_of(CRATEWISE)
        self.assertIn("rolled_back", j["checkout"]["outcome"]["values"])
        self.assertIn("settled", j["refund"]["outcome"]["values"])

    def test_success_values_are_never_guessed(self):
        """Which outcomes count as success is a human decision."""
        j = journeys_of(CRATEWISE)
        self.assertNotIn("success_values", j["checkout"]["outcome"])


class TestStepOrdering(unittest.TestCase):
    def test_step_rank_matches_whole_tokens_not_substrings(self):
        """`"view" in "review"` is True, which ranked `review_refund` first and
        produced review → request → settle."""
        self.assertLess(step_rank("request_refund"), step_rank("review_refund"))
        self.assertLess(step_rank("review_refund"), step_rank("settle_refund"))

    def test_prefix_matching_still_works(self):
        self.assertLess(step_rank("payment_authorized"), step_rank("confirm_order"))
        self.assertLess(step_rank("validate_cart"), step_rank("capture_payment"))

    def test_unhinted_step_sorts_last_not_first(self):
        self.assertEqual(step_rank("zzz_unknown_thing"), step_rank("also_unknown"))

    def test_routes_keep_declaration_order(self):
        """Hint-sorting routes put `shipping` (no lifecycle verb) after `payment`
        and `confirm` — a visibly wrong journey."""
        j = journeys_of(CRATEWISE)
        self.assertEqual([s["id"] for s in j["checkout"]["steps"]],
                         ["revalidate", "shipping", "payment", "confirm"])

    def test_function_derived_steps_use_lifecycle_hints(self):
        j = journeys_of(CRATEWISE)
        self.assertEqual([s["id"] for s in j["refund"]["steps"]],
                         ["request_refund", "review_refund", "settle_refund"])

    def test_every_step_carries_its_evidence(self):
        for j in journeys_of(CRATEWISE).values():
            for s in j["steps"]:
                self.assertTrue(s.get("evidence"), f"{j['id']}/{s['id']}")


class TestAttributeCandidates(unittest.TestCase):
    def test_minor_units_typed_as_int_with_unit(self):
        """`*_minor` means integer minor units. Calling it a double misdescribes
        the field and invites the stringified-float defect."""
        m = journeys_of(CRATEWISE)["checkout"]["magnitude"][0]
        self.assertEqual(m["type"], "int")
        self.assertEqual(m["unit"], "minor_currency_unit")

    def test_segments_normalised_and_deduped(self):
        """`orgId` and `org_id` are one attribute written twice; emitting both
        makes the proposal look careless."""
        segs = [s["attribute"] for s in journeys_of(CRATEWISE)["checkout"]["actor_segment"]]
        self.assertEqual(len(segs), len(set(segs)))
        self.assertTrue(all("_" not in s for s in segs), segs)

    def test_correlation_key_derived_from_the_domain(self):
        j = journeys_of(CRATEWISE)
        self.assertEqual(j["checkout"]["correlation_key"]["attribute"], "checkout.id")
        self.assertEqual(j["refund"]["correlation_key"]["attribute"], "refund.id")


class TestWhatItRefusesToDecide(unittest.TestCase):
    def test_business_impact_never_set(self):
        """Nothing in source says which flow earns revenue, and volume is a
        misleading proxy."""
        for j in journeys_of(CRATEWISE).values():
            self.assertNotIn("business_impact", j, j["id"])

    def test_impact_absence_is_flagged_not_silent(self):
        for j in journeys_of(CRATEWISE).values():
            self.assertTrue(any("business_impact" in q for q in j["needs_clarification"]))

    def test_step_order_flagged_as_unverified(self):
        for j in journeys_of(CRATEWISE).values():
            self.assertTrue(any("step order" in q.lower() for q in j["needs_clarification"]))

    def test_missing_state_machine_is_stated_not_invented(self):
        j = journeys_of({"src/widgets/svc.ts": "export function makeWidget() {}\n"
                                               "export function shipWidget() {}\n"})
        w = j.get("widget") or j.get("widgets")
        self.assertIsNotNone(w)
        self.assertNotIn("outcome", w)
        self.assertTrue(any("no state machine" in q.lower()
                            for q in w["needs_clarification"]))

    def test_provenance_is_discovered_code(self):
        for j in journeys_of(CRATEWISE).values():
            self.assertEqual(j["source"], "discovered:code")


class TestNoiseRejection(unittest.TestCase):
    def test_infrastructure_directories_are_not_journeys(self):
        j = journeys_of(CRATEWISE)
        for infra in ("util", "utils", "config", "middleware", "type", "types"):
            self.assertNotIn(infra, j)

    def test_single_step_candidates_are_dropped(self):
        j = journeys_of({"src/pings/svc.ts": "export function ping() {}"})
        self.assertNotIn("ping", j)

    def test_payment_dependency_raises_confidence(self):
        ev = collect(repo(CRATEWISE))
        self.assertIn("stripe", ev.providers)
        self.assertEqual(journeys_of(CRATEWISE)["checkout"]["confidence"], "high")


class TestOutputContract(unittest.TestCase):
    def test_yaml_is_schema_valid(self):
        try:
            import yaml
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("needs pyyaml + jsonschema")
        js = list(journeys_of(CRATEWISE).values())
        doc = yaml.safe_load(to_yaml(js))
        schema = json.loads(
            (HERE.parent / "intake" / "schema" / "journey-candidate.schema.json").read_text())
        errs = list(Draft202012Validator(schema).iter_errors(doc))
        self.assertEqual(errs, [], [(list(e.path), e.message[:120]) for e in errs[:3]])

    def test_yaml_leaves_impact_commented_for_the_human(self):
        out = to_yaml(list(journeys_of(CRATEWISE).values()))
        self.assertIn("# business_impact:", out)
        self.assertIn("UNCOMMENT AND SET (human only)", out)

    def test_end_to_end_through_intake_and_gap(self):
        r = repo(CRATEWISE)
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            y, res, obs, gap = (work / "j.yaml", work / "r.json",
                                work / "o.json", work / "g.json")
            for args in (
                ["propose", "--repo", str(r), "--out", str(y)],
                ["intake", "--discovered", str(y), "--out-json", str(res)],
                ["scan", "--repo", str(r), "--out", str(obs), "--quiet"],
                ["gap", "--resolved", str(res), "--observed", str(obs),
                 "--include-unready", "--out-json", str(gap)],
            ):
                p = subprocess.run([sys.executable, str(CLI), *args],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, f"{args[0]}: {p.stderr[-500:]}")
            doc = json.loads(gap.read_text())
            self.assertGreaterEqual(len(doc["journeys"]), 3)
            names = {j["id"] for j in doc["journeys"]}
            self.assertIn("checkout", names)

    def test_empty_repo_exits_2_with_advice(self):
        p = subprocess.run(
            [sys.executable, str(CLI), "propose", "--repo", str(repo({"a.txt": "x"})),
             "--out", "/dev/null"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ce init", p.stderr)

    def test_skips_secret_files(self):
        """Propose used to be willing to open anything with a code suffix. A
        `credentials.json` sitting next to routes is not a journey."""
        self.assertTrue(is_secret_path(Path(".env")))
        self.assertTrue(is_secret_path(Path("credentials.json")))
        self.assertTrue(is_secret_path(Path("prod.pem")))
        d = repo({
            ".env/leak.ts": 'router.post("/api/checkout/secret", x);\n',
            "src/checkout/routes.ts": CRATEWISE["src/checkout/routes.ts"],
        })
        names = {p.name for p in iter_files(d)}
        self.assertIn("routes.ts", names)
        self.assertNotIn("leak.ts", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
