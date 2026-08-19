#!/usr/bin/env python3
"""
Tests for the static code scan and the three starting states.

The kit has to work equally well on:
  A. Sentry installed, no journey coverage
  B. No Sentry at all
  C. Some custom instrumentation, improvable

Each state used to fail differently, so each gets explicit coverage here.
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

from analyze import Observed, analyze_journey  # noqa: E402
from code_scan import build, guess_type, scan_js, scan_python, _is_secret_path, _iter_files, JS_SUFFIX, PY_SUFFIX  # noqa: E402
from instrumentation_profile import classify  # noqa: E402


def repo(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp())
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return d


PY_INSTRUMENTED = """
    import os, sentry_sdk
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0)

    def run(cart, i):
        with sentry_sdk.start_transaction(op="ui.action", name="checkout") as t:
            t.set_data("checkout.id", f"c{i}")
            t.set_data("checkout.value", str(cart["total"]))
            with sentry_sdk.start_span(op="function", name="checkout.submitted") as s:
                s.set_data("checkout.step", "submitted")
"""

JS_INSTRUMENTED = """
    import * as Sentry from "@sentry/node";
    Sentry.init({ dsn: process.env.SENTRY_DSN, tracesSampleRate: 1.0 });
    export function run(cart) {
      return Sentry.startSpan(
        { name: "checkout", op: "ui.action", attributes: { "checkout.id": "c1", "cart.value": cart.total } },
        () => Sentry.startSpan({ name: "checkout.order.create", op: "function" }, () => {}),
      );
    }
"""


class TestSdkDetection(unittest.TestCase):
    def test_comma_separated_python_import_detected(self):
        """`import os, sentry_sdk` broke a regex that only matched
        `import sentry_sdk`, reporting a fully instrumented service as having none."""
        _, _, imported, init = scan_python(repo({"a.py": PY_INSTRUMENTED}))
        self.assertTrue(imported)
        self.assertTrue(init)

    def test_skips_dotenv_disguised_as_python(self):
        """A scan that opened `.env.py` would be how a customer loses trust."""
        d = repo({".env.py": "import sentry_sdk\nsentry_sdk.init()\n",
                  "app.py": "x = 1\n"})
        doc = build(d, "t")
        self.assertFalse(doc["sdk"]["any_sdk_present"])

    def test_no_sdk_reports_absent(self):
        d = repo({"a.py": "def f():\n    return 1\n", "b.ts": "export const x = 1;\n"})
        doc = build(d, "t")
        self.assertFalse(doc["sdk"]["any_sdk_present"])
        self.assertFalse(doc["sdk"]["any_sdk_initialised"])
        self.assertEqual(doc["source_languages"], ["javascript", "python"])

    def test_init_alone_counts_as_present(self):
        """An import can be aliased or re-exported past a regex; an init call cannot
        be anything other than proof the SDK is there."""
        doc = build(repo({"a.py": 'sentry_sdk.init(dsn="x")\n'}), "t")
        self.assertTrue(doc["sdk"]["any_sdk_present"])

    def test_javascript_detected(self):
        doc = build(repo({"a.ts": JS_INSTRUMENTED}), "t")
        self.assertTrue(doc["sdk"]["javascript"]["imported"])
        self.assertTrue(doc["sdk"]["javascript"]["initialised"])

    def test_skips_secret_files(self):
        """Opening `.env` or a PEM during scan is how a customer loses trust.
        The first version walked every suffix and would have read them."""
        self.assertTrue(_is_secret_path(Path(".env")))
        self.assertTrue(_is_secret_path(Path("credentials.json")))
        self.assertTrue(_is_secret_path(Path("key.pem")))
        self.assertFalse(_is_secret_path(Path("src/checkout/routes.ts")))
        d = repo({
            ".env/leak.py": "import sentry_sdk\nsentry_sdk.init()\n",
            "src/checkout.py": "def start():\n    return 1\n",
        })
        walked = {p.name for p in _iter_files(d, JS_SUFFIX | PY_SUFFIX)}
        self.assertIn("checkout.py", walked)
        self.assertNotIn("leak.py", walked)


class TestScanExtraction(unittest.TestCase):
    def test_python_spans_and_ops(self):
        names, attrs, _, _ = scan_python(repo({"a.py": PY_INSTRUMENTED}))
        self.assertEqual(names, {"checkout": "ui.action", "checkout.submitted": "function"})
        self.assertIn("checkout.id", attrs)
        self.assertIn("checkout.value", attrs)

    def test_javascript_spans_and_ops(self):
        names, attrs, _, _ = scan_js(repo({"a.ts": JS_INSTRUMENTED}))
        self.assertEqual(names.get("checkout"), "ui.action")
        self.assertEqual(names.get("checkout.order.create"), "function")
        self.assertIn("cart.value", attrs)

    def test_both_languages_merge(self):
        doc = build(repo({"a.py": PY_INSTRUMENTED, "b.ts": JS_INSTRUMENTED}), "t")
        found = {s["name"] for s in doc["span_names"]}
        self.assertTrue({"checkout", "checkout.submitted", "checkout.order.create"} <= found)

    def test_tests_and_vendor_dirs_skipped(self):
        d = repo({
            "src/real.py": PY_INSTRUMENTED,
            "test_thing.py": 'sentry_sdk.start_span(name="nope.test")\n',
            "node_modules/x.ts": 'Sentry.startSpan({name:"nope.vendor"})\n',
        })
        found = {s["name"] for s in build(d, "t")["span_names"]}
        self.assertNotIn("nope.test", found)
        self.assertNotIn("nope.vendor", found)


class TestTypeHonesty(unittest.TestCase):
    def test_literals_are_claimed(self):
        self.assertEqual(guess_type(["129.99"]), "number")
        self.assertEqual(guess_type(['"abandoned"']), "string")
        self.assertEqual(guess_type(["True"]), "boolean")

    def test_stringifiers_detected(self):
        for v in ["str(x)", "x.toFixed(2)", "String(x)", 'f"{x}"']:
            self.assertEqual(guess_type([v]), "string", v)

    def test_unprovable_values_are_flagged_not_guessed(self):
        doc = build(repo({"a.py": PY_INSTRUMENTED}), "t")
        self.assertIn("checkout.id", doc["unprovable_types"])
        rec = next(a for a in doc["attributes"] if a["key"] == "checkout.id")
        self.assertEqual(rec["type_from_source"], "unprovable")


class TestSyntheticCounts(unittest.TestCase):
    def test_counts_are_marked_synthetic(self):
        doc = build(repo({"a.py": PY_INSTRUMENTED}), "t")
        self.assertTrue(doc["_synthetic_counts"])
        self.assertTrue(all(s["count"] == 1 for s in doc["span_names"]))

    def test_analyzer_suppresses_fabricated_extent(self):
        """With every count at 1, an extent percentage would be invented."""
        doc = build(repo({"a.py": PY_INSTRUMENTED}), "t")
        journey = {
            "id": "checkout", "name": "Checkout", "spec_ready": True, "excluded": False,
            "blockers": [], "roles": {
                "correlation_key": {"attribute": "checkout.id"},
                "steps": [{"id": "a", "span_name": "checkout"},
                          {"id": "b", "span_name": "checkout.missing"}],
                "outcome": {"attribute": "checkout.outcome", "values": ["ok", "no"],
                            "success_values": ["ok"]},
            }}
        g = analyze_journey(journey, Observed(doc))
        self.assertTrue(all(f.extent is None for f in g.findings))

    def test_real_counts_still_produce_extent(self):
        obs = Observed({"span_names": [{"name": "checkout", "count": 100},
                                       {"name": "checkout.b", "count": 40}],
                        "attributes": []})
        journey = {
            "id": "checkout", "name": "Checkout", "roles": {
                "steps": [{"id": "a", "span_name": "checkout"},
                          {"id": "b", "span_name": "checkout.b"}]}}
        g = analyze_journey(journey, obs)
        self.assertTrue(any(f.extent for f in g.findings))


class TestProfileUsesOpNotName(unittest.TestCase):
    def test_sql_span_name_is_not_a_business_span(self):
        """`SELECT * FROM carts` read as custom business instrumentation and flipped
        an auto-only org out of the `automatic only` tier — the strongest line in
        the report for an uninstrumented service."""
        observed = {
            "org": "t", "stats_period": "runtime",
            "span_ops": [{"op": "http.server", "count": 4}, {"op": "db.query", "count": 4}],
            "span_pairs": [{"name": "POST /checkout", "op": "http.server", "count": 4},
                           {"name": "SELECT * FROM carts", "op": "db.query", "count": 4}],
            "span_names": [{"name": "POST /checkout", "count": 4},
                           {"name": "SELECT * FROM carts", "count": 4}],
            "attributes": [],
        }
        p = classify(observed)
        self.assertEqual(p.custom_spans, [])
        self.assertEqual(p.tier, "automatic only")

    def test_business_span_still_recognised_when_op_is_generic(self):
        observed = {
            "org": "t", "span_ops": [{"op": "function", "count": 2}],
            "span_pairs": [{"name": "checkout", "op": "function", "count": 2}],
            "span_names": [{"name": "checkout", "count": 2}],
            "attributes": [{"key": "checkout.id", "attributeType": "string",
                            "attributeSource": {"source_type": "user"}}],
        }
        p = classify(observed)
        self.assertEqual([n for n, _ in p.custom_spans], ["checkout"])
        self.assertEqual(p.tier, "custom instrumentation present")

    def test_falls_back_to_name_when_pairs_absent(self):
        observed = {"org": "t", "span_names": [{"name": "http.server", "count": 9}],
                    "attributes": []}
        p = classify(observed)
        self.assertEqual(p.custom_spans, [])


class TestThreeStatesEndToEnd(unittest.TestCase):
    """Each state must produce a usable report, not an error."""

    KIT = HERE.parent

    def _pipeline(self, files: dict[str, str], extra_spec: list[str] | None = None):
        d = repo(files)
        ce = [sys.executable, str(self.KIT / "cli.py")]
        subprocess.run(ce + ["init", "--out", str(d), "--journey-id", "checkout"],
                       capture_output=True, check=True)
        subprocess.run(ce + ["intake", "--declared", str(d / "journeys.yaml"),
                             "--out-json", str(d / "resolved.json")],
                       capture_output=True, check=True)
        subprocess.run(ce + ["scan", "--repo", str(d), "--out", str(d / "observed.json"),
                             "--quiet"], capture_output=True, check=True)
        r = subprocess.run(ce + ["gap", "--resolved", str(d / "resolved.json"),
                                 "--observed", str(d / "observed.json"),
                                 "--include-unready", "--out-md", str(d / "gap.md"),
                                 "--out-json", str(d / "gap.json")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = subprocess.run(ce + ["spec", "--resolved", str(d / "resolved.json"),
                                 "--gap", str(d / "gap.json"), "--out-dir", str(d / "specs"),
                                 "--rubric", *(extra_spec or [])],
                           capture_output=True, text=True)
        return d, json.loads((d / "gap.json").read_text()), (d / "gap.md").read_text(), s

    def test_state_b_no_sdk_produces_a_report_and_a_spec(self):
        d, gap, md, spec = self._pipeline(
            {"src/checkout.py": "def start(cart):\n    return cart\n"},
            extra_spec=["--include-absent"])
        self.assertIn("No Sentry SDK found", md)
        self.assertIn("Source-derived", md)
        self.assertEqual(spec.returncode, 0, spec.stderr)
        self.assertTrue((d / "specs" / "checkout-RUBRIC.json").exists())
        j = gap["journeys"][0]
        self.assertEqual(j["coverage_state"], "absent")

    def test_state_c_partial_instrumentation_asks_only_for_the_delta(self):
        d, gap, md, spec = self._pipeline({"src/checkout.py": PY_INSTRUMENTED})
        self.assertNotIn("No Sentry SDK found", md)
        self.assertEqual(spec.returncode, 0, spec.stderr)
        rubric = json.loads((d / "specs" / "checkout-RUBRIC.json").read_text())
        guarded = {g["check"].get("span") or g["check"].get("attribute")
                   for g in rubric["guards"]}
        asked = {q["check"].get("span") or q["check"].get("attribute")
                 for q in rubric["requirements"] if q["check"]}
        self.assertIn("checkout", guarded, "existing span must be guarded, not re-asked")
        self.assertNotIn("checkout", asked)

    def test_state_c_is_partial_not_absent(self):
        _, gap, _, _ = self._pipeline({"src/checkout.py": PY_INSTRUMENTED})
        self.assertEqual(gap["journeys"][0]["coverage_state"], "partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
