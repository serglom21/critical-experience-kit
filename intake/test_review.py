#!/usr/bin/env python3
"""
Tests for `ce review`.

The wrong path was: discover proposed `web`, nobody edited YAML, report spec'd
a frontend directory. Default-drop + POST apply + the `.reviewed` stamp are
what close that.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from review import (  # noqa: E402
    _page,
    apply_decisions,
    dump_journeys,
    expected_name,
    serve,
    suggest_drop,
)


SAMPLE = {
    "version": 1,
    "journeys": [
        {
            "id": "checkout",
            "name": "Checkout",
            "source": "discovered:code",
            "steps": [
                {"id": "session", "surface": "node",
                 "evidence": "POST /api/checkout/session (src/checkout/routes.ts)"},
                {"id": "confirm", "surface": "node",
                 "evidence": "POST /api/checkout (src/checkout/routes.ts)"},
            ],
        },
        {
            "id": "web",
            "name": "Web",
            "source": "discovered:code",
            "steps": [
                {"id": "index", "surface": "browser",
                 "evidence": "GET / (src/web/index.ts)"},
                {"id": "page", "surface": "browser",
                 "evidence": "GET /app (src/web/app.ts)"},
            ],
        },
    ],
}


class TestDefaultDrop(unittest.TestCase):
    def test_web_defaults_to_drop(self):
        """`web` used to ship as a kept journey because propose saw src/web/
        routes. The review page must default it to drop."""
        self.assertTrue(suggest_drop({"id": "web", "name": "Web"}))
        self.assertFalse(suggest_drop({"id": "checkout", "name": "Checkout"}))
        html = _page(SAMPLE).decode()
        self.assertIn('name="keep-web" value="drop" checked', html)
        self.assertIn('name="keep-checkout" value="keep" checked', html)
        # success_values must never be pre-filled — that is inferring
        # which outcomes count as success.
        self.assertIn('name="success-checkout" value=""', html)
        self.assertIn("Probably not journeys", html)
        self.assertGreater(html.find('class="skips"'), html.find("keep-checkout"))
        self.assertGreater(html.find("keep-web"), html.find('class="skips"'))


class TestGuidedPage(unittest.TestCase):
    def test_page_explains_evidence_and_contract(self):
        """The first review HTML dumped radios with no briefing, so engineers
        did not know why a journey was proposed or what keeping it meant."""
        html = _page(SAMPLE).decode()
        self.assertIn("Which customer flows should Sentry treat as journeys?", html)
        self.assertIn("Why we proposed this", html)
        self.assertIn("From source", html)
        self.assertIn("Now (current)", html)
        self.assertIn("If you keep this", html)
        self.assertIn("does not change application code", html)
        self.assertIn("ce does not patch your app", html)
        self.assertIn("POST /api/checkout/session", html)
        self.assertIn("data-tab=\"now\"", html)
        self.assertIn("data-tab=\"after\"", html)
        self.assertIn("we would page", html)
        self.assertIn("Proposed from source", html)
        self.assertIn("<summary>More</summary>", html)

    def test_expected_span_names_follow_the_spec_convention(self):
        self.assertEqual(
            expected_name("checkout", {"id": "session"}, True), "checkout")
        self.assertEqual(
            expected_name("checkout", {"id": "confirm"}, False),
            "checkout.confirm")
        html = _page(SAMPLE).decode()
        self.assertIn("checkout.confirm", html)

    def test_state_b_copy_when_no_sdk(self):
        """A scan with no SDK used to look like missing spans rather than
        'nothing emits yet', which made people think the tool was broken."""
        observed = {
            "sdk": {"any_sdk_present": False},
            "_synthetic_counts": True,
            "span_names": [{"name": "http.server", "count": 1}],
            "attributes": [],
            "_provenance": {"source": "static code scan of /app"},
        }
        html = _page(SAMPLE, observed).decode()
        body = html.split("</style>", 1)[-1]
        self.assertIn("No Sentry SDK", body)
        self.assertIn("Nothing emits these spans yet", body)
        # Synthetic count 1 must not be presented as a frequency or percentage.
        self.assertNotIn("100%", body)
        self.assertNotIn("count: 1", body)

    def test_live_sentry_banner_and_user_attributes(self):
        observed = {
            "sdk": {"any_sdk_present": True,
                    "javascript": {"imported": True, "initialised": True}},
            "_synthetic_counts": False,
            "span_names": [{"name": "checkout"}],
            "attributes": [{
                "key": "checkout.id",
                "attributeSource": {"source_type": "user"},
            }],
            "_provenance": {
                "attributes": "GET /api/0/organizations/{org}/trace-items/attributes/",
                "merged": "scan (sdk) + live Sentry",
            },
        }
        doc = json.loads(json.dumps(SAMPLE))
        doc["journeys"][0]["correlation_key"] = {"attribute": "checkout.id"}
        html = _page(doc, observed).decode()
        body = html.split("</style>", 1)[-1]
        self.assertIn("live Sentry", body)
        self.assertIn("source_type=user", body)
        self.assertIn("checkout.id", body)
        self.assertIn("present", body)
        self.assertNotIn("100%", body)

    def test_span_evidence_is_labelled_from_sentry(self):
        """A telemetry-derived step used to look identical to a route match,
        so reviewers could not tell code from Sentry."""
        doc = {
            "version": 1,
            "journeys": [{
                "id": "checkout",
                "name": "Checkout",
                "source": "discovered:telemetry",
                "steps": [
                    {"id": "pay", "surface": "node",
                     "evidence": "span: checkout.payment.authorize"},
                    {"id": "done", "surface": "node",
                     "evidence": "span: checkout.order.create"},
                ],
            }],
        }
        html = _page(doc).decode()
        self.assertIn("From Sentry", html)
        self.assertIn("span: checkout.payment.authorize", html)

    def test_waterfall_sketches_separate_requests_not_latency(self):
        """A nested 'current' tree made from routes would pretend one trace
        already exists. Today they are separate HTTP requests with no
        correlation key — that is the point of the mockup."""
        html = _page(SAMPLE).decode()
        body = html.split("</style>", 1)[-1]
        self.assertIn(">Today</button>", body)
        self.assertIn("After this spec", body)
        self.assertIn("POST /api/checkout/session", body)
        self.assertIn("separate request", body.lower())
        self.assertIn("checkout.confirm", body)
        self.assertIn('data-depth="1"', body)
        self.assertIn('class="wf-opname">function</span>', body)
        self.assertIn('class="card proposed open"', html)
        self.assertIn("One root, one key", body)
        self.assertIn("illustrated", body.lower())
        self.assertIn("failure_reason", body)
        self.assertIn("wf-row error", body)
        self.assertIn("who called this URL", body)
        self.assertNotRegex(body, r"\d+ms")
        self.assertNotIn("100%", body)

    def test_illustrated_error_is_not_a_recorded_incident(self):
        """Painting a 500 on the last step used to look like we observed it.
        The sketch must say illustrated, and must not invent a duration."""
        html = _page(SAMPLE).decode()
        body = html.split("</style>", 1)[-1]
        self.assertIn("not a recorded incident", body.lower())
        self.assertIn("coded reason", body)
        self.assertIn("failure_reason", body)
        self.assertNotIn("token_expired", body)
        self.assertNotRegex(body, r"\d+ms")

    def test_waterfall_uses_recorded_trace_nesting(self):
        traces = [
            {"span_id": "a", "parent_span_id": "",
             "name": "POST /api/checkout/session", "op": "http.server",
             "data": {"http.method": "POST"}},
            {"span_id": "b", "parent_span_id": "a",
             "name": "db", "op": "db", "data": {"db.system": "postgres"}},
        ]
        html = _page(SAMPLE, traces=traces).decode()
        body = html.split("</style>", 1)[-1]
        self.assertIn("Recorded Sentry trace", body)
        self.assertIn("db.system", body)
        self.assertIn("postgres", body)
        self.assertIn('data-span=', body)
        self.assertNotRegex(body, r"\d+ms")


class TestApply(unittest.TestCase):
    def test_apply_keeps_impact_and_drops_web(self):
        """A keep without impact used to write the stamp anyway, and report
        then ranked by whatever was left — including volume-shaped junk."""
        doc = json.loads(json.dumps(SAMPLE))
        apply_decisions(doc, {"journeys": [
            {"id": "checkout", "keep": True, "business_impact": "critical"},
            {"id": "web", "keep": False},
        ]})
        by_id = {j["id"]: j for j in doc["journeys"]}
        self.assertEqual(by_id["checkout"]["business_impact"], "critical")
        self.assertFalse(by_id["checkout"].get("excluded"))
        self.assertTrue(by_id["web"]["excluded"])
        self.assertNotIn("business_impact", by_id["web"])

    def test_apply_refuses_to_invent_impact(self):
        doc = json.loads(json.dumps(SAMPLE))
        with self.assertRaises(SystemExit):
            apply_decisions(doc, {"journeys": [
                {"id": "checkout", "keep": True, "business_impact": ""},
            ]})

    def test_drop_steps_are_removed(self):
        doc = json.loads(json.dumps(SAMPLE))
        apply_decisions(doc, {"journeys": [
            {"id": "checkout", "keep": True, "business_impact": "important",
             "drop_steps": ["confirm"]},
            {"id": "web", "keep": False},
        ]})
        steps = [s["id"] for s in doc["journeys"][0]["steps"]]
        self.assertEqual(steps, ["session"])


class TestPostApply(unittest.TestCase):
    def test_post_writes_yaml_and_stamp(self):
        """The browser path is POST /apply. A GET-only server looked like it
        worked (the page loaded) and left journeys.yaml untouched."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            yaml_path = work / "journeys.yaml"
            yaml_path.write_text(dump_journeys(SAMPLE))
            t = threading.Thread(
                target=lambda: serve(work, yaml_path, port=0, open_browser=False),
                daemon=True,
            )
            t.start()
            url = None
            for _ in range(50):
                marker = work / "review.url"
                if marker.is_file() and marker.read_text().strip():
                    url = marker.read_text().strip()
                    break
                time.sleep(0.05)
            self.assertIsNotNone(url, "server never wrote review.url")
            payload = json.dumps({"journeys": [
                {"id": "checkout", "keep": True, "business_impact": "critical"},
                {"id": "web", "keep": False},
            ]}).encode()
            req = urllib.request.Request(
                url + "apply", data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            t.join(timeout=5)
            self.assertTrue((work / ".reviewed").is_file())
            stamp = json.loads((work / ".reviewed").read_text())
            self.assertIn("checkout", stamp["kept"])
            self.assertIn("web", stamp["excluded"])
            text = yaml_path.read_text()
            self.assertIn("business_impact: critical", text)
            self.assertIn("excluded: true", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
