#!/usr/bin/env python3
"""Tests for the before/after visibility diff."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diff import comparability, diff_journey, render_markdown, to_json  # noqa: E402

HERE = Path(__file__).parent
BEFORE = HERE / "example-gap.json"
AFTER = HERE / "example-gap-after.json"


def finding(rule, passed, entity=None, impact="normal", detail="") -> dict:
    return {"rule": rule, "entity": entity, "passed": passed, "impact": impact,
            "detail": detail, "description": f"{rule} desc",
            "rationale": f"{rule} rationale", "extent": None, "example": None}


def journey(findings, **over) -> dict:
    j = {"id": "checkout", "name": "Checkout", "score": 50.0, "grade": "needs improvement",
         "coverage_state": "partial", "steps_instrumented": 3, "steps_total": 5,
         "dark_segments": [], "findings": findings}
    j.update(over)
    return j


def snap(journeys, **over) -> dict:
    d = {"org": "acme", "stats_period": "30d", "traces_sample_rate": 0.25,
         "low_confidence": False, "journeys": journeys}
    d.update(over)
    return d


class TestRuleClassification(unittest.TestCase):
    def test_pass_to_fail_is_a_regression(self):
        d = diff_journey(journey([finding("CE-003", True, "a")]),
                         journey([finding("CE-003", False, "a")]))
        self.assertEqual(len(d.regressed), 1)
        self.assertEqual(d.resolved, [])

    def test_fail_to_pass_is_resolved(self):
        d = diff_journey(journey([finding("CE-003", False, "a")]),
                         journey([finding("CE-003", True, "a")]))
        self.assertEqual(len(d.resolved), 1)
        self.assertEqual(d.regressed, [])

    def test_fail_to_fail_is_still_failing(self):
        d = diff_journey(journey([finding("CE-003", False, "a")]),
                         journey([finding("CE-003", False, "a")]))
        self.assertEqual(len(d.still_failing), 1)

    def test_pass_to_pass_is_silent(self):
        d = diff_journey(journey([finding("CE-003", True, "a")]),
                         journey([finding("CE-003", True, "a")]))
        self.assertEqual((d.resolved, d.regressed, d.still_failing), ([], [], []))

    def test_same_rule_different_entities_do_not_collide(self):
        """CE-003 fires once per step; keying on rule alone would conflate them."""
        d = diff_journey(
            journey([finding("CE-003", True, "step1"), finding("CE-003", False, "step2")]),
            journey([finding("CE-003", False, "step1"), finding("CE-003", True, "step2")]),
        )
        self.assertEqual(len(d.regressed), 1)
        self.assertEqual(len(d.resolved), 1)
        self.assertEqual(d.regressed[0].entity, "step1")

    def test_new_rule_is_newly_measured_not_a_regression(self):
        d = diff_journey(journey([]), journey([finding("CE-010", False, "x")]))
        self.assertEqual(d.regressed, [])
        self.assertEqual(d.newly_measured, ["CE-010 (x)"])

    def test_disappeared_rule_is_reported(self):
        d = diff_journey(journey([finding("CE-010", False, "x")]), journey([]))
        self.assertEqual(d.no_longer_measured, ["CE-010 (x)"])

    def test_changes_sorted_by_impact(self):
        d = diff_journey(
            journey([finding("CE-A", True, impact="normal"),
                     finding("CE-B", True, impact="critical")]),
            journey([finding("CE-A", False, impact="normal"),
                     finding("CE-B", False, impact="critical")]),
        )
        self.assertEqual([c.impact for c in d.regressed], ["critical", "normal"])


class TestCoverageAndScore(unittest.TestCase):
    def test_score_delta(self):
        d = diff_journey(journey([], score=50.0), journey([], score=92.1))
        self.assertEqual(d.score_delta, 42.1)

    def test_coverage_direction(self):
        up = diff_journey(journey([], coverage_state="absent"),
                          journey([], coverage_state="partial"))
        down = diff_journey(journey([], coverage_state="complete"),
                            journey([], coverage_state="partial"))
        flat = diff_journey(journey([]), journey([]))
        self.assertEqual(up.coverage_direction, "improved")
        self.assertEqual(down.coverage_direction, "regressed")
        self.assertEqual(flat.coverage_direction, "unchanged")

    def test_regression_wins_the_headline_over_improvement(self):
        """A journey that improved coverage AND regressed a rule must lead with
        the regression."""
        d = diff_journey(
            journey([finding("CE-003", True, "a")], coverage_state="absent"),
            journey([finding("CE-003", False, "a")], coverage_state="partial"),
        )
        self.assertIn("regression", d.headline)

    def test_dark_segment_transitions(self):
        d = diff_journey(journey([], dark_segments=[["b"]]),
                         journey([], dark_segments=[["c"]]))
        self.assertEqual(d.dark_before, [["b"]])
        self.assertEqual(d.dark_after, [["c"]])


class TestComparability(unittest.TestCase):
    def test_clean_snapshots_have_no_warnings(self):
        self.assertEqual(comparability(snap([]), snap([])), [])

    def test_different_orgs_flagged(self):
        w = comparability(snap([]), snap([], org="other"))
        self.assertTrue(any("different orgs" in x for x in w))

    def test_different_windows_flagged(self):
        w = comparability(snap([]), snap([], stats_period="7d"))
        self.assertTrue(any("different windows" in x for x in w))

    def test_material_sample_rate_change_flagged(self):
        w = comparability(snap([]), snap([], traces_sample_rate=0.05))
        self.assertTrue(any("sample rate changed" in x for x in w))

    def test_small_sample_rate_change_tolerated(self):
        self.assertEqual(comparability(snap([]), snap([], traces_sample_rate=0.3)), [])

    def test_one_sided_sample_rate_flagged(self):
        w = comparability(snap([]), snap([], traces_sample_rate=None))
        self.assertTrue(any("only one side" in x for x in w))

    def test_low_confidence_propagates(self):
        w = comparability(snap([]), snap([], low_confidence=True))
        self.assertTrue(any("low-confidence" in x for x in w))


class TestReporting(unittest.TestCase):
    def test_regressions_appear_before_the_scoreboard(self):
        before = snap([journey([finding("CE-003", True, "a")])])
        after = snap([journey([finding("CE-003", False, "a")])])
        md = render_markdown([diff_journey(before["journeys"][0], after["journeys"][0])],
                             before, after, [], [])
        self.assertLess(md.index("## Regressions"), md.index("## Journeys"))

    def test_regression_carries_its_rationale(self):
        before = snap([journey([finding("CE-003", True, "a")])])
        after = snap([journey([finding("CE-003", False, "a")])])
        md = render_markdown([diff_journey(before["journeys"][0], after["journeys"][0])],
                             before, after, [], [])
        self.assertIn("CE-003 rationale", md)

    def test_no_regression_section_when_clean(self):
        before = snap([journey([finding("CE-003", False, "a")])])
        after = snap([journey([finding("CE-003", True, "a")])])
        md = render_markdown([diff_journey(before["journeys"][0], after["journeys"][0])],
                             before, after, [], [])
        self.assertNotIn("## Regressions", md)

    def test_comparability_warnings_rendered(self):
        before, after = snap([journey([])]), snap([journey([])], stats_period="7d")
        md = render_markdown([diff_journey(before["journeys"][0], after["journeys"][0])],
                             before, after, [], [])
        self.assertIn("Comparability caveats", md)

    def test_json_summary(self):
        before = snap([journey([finding("CE-003", True, "a"), finding("CE-007", False)])])
        after = snap([journey([finding("CE-003", False, "a"), finding("CE-007", True)])])
        d = to_json([diff_journey(before["journeys"][0], after["journeys"][0])],
                    before, after, [], [])
        self.assertEqual(d["summary"]["resolved"], 1)
        self.assertEqual(d["summary"]["regressed"], 1)


class TestCli(unittest.TestCase):
    def setUp(self):
        if not (BEFORE.exists() and AFTER.exists()):
            self.skipTest("run analyze.py on both fixtures first")

    def _run(self, *extra):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "diff.json"
            r = subprocess.run(
                [sys.executable, str(HERE / "diff.py"),
                 "--baseline", str(BEFORE), "--current", str(AFTER),
                 "--out-json", str(out), *extra],
                capture_output=True, text=True)
            payload = json.loads(out.read_text()) if out.exists() else None
            return r, payload

    def test_end_to_end_on_the_paired_fixtures(self):
        r, payload = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(payload["summary"]["resolved"], 8)
        self.assertEqual(payload["summary"]["regressed"], 1)

    def test_checkout_improvement_and_regression_both_reported(self):
        _, payload = self._run()
        c = next(j for j in payload["journeys"] if j["id"] == "checkout")
        self.assertEqual(c["score_before"], 53.3)
        self.assertEqual(c["score_after"], 92.1)
        self.assertGreater(c["score_delta"], 38)
        self.assertEqual(c["grade_after"], "excellent")
        self.assertEqual([x["entity"] for x in c["regressed"]],
                         ["checkout.shipping_submitted"])

    def test_drift_fix_shows_as_resolved(self):
        _, payload = self._run()
        c = next(j for j in payload["journeys"] if j["id"] == "checkout")
        rules = {x["rule"] for x in c["resolved"]}
        self.assertIn("CE-013", rules, "the rename should register as resolved")
        self.assertIn("CE-002", rules, "the terminal span should register as resolved")

    def test_dark_segment_closed_and_opened(self):
        _, payload = self._run()
        c = next(j for j in payload["journeys"] if j["id"] == "checkout")
        self.assertEqual(c["dark_segments_before"], [["payment_authorized"]])
        self.assertEqual(c["dark_segments_after"], [["shipping_submitted"]])

    def test_fail_on_regression_exits_3(self):
        r, _ = self._run("--fail-on-regression")
        self.assertEqual(r.returncode, 3)

    def test_no_common_journeys_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.json"
            a.write_text(json.dumps(snap([journey([], id="only_here")])))
            b = Path(d) / "b.json"
            b.write_text(json.dumps(snap([journey([], id="only_there")])))
            r = subprocess.run(
                [sys.executable, str(HERE / "diff.py"),
                 "--baseline", str(a), "--current", str(b)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)

    def test_reversing_the_snapshots_inverts_the_verdict(self):
        """Sanity check that the direction is real and not an artefact."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "rev.json"
            subprocess.run(
                [sys.executable, str(HERE / "diff.py"),
                 "--baseline", str(AFTER), "--current", str(BEFORE),
                 "--out-json", str(out)], capture_output=True, text=True)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["summary"]["regressed"], 8)
            self.assertEqual(payload["summary"]["resolved"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
