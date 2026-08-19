#!/usr/bin/env python3
"""
Tests for the static grader and the eval runner.

The golden solutions carry most of the weight here: a grader you cannot show
discriminating between a correct implementation and five specific wrong ones is
just a script that prints numbers.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from grade import (  # noqa: E402
    _split_top_level,
    _value_verdict,
    grade,
    run_check,
    scan_repo,
    strip_comments,
)

RUBRIC = HERE.parent / "spec" / "out" / "checkout-RUBRIC.json"
TASK = HERE / "tasks" / "checkout-js"


def sol(name: str) -> Path:
    return TASK / "solutions" / name


class TestStripComments(unittest.TestCase):
    def test_line_comment_blanked_length_preserved(self):
        src = 'const a = 1; // note\nconst b = 2;'
        out = strip_comments(src)
        self.assertEqual(len(out), len(src))
        self.assertNotIn("note", out)
        self.assertIn("const b = 2;", out)

    def test_block_comment_blanked(self):
        out = strip_comments("a /* hide\nme */ b")
        self.assertNotIn("hide", out)
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_url_inside_string_survives(self):
        """A naive stripper eats `https://` and corrupts the source."""
        src = 'const u = "https://example.com/x"; // gone'
        out = strip_comments(src)
        self.assertIn("https://example.com/x", out)
        self.assertNotIn("gone", out)

    def test_comment_between_object_properties_does_not_hide_the_next_one(self):
        """The regression this function exists for: a `// BUG:` line above
        `"cart.value": ...` made the attribute read as never set."""
        code = '''
        Sentry.startSpan({ name: "x", attributes: {
          "a.one": 1,
          // explanatory comment
          "a.two": 2,
        }});'''
        s = scan_repo_from_string(code)
        self.assertIn("a.two", s.attributes)


def scan_repo_from_string(code: str):
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "x.ts").write_text(code)
    return scan_repo(d)


class TestScanner(unittest.TestCase):
    def test_span_names_from_all_starter_variants(self):
        s = scan_repo_from_string('''
        Sentry.startSpan({ name: "a" }, () => {});
        Sentry.startSpanManual({ name: "b" }, () => {});
        Sentry.startInactiveSpan({ name: "c" });''')
        self.assertEqual(s.span_names, {"a", "b", "c"})

    def test_attributes_from_setAttribute_and_object_forms(self):
        s = scan_repo_from_string('''
        span.setAttribute("x.one", 1);
        span.setAttributes({ "x.two": 2 });
        Sentry.startSpan({ name: "n", attributes: { "x.three": 3 } }, () => {});''')
        self.assertEqual(set(s.attributes), {"x.one", "x.two", "x.three"})

    def test_nested_object_does_not_break_pair_splitting(self):
        s = scan_repo_from_string(
            'span.setAttributes({ "a": f({x: 1, y: 2}), "b": [1, 2] });')
        self.assertEqual(set(s.attributes), {"a", "b"})

    def test_test_files_and_node_modules_skipped(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "a.test.ts").write_text('Sentry.startSpan({ name: "nope" });')
        (d / "node_modules").mkdir()
        (d / "node_modules" / "b.ts").write_text('Sentry.startSpan({ name: "nope2" });')
        (d / "real.ts").write_text('Sentry.startSpan({ name: "yes" });')
        s = scan_repo(d)
        self.assertEqual(s.span_names, {"yes"})


class TestValueVerdict(unittest.TestCase):
    def test_string_literals_and_stringifiers(self):
        for v in ['"129.99"', "'x'", "`${a}`", "String(n)", "n.toFixed(2)",
                  "n.toString()", "JSON.stringify(o)"]:
            self.assertEqual(_value_verdict([v]), "string", v)

    def test_numeric_forms(self):
        for v in ["129.99", "-3", "Number(x)", "parseFloat(x)"]:
            self.assertEqual(_value_verdict([v]), "numeric", v)

    def test_boolean(self):
        self.assertEqual(_value_verdict(["true"]), "boolean")

    def test_property_access_is_unresolved_not_a_failure(self):
        """`cart.total` is how nearly every real numeric attribute looks. Treating
        it as indeterminate made a perfect solution score 7/9."""
        self.assertEqual(_value_verdict(["cart.total"]), "unresolved")
        self.assertEqual(_value_verdict(["computeTotal(cart)"]), "unresolved")

    def test_worst_verdict_wins_across_call_sites(self):
        self.assertEqual(_value_verdict(["cart.total", "cart.total.toFixed(2)"]), "string")

    def test_split_top_level_respects_nesting_and_strings(self):
        self.assertEqual(len(_split_top_level('a: 1, b: f(1, 2), c: "x,y"')), 3)


class TestChecks(unittest.TestCase):
    def test_span_renamed_detects_duplication(self):
        s = scan_repo_from_string('''
        Sentry.startSpan({ name: "new.name" });
        Sentry.startSpan({ name: "old.name" });''')
        passed, detail = run_check(
            {"kind": "span_renamed", "to": "new.name", "from": "old.name"}, s)
        self.assertFalse(passed)
        self.assertIn("duplicated", detail)

    def test_span_renamed_passes_when_old_is_gone(self):
        s = scan_repo_from_string('Sentry.startSpan({ name: "new.name" });')
        passed, _ = run_check(
            {"kind": "span_renamed", "to": "new.name", "from": "old.name"}, s)
        self.assertTrue(passed)

    def test_attribute_key_exact_flags_coexisting_near_miss(self):
        """Repo-wide scanning means one file's typo hides behind other files that
        spell the key correctly. Coexistence is the defect."""
        s = scan_repo_from_string('''
        span.setAttribute("checkout.id", a);
        span.setAttribute("checkoutId", a);''')
        passed, detail = run_check(
            {"kind": "attribute_key_exact", "attribute": "checkout.id"}, s)
        self.assertFalse(passed)
        self.assertIn("near-miss", detail)

    def test_attribute_key_exact_passes_when_only_correct_key_present(self):
        s = scan_repo_from_string('span.setAttribute("checkout.id", a);')
        passed, _ = run_check(
            {"kind": "attribute_key_exact", "attribute": "checkout.id"}, s)
        self.assertTrue(passed)

    def test_pii_detected_in_key_and_value(self):
        s = scan_repo_from_string('span.setAttribute("user.email", u.email);')
        passed, _ = run_check({"kind": "no_pii"}, s)
        self.assertFalse(passed)

    def test_deprecated_api_detected(self):
        s = scan_repo_from_string('span.setData("k", v);')
        passed, detail = run_check({"kind": "no_deprecated_api"}, s)
        self.assertFalse(passed)
        self.assertIn("setData", detail)

    def test_unknown_check_kind_is_indeterminate_not_a_pass(self):
        passed, _ = run_check({"kind": "no_such_check"}, scan_repo_from_string(""))
        self.assertIsNone(passed)


@unittest.skipUnless(RUBRIC.exists(), "run spec/generate.py --rubric first")
class TestGoldenSolutions(unittest.TestCase):
    """The whole point: prove the grader discriminates."""

    @classmethod
    def setUpClass(cls):
        cls.rubric = json.loads(RUBRIC.read_text())

    def g(self, path: Path) -> dict:
        return grade(self.rubric, scan_repo(path))

    def test_before_scores_low_and_is_not_clean(self):
        r = self.g(TASK / "before")
        self.assertFalse(r["clean"])
        self.assertLess(r["score"], 40)
        self.assertGreaterEqual(r["failed"], 6)

    def test_correct_is_clean_and_100(self):
        r = self.g(sol("correct"))
        self.assertTrue(r["clean"], [x for x in r["results"] if x["status"] != "pass"])
        self.assertEqual(r["score"], 100.0)
        self.assertEqual(r["guard_failures"], 0)

    def test_attribute_typo_passes_requirements_but_is_not_clean(self):
        """The sharpest case: 100% on requirements, still broken. Without guards
        this scored a clean 100%."""
        r = self.g(sol("attribute-typo"))
        self.assertEqual(r["score"], 100.0)
        self.assertFalse(r["clean"])
        self.assertEqual(r["guard_failures"], 1)
        self.assertTrue(any("checkout.id" in (g["target"] or "")
                            for g in r["guards"] if g["status"] == "fail"))

    def test_duplicated_span_caught(self):
        r = self.g(sol("duplicated-span"))
        f = [x for x in r["results"] if x["status"] == "fail"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["check"], "span_renamed")

    def test_deprecated_api_caught(self):
        r = self.g(sol("deprecated-api"))
        f = [x for x in r["results"] if x["status"] == "fail"]
        self.assertEqual([x["check"] for x in f], ["no_deprecated_api"])

    def test_pii_caught(self):
        r = self.g(sol("pii"))
        f = [x for x in r["results"] if x["status"] == "fail"]
        self.assertEqual([x["check"] for x in f], ["no_pii"])

    def test_stringified_magnitude_caught(self):
        r = self.g(sol("indeterminate"))   # legacy dir name; see solutions/README.md
        f = [x for x in r["results"] if x["status"] == "fail"]
        self.assertEqual(len(f), 1)
        self.assertIn("cannot be aggregated", f[0]["detail"])

    def test_every_variant_has_a_distinct_verdict(self):
        names = ["correct", "attribute-typo", "duplicated-span", "deprecated-api",
                 "pii", "indeterminate"]
        sigs = set()
        for n in names:
            r = self.g(sol(n))
            sigs.add((r["score"], r["guard_failures"],
                      tuple(sorted(x["check"] for x in r["results"]
                                   if x["status"] == "fail"))))
        self.assertEqual(len(sigs), len(names), "variants must be distinguishable")


@unittest.skipUnless(RUBRIC.exists(), "run spec/generate.py --rubric first")
class TestRubricShape(unittest.TestCase):
    def setUp(self):
        self.r = json.loads(RUBRIC.read_text())

    def test_all_requirements_gradeable(self):
        ungraded = [q["id"] for q in self.r["requirements"] if not q["gradeable"]]
        self.assertEqual(ungraded, [], "every requirement should carry a check")

    def test_guards_cover_already_present_spans(self):
        targets = {g["check"].get("span") for g in self.r["guards"]}
        for name in self.r["spans_already_present"]:
            self.assertIn(name, targets)

    def test_guards_cover_the_correlation_key(self):
        attrs = {g["check"].get("attribute") for g in self.r["guards"]}
        self.assertIn(self.r["correlation_key"], attrs)


@unittest.skipUnless(RUBRIC.exists(), "run spec/generate.py --rubric first")
class TestRunner(unittest.TestCase):
    def test_dry_run_establishes_the_floor(self):
        r = subprocess.run(
            [sys.executable, str(HERE / "run.py"), "--dry-run", "--out-json", "/dev/stdout"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["aggregate"]["runs"], 1)
        self.assertEqual(d["aggregate"]["clean_rate"], 0.0)

    def test_solution_all_reports_variance_and_tuning_signal(self):
        r = subprocess.run(
            [sys.executable, str(HERE / "run.py"), "--solution", "all",
             "--out-json", "/dev/stdout"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        agg = d["aggregate"]
        self.assertGreaterEqual(agg["runs"], 6)
        self.assertGreater(agg["stdev_score"], 0, "variants should not all score alike")
        self.assertIn("by_check_kind", agg)
        self.assertGreater(agg["total_regressions"], 0, "attribute-typo should register")

    def test_requires_a_mode(self):
        r = subprocess.run([sys.executable, str(HERE / "run.py")],
                           capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 1)

    def test_fail_under_gates(self):
        r = subprocess.run(
            [sys.executable, str(HERE / "run.py"), "--dry-run", "--fail-under", "90"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 2)

    def test_unknown_solution_errors(self):
        r = subprocess.run(
            [sys.executable, str(HERE / "run.py"), "--solution", "nope"],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no solution", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
