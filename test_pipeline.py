#!/usr/bin/env python3
"""
Seam tests for the whole pipeline: intake -> gap -> spec.

Why this file exists separately from the per-layer suites: those suites build
their inputs inline as dicts, so a rename in resolve.py's output shape would
leave every gap test passing while the real pipeline broke. These tests only
use artifacts produced by the previous stage.

Run from the kit root:  python3 -m unittest test_pipeline -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
INTAKE = ROOT / "intake"
GAP = ROOT / "gap"
SPEC = ROOT / "spec"
CANDIDATES = INTAKE / "candidates"
FIXTURE = GAP / "fixtures" / "observed-customer.example.json"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.resolved = d / "resolved.json"
        cls.gap = d / "gap.json"
        cls.specdir = d / "specs"

        r = run("resolve.py",
                "--declared", str(CANDIDATES / "declared.example.yaml"),
                "--discovered", str(CANDIDATES / "discovered-code.example.json"),
                "--discovered", str(CANDIDATES / "discovered-telemetry.example.json"),
                "--out-json", str(cls.resolved), cwd=INTAKE)
        if r.returncode != 0:
            raise unittest.SkipTest(f"intake failed (PyYAML missing?): {r.stderr}")
        cls.resolved_doc = json.loads(cls.resolved.read_text())

        a = run("analyze.py", "--resolved", str(cls.resolved),
                "--observed", str(FIXTURE), "--include-unready",
                "--out-json", str(cls.gap), cwd=GAP)
        assert a.returncode == 0, a.stderr
        cls.gap_doc = json.loads(cls.gap.read_text())

        cls.spec_rc = None
        if (SPEC / "generate.py").exists():
            s = run("generate.py", "--resolved", str(cls.resolved),
                    "--gap", str(cls.gap), "--out-dir", str(cls.specdir), cwd=SPEC)
            cls.spec_rc = s.returncode
            cls.spec_err = s.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ---- intake -> gap contract -----------------------------------------

    def test_resolver_emits_every_field_the_analyzer_reads(self):
        for j in self.resolved_doc["journeys"]:
            for key in ("id", "name", "excluded", "spec_ready", "blockers", "roles"):
                self.assertIn(key, j, f"{j.get('name')} missing {key}")
            for role in ("correlation_key", "steps", "outcome",
                         "failure_reason", "magnitude", "actor_segment"):
                self.assertIn(role, j["roles"], f"{j['name']} roles missing {role}")

    def test_every_non_excluded_stepful_journey_reaches_the_gap_report(self):
        expected = {
            j["name"] for j in self.resolved_doc["journeys"]
            if not j["excluded"] and (j["roles"].get("steps") or [])
        }
        got = {j["name"] for j in self.gap_doc["journeys"]}
        self.assertEqual(expected, got)

    def test_excluded_journeys_never_reach_the_gap_report(self):
        excluded = {j["name"] for j in self.resolved_doc["journeys"] if j["excluded"]}
        self.assertTrue(excluded, "fixture should contain an excluded journey")
        self.assertFalse(excluded & {j["name"] for j in self.gap_doc["journeys"]})

    def test_step_counts_agree_across_the_seam(self):
        declared = {j["name"]: len(j["roles"]["steps"] or [])
                    for j in self.resolved_doc["journeys"]}
        for g in self.gap_doc["journeys"]:
            self.assertEqual(g["steps_total"], declared[g["name"]], g["name"])

    # ---- span-name binding consistency ----------------------------------

    def test_declared_span_names_match_the_registry_exemplar(self):
        """Two artifacts naming the same span differently is the drift the
        Warden skill exists to catch. Don't ship it in our own kit."""
        registry = (ROOT / "registry" / "spans" / "checkout.yaml").read_text()
        checkout = next(j for j in self.resolved_doc["journeys"] if j["id"] == "checkout")
        for step in checkout["roles"]["steps"]:
            name = step.get("span_name")
            self.assertTrue(name, f"step {step['id']} has no explicit span_name")
            self.assertIn(f'sentry_span_name: "{name}"', registry,
                          f"`{name}` is not in registry/spans/checkout.yaml")

    def test_checkout_binding_would_differ_from_the_convention(self):
        """Guards the reason the explicit bindings exist: convention alone would
        derive different names, so dropping span_name silently breaks matching."""
        sys.path.insert(0, str(GAP))
        from analyze import expected_span_name
        checkout = next(j for j in self.resolved_doc["journeys"] if j["id"] == "checkout")
        steps = checkout["roles"]["steps"]
        derived = [expected_span_name("checkout", {"id": s["id"]}, i == 0)
                   for i, s in enumerate(steps)]
        bound = [s["span_name"] for s in steps]
        self.assertNotEqual(derived, bound)

    # ---- findings survive the seam --------------------------------------

    def test_checkout_is_partial_with_the_drift_finding(self):
        c = next(j for j in self.gap_doc["journeys"] if j["id"] == "checkout")
        self.assertEqual(c["coverage_state"], "partial")
        drift = next(f for f in c["findings"] if f["rule"] == "CE-013")
        self.assertFalse(drift["passed"])
        self.assertIn("checkout.payment_authorize", drift["detail"])
        self.assertIn("checkout.payment.authorize", drift["detail"])

    def test_drift_note_quantifies_the_wasted_spans(self):
        c = next(j for j in self.gap_doc["journeys"] if j["id"] == "checkout")
        self.assertTrue(any("43,800" in n for n in c["notes"]), c["notes"])

    def test_every_finding_carries_a_rationale(self):
        for j in self.gap_doc["journeys"]:
            for f in j["findings"]:
                self.assertTrue(f["rationale"].strip(), f"{j['name']} {f['rule']}")

    def test_undeclared_discovered_step_is_surfaced_not_swallowed(self):
        """discovered-code declares payment_authorized etc.; if a discovery pass
        ever contributes a step the customer didn't declare, it must appear as a
        clarification rather than vanish."""
        for j in self.resolved_doc["journeys"]:
            self.assertIn("undeclared_steps", j)
            for sid in j["undeclared_steps"]:
                self.assertTrue(
                    any(sid in q for q in j["needs_clarification"]),
                    f"{j['name']}: undeclared step {sid} not surfaced",
                )

    # ---- intake -> registry contract ------------------------------------

    def test_registry_generates_and_validates_from_resolver_output(self):
        regdir = Path(self.tmp.name) / "registry"
        g = run("generate.py", "--resolved", str(self.resolved), "--out-dir", str(regdir),
                cwd=ROOT / "registry_gen")
        self.assertEqual(g.returncode, 0, g.stderr)
        v = run("validate.py", "--registry", str(regdir), cwd=ROOT / "registry_gen")
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_registry_span_names_match_the_spec_bindings(self):
        """The registry and the spec must agree on every span name, or the
        coverage checker measures one thing and the customer implements another."""
        regdir = Path(self.tmp.name) / "registry2"
        run("generate.py", "--resolved", str(self.resolved), "--out-dir", str(regdir),
            cwd=ROOT / "registry_gen")
        spans = (regdir / "spans" / "checkout.yaml").read_text()
        checkout = next(j for j in self.resolved_doc["journeys"] if j["id"] == "checkout")
        for step in checkout["roles"]["steps"]:
            name = step.get("span_name") or f"checkout.{step['id']}"
            self.assertIn(f'sentry_span_name: "{name}"', spans, name)

    # ---- gap -> spec contract -------------------------------------------

    def test_spec_generation_runs_and_emits_files(self):
        if self.spec_rc is None:
            self.skipTest("spec/generate.py not present yet")
        self.assertEqual(self.spec_rc, 0, self.spec_err)
        produced = sorted(p.name for p in self.specdir.glob("*.md"))
        self.assertTrue(produced, "no spec files written")

    def test_spec_only_asks_for_what_is_actually_missing(self):
        if self.spec_rc is None:
            self.skipTest("spec/generate.py not present yet")
        spec = next(p for p in self.specdir.glob("*checkout*SPEC.md"))
        text = spec.read_text()
        # Present and correct in the fixture — must not be re-requested.
        self.assertNotIn("checkout.shipping_submitted` MUST be created", text)
        # Missing in the fixture — must be requested.
        self.assertIn("checkout.confirmation_viewed", text)

    def test_spec_carries_no_deprecated_api(self):
        if self.spec_rc is None:
            self.skipTest("spec/generate.py not present yet")
        banned = ["startTransaction(", "startChild(", ".setData(", ".finish()",
                  "configureScope(", "getCurrentHub("]
        for p in self.specdir.glob("*SPEC.md"):
            text = p.read_text()
            body = text.split("## 6.")[0]  # section 6 is the do-not-use table
            for token in banned:
                self.assertNotIn(token, body, f"{p.name} uses {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
