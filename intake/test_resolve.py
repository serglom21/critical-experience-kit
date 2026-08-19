#!/usr/bin/env python3
"""Tests for the intake resolver. Run: python3 -m unittest discover -s intake"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from resolve import (  # noqa: E402
    CORROBORATED,
    DECLARED_UNCONFIRMED,
    PROPOSED,
    Journey,
    match_keys,
    normalize,
    rank,
    render_markdown,
    resolve,
    to_json,
)

HERE = Path(__file__).parent
CANDIDATES = HERE / "candidates"


def j(**kw) -> dict:
    base = {"id": "x", "name": "X", "source": "declared"}
    base.update(kw)
    return base


class TestNormalizeAndMatch(unittest.TestCase):
    def test_normalize_folds_case_and_punctuation(self):
        self.assertEqual(normalize("Checkout Flow"), "checkout flow")
        self.assertEqual(normalize("Check-out  FLOW!"), "check out flow")

    def test_match_keys_includes_aliases_and_correlation_key(self):
        keys = match_keys(
            j(id="checkout", name="Checkout", aliases=["Checkout Flow"],
              correlation_key={"attribute": "checkout.id"})
        )
        self.assertIn("id:checkout", keys)
        self.assertIn("name:checkout", keys)
        self.assertIn("name:checkout flow", keys)
        self.assertIn("corr:checkout.id", keys)


class TestResolveMatching(unittest.TestCase):
    def test_merges_on_alias(self):
        out = resolve([
            j(id="checkout", name="Checkout", aliases=["Checkout Flow"]),
            j(id="checkout_flow", name="Checkout Flow", source="discovered:code"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, CORROBORATED)

    def test_merges_on_correlation_key_despite_different_names(self):
        out = resolve([
            j(id="subscription_upgrade", name="Subscription Upgrade",
              correlation_key={"attribute": "upgrade.id"}),
            j(id="upgrade_path", name="Upgrade Path", source="discovered:telemetry",
              correlation_key={"attribute": "upgrade.id"}),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, CORROBORATED)

    def test_declared_name_and_id_win(self):
        """The business name must survive contact with an inferred one."""
        out = resolve([
            j(id="checkout_flow", name="Checkout Flow", source="discovered:code",
              correlation_key={"attribute": "checkout.id"}),
            j(id="checkout", name="Checkout", correlation_key={"attribute": "checkout.id"}),
        ])
        self.assertEqual(out[0].name, "Checkout")
        self.assertEqual(out[0].id, "checkout")

    def test_unrelated_journeys_are_not_merged(self):
        out = resolve([j(id="a", name="Alpha"), j(id="b", name="Beta", source="discovered:code")])
        self.assertEqual(len(out), 2)

    def test_transitive_merge_across_three_sources(self):
        out = resolve([
            j(id="a", name="Alpha"),
            j(id="b", name="Beta", source="discovered:code", correlation_key={"attribute": "k.id"}),
            j(id="c", name="Alpha", source="discovered:telemetry",
              correlation_key={"attribute": "k.id"}),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].contributions), 3)


class TestTwoByTwo(unittest.TestCase):
    def test_declared_only_is_a_finding_not_an_error(self):
        out = resolve([j(id="refund", name="Refund")])
        self.assertEqual(out[0].status, DECLARED_UNCONFIRMED)

    def test_discovered_only_is_proposed(self):
        out = resolve([j(id="pw", name="Password Reset", source="discovered:code")])
        self.assertEqual(out[0].status, PROPOSED)

    def test_both_is_corroborated(self):
        out = resolve([j(id="a", name="A"), j(id="a", name="A", source="discovered:code")])
        self.assertEqual(out[0].status, CORROBORATED)


class TestRoleCompletion(unittest.TestCase):
    def _full(self, **over) -> Journey:
        raw = j(
            correlation_key={"attribute": "k.id"},
            steps=[{"id": "one"}, {"id": "two"}],
            outcome={"attribute": "k.outcome", "values": ["completed", "failed"],
                     "success_values": ["completed"]},
            failure_reason={"attribute": "k.reason"},
            magnitude=[{"attribute": "k.value", "type": "double"}],
            actor_segment=[{"attribute": "user.tier"}],
        )
        raw.update(over)
        return resolve([raw])[0]

    def test_fully_specified_is_spec_ready(self):
        jr = self._full()
        self.assertTrue(jr.spec_ready, jr.blockers)
        self.assertEqual(jr.missing_roles, [])

    def test_single_step_is_not_a_journey(self):
        jr = self._full(steps=[{"id": "only"}])
        self.assertFalse(jr.role_filled("steps"))
        self.assertFalse(jr.spec_ready)

    def test_missing_correlation_key_blocks(self):
        raw = j(steps=[{"id": "a"}, {"id": "b"}],
                outcome={"attribute": "o", "values": ["a", "b"], "success_values": ["a"]})
        jr = resolve([raw])[0]
        self.assertFalse(jr.spec_ready)
        self.assertTrue(any("correlation key" in b for b in jr.blockers))

    def test_non_success_outcome_without_failure_reason_blocks(self):
        """The conditional invariant: coded reasons are required once you admit
        the journey can fail."""
        jr = self._full(failure_reason=None)
        self.assertFalse(jr.spec_ready)
        self.assertTrue(any("failure reason" in b for b in jr.blockers))

    def test_outcome_without_success_values_blocks(self):
        jr = self._full(outcome={"attribute": "o", "values": ["completed", "failed"]})
        self.assertFalse(jr.spec_ready)
        self.assertTrue(any("success" in b for b in jr.blockers))

    def test_magnitude_and_segment_are_not_blockers(self):
        jr = self._full(magnitude=None, actor_segment=None)
        self.assertTrue(jr.spec_ready, jr.blockers)
        self.assertIn("magnitude", jr.missing_roles)

    def test_missing_semantic_roles_become_clarifications(self):
        jr = self._full(outcome=None, failure_reason=None, magnitude=None, actor_segment=None)
        text = " ".join(jr.clarifications)
        for token in ("outcome", "failure reason", "magnitude", "actor segment"):
            self.assertIn(token, text)

    def test_explicit_clarifications_are_preserved(self):
        jr = self._full(needs_clarification=["Is entitlement granted async?"])
        self.assertIn("Is entitlement granted async?", jr.clarifications)

    def test_unassigned_impact_is_flagged(self):
        jr = self._full()
        self.assertTrue(any("business impact" in c for c in jr.clarifications))


class TestMergePrecedence(unittest.TestCase):
    def test_declared_role_value_beats_discovered(self):
        out = resolve([
            j(id="a", name="A", source="discovered:code",
              correlation_key={"attribute": "wrong.id"}),
            j(id="a", name="A", correlation_key={"attribute": "right.id"}),
        ])
        self.assertEqual(out[0].role_value("correlation_key")["attribute"], "right.id")

    def test_discovered_fills_gaps_declared_left_open(self):
        out = resolve([
            j(id="a", name="A"),
            j(id="a", name="A", source="discovered:code", steps=[{"id": "x"}, {"id": "y"}]),
        ])
        self.assertTrue(out[0].role_filled("steps"))

    def test_declared_impact_wins_over_discovered(self):
        out = resolve([
            j(id="a", name="A", source="discovered:code", business_impact="normal"),
            j(id="a", name="A", business_impact="critical"),
        ])
        self.assertEqual(out[0].business_impact, "critical")

    def test_best_confidence_wins(self):
        out = resolve([
            j(id="a", name="A", confidence="low"),
            j(id="a", name="A", source="discovered:code", confidence="high"),
        ])
        self.assertEqual(out[0].confidence, "high")


class TestRanking(unittest.TestCase):
    def test_declared_outranks_discovered_regardless_of_volume(self):
        out = rank(resolve([
            j(id="probe", name="Health Probe", source="discovered:telemetry",
              observed_volume=9_200_000),
            j(id="refund", name="Refund", business_impact="critical"),
        ]))
        self.assertEqual(out[0].name, "Refund")

    def test_impact_orders_within_declared(self):
        out = rank(resolve([
            j(id="b", name="B", business_impact="normal"),
            j(id="a", name="A", business_impact="critical"),
        ]))
        self.assertEqual([x.name for x in out], ["A", "B"])

    def test_volume_tiebreaks_only_once_impact_is_assigned(self):
        """Same source, same impact, same readiness -> volume decides."""
        out = rank(resolve([
            j(id="a", name="A", source="discovered:code", business_impact="normal", observed_volume=10),
            j(id="b", name="B", source="discovered:code", business_impact="normal", observed_volume=99),
        ]))
        self.assertEqual([x.name for x in out], ["B", "A"])

    def test_volume_does_not_lift_unassigned(self):
        """Regression. Volume must be inert until a human assigns impact, or a
        9.2M-instance /healthz probe outranks a low-volume refund flow — the
        exact failure the ranking rule exists to prevent, displaced one level
        down into the proposed set."""
        out = rank(resolve([
            j(id="probe", name="ZZZ Probe", source="discovered:telemetry", observed_volume=9_200_000),
            j(id="reset", name="AAA Password Reset", source="discovered:code", observed_volume=12),
        ]))
        self.assertEqual([x.name for x in out], ["AAA Password Reset", "ZZZ Probe"])


class TestExclusion(unittest.TestCase):
    def test_excluded_is_detected_with_reason(self):
        out = resolve([j(id="a", name="A", source="discovered:telemetry",
                         excluded=True, excluded_reason="CDN noise")])
        self.assertTrue(out[0].excluded)
        self.assertEqual(out[0].excluded_reason, "CDN noise")

    def test_excluded_drops_from_worklist_and_counts(self):
        payload = to_json(resolve([
            j(id="a", name="A"),
            j(id="b", name="B", source="discovered:telemetry", excluded=True,
              excluded_reason="probe"),
        ]))
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["summary"]["excluded"], 1)

    def test_excluded_still_present_in_json_for_audit(self):
        payload = to_json(resolve([
            j(id="b", name="B", source="discovered:telemetry", excluded=True,
              excluded_reason="probe"),
        ]))
        self.assertTrue(payload["journeys"][0]["excluded"])
        self.assertEqual(payload["journeys"][0]["excluded_reason"], "probe")

    def test_excluded_gets_its_own_report_section(self):
        md = render_markdown(resolve([
            j(id="a", name="A"),
            j(id="b", name="Static Asset Load", source="discovered:telemetry",
              excluded=True, excluded_reason="CDN noise"),
        ]))
        self.assertIn("## Excluded", md)
        self.assertIn("CDN noise", md)

    def test_spec_ready_outranks_blocked_at_equal_impact(self):
        ready = j(id="r", name="Ready", business_impact="important",
                  correlation_key={"attribute": "k"},
                  steps=[{"id": "a"}, {"id": "b"}],
                  outcome={"attribute": "o", "values": ["ok", "no"], "success_values": ["ok"]},
                  failure_reason={"attribute": "why"})
        out = rank(resolve([j(id="b", name="Blocked", business_impact="important"), ready]))
        self.assertEqual(out[0].name, "Ready")


class TestOutputs(unittest.TestCase):
    def test_markdown_names_all_three_statuses(self):
        md = render_markdown(resolve([
            j(id="a", name="A"),
            j(id="a", name="A", source="discovered:code"),
            j(id="b", name="B"),
            j(id="c", name="C", source="discovered:code"),
        ]))
        for token in (CORROBORATED, DECLARED_UNCONFIRMED, PROPOSED, "Worklist"):
            self.assertIn(token, md)

    def test_json_summary_counts(self):
        payload = to_json(resolve([j(id="a", name="A"), j(id="c", name="C", source="discovered:code")]))
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["by_status"][DECLARED_UNCONFIRMED], 1)
        self.assertEqual(payload["summary"]["by_status"][PROPOSED], 1)


class TestEndToEndOnExamples(unittest.TestCase):
    """Runs the CLI against the shipped example inputs."""

    def setUp(self):
        if not (CANDIDATES / "declared.example.yaml").exists():
            self.skipTest("example candidates not present")
        self.out = subprocess.run(
            [sys.executable, str(HERE / "resolve.py"),
             "--declared", str(CANDIDATES / "declared.example.yaml"),
             "--discovered", str(CANDIDATES / "discovered-code.example.json"),
             "--discovered", str(CANDIDATES / "discovered-telemetry.example.json"),
             "--out-json", "/dev/stdout"],
            capture_output=True, text=True,
        )
        if self.out.returncode != 0:
            self.skipTest(f"resolver failed (PyYAML missing?): {self.out.stderr}")
        self.payload = json.loads(self.out.stdout)

    def _find(self, name):
        return next(x for x in self.payload["journeys"] if x["name"] == name)

    def test_checkout_is_corroborated_and_ready(self):
        c = self._find("Checkout")
        self.assertEqual(c["status"], CORROBORATED)
        self.assertTrue(c["spec_ready"], c["blockers"])

    def test_refund_is_the_declared_unconfirmed_finding(self):
        r = self._find("Refund Request")
        self.assertEqual(r["status"], DECLARED_UNCONFIRMED)
        self.assertEqual(r["business_impact"], "critical")

    def test_upgrade_merged_on_correlation_key(self):
        u = self._find("Subscription Upgrade")
        self.assertEqual(u["status"], CORROBORATED)
        self.assertIn("discovered:telemetry", u["sources"])
        self.assertTrue(any("failure reason" in b for b in u["blockers"]))

    def test_health_probe_volume_does_not_lift_it(self):
        """9.2M instances, and it must still sit below every declared journey and
        gain no advantage over its fellow proposed candidates.

        Note what is NOT asserted: that it ranks *last*. The resolver has no way
        to know a /healthz probe is uninteresting — that requires semantics no
        telemetry contains. Ordering noise out is a human act (`excluded: true`),
        which is why exclusion exists.
        """
        active = [x for x in self.payload["journeys"] if not x["excluded"]]
        names = [x["name"] for x in active]
        probe = self._find("Health Probe")
        self.assertEqual(probe["observed_volume"], 9_200_000)

        probe_i = names.index("Health Probe")
        declared = [i for i, x in enumerate(active) if x["status"] != PROPOSED]
        self.assertTrue(all(i < probe_i for i in declared),
                        f"probe outranked a declared journey: {names}")

        others = [x for x in active if x["status"] == PROPOSED and x["name"] != "Health Probe"]
        self.assertTrue(all(x["observed_volume"] is None or x["observed_volume"] < 9_200_000
                            for x in others))

    def test_static_asset_load_is_excluded(self):
        s = self._find("Static Asset Load")
        self.assertTrue(s["excluded"])
        self.assertEqual(self.payload["summary"]["excluded"], 1)
        self.assertNotIn("Static Asset Load",
                         [x["name"] for x in self.payload["journeys"] if not x["excluded"]])

    def test_declared_journeys_rank_above_all_proposed(self):
        statuses = [x["status"] for x in self.payload["journeys"]]
        last_declared = max(i for i, s in enumerate(statuses) if s != PROPOSED)
        first_proposed = min(i for i, s in enumerate(statuses) if s == PROPOSED)
        self.assertLess(last_declared, first_proposed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
