#!/usr/bin/env python3
"""
Tests for gap/explore.py. Each docstring says what the wrong output looked like.

The explorer is an LLM, so the contract under test is the part that is NOT an LLM:
what the validator keeps and drops, how confidence is computed, and that the tool loop
cannot be steered outside the repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import explore as ex  # noqa: E402

KIT = HERE.parent


def make_repo(files: dict) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def journey(jid="checkout", name="Checkout", steps=None, **kw):
    steps = steps if steps is not None else [
        {"id": "cart", "evidence": [{"file": "src/cart.ts", "line": 1, "symbol": "addToCart"}]},
        {"id": "pay", "evidence": [{"file": "src/pay.ts", "line": 1, "symbol": "chargeCard"}]},
    ]
    return dict({"id": jid, "name": name, "steps": steps}, **kw)


REPO = {
    "src/cart.ts": "export function addToCart() {}\n",
    "src/pay.ts": "export function chargeCard() {}\n",
    ".env": "SECRET=1\n",
    "src/__mocks__/pay.ts": "export function chargeCard() {}\n",
}


class TestEvidenceValidation(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(REPO)

    def status(self, **ev):
        return ex.check_evidence(dict({"file": "src/cart.ts", "line": None, "symbol": None}, **ev),
                                 self.root, {})["status"]

    def test_verified_when_file_symbol_and_line_agree(self):
        self.assertEqual(self.status(line=1, symbol="addToCart"), "verified")

    def test_file_only_without_symbol_or_line(self):
        self.assertEqual(self.status(), "file_only")

    def test_missing_file_is_rejected(self):
        """A mistyped directory (Billing/ vs BillingSettings/) was a real miss while
        building this: it must not pass as evidence."""
        self.assertEqual(self.status(file="src/nope.ts"), "missing_file")

    def test_symbol_not_in_file(self):
        self.assertEqual(self.status(line=1, symbol="doesNotExist"), "symbol_not_found")

    def test_line_out_of_range(self):
        self.assertEqual(self.status(line=999), "bad_line")

    def test_path_traversal_is_outside_repo(self):
        self.assertEqual(self.status(file="../../etc/passwd"), "outside_repo")

    def test_absolute_path_outside_repo(self):
        self.assertEqual(self.status(file="/etc/passwd"), "outside_repo")

    def test_secret_file_is_never_evidence(self):
        self.assertEqual(self.status(file=".env"), "secret")

    def test_mock_paths_do_not_count(self):
        """Mock builders became "journey steps" in the heuristic baseline."""
        self.assertEqual(self.status(file="src/__mocks__/pay.ts"), "noise_path")


class TestJourneyValidation(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(REPO)

    def run_validate(self, journeys, signals=None):
        return ex.validate({"journeys": journeys}, self.root, signals)

    def test_journey_with_one_grounded_step_is_dropped(self):
        j = journey(steps=[
            {"id": "a", "evidence": [{"file": "src/cart.ts", "line": 1, "symbol": "addToCart"}]},
            {"id": "b", "evidence": [{"file": "src/ghost.ts", "line": 1, "symbol": "x"}]}])
        kept, report = self.run_validate([j])
        self.assertEqual(kept, [])
        self.assertIn("fewer than 2", report["dropped"][0]["reason"])

    def test_model_confidence_is_ignored(self):
        """The model said `high`; nothing independent corroborates, so it is not high."""
        kept, _ = self.run_validate([journey(confidence="high")])
        self.assertNotEqual(kept[0]["confidence"], "high")

    def test_corroborated_and_grounded_is_high(self):
        sig = {"e2e_groups": [{"dir": "e2e/features", "files": 1, "stems": ["checkout"], "titles": []}]}
        kept, _ = self.run_validate([journey()], sig)
        self.assertEqual(kept[0]["confidence"], "high")
        self.assertIn("e2e", kept[0]["corroboration"])

    def test_duplicate_ids_are_disambiguated(self):
        kept, _ = self.run_validate([journey(), journey()])
        self.assertEqual(len({j["id"] for j in kept}), 2)

    def test_order_within_band_follows_the_model(self):
        kept, _ = self.run_validate([journey("b", "Beta"), journey("a", "Alpha")])
        self.assertEqual([j["id"] for j in kept], ["b", "a"])


class TestCorroboration(unittest.TestCase):
    def j(self, jid, name):
        return {"id": jid, "name": name, "steps": []}

    def test_single_shared_common_word_is_not_evidence(self):
        """`api-access-toggle` corroborated "Create access tokens" through the lone
        word `access`; every journey came back corroborated."""
        sig = {"e2e_groups": [{"dir": "e2e/studio/features", "files": 1, "titles": [],
                               "stems": ["api-access-toggle"]}]}
        self.assertEqual(ex.corroboration(self.j("create_access_token", "Create access tokens"), sig), [])

    def test_whole_spec_name_contained_in_journey_name(self):
        sig = {"e2e_groups": [{"dir": "e2e/studio/features", "files": 1, "titles": [], "stems": ["storage"]}]}
        self.assertEqual(ex.corroboration(self.j("storage", "Create buckets and upload files for storage"), sig), ["e2e"])

    def test_stemming_creation_matches_create(self):
        sig = {"analytics_events": [{"event": "project_creation", "count": 1, "files": []}]}
        self.assertEqual(ex.corroboration(self.j("create_project", "Create project"), sig), ["analytics"])

    def test_e2e_support_dirs_do_not_corroborate(self):
        sig = {"e2e_groups": [{"dir": "test/e2e/page-objects/flows", "files": 1, "titles": [],
                               "stems": ["checkout.flow"]}]}
        self.assertEqual(ex.corroboration(self.j("checkout", "Checkout"), sig), [])


class TestCandidates(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(REPO)

    def cands(self, journeys):
        kept, _ = ex.validate({"journeys": journeys}, self.root, None)
        return ex.to_candidates(kept, "repo")["journeys"]

    def test_source_and_impact(self):
        c = self.cands([journey()])[0]
        self.assertEqual(c["source"], "discovered:code")
        self.assertNotIn("business_impact", c)
        self.assertTrue(any("business_impact" in n for n in c["needs_clarification"]))

    def test_shared_correlation_key_is_namespaced(self):
        """Two journeys keyed on `organization.slug` were merged by `ce intake` into
        one, silently hiding a journey."""
        a = journey("create_org", "Create org", correlation_key={"attribute": "organization.slug"})
        b = journey("change_plan", "Change plan", correlation_key={"attribute": "organization.slug"})
        cs = self.cands([a, b])
        keys = [c["correlation_key"]["attribute"] for c in cs]
        self.assertEqual(len(set(keys)), 2)

    def test_outcome_with_fewer_than_two_values_is_omitted(self):
        c = self.cands([journey(outcome={"attribute": "x.outcome", "values": ["ok"]})])[0]
        self.assertNotIn("outcome", c)

    def test_outcome_values_are_flagged_for_confirmation(self):
        c = self.cands([journey(outcome={"attribute": "x.outcome", "values": ["ok", "failed"]})])[0]
        self.assertTrue(any("Outcome values" in n for n in c["needs_clarification"]))

    def test_output_is_accepted_by_intake_one_for_one(self):
        """The seam: explorer output must flow through `ce intake` without merges."""
        cs = self.cands([journey("a", "Alpha", correlation_key={"attribute": "k.id"}),
                         journey("b", "Beta", correlation_key={"attribute": "k.id"})])
        d = Path(tempfile.mkdtemp())
        (d / "j.yaml").write_text(yaml.safe_dump({"version": 1, "journeys": cs}))
        r = subprocess.run([sys.executable, str(KIT / "intake" / "resolve.py"), "--discovered",
                            str(d / "j.yaml"), "--out-json", str(d / "r.json")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads((d / "r.json").read_text())["journeys"]), 2)


class TestRepoTools(unittest.TestCase):
    def setUp(self):
        self.root = make_repo(dict(REPO, **{"node_modules/x/index.js": "needle\n", "src/a.ts": "needle\n"}))
        self.t = ex.RepoTools(self.root)

    def test_read_outside_repo_refused(self):
        self.assertIn("error", self.t.read_file("../../../etc/passwd"))

    def test_secret_unreadable(self):
        self.assertIn("error", self.t.read_file(".env"))

    def test_list_hides_secrets_and_vendored_dirs(self):
        out = self.t.list_dir(".")
        self.assertNotIn(".env", out)
        self.assertNotIn("node_modules", out)

    def test_grep_skips_vendored_and_secret_files(self):
        out = self.t.grep("needle")
        self.assertIn("src/a.ts", out)
        self.assertNotIn("node_modules", out)
        self.assertNotIn("SECRET", self.t.grep("SECRET"))

    def test_read_is_line_numbered_and_capped(self):
        (self.root / "big.ts").write_text("\n".join(f"l{i}" for i in range(1000)))
        out = self.t.read_file("big.ts", 1, 0)
        self.assertLessEqual(out.count("\n"), ex.MAX_FILE_LINES + 1)
        self.assertIn("1: l0", out)


class TestAgentLoop(unittest.TestCase):
    def test_tool_calls_then_submit(self):
        root = make_repo(REPO)
        script = [
            {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {"name": "list_dir", "arguments": json.dumps({"path": "src"})}}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "2", "function": {"name": "submit_discovery",
                                         "arguments": json.dumps({"discovery": {"journeys": [journey()]}})}}]}}]},
        ]
        seen = []

        def chat(msgs):
            seen.append(len(msgs))
            return script[len(seen) - 1]

        doc = ex.run_agent(root, "brief", "m", "k", 5, chat=chat, log=lambda *_: None)
        self.assertEqual(doc["journeys"][0]["id"], "checkout")
        self.assertEqual(len(seen), 2)

    def test_tool_result_is_fed_back(self):
        root = make_repo(REPO)
        captured = {}

        def chat(msgs):
            if len(msgs) == 2:
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "1", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/cart.ts"})}}]}}]}
            captured["last"] = msgs[-1]
            return {"choices": [{"message": {"role": "assistant", "content": '{"journeys": []}'}}]}

        ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lambda *_: None)
        self.assertEqual(captured["last"]["role"], "tool")
        self.assertIn("addToCart", captured["last"]["content"])

    def test_plain_json_reply_is_accepted(self):
        root = make_repo(REPO)
        chat = lambda msgs: {"choices": [{"message": {"role": "assistant", "content": 'here: {"journeys": []}'}}]}
        self.assertEqual(ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lambda *_: None), {"journeys": []})

    def test_budget_exhaustion_still_returns_something(self):
        root = make_repo(REPO)
        n = {"i": 0}

        def chat(msgs):
            n["i"] += 1
            if msgs[-1]["role"] == "user" and "Out of budget" in msgs[-1]["content"]:
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "z", "function": {"name": "submit_discovery",
                                             "arguments": json.dumps({"discovery": {"journeys": []}})}}]}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": str(n["i"]), "function": {"name": "list_dir", "arguments": "{}"}}]}}]}

        self.assertEqual(ex.run_agent(root, "b", "m", "k", 5, chat=chat, turns=3, log=lambda *_: None),
                         {"journeys": []})


class TestCli(unittest.TestCase):
    def test_task_mode_emits_task_without_network_or_key(self):
        root = make_repo({"src/a.go": "package main\nfunc Checkout() {}\n", "e2e/checkout.spec.ts": "test('pays', () => {})\n"})
        out = Path(tempfile.mkdtemp())
        r = subprocess.run([sys.executable, str(HERE / "explore.py"), "--repo", str(root), "--out", str(out),
                            "--mode", "task"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        task = (out / "discovery-task.md").read_text()
        self.assertIn("checkout", task)  # the e2e spec name reached the brief
        self.assertIn("--ingest", task)  # delivery instructions for the customer's own agent
        self.assertIn("You do NOT write or change any code", task)

    def test_openrouter_mode_without_key_fails_clearly(self):
        root = make_repo({"src/a.ts": "x\n"})
        r = subprocess.run([sys.executable, str(HERE / "explore.py"), "--repo", str(root), "--out",
                            tempfile.mkdtemp(), "--mode", "openrouter"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("OPENROUTER_API_KEY", r.stderr)

    def test_ingest_end_to_end(self):
        root = make_repo(REPO)
        out = Path(tempfile.mkdtemp())
        (out / "discovery.json").write_text(json.dumps({"journeys": [journey()]}))
        r = subprocess.run([sys.executable, str(HERE / "explore.py"), "--repo", str(root), "--out", str(out),
                            "--ingest", str(out / "discovery.json")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(yaml.safe_load((out / "journeys.yaml").read_text())["journeys"]), 1)
        self.assertTrue((out / "discovery-report.md").exists())


if __name__ == "__main__":
    unittest.main()
