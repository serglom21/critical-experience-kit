#!/usr/bin/env python3
"""Warden path scaffolding. A checkout-hardcoded glob was the wrong default."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from publish import _glob_from_step, _warden_paths, _warden_toml  # noqa: E402


class TestWardenPaths(unittest.TestCase):
    def test_globs_follow_evidence_not_checkout(self):
        """The kit's own warden.toml listed src/checkout/**. Copying that into
        a refunds service reviewed the wrong tree and then got ignored."""
        g = _glob_from_step({
            "id": "settle",
            "evidence": "POST /api/refunds/settle (src/refunds/service.ts)",
        })
        self.assertEqual(g, "src/refunds/**/*.{ts,tsx,js,jsx}")
        self.assertNotIn("checkout", g)

    def test_toml_is_advisory(self):
        text = _warden_toml(["src/refunds/**/*.ts"])
        self.assertIn('failOn = "off"', text)
        self.assertIn('name = "./.agents/skills/sentry-critical-experience"', text)
        self.assertNotIn("src/checkout", text)

    def test_resolved_steps_drive_paths(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "resolved.json").write_text(json.dumps({
                "journeys": [{
                    "id": "refunds",
                    "roles": {
                        "steps": [{
                            "id": "settle",
                            "evidence": "function settleRefund() (app/refunds/settle.py)",
                        }],
                    },
                }],
            }))
            paths = _warden_paths(work, ["refunds"])
            self.assertEqual(paths, ["app/refunds/**/*.py"])
