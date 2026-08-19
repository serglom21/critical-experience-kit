#!/usr/bin/env python3
"""§5/§6 of the generated spec must match the customer's SDK.

The first version was JS-only. A Python customer got `setAttribute` and
`startSpan` — APIs that do not exist on sentry-sdk — which is the thing this
file guards against.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from generate import detect_sdks, render_spec, render_why  # noqa: E402


def _rj() -> dict:
    return {
        "id": "checkout", "name": "Checkout",
        "roles": {
            "correlation_key": {"attribute": "checkout.id"},
            "steps": [
                {"id": "started", "span_name": "checkout", "surface": "node"},
                {"id": "submitted", "span_name": "checkout.submitted", "surface": "node"},
            ],
            "outcome": {"attribute": "checkout.outcome", "values": ["completed", "failed"],
                        "success_values": ["completed"]},
            "failure_reason": {"attribute": "checkout.failure_reason"},
            "magnitude": [{"attribute": "checkout.value", "type": "double"}],
            "actor_segment": [],
        },
        "needs_clarification": [],
    }


def _gj() -> dict:
    return {
        "id": "checkout", "name": "Checkout",
        "steps_instrumented": 0, "steps_total": 2,
        "score": 0, "grade": "F", "coverage_state": "absent",
        "caps": [], "dark_segments": [], "notes": [],
        "findings": [{
            "rule": "CE-003", "passed": False, "entity": "checkout",
            "detail": "`checkout` is missing",
            "description": "missing span", "rationale": "x",
        }],
    }


class TestDetectSdks(unittest.TestCase):
    def test_explicit_python_wins(self):
        self.assertEqual(detect_sdks(None, ["python"]), ["python"])

    def test_auto_without_observed_stays_javascript(self):
        """Previous hardcoded output. Fixtures that don't pass --observed must
        not suddenly become Python."""
        self.assertEqual(detect_sdks(None, ["auto"]), ["javascript"])

    def test_auto_uses_installed_sdk_family(self):
        obs = {"sdk": {
            "javascript": {"imported": False, "initialised": False},
            "python": {"imported": True, "initialised": True},
        }}
        self.assertEqual(detect_sdks(obs, ["auto"]), ["python"])

    def test_state_b_uses_source_languages(self):
        """No SDK in the tree — pick the language of the source, not JS by habit."""
        obs = {"sdk": {
            "javascript": {"imported": False, "initialised": False},
            "python": {"imported": False, "initialised": False},
            "any_sdk_present": False,
        }, "source_languages": ["python"]}
        self.assertEqual(detect_sdks(obs, ["auto"]), ["python"])


class TestApiSection(unittest.TestCase):
    def test_python_spec_does_not_tell_them_to_call_setAttribute(self):
        text = render_spec(_rj(), _gj(), {"org": "t", "stats_period": "x"}, ["python"])
        body = text.split("## 6.")[0]
        self.assertIn("start_transaction", body)
        self.assertIn("set_data", body)
        self.assertNotIn("span.setAttribute", body)
        self.assertNotIn("Sentry.startSpan", body)

    def test_javascript_spec_still_uses_startSpan(self):
        text = render_spec(_rj(), _gj(), {"org": "t", "stats_period": "x"}, ["javascript"])
        body = text.split("## 6.")[0]
        self.assertIn("Sentry.startSpan", body)
        self.assertNotIn("start_transaction", body)

    def test_python_do_not_use_table_names_the_orphan_span_trap(self):
        text = render_spec(_rj(), _gj(), {"org": "t", "stats_period": "x"}, ["python"])
        self.assertIn("dropped silently", text)

    def test_no_companion_section_5_stays_span_only(self):
        """A span-only gap used to still list logger/metrics in §5 because the
        generator dumped the whole SDK surface. Companions are opt-in."""
        text = render_spec(_rj(), _gj(), {"org": "t", "stats_period": "x"}, ["javascript"])
        body = text.split("## 6.")[0]
        self.assertNotIn("Sentry.logger", body)
        self.assertNotIn("Sentry.metrics.count", body)
        self.assertNotIn("captureException", body)


def _finding(rule: str, entity: str = "", detail: str = "missing") -> dict:
    return {
        "rule": rule, "passed": False, "entity": entity,
        "detail": detail, "description": "x", "rationale": "x",
    }


def _gap(*findings: dict) -> dict:
    g = _gj()
    g["findings"] = list(findings)
    return g


def _doc(rate=None) -> dict:
    d: dict = {"org": "t", "stats_period": "x"}
    if rate is not None:
        d["traces_sample_rate"] = rate
    return d


class TestCompanionSignals(unittest.TestCase):
    def test_ce008_declined_asks_captureException_and_still_the_attribute(self):
        """Wrong output was a span-only CE-008 ask, so Issues never saw the
        coded reason."""
        rj = _rj()
        rj["roles"]["outcome"]["values"] = ["authorized", "declined"]
        gj = _gap(_finding("CE-008", "checkout.failure_reason"))
        text = render_spec(rj, gj, _doc(), ["javascript"])
        self.assertIn("MUST carry `checkout.failure_reason`", text)
        self.assertIn("captureException", text)
        self.assertIn("capture_exception", text)
        self.assertIn("Fingerprint", text)
        why = render_why(rj, gj)
        self.assertIn("Issues will group by coded reason.", why)

    def test_abandoned_only_does_not_ask_captureException(self):
        """Wrong output treated abandoned as a triageable failure and asked
        for captureException on a journey that never throws."""
        rj = _rj()
        rj["roles"]["outcome"]["values"] = ["completed", "abandoned"]
        gj = _gap(_finding("CE-008", "checkout.failure_reason"))
        text = render_spec(rj, gj, _doc(), ["javascript"])
        self.assertIn("MUST carry `checkout.failure_reason`", text)
        self.assertNotIn("captureException", text)
        self.assertNotIn("capture_exception", text)

    def test_ce009_low_sample_rate_asks_application_metrics_not_increment(self):
        """Wrong output was the v9 'metrics namespace is dead' line, so the
        spec forbade the API that would keep magnitude under 1% sampling."""
        gj = _gap(_finding("CE-009", "checkout.value"))
        text = render_spec(_rj(), gj, _doc(0.01), ["javascript"])
        body = text.split("## 6.")[0]
        self.assertIn("Sentry.metrics.count", body)
        self.assertIn("**double**", body)
        self.assertNotIn("increment", body)
        why = render_why(_rj(), gj, {"traces_sample_rate": 0.01})
        self.assertIn("Metrics keep magnitude when traces are sampled.", why)

    def test_ce009_full_sample_or_unset_stays_span_attribute(self):
        """Wrong output always recommended metrics whenever CE-009 failed."""
        gj = _gap(_finding("CE-009", "checkout.value"))
        for rate in (1.0, None):
            text = render_spec(_rj(), gj, _doc(rate), ["javascript"])
            body = text.split("## 6.")[0]
            self.assertNotIn("Sentry.metrics.count", body, f"rate={rate}")
            self.assertIn("checkout.value", body)

    def test_unknown_sample_rate_does_not_invent_a_log_companion(self):
        """Wrong output assumed traces were sampled and asked for Sentry.logger
        with no rate on the snapshot."""
        gj = _gap(_finding("CE-004", "checkout.id"))
        text = render_spec(_rj(), gj, _doc(), ["javascript"])
        body = text.split("## 6.")[0]
        self.assertNotIn("Sentry.logger", body)
        self.assertIn("checkout.id", body)

    def test_ce004_low_sample_rate_asks_structured_log(self):
        """Wrong output left correlation-key gaps span-only, so a 1% sample
        rate dropped the instance that logs would have kept."""
        gj = _gap(_finding("CE-004", "checkout.id"))
        text = render_spec(_rj(), gj, _doc(0.01), ["javascript"])
        body = text.split("## 6.")[0]
        self.assertIn("Sentry.logger", body)
        self.assertIn("enableLogs", body)
        why = render_why(_rj(), gj, {"traces_sample_rate": 0.01})
        self.assertIn("Logs keep the instance when traces are sampled.", why)

    def test_python_error_companion_uses_capture_exception_not_setAttribute(self):
        rj = _rj()
        rj["roles"]["outcome"]["values"] = ["authorized", "declined"]
        gj = _gap(_finding("CE-008", "checkout.failure_reason"))
        text = render_spec(rj, gj, _doc(), ["python"])
        body = text.split("## 6.")[0]
        self.assertIn("capture_exception", body)
        self.assertNotIn("span.setAttribute", body)
        self.assertNotIn("Sentry.startSpan", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
