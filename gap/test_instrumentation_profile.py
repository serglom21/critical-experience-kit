#!/usr/bin/env python3
"""Tests for the automatic-vs-custom instrumentation profile."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from instrumentation_profile import (  # noqa: E402
    classify,
    family_for_op,
    looks_like_code_location,
    recommend,
    render_markdown,
    to_json,
)

HERE = Path(__file__).parent
LIVE = HERE / "fixtures" / "observed-demo-org.live.json"
SYNTH = HERE / "fixtures" / "observed-customer.example.json"


def obs(**kw) -> dict:
    base = {"org": "t", "stats_period": "7d", "span_ops": [], "span_names": [],
            "attributes": []}
    base.update(kw)
    return base


def user(key, t="string"):
    return {"key": key, "attributeType": t, "attributeSource": {"source_type": "user"}}


def sdk(key, t="string"):
    return {"key": key, "attributeType": t, "attributeSource": {"source_type": "sentry"}}


class TestOpFamilyMatching(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(family_for_op("http.server"), "HTTP server (inbound requests)")

    def test_prefix_match_covers_real_leaf_ops(self):
        """Regression: exact matching dumped all of these into 'unclassified',
        making an incomplete lookup table read as a customer finding."""
        for op in ("browser.DNS", "browser.TLS/SSL", "browser.domContentLoadedEvent",
                   "ui.webvital.cls", "ui.webvital.lcp", "ui.react.update",
                   "ui.react.mount", "db.redis", "resource.img", "resource.other"):
            self.assertIsNotNone(family_for_op(op), op)

    def test_longest_prefix_wins(self):
        self.assertEqual(family_for_op("ui.webvital.cls"), "Web vitals")
        self.assertEqual(family_for_op("ui.react.mount"), "UI rendering")

    def test_unknown_op_is_unmatched(self):
        self.assertIsNone(family_for_op("acme.custom.thing"))
        self.assertIsNone(family_for_op("checkout"))


class TestCodeLocationHeuristic(unittest.TestCase):
    def test_module_paths_are_code_level(self):
        for name in ("src.db.get_products", "src.main.get_api_response_with_caching",
                     "UIKit.NavigationBarContentView.__backButtonAction",
                     "SwiftMessages.MaskingView.tapped", "<unknown>",
                     "_TtCCC5UIKit19Nav.foo(forEvent:)"):
            self.assertTrue(looks_like_code_location(name), name)

    def test_business_names_are_not(self):
        for name in ("items_added_to_cart", "processCheckout", "handleApplyPromoCode",
                     "checkout", "User Typing", "Focus Chat Input"):
            self.assertFalse(looks_like_code_location(name), name)


class TestTiers(unittest.TestCase):
    def test_automatic_only(self):
        p = classify(obs(span_ops=[{"op": "http.server", "count": 10}],
                         attributes=[sdk("http.request.method")]))
        self.assertEqual(p.tier, "automatic only")
        self.assertIn("cannot determine", p.headline)

    def test_attributes_without_journey_spans(self):
        p = classify(obs(span_ops=[{"op": "http.server", "count": 10}],
                         attributes=[user("checkout.id")]))
        self.assertEqual(p.tier, "attributes without journey spans")
        self.assertIn("plumbing exists", p.headline)

    def test_spans_without_business_attributes(self):
        p = classify(obs(span_names=[{"name": "processCheckout", "count": 5}],
                         attributes=[sdk("http.request.method")]))
        self.assertEqual(p.tier, "spans without business attributes")

    def test_both_layers_present(self):
        p = classify(obs(span_names=[{"name": "processCheckout", "count": 5}],
                         attributes=[user("checkout.id")]))
        self.assertEqual(p.tier, "custom instrumentation present")

    def test_nothing_at_all(self):
        self.assertEqual(classify(obs()).tier, "none")


class TestClassification(unittest.TestCase):
    def test_source_type_splits_attributes(self):
        p = classify(obs(attributes=[user("cart.value"), sdk("db.system")]))
        self.assertEqual(p.custom_attributes, ["cart.value"])
        self.assertEqual(p.auto_attributes, ["db.system"])

    def test_namespace_collision_flagged(self):
        p = classify(obs(attributes=[user("http.my_thing"), user("cart.value")]))
        self.assertEqual(p.namespace_collisions, ["http.my_thing"])

    def test_unclassified_ops_surfaced(self):
        p = classify(obs(span_ops=[{"op": "acme.weird", "count": 7}]))
        self.assertEqual(p.unclassified_ops, {"acme.weird": 7})

    def test_custom_share_uses_span_volume(self):
        p = classify(obs(span_ops=[{"op": "http.server", "count": 900},
                                   {"op": "function", "count": 100}],
                         span_names=[{"name": "processCheckout", "count": 100}]))
        self.assertAlmostEqual(p.custom_share, 0.1, places=3)


class TestRecommendations(unittest.TestCase):
    def test_automatic_only_gets_the_five_attribute_ask(self):
        p = classify(obs(span_ops=[{"op": "http.server", "count": 10}],
                         attributes=[sdk("db.system")]))
        recs = recommend(p)
        self.assertTrue(recs)
        self.assertEqual(recs[0]["priority"], "critical")
        self.assertIn("Five attributes", recs[0]["ask"])

    def test_drift_becomes_a_critical_recommendation(self):
        p = classify(obs(span_names=[{"name": "processCheckout", "count": 5}]))
        gapdoc = {"journeys": [{
            "name": "Checkout", "coverage_state": "partial", "dark_segments": [],
            "findings": [{"rule": "CE-013", "passed": False,
                          "detail": "expected `a`, found `b`", "description": "d",
                          "rationale": "r"}],
        }]}
        recs = recommend(p, gapdoc)
        self.assertTrue(any("rename" in r["title"].lower() for r in recs))

    def test_absent_journey_with_correlation_key_is_flagged_as_cheap(self):
        p = classify(obs())
        gapdoc = {"journeys": [{
            "name": "Refund", "coverage_state": "absent", "dark_segments": [],
            "findings": [{"rule": "CE-004", "passed": True, "detail": "", "description": "d",
                          "rationale": "r"}],
        }]}
        recs = recommend(p, gapdoc)
        self.assertTrue(any("correlation key already exists" in r["title"] for r in recs))

    def test_recommendations_are_priority_ordered(self):
        p = classify(obs(span_ops=[{"op": "acme.weird", "count": 5},
                                   {"op": "http.server", "count": 5}],
                         attributes=[user("http.oops")]))
        recs = recommend(p)
        order = {"critical": 0, "important": 1, "normal": 2}
        self.assertEqual([order[r["priority"]] for r in recs],
                         sorted(order[r["priority"]] for r in recs))


class TestLiveFixture(unittest.TestCase):
    """Against real data pulled from the `demo` org, which is what exposed the
    prefix-matching and code-location bugs."""

    def setUp(self):
        if not LIVE.exists():
            self.skipTest("live fixture missing")
        self.p = classify(json.loads(LIVE.read_text()))

    def test_no_unclassified_ops(self):
        self.assertEqual(self.p.unclassified_ops, {},
                         "real ops must all map to a family")

    def test_business_spans_identified(self):
        names = [n for n, _ in self.p.custom_spans]
        for expected in ("items_added_to_cart", "processCheckout", "handleApplyPromoCode"):
            self.assertIn(expected, names)

    def test_sdk_function_tracing_not_counted_as_business(self):
        names = [n for n, _ in self.p.custom_spans]
        for code in ("src.db.get_products", "src.db.get_inventory", "<unknown>"):
            self.assertNotIn(code, names)
        self.assertIn("src.db.get_products", [n for n, _ in self.p.code_level_spans])

    def test_expected_auto_families_detected(self):
        for fam in ("HTTP server (inbound requests)", "Database", "UI rendering",
                    "Web vitals", "Browser resources"):
            self.assertIn(fam, self.p.auto_families, fam)

    def test_tier_reflects_missing_business_attributes(self):
        self.assertEqual(self.p.tier, "spans without business attributes")

    def test_report_labels_the_heuristic(self):
        md = render_markdown(self.p, recommend(self.p))
        self.assertIn("heuristic", md.lower())
        self.assertIn("Code-level spans", md)

    def test_json_shape(self):
        d = to_json(self.p, recommend(self.p))
        for key in ("tier", "headline", "automatic", "custom", "recommendations"):
            self.assertIn(key, d)


class TestSyntheticFixtureStillWorks(unittest.TestCase):
    def setUp(self):
        if not SYNTH.exists():
            self.skipTest("synthetic fixture missing")
        self.p = classify(json.loads(SYNTH.read_text()))

    def test_custom_attributes_found_via_source_type(self):
        for k in ("checkout.id", "cart.value", "user.plan_tier"):
            self.assertIn(k, self.p.custom_attributes)

    def test_sdk_attributes_excluded(self):
        for k in ("http.request.method", "db.system", "server.address"):
            self.assertNotIn(k, self.p.custom_attributes)

    def test_checkout_spans_read_as_business(self):
        names = [n for n, _ in self.p.custom_spans]
        self.assertIn("checkout", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
