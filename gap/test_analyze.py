#!/usr/bin/env python3
"""Tests for the gap analyzer. Run: python3 -m unittest discover -s gap"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analyze import (  # noqa: E402
    CAP_NEEDS_IMPROVEMENT,
    CAP_POOR,
    Observed,
    analyze_journey,
    band,
    expected_span_name,
    normalize_span_name,
    render_markdown,
    to_json,
)

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "observed-customer.example.json"
RESOLVED = HERE.parent / "intake" / "example-resolved.json"


def journey(**over) -> dict:
    """A fully healthy journey definition, in resolve.py's output shape."""
    j = {
        "id": "checkout",
        "name": "Checkout",
        "spec_ready": True,
        "excluded": False,
        "blockers": [],
        "roles": {
            "correlation_key": {"attribute": "checkout.id"},
            "steps": [
                {"id": "cart_reviewed", "impact": "normal"},
                {"id": "payment_authorized", "impact": "critical"},
                {"id": "confirmation_viewed", "impact": "important"},
            ],
            "outcome": {
                "attribute": "checkout.outcome",
                "values": ["completed", "failed"],
                "success_values": ["completed"],
            },
            "failure_reason": {"attribute": "payment.decline_reason"},
            "magnitude": [{"attribute": "cart.value", "type": "double"}],
            "actor_segment": [{"attribute": "user.plan_tier"}],
        },
    }
    j["roles"].update(over.pop("roles", {}))
    j.update(over)
    return j


def observed(**over) -> Observed:
    """Everything present and correctly typed."""
    doc = {
        "org": "test",
        "stats_period": "30d",
        "traces_sample_rate": 0.5,
        "span_names": [
            {"name": "checkout", "count": 1000},
            {"name": "checkout.payment_authorized", "count": 1000},
            {"name": "checkout.confirmation_viewed", "count": 1000},
        ],
        "attributes": [
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "checkout.outcome", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "payment.decline_reason", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "cart.value", "attributeType": "number", "attributeSource": {"source_type": "user"}},
            {"key": "user.plan_tier", "attributeType": "string", "attributeSource": {"source_type": "user"}},
        ],
    }
    doc.update(over)
    return Observed(doc)


def failed(gap, rule):
    return [f for f in gap.findings if f.rule == rule and not f.passed]


class TestHelpers(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(band(100), "excellent")
        self.assertEqual(band(90), "excellent")
        self.assertEqual(band(89.9), "good")
        self.assertEqual(band(75), "good")
        self.assertEqual(band(74.9), "needs improvement")
        self.assertEqual(band(50), "needs improvement")
        self.assertEqual(band(49.9), "poor")
        self.assertEqual(band(0), "poor")

    def test_normalize_folds_separators(self):
        self.assertEqual(
            normalize_span_name("checkout.payment_authorized"),
            normalize_span_name("checkout.payment.authorized"),
        )
        self.assertNotEqual(
            normalize_span_name("checkout.payment_authorized"),
            normalize_span_name("checkout.payment.authorize"),
        )

    def test_expected_name_convention_and_override(self):
        self.assertEqual(expected_span_name("checkout", {"id": "cart"}, True), "checkout")
        self.assertEqual(expected_span_name("checkout", {"id": "cart"}, False), "checkout.cart")
        self.assertEqual(
            expected_span_name("checkout", {"id": "cart", "span_name": "cart.review"}, False),
            "cart.review",
        )


class TestHealthyBaseline(unittest.TestCase):
    def test_everything_present_scores_100(self):
        g = analyze_journey(journey(), observed())
        self.assertEqual(g.failures, [], [f.rule for f in g.failures])
        self.assertEqual(g.score, 100.0)
        self.assertEqual(g.grade, "excellent")
        self.assertEqual(g.caps, [])


class TestSpanRules(unittest.TestCase):
    def test_ce001_missing_root_caps_at_poor(self):
        obs = observed(span_names=[
            {"name": "checkout.payment_authorized", "count": 10},
            {"name": "checkout.confirmation_viewed", "count": 10},
        ])
        g = analyze_journey(journey(), obs)
        self.assertTrue(failed(g, "CE-001"))
        self.assertLessEqual(g.score, CAP_POOR)
        self.assertEqual(g.grade, "poor")
        self.assertTrue(any("CE-001" in c for c in g.caps))

    def test_ce002_missing_terminal_caps_at_needs_improvement(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 1000},
            {"name": "checkout.payment_authorized", "count": 1000},
        ])
        g = analyze_journey(journey(), obs)
        self.assertTrue(failed(g, "CE-002"))
        self.assertLessEqual(g.score, CAP_NEEDS_IMPROVEMENT)
        self.assertGreater(g.raw_score, g.score, "cap must actually bite")

    def test_ce003_per_step_weight_follows_step_impact(self):
        g = analyze_journey(journey(), observed())
        weights = {f.entity: f.weight for f in g.findings if f.rule == "CE-003"}
        self.assertEqual(weights["checkout.payment_authorized"], 40)  # critical
        self.assertEqual(weights["checkout.confirmation_viewed"], 30)  # important
        self.assertEqual(weights["checkout"], 20)  # normal

    def test_ce012_dark_segment_only_when_bounded_both_sides(self):
        """A gap at the END is not a dark segment — it's a truncated journey."""
        obs = observed(span_names=[
            {"name": "checkout", "count": 100},
            {"name": "checkout.payment_authorized", "count": 100},
        ])
        g = analyze_journey(journey(), obs)
        self.assertEqual(g.dark_segments, [])
        self.assertFalse(failed(g, "CE-012"))

    def test_ce012_detects_mid_funnel_gap(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 100},
            {"name": "checkout.confirmation_viewed", "count": 60},
        ])
        g = analyze_journey(journey(), obs)
        self.assertEqual(g.dark_segments, [["payment_authorized"]])
        self.assertTrue(failed(g, "CE-012"))

    def test_ce012_no_leading_gap_false_positive(self):
        """Nothing before the first step, so a missing root can't open a segment."""
        obs = observed(span_names=[{"name": "checkout.payment_authorized", "count": 10}])
        g = analyze_journey(journey(), obs)
        self.assertEqual(g.dark_segments, [])

    def test_ce013_name_drift_detected(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 100},
            {"name": "checkout.payment.authorized", "count": 99},
            {"name": "checkout.confirmation_viewed", "count": 90},
        ])
        g = analyze_journey(journey(), obs)
        drift = failed(g, "CE-013")
        self.assertTrue(drift)
        self.assertIn("checkout.payment.authorized", drift[0].detail)
        # And the step still reads as missing, because queries on the expected
        # name return nothing.
        self.assertTrue(failed(g, "CE-003"))

    def test_extent_reported_for_missing_step(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 48210},
            {"name": "checkout.confirmation_viewed", "count": 100},
        ])
        g = analyze_journey(journey(), obs)
        f = next(f for f in failed(g, "CE-003") if f.entity == "checkout.payment_authorized")
        self.assertIn("48,210", f.extent)

    def test_extent_reported_for_partial_step(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 1000},
            {"name": "checkout.payment_authorized", "count": 620},
            {"name": "checkout.confirmation_viewed", "count": 600},
        ])
        g = analyze_journey(journey(), obs)
        f = next(f for f in g.findings
                 if f.rule == "CE-003" and f.entity == "checkout.payment_authorized")
        self.assertTrue(f.passed)
        self.assertIn("38%", f.extent)


class TestAttributeRules(unittest.TestCase):
    def test_ce004_missing_correlation_key(self):
        obs = observed(attributes=[
            {"key": "checkout.outcome", "attributeType": "string", "attributeSource": {"source_type": "user"}},
        ])
        g = analyze_journey(journey(), obs)
        self.assertTrue(failed(g, "CE-004"))

    def test_ce004_fails_when_not_declared(self):
        g = analyze_journey(journey(roles={"correlation_key": None}), observed())
        f = failed(g, "CE-004")[0]
        self.assertIn("not declared", f.detail)

    def test_ce005_flags_sentry_owned_correlation_key(self):
        obs = observed(attributes=[
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "sentry"}},
        ])
        g = analyze_journey(journey(), obs)
        self.assertFalse(failed(g, "CE-004"))
        self.assertTrue(failed(g, "CE-005"))

    def test_ce007_boolean_outcome_flagged(self):
        obs = observed(attributes=[
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "checkout.outcome", "attributeType": "boolean", "attributeSource": {"source_type": "user"}},
        ])
        g = analyze_journey(journey(), obs)
        self.assertFalse(failed(g, "CE-006"), "present but wrong type still counts as present")
        self.assertTrue(failed(g, "CE-007"))

    def test_ce008_only_applies_when_outcome_admits_failure(self):
        all_success = journey(roles={"outcome": {
            "attribute": "checkout.outcome", "values": ["completed", "partial"],
            "success_values": ["completed", "partial"]}})
        g = analyze_journey(all_success, observed())
        self.assertEqual([f for f in g.findings if f.rule == "CE-008"], [])

    def test_ce008_fails_when_reason_absent(self):
        obs = observed(attributes=[
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "checkout.outcome", "attributeType": "string", "attributeSource": {"source_type": "user"}},
        ])
        g = analyze_journey(journey(), obs)
        self.assertTrue(failed(g, "CE-008"))
        self.assertTrue(any("non-success" in n for n in g.notes))

    def test_ce010_stringified_magnitude_is_the_silent_defect(self):
        obs = observed(attributes=[
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "checkout.outcome", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "payment.decline_reason", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "cart.value", "attributeType": "string", "attributeSource": {"source_type": "user"}},
        ])
        g = analyze_journey(journey(), obs)
        self.assertFalse(failed(g, "CE-009"), "it is present...")
        self.assertTrue(failed(g, "CE-010"), "...but not aggregatable")

    def test_ce011_segment_absent_is_only_normal_impact(self):
        obs = observed(attributes=[
            {"key": "checkout.id", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "checkout.outcome", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "payment.decline_reason", "attributeType": "string", "attributeSource": {"source_type": "user"}},
            {"key": "cart.value", "attributeType": "number", "attributeSource": {"source_type": "user"}},
        ])
        g = analyze_journey(journey(), obs)
        f = failed(g, "CE-011")[0]
        self.assertEqual(f.weight, 20)


class TestSamplingGuard(unittest.TestCase):
    def test_low_sample_rate_degrades_confidence(self):
        g = analyze_journey(journey(), observed(traces_sample_rate=0.01))
        self.assertTrue(g.low_confidence)

    def test_normal_sample_rate_does_not(self):
        self.assertFalse(analyze_journey(journey(), observed(traces_sample_rate=0.25)).low_confidence)

    def test_unknown_sample_rate_is_not_treated_as_low(self):
        self.assertFalse(analyze_journey(journey(), observed(traces_sample_rate=None)).low_confidence)

    def test_report_warns_on_low_confidence(self):
        obs = observed(traces_sample_rate=0.01)
        md = render_markdown([analyze_journey(journey(), obs)], obs, [])
        self.assertIn("Low confidence", md)


class TestReporting(unittest.TestCase):
    def test_rationale_present_on_every_finding(self):
        """The rationale field is what makes a finding persuasive rather than a scold."""
        g = analyze_journey(journey(), observed(span_names=[]))
        for f in g.findings:
            self.assertTrue(f.rationale.strip(), f"{f.rule} has no rationale")

    def test_markdown_surfaces_dark_segment_language(self):
        obs = observed(span_names=[
            {"name": "checkout", "count": 100},
            {"name": "checkout.confirmation_viewed", "count": 60},
        ])
        md = render_markdown([analyze_journey(journey(), obs)], obs, [])
        self.assertIn("Goes dark at", md)

    def test_markdown_reports_cap(self):
        obs = observed(span_names=[{"name": "checkout.payment_authorized", "count": 5}])
        md = render_markdown([analyze_journey(journey(), obs)], obs, [])
        self.assertIn("Capped", md)

    def test_json_carries_weights_and_rationale(self):
        obs = observed()
        payload = to_json([analyze_journey(journey(), obs)], obs, [])
        f = payload["journeys"][0]["findings"][0]
        for key in ("rule", "rationale", "impact", "weight", "passed"):
            self.assertIn(key, f)

    def test_journeys_scored_independently_not_anded(self):
        good = analyze_journey(journey(), observed())
        bad = analyze_journey(journey(id="other", name="Other"), observed(span_names=[]))
        payload = to_json([good, bad], observed(), [])
        scores = {j["name"]: j["score"] for j in payload["journeys"]}
        self.assertEqual(scores["Checkout"], 100.0)
        self.assertLess(scores["Other"], 50)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        if not RESOLVED.exists():
            self.skipTest("run intake/resolve.py first")
        out = subprocess.run(
            [sys.executable, str(HERE / "analyze.py"),
             "--resolved", str(RESOLVED), "--observed", str(FIXTURE),
             "--include-unready", "--out-json", "/dev/stdout"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.payload = json.loads(out.stdout)

    def _j(self, name):
        return next(x for x in self.payload["journeys"] if x["name"] == name)

    def test_excluded_and_stepless_journeys_are_skipped(self):
        reasons = {s["name"]: s["reason"] for s in self.payload["skipped"]}
        self.assertIn("Static Asset Load", reasons)
        self.assertIn("excluded", reasons["Static Asset Load"])

    def test_checkout_is_capped_by_missing_terminal_span(self):
        c = self._j("Checkout")
        self.assertTrue(any("CE-002" in cap for cap in c["caps"]))
        self.assertLessEqual(c["score"], CAP_NEEDS_IMPROVEMENT)

    def test_checkout_dark_segment_is_payment_authorized(self):
        self.assertEqual(self._j("Checkout")["dark_segments"], [["payment_authorized"]])

    def test_checkout_drift_names_the_real_span(self):
        drift = next(f for f in self._j("Checkout")["findings"] if f["rule"] == "CE-013")
        self.assertFalse(drift["passed"])
        self.assertIn("checkout.payment.authorize", drift["detail"])   # bound
        self.assertIn("checkout.payment_authorize", drift["detail"])   # actually sent

    def test_drift_note_quantifies_the_wasted_spans(self):
        notes = self._j("Checkout")["notes"]
        self.assertTrue(any("43,800" in n for n in notes), notes)

    def test_checkout_boolean_outcome_and_string_magnitude_both_caught(self):
        rules = {f["rule"]: f["passed"] for f in self._j("Checkout")["findings"]}
        self.assertFalse(rules["CE-007"], "boolean outcome should fail")
        self.assertFalse(rules["CE-010"], "stringified cart.value should fail")

    def test_subscription_upgrade_has_key_but_no_spans(self):
        u = self._j("Subscription Upgrade")
        rules = {f["rule"]: f["passed"] for f in u["findings"]}
        self.assertTrue(rules["CE-004"], "upgrade.id is present in the org")
        self.assertFalse(rules["CE-001"], "but no journey spans exist")
        self.assertEqual(u["grade"], "poor")

    def test_partial_journeys_lead_the_report(self):
        """Regression. Sorting by score alone put four entirely-uninstrumented
        journeys on top, all on the same meaningless ~15, and buried the one
        journey with a diagnosable gap at the bottom."""
        states = [j["coverage_state"] for j in self.payload["journeys"]]
        tier = {"partial": 0, "complete": 1, "absent": 2}
        self.assertEqual([tier[s] for s in states], sorted(tier[s] for s in states))
        self.assertEqual(self.payload["journeys"][0]["name"], "Checkout")

    def test_checkout_is_the_partial_one(self):
        c = self._j("Checkout")
        self.assertEqual(c["coverage_state"], "partial")
        self.assertEqual(c["steps_instrumented"], 5)
        self.assertEqual(c["steps_total"], 7)

    def test_findings_render_an_example_trace(self):
        """rules.md requires extent + entity + example on every finding. The
        example column was collected but never rendered — regression guard."""
        out = subprocess.run(
            [sys.executable, str(HERE / "analyze.py"),
             "--resolved", str(RESOLVED), "--observed", str(FIXTURE), "--include-unready"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| Example |", out.stdout)
        self.assertIn("Open one of these traces to see it", out.stdout)
        self.assertIn("7f3c1a9b4e2d48f0a1c6b8e5d2409af1", out.stdout)

    def test_uninstrumented_journeys_are_classified_absent(self):
        for name in ("Refund Request", "Password Reset", "Plan Downgrade"):
            self.assertEqual(self._j(name)["coverage_state"], "absent", name)

    def test_absent_section_replaces_grades_in_report(self):
        out = subprocess.run(
            [sys.executable, str(HERE / "analyze.py"),
             "--resolved", str(RESOLVED), "--observed", str(FIXTURE),
             "--include-unready"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("## Not instrumented", out.stdout)
        self.assertIn("## Where visibility breaks", out.stdout)
        # Absent journeys must not get a detail block.
        self.assertNotIn("### Refund Request", out.stdout)


class TestCoverageState(unittest.TestCase):
    def test_absent_when_no_step_spans(self):
        self.assertEqual(analyze_journey(journey(), observed(span_names=[])).coverage_state, "absent")

    def test_partial_when_some(self):
        obs = observed(span_names=[{"name": "checkout", "count": 5}])
        g = analyze_journey(journey(), obs)
        self.assertEqual(g.coverage_state, "partial")
        self.assertEqual(g.coverage_label, "1/3 steps")

    def test_complete_when_all(self):
        self.assertEqual(analyze_journey(journey(), observed()).coverage_state, "complete")

    def test_stepless_journey_is_absent_not_crash(self):
        g = analyze_journey(journey(roles={"steps": []}), observed())
        self.assertEqual(g.coverage_state, "absent")
        self.assertEqual(g.steps_total, 0)

    def test_ordering_puts_partial_first(self):
        good = analyze_journey(journey(), observed())
        gone = analyze_journey(journey(id="a", name="A"), observed(span_names=[]))
        half = analyze_journey(journey(id="b", name="B"),
                               observed(span_names=[{"name": "b", "count": 1}]))
        from analyze import order_for_report
        self.assertEqual([g.name for g in order_for_report([good, gone, half])],
                         ["B", "Checkout", "A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
