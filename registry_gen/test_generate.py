#!/usr/bin/env python3
"""Tests for the Weaver registry generator."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from generate import (  # noqa: E402
    PLACEHOLDER,
    attribute_defs,
    expected_span_name,
    render_attribute_group,
    render_manifest,
    render_span_groups,
    slug_ok,
)

HERE = Path(__file__).parent
RESOLVED = HERE.parent / "intake" / "example-resolved.json"

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def journey(**over) -> dict:
    j = {
        "id": "checkout",
        "name": "Checkout",
        "excluded": False,
        "roles": {
            "correlation_key": {"attribute": "checkout.id",
                                "persists_across": ["page_load", "service"]},
            "steps": [
                {"id": "cart_reviewed", "span_name": "checkout", "surface": "browser",
                 "impact": "normal"},
                {"id": "payment_authorized", "span_name": "checkout.payment.authorize",
                 "surface": "node", "impact": "critical"},
                {"id": "confirmation_viewed", "surface": "browser", "impact": "important"},
            ],
            "outcome": {"attribute": "checkout.outcome",
                        "values": ["completed", "failed", "abandoned"],
                        "success_values": ["completed"],
                        "default_value": "abandoned"},
            "failure_reason": {"attribute": "payment.decline_reason",
                               "known_values": ["insuffic_funds", "gateway_timeout"]},
            "magnitude": [{"attribute": "cart.value", "type": "double",
                           "step": "cart_reviewed", "unit": "major_currency_unit"}],
            "actor_segment": [{"attribute": "user.plan_tier", "already_available": True}],
        },
    }
    j["roles"].update(over.pop("roles", {}))
    j.update(over)
    return j


@unittest.skipIf(yaml is None, "PyYAML required")
class TestYamlValidity(unittest.TestCase):
    """The generator emits YAML as text, so 'it parses' is not a given."""

    def test_attribute_group_parses(self):
        j = journey()
        attrs, _ = attribute_defs(j)
        yaml.safe_load(render_attribute_group(j, attrs))

    def test_span_groups_parse(self):
        yaml.safe_load(render_span_groups(journey()))

    def test_manifest_parses(self):
        yaml.safe_load(render_manifest("https://x.example.com/schemas/y/0.1.0", [journey()]))

    def test_conditionally_required_with_backticks_parses(self):
        """Regression. The condition text contains backticks; emitting it as a bare
        multi-line mapping value produced `found character '`' that cannot start
        any token`. It must be a folded scalar."""
        doc = yaml.safe_load(render_span_groups(journey()))
        root = doc["groups"][0]
        oc = next(a for a in root["attributes"] if a.get("ref") == "checkout.outcome")
        self.assertIsInstance(oc["requirement_level"], dict)
        self.assertIn("conditionally_required", oc["requirement_level"])
        self.assertIn("abandoned", oc["requirement_level"]["conditionally_required"])


@unittest.skipIf(yaml is None, "PyYAML required")
class TestAttributeGroup(unittest.TestCase):
    def setUp(self):
        self.j = journey()
        self.attrs, self.placeholders = attribute_defs(self.j)
        self.doc = yaml.safe_load(render_attribute_group(self.j, self.attrs))
        self.group = self.doc["groups"][0]

    def test_group_id_is_registry_prefixed(self):
        """Weaver only allows attribute DEFINITIONS in `registry.`-prefixed groups."""
        self.assertTrue(self.group["id"].startswith("registry."))
        self.assertEqual(self.group["type"], "attribute_group")

    def test_attribute_group_needs_no_stability(self):
        self.assertNotIn("stability", self.group)

    def test_no_prefix_field(self):
        """`prefix:` is rejected outright (InvalidGroupUsesPrefix)."""
        self.assertNotIn("prefix", self.group)

    def test_every_attribute_has_stability(self):
        for a in self.group["attributes"]:
            self.assertEqual(a["stability"], "development", a["id"])

    def test_step_marker_is_an_enum_of_step_ids(self):
        step = next(a for a in self.group["attributes"] if a["id"] == "checkout.step")
        ids = [m["id"] for m in step["type"]["members"]]
        self.assertEqual(ids, ["cart_reviewed", "payment_authorized", "confirmation_viewed"])

    def test_outcome_is_an_enum_never_a_boolean(self):
        oc = next(a for a in self.group["attributes"] if a["id"] == "checkout.outcome")
        self.assertIsInstance(oc["type"], dict)
        self.assertEqual([m["id"] for m in oc["type"]["members"]],
                         ["completed", "failed", "abandoned"])

    def test_enum_members_carry_required_fields(self):
        for a in self.group["attributes"]:
            if isinstance(a["type"], dict):
                for m in a["type"]["members"]:
                    for req in ("id", "value", "stability"):
                        self.assertIn(req, m)

    def test_magnitude_type_is_numeric(self):
        m = next(a for a in self.group["attributes"] if a["id"] == "cart.value")
        self.assertEqual(m["type"], "double")
        self.assertTrue(all(isinstance(x, (int, float)) for x in m["examples"]))

    def test_failure_reason_uses_known_values_as_examples(self):
        fr = next(a for a in self.group["attributes"]
                  if a["id"] == "payment.decline_reason")
        self.assertEqual(fr["examples"], ["insuffic_funds", "gateway_timeout"])
        self.assertNotIn("payment.decline_reason", self.placeholders)

    def test_string_attributes_without_known_values_are_flagged(self):
        """Inventing plausible customer data would be worse than flagging it."""
        self.assertIn("checkout.id", self.placeholders)
        corr = next(a for a in self.group["attributes"] if a["id"] == "checkout.id")
        self.assertEqual(corr["examples"], [PLACEHOLDER])

    def test_every_string_attribute_has_examples(self):
        """`weaver registry check` requires examples on string attributes."""
        for a in self.group["attributes"]:
            if a["type"] in ("string", "string[]"):
                self.assertTrue(a.get("examples"), a["id"])


@unittest.skipIf(yaml is None, "PyYAML required")
class TestSpanGroups(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(render_span_groups(journey()))
        self.groups = self.doc["groups"]

    def test_one_group_per_step(self):
        self.assertEqual(len(self.groups), 3)

    def test_span_groups_require_kind_and_stability(self):
        for g in self.groups:
            self.assertEqual(g["type"], "span")
            self.assertIn(g["span_kind"],
                          ("client", "server", "producer", "consumer", "internal"))
            self.assertEqual(g["stability"], "development")

    def test_surface_maps_to_span_kind(self):
        self.assertEqual(self.groups[0]["span_kind"], "internal")  # browser
        self.assertEqual(self.groups[1]["span_kind"], "server")    # node

    def test_refs_carry_no_forbidden_keys(self):
        """A ref MUST NOT carry id/type/stability/deprecated."""
        for g in self.groups:
            for a in g["attributes"]:
                if "ref" in a:
                    for banned in ("id", "type", "stability", "deprecated"):
                        self.assertNotIn(banned, a)

    def test_correlation_key_required_on_every_span(self):
        for g in self.groups:
            corr = next(a for a in g["attributes"] if a.get("ref") == "checkout.id")
            self.assertEqual(corr["requirement_level"], "required")

    def test_step_marker_note_pins_the_value(self):
        g = self.groups[1]
        step = next(a for a in g["attributes"] if a.get("ref") == "checkout.step")
        self.assertIn("payment_authorized", step["note"])

    def test_journey_metadata_lives_in_note_not_top_level(self):
        """Unknown top-level keys fail validation, so metadata goes in `note`."""
        g = self.groups[0]
        for leaked in ("journey", "journey_step", "impact", "sentry_op"):
            self.assertNotIn(leaked, g)
        self.assertIn("journey_step: 1", g["note"])
        self.assertIn("impact: normal", g["note"])

    def test_explicit_span_name_binding_is_preserved(self):
        self.assertIn('sentry_span_name: "checkout.payment.authorize"',
                      self.groups[1]["note"])

    def test_convention_fills_unbound_step(self):
        self.assertIn('sentry_span_name: "checkout.confirmation_viewed"',
                      self.groups[2]["note"])

    def test_root_and_terminal_roles_marked(self):
        self.assertIn("journey_role: root", self.groups[0]["note"])
        self.assertIn("journey_role: terminal", self.groups[-1]["note"])

    def test_magnitude_attaches_to_its_declared_step(self):
        root = next(a for a in self.groups[0]["attributes"]
                    if a.get("ref") == "cart.value")
        self.assertEqual(root["requirement_level"], "required")
        self.assertFalse(any(a.get("ref") == "cart.value"
                             for a in self.groups[1]["attributes"]))

    def test_failure_reason_conditional_lists_non_success_values(self):
        cond = next(a for a in self.groups[0]["attributes"]
                    if a.get("ref") == "payment.decline_reason")
        text = cond["requirement_level"]["conditionally_required"]
        self.assertIn("abandoned", text)
        self.assertIn("failed", text)
        self.assertNotIn("completed", text)

    def test_no_failure_reason_when_all_outcomes_succeed(self):
        j = journey(roles={"outcome": {"attribute": "o", "values": ["a", "b"],
                                      "success_values": ["a", "b"]}})
        doc = yaml.safe_load(render_span_groups(j))
        for g in doc["groups"]:
            self.assertFalse(any(a.get("ref") == "payment.decline_reason"
                                 for a in g["attributes"]))


@unittest.skipIf(yaml is None, "PyYAML required")
class TestManifest(unittest.TestCase):
    def setUp(self):
        self.m = yaml.safe_load(
            render_manifest("https://acme.example.com/schemas/ce/0.1.0", [journey()]))

    def test_schema_url_present(self):
        self.assertEqual(self.m["schema_url"], "https://acme.example.com/schemas/ce/0.1.0")

    def test_deprecated_keys_absent(self):
        for k in ("name", "semconv_version", "schema_base_url"):
            self.assertNotIn(k, self.m)

    def test_single_dependency_with_schema_url(self):
        deps = self.m["dependencies"]
        self.assertEqual(len(deps), 1, "weaver supports at most one dependency")
        self.assertTrue(deps[0]["schema_url"].startswith("https://opentelemetry.io/schemas/"))
        self.assertIn("registry_path", deps[0])
        self.assertNotIn("name", deps[0], "a dependency with only `name` hard-fails")


class TestHelpers(unittest.TestCase):
    def test_slug_validation(self):
        self.assertTrue(slug_ok("checkout"))
        self.assertTrue(slug_ok("plan_downgrade"))
        self.assertFalse(slug_ok("Checkout"))
        self.assertFalse(slug_ok("check out"))

    def test_expected_span_name(self):
        self.assertEqual(expected_span_name("c", {"id": "a"}, True), "c")
        self.assertEqual(expected_span_name("c", {"id": "a"}, False), "c.a")
        self.assertEqual(expected_span_name("c", {"id": "a", "span_name": "z"}, False), "z")


class TestEndToEnd(unittest.TestCase):
    def test_generates_and_validates_from_real_resolved_output(self):
        if not RESOLVED.exists():
            self.skipTest("run intake/resolve.py first")
        with tempfile.TemporaryDirectory() as d:
            g = subprocess.run(
                [sys.executable, str(HERE / "generate.py"),
                 "--resolved", str(RESOLVED), "--out-dir", d],
                capture_output=True, text=True)
            self.assertEqual(g.returncode, 0, g.stderr)
            self.assertTrue((Path(d) / "manifest.yaml").exists())
            self.assertTrue((Path(d) / "registry" / "checkout.yaml").exists())
            self.assertTrue((Path(d) / "spans" / "checkout.yaml").exists())

            v = subprocess.run(
                [sys.executable, str(HERE / "validate.py"), "--registry", d],
                capture_output=True, text=True)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
            self.assertIn("PASS", v.stdout)

    def test_excluded_journeys_are_not_emitted(self):
        if not RESOLVED.exists():
            self.skipTest("run intake/resolve.py first")
        resolved = json.loads(RESOLVED.read_text())
        excluded = [j["id"] for j in resolved["journeys"] if j.get("excluded")]
        self.assertTrue(excluded)
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, str(HERE / "generate.py"),
                            "--resolved", str(RESOLVED), "--out-dir", d],
                           capture_output=True, text=True)
            for jid in excluded:
                self.assertFalse((Path(d) / "registry" / f"{jid}.yaml").exists())

    def test_strict_examples_exits_3(self):
        if not RESOLVED.exists():
            self.skipTest("run intake/resolve.py first")
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(
                [sys.executable, str(HERE / "generate.py"), "--resolved", str(RESOLVED),
                 "--out-dir", d, "--strict-examples"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 3)
            self.assertIn("need a real `examples`", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
