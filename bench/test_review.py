#!/usr/bin/env python3
"""Tests for bench/review.py (post-run human review of explorer output)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import review as rv  # noqa: E402

VALIDATED = {"journeys": [
    {"id": "sign_up", "name": "Sign up", "why": "identity", "confidence": "high",
     "steps": [{"id": "a", "description": "submit", "evidence": [
         {"file": "api/signup.go", "line": 10, "status": "verified"},
         {"file": "api/bogus.go", "status": "missing_file"}]}]},
    {"id": "theme", "name": "Change theme", "why": "", "confidence": "low", "steps": []},
], "report": {"accounting": {"ok": 3, "total": 4}}}


class TestReview(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "out").mkdir()
        (self.d / "out" / "discovery.validated.json").write_text(json.dumps(VALIDATED))
        self.sheet = self.d / "sheet.yaml"

    def test_make_lists_only_verified_citations_and_blank_verdicts(self):
        self.assertEqual(rv.make(self.d / "out", self.sheet), 0)
        doc = yaml.safe_load(self.sheet.read_text())
        self.assertEqual([j["id"] for j in doc["journeys"]], ["sign_up", "theme"])
        self.assertIn("api/signup.go:10", doc["journeys"][0]["steps"][0])
        self.assertNotIn("bogus", doc["journeys"][0]["steps"][0])
        self.assertTrue(all(j["verdict"] == "" for j in doc["journeys"]))
        self.assertEqual(doc["e2e_coverage"], "3 of 4 test names")

    def test_make_refuses_to_overwrite_a_review_in_progress(self):
        rv.make(self.d / "out", self.sheet)
        self.assertEqual(rv.make(self.d / "out", self.sheet), 2)

    def test_score(self):
        rv.make(self.d / "out", self.sheet)
        doc = yaml.safe_load(self.sheet.read_text())
        doc["journeys"][0].update(verdict="critical", steps_ok="right")
        doc["journeys"][1].update(verdict="wrong", steps_ok="wrong")
        doc["missing"] = [{"name": "MFA", "where": "api/mfa.go", "severity": "critical"},
                          {"name": "Avatar", "severity": "minor"}]
        r = rv.score_one(doc)
        self.assertEqual((r["critical"], r["wrong"], r["unfilled"]), (1, 1, 0))
        self.assertEqual(r["precision_strict"], 0.5)
        self.assertEqual(r["missing_critical"], 1)
        self.assertEqual(r["found_of_known_critical"], 0.5)

    def test_unfilled_is_counted(self):
        rv.make(self.d / "out", self.sheet)
        self.assertEqual(rv.score_one(yaml.safe_load(self.sheet.read_text()))["unfilled"], 2)


if __name__ == "__main__":
    unittest.main()
