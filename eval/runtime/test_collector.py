#!/usr/bin/env python3
"""
Tests for the envelope collector and the runtime eval.

The envelope-parsing tests use fixtures copied from a real @sentry/node 10.70.0
capture, so they encode the actual wire format rather than a reading of the docs.
The end-to-end tests boot the real SDK and are skipped when node or the installed
dependencies are absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from collector import (  # noqa: E402
    EnvelopeCollector,
    build_observed,
    json_type,
    parse_envelope,
    spans_from_payload,
)

TASK = HERE / "tasks" / "checkout-js"
RESOLVED = HERE.parent.parent / "intake" / "example-resolved.json"
HAVE_NODE = shutil.which("node") is not None
HAVE_DEPS = (TASK / "node_modules" / "@sentry" / "node").exists()

# Trimmed from a real capture. Structure preserved exactly: envelope header line,
# item header line, payload line.
REAL_ENVELOPE = (
    b'{"event_id":"f771f34192334166a3c29bc1438345c2","sent_at":"2026-08-12T18:06:07.226Z",'
    b'"sdk":{"name":"sentry.javascript.node","version":"10.70.0"},'
    b'"trace":{"environment":"production","public_key":"publickey",'
    b'"trace_id":"a0c30a57beda4efba1373d9204d25e87","transaction":"checkout","sampled":"true"}}\n'
    b'{"type":"transaction"}\n'
    b'{"transaction":"checkout","type":"transaction",'
    b'"contexts":{"trace":{"span_id":"bdf635ab5074aaa4",'
    b'"trace_id":"a0c30a57beda4efba1373d9204d25e87",'
    b'"data":{"sentry.origin":"manual","sentry.op":"ui.action","sentry.source":"custom",'
    b'"sentry.sample_rate":1,"checkout.id":"chk_1","cart.value":129.99,'
    b'"checkout.outcome":"completed","cart.item_count":2},'
    b'"op":"ui.action","status":"ok","origin":"manual"}},'
    b'"spans":[{"data":{"sentry.origin":"manual","sentry.op":"function",'
    b'"order.value":134.48,"order.id":"ord_9"},"description":"checkout.order.create",'
    b'"op":"function","parent_span_id":"bdf635ab5074aaa4","span_id":"a04b575f1f03db30",'
    b'"trace_id":"a0c30a57beda4efba1373d9204d25e87","status":"ok"}]}\n'
)


class TestJsonType(unittest.TestCase):
    def test_bool_checked_before_int(self):
        """bool subclasses int in Python — order matters or every boolean reads
        as a number, which would hide the CE-007 defect entirely."""
        self.assertEqual(json_type(True), "boolean")
        self.assertEqual(json_type(False), "boolean")

    def test_number_string_array(self):
        self.assertEqual(json_type(1), "number")
        self.assertEqual(json_type(129.99), "number")
        self.assertEqual(json_type("129.99"), "string")
        self.assertEqual(json_type([1, 2]), "array")


class TestEnvelopeParsing(unittest.TestCase):
    def test_parses_header_and_items(self):
        header, items = parse_envelope(REAL_ENVELOPE)
        self.assertEqual(header["sdk"]["name"], "sentry.javascript.node")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0]["type"], "transaction")

    def test_empty_and_garbage_are_survivable(self):
        self.assertEqual(parse_envelope(b""), ({}, []))
        self.assertEqual(parse_envelope(b"not json\n"), ({}, []))

    def test_root_span_name_comes_from_the_transaction_field(self):
        """Not from contexts.trace — verified against a live SDK capture."""
        _, items = parse_envelope(REAL_ENVELOPE)
        spans = spans_from_payload(*items[0])
        root = next(s for s in spans if s["is_root"])
        self.assertEqual(root["name"], "checkout")
        self.assertEqual(root["op"], "ui.action")

    def test_child_span_name_comes_from_description(self):
        _, items = parse_envelope(REAL_ENVELOPE)
        spans = spans_from_payload(*items[0])
        child = next(s for s in spans if not s["is_root"])
        self.assertEqual(child["name"], "checkout.order.create")
        self.assertEqual(child["parent_span_id"], "bdf635ab5074aaa4")

    def test_real_value_types_survive_the_round_trip(self):
        _, items = parse_envelope(REAL_ENVELOPE)
        spans = spans_from_payload(*items[0])
        root = next(s for s in spans if s["is_root"])
        self.assertIsInstance(root["attributes"]["cart.value"], float)
        self.assertIsInstance(root["attributes"]["cart.item_count"], int)

    def test_standalone_span_item_supported(self):
        raw = (b'{"event_id":"x"}\n{"type":"span"}\n'
               b'{"description":"checkout.step","op":"function","span_id":"a",'
               b'"trace_id":"t","data":{"cart.value":1.5},"is_segment":true}\n')
        _, items = parse_envelope(raw)
        spans = spans_from_payload(*items[0])
        self.assertEqual(spans[0]["name"], "checkout.step")
        self.assertTrue(spans[0]["is_root"])


class TestBuildObserved(unittest.TestCase):
    def setUp(self):
        _, items = parse_envelope(REAL_ENVELOPE)
        self.spans = spans_from_payload(*items[0])
        self.obs = build_observed(self.spans)

    def test_shape_matches_what_analyze_consumes(self):
        for key in ("org", "stats_period", "span_names", "span_ops", "attributes",
                    "example_traces"):
            self.assertIn(key, self.obs)

    def test_span_names_and_counts(self):
        names = {s["name"]: s["count"] for s in self.obs["span_names"]}
        self.assertEqual(names, {"checkout": 1, "checkout.order.create": 1})

    def test_attribute_types_derived_from_real_values(self):
        a = {x["key"]: x for x in self.obs["attributes"]}
        self.assertEqual(a["cart.value"]["attributeType"], "number")
        self.assertEqual(a["checkout.outcome"]["attributeType"], "string")

    def test_sdk_attributes_classified_as_sentry_sourced(self):
        a = {x["key"]: x for x in self.obs["attributes"]}
        self.assertEqual(a["sentry.op"]["attributeSource"]["source_type"], "sentry")
        self.assertEqual(a["cart.value"]["attributeSource"]["source_type"], "user")

    def test_type_conflict_is_surfaced_not_silently_resolved(self):
        spans = [
            {"name": "a", "op": "function", "attributes": {"cart.value": 1.0}},
            {"name": "a", "op": "function", "attributes": {"cart.value": "1.00"}},
        ]
        obs = build_observed(spans)
        rec = next(x for x in obs["attributes"] if x["key"] == "cart.value")
        self.assertIn("type_conflict", rec)
        self.assertEqual(rec["attributeType"], "string",
                         "the unaggregatable type must win, not be averaged away")

    def test_provenance_flags_the_heuristic(self):
        p = self.obs["_provenance"]
        self.assertIn("HEURISTIC", p["attributeSource"])
        self.assertIn("REAL JSON value type", p["attributeType"])


class TestCollectorServer(unittest.TestCase):
    def test_accepts_chunked_bodies(self):
        """The first implementation read Content-Length only and captured zero
        bytes — the SDK sends Transfer-Encoding: chunked."""
        import http.client
        with EnvelopeCollector() as c:
            conn = http.client.HTTPConnection(c.host, c.port, timeout=5)
            conn.putrequest("POST", "/api/1/envelope/")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            for part in (REAL_ENVELOPE[:120], REAL_ENVELOPE[120:]):
                conn.send(b"%x\r\n" % len(part) + part + b"\r\n")
            conn.send(b"0\r\n\r\n")
            self.assertEqual(conn.getresponse().status, 200)
            self.assertEqual(len(c.envelopes), 1)
            self.assertEqual(c.envelopes[0], REAL_ENVELOPE)

    def test_accepts_gzipped_bodies(self):
        import gzip
        import http.client
        blob = gzip.compress(REAL_ENVELOPE)
        with EnvelopeCollector() as c:
            conn = http.client.HTTPConnection(c.host, c.port, timeout=5)
            conn.request("POST", "/api/1/envelope/", body=blob,
                         headers={"Content-Encoding": "gzip",
                                  "Content-Length": str(len(blob))})
            self.assertEqual(conn.getresponse().status, 200)
            self.assertEqual(c.envelopes[0], REAL_ENVELOPE)

    def test_dsn_points_at_itself(self):
        with EnvelopeCollector() as c:
            self.assertTrue(c.dsn.startswith("http://publickey@"))
            self.assertIn(str(c.port), c.dsn)


@unittest.skipUnless(HAVE_NODE and HAVE_DEPS, "needs node and `npm install` in the task")
class TestEndToEndAgainstRealSdk(unittest.TestCase):
    """Boots the real @sentry/node SDK against the collector."""

    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            [sys.executable, str(HERE / "run_runtime.py"), "--variant", "all",
             "--runs", "4", "--out-json", "/dev/stdout"],
            capture_output=True, text=True, timeout=420)
        if r.returncode != 0:
            raise unittest.SkipTest(f"runtime run failed: {r.stderr[-800:]}")
        cls.payload = json.loads(r.stdout)
        cls.by = {v["variant"]: v for v in cls.payload["variants"]}

    def test_envelopes_actually_arrived(self):
        for v in self.by.values():
            self.assertGreater(v["driver"]["envelopes"], 0, v["variant"])

    def test_correct_variant_is_fully_instrumented(self):
        j = self.by["correct"]["journey"]
        self.assertEqual(j["steps_instrumented"], j["steps_total"])
        self.assertEqual(j["grade"], "excellent")
        self.assertEqual(j["dark_segments"], [])

    def test_stringified_value_caught_from_the_wire(self):
        """The source reads `cart.total` in both variants, so static analysis
        cannot separate them. The wire can."""
        types = {t["key"]: t["type"] for t in self.by["stringified"]["types"]}
        self.assertEqual(types["cart.value"], "string")
        self.assertEqual(
            {t["key"]: t["type"] for t in self.by["correct"]["types"]}["cart.value"],
            "number")
        fails = [f["rule"] for f in self.by["stringified"]["journey"]["findings"]
                 if not f["passed"]]
        self.assertIn("CE-010", fails)

    def test_written_but_never_executed_span_caught(self):
        """`skip-terminal` contains the confirmation span in source; the path
        never reaches it. Static analysis passes this."""
        j = self.by["skip-terminal"]["journey"]
        self.assertIn("checkout.confirmation_viewed", j["missing_steps"])
        self.assertLess(j["steps_instrumented"], j["steps_total"])
        self.assertTrue(any("CE-002" in c for c in j["caps"]))

    def test_variants_are_distinguishable(self):
        scores = {k: v["journey"]["score"] for k, v in self.by.items()}
        self.assertEqual(len(set(scores.values())), len(scores), scores)
        self.assertEqual(max(scores, key=scores.get), "correct")

    def test_decline_path_exercised_so_failure_reason_appears(self):
        keys = {t["key"] for t in self.by["correct"]["types"]}
        self.assertIn("payment.decline_reason", keys,
                      "the driver must exercise the non-success branch")


@unittest.skipUnless(HAVE_NODE and HAVE_DEPS, "needs node and `npm install` in the task")
class TestStaticAndRuntimeDisagree(unittest.TestCase):
    """The justification for building this at all: the two halves must reach
    different verdicts on the same code."""

    def test_static_grader_cannot_see_what_runtime_sees(self):
        sys.path.insert(0, str(HERE.parent))
        from grade import _value_verdict

        # Both variants write `cart.total` / `cart.total.toFixed(2)`; only the
        # latter is statically detectable. A stringifier applied elsewhere, or a
        # value whose type depends on a branch, is invisible.
        self.assertEqual(_value_verdict(["cart.total"]), "unresolved")
        self.assertEqual(_value_verdict(["cartValue"]), "unresolved")

        # And a span written but never executed is a plain pass statically.
        journey = (TASK / "journey.mjs").read_text()
        self.assertIn('name: "checkout.confirmation_viewed"', journey,
                      "the span IS in the source for every variant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
