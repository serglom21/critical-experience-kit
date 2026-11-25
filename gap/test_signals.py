#!/usr/bin/env python3
"""Tests for gap/signals.py — the language-agnostic evidence brief."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signals as sig  # noqa: E402


def make_repo(files: dict) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


class TestSignals(unittest.TestCase):
    def test_e2e_spec_names_are_collected_per_dir(self):
        root = make_repo({
            "e2e/features/storage.spec.ts": "test('uploads a file', async () => {})\n",
            "e2e/features/auth-users.spec.ts": "test('creates a user', async () => {})\n"})
        g = sig.collect(root)["e2e_groups"][0]
        self.assertEqual(g["dir"], "e2e/features")
        self.assertEqual(sorted(g["stems"]), ["auth-users", "storage"])

    def test_colocated_unit_tests_are_not_e2e(self):
        """Unit tests under apps/*/components drowned the real e2e suites at first."""
        root = make_repo({"src/billing/plan.test.ts": "it('computes a plan', () => {})\n"})
        s = sig.collect(root)
        self.assertEqual(s["e2e_groups"], [])
        self.assertEqual(s["unit_test_dirs"][0]["dir"], "src/billing")

    def test_features_dir_alone_is_not_e2e(self):
        """`apps/docs/features/` is a source layout convention, not a test suite."""
        root = make_repo({"apps/docs/features/nav.ts": "export const nav = 1\n"})
        self.assertEqual(sig.collect(root)["e2e_groups"], [])

    def test_gherkin_feature_files_count_as_e2e(self):
        root = make_repo({"features/checkout.feature": "Feature: Checkout\n  Scenario: Pay by card\n"})
        self.assertEqual(sig.collect(root)["e2e_groups"][0]["stems"], ["checkout"])

    def test_secrets_and_vendored_dirs_are_never_read(self):
        root = make_repo({
            ".env": "track('leak_event_name')\n",
            "node_modules/x/a.js": "track('vendored_event')\n",
            "src/a.ts": "track('checkout_completed')\n"})
        events = [e["event"] for e in sig.collect(root)["analytics_events"]]
        self.assertEqual(events, ["checkout_completed"])

    def test_analytics_events_found_in_any_language_shape(self):
        root = make_repo({"src/a.ts": "analytics.track('plan_upgraded')\n",
                          "src/b.py": "posthog.capture('invite_sent')\n"})
        events = {e["event"] for e in sig.collect(root)["analytics_events"]}
        self.assertEqual(events, {"plan_upgraded", "invite_sent"})

    def test_i18n_readmes_are_skipped(self):
        root = make_repo({"README.md": "# Product\n## How it works\n",
                          "i18n/README.fr.md": "# Produit\n"})
        self.assertEqual([d["file"] for d in sig.collect(root)["docs"]], ["README.md"])

    def test_noise_dirs_are_not_hot(self):
        body = "stripe.charge(); refund(); invoice(); payment(); billing();\n" * 6
        root = make_repo({"apps/www/pay/a.ts": body, "apps/web/pay/a.ts": body,
                          "src/__mocks__/a.ts": body})
        hot = [h["dir"] for h in sig.collect(root)["hot_dirs"]]
        self.assertIn("apps/web/pay", hot)
        self.assertNotIn("apps/www/pay", hot)
        self.assertFalse(any("__mocks__" in d for d in hot))

    def test_works_on_a_language_propose_cannot_read(self):
        """propose.py fails loudly on Go; signals must still produce evidence."""
        root = make_repo({
            "cmd/server/main.go": "func main() { http.HandleFunc(\"/pay\", pay) }\n",
            "internal/billing/charge.go": "// stripe charge refund invoice payment\n" * 8,
            "e2e/checkout_test.go": "func TestCheckout(t *testing.T) {}\n"})
        s = sig.collect(root)
        self.assertIn("Go", s["languages"])
        self.assertTrue(any(h["entry_points"].get("http_route") for h in s["hot_dirs"]))
        self.assertTrue(s["e2e_groups"])

    def test_brief_is_compact_and_marks_ranking_policy(self):
        root = make_repo({"e2e/a.spec.ts": "test('x y z w', () => {})\n"})
        brief = sig.render_brief(sig.collect(root))
        self.assertIn("NOT ranked by traffic", brief)
        self.assertLess(len(brief), 30_000)


if __name__ == "__main__":
    unittest.main()
