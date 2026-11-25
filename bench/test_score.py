#!/usr/bin/env python3
"""Tests for bench/score.py. The scorer is the measuring stick, so it gets tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score  # noqa: E402

TRUTH = {"journeys": [
    {"id": "storage", "tier": "e2e", "aliases": ["storage", "bucket"], "anchors": ["apps/studio/Storage"]},
    {"id": "billing", "tier": "code", "aliases": ["billing"], "anchors": ["apps/studio/Billing"]},
    {"id": "logs", "tier": "e2e", "aliases": ["log"], "anchors": ["apps/studio/Logs"]},
]}


def prop(jid, *files):
    return {"id": jid, "name": jid, "steps": [{"id": "s", "evidence": f"x — {f}:1 fn"} for f in files]}


class TestScore(unittest.TestCase):
    def test_paths_need_an_extension(self):
        """"connect/disconnect events" in prose was scored as a missing file."""
        self.assertEqual(score.paths_in("device connect/disconnect events"), [])
        self.assertEqual(score.paths_in("see ui/pages/a.tsx:42 handler"), ["ui/pages/a.tsx"])

    def test_anchor_match_is_primary(self):
        r = score.score(TRUTH, {"journeys": [prop("misc", "apps/studio/Storage/a.tsx")]}, None, [1])
        self.assertEqual(r["at"][0]["truth_covered"], ["storage"])

    def test_alias_match_on_id_tokens(self):
        r = score.score(TRUTH, {"journeys": [prop("billing_plan")]}, None, [1])
        self.assertEqual(r["at"][0]["truth_covered"], ["billing"])

    def test_alias_must_be_a_whole_token(self):
        """`catalog` must not match the alias `log`."""
        r = score.score(TRUTH, {"journeys": [prop("catalog")]}, None, [1])
        self.assertEqual(r["at"][0]["truth_covered"], [])

    def test_precision_and_recall_at_k(self):
        proposed = {"journeys": [prop("junk", "apps/www/x.ts"), prop("storage", "apps/studio/Storage/a.ts"),
                                 prop("billing", "apps/studio/Billing/b.ts")]}
        r = score.score(TRUTH, proposed, None, [2])
        at2 = r["at"][0]
        self.assertEqual(at2["recall"], round(1 / 3, 3))
        self.assertEqual(at2["precision"], 0.5)
        self.assertEqual(r["at"][-1]["recall"], round(2 / 3, 3))
        self.assertEqual(r["missed_truth"], ["logs"])
        self.assertEqual(r["unmatched_proposals"], ["junk"])

    def test_recall_by_tier(self):
        r = score.score(TRUTH, {"journeys": [prop("billing", "apps/studio/Billing/a.ts")]}, None, [1])
        self.assertEqual(r["at"][0]["recall_by_tier"], {"code": 1.0, "e2e": 0.0})

    def test_evidence_existence_check(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "a").mkdir()
        (repo / "a" / "real.ts").write_text("x")
        r = score.score(TRUTH, {"journeys": [prop("storage", "a/real.ts", "a/fake.ts")]}, repo, [1])
        self.assertEqual(r["evidence_ok"], 0.5)
        self.assertEqual(r["evidence_missing_sample"], ["a/fake.ts"])


if __name__ == "__main__":
    unittest.main()
