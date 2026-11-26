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
    "src/refund.ts": "export function refundOrder() {}\n",
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

        doc = ex.run_agent(root, "b", "m", "k", 5, chat=chat, turns=3, log=lambda *_: None)
        self.assertEqual(doc["journeys"], [])
        self.assertTrue(doc["_accepted_at_cap"])


SIGNALS = {"e2e_groups": [
    {"dir": "e2e/billing", "files": 3, "stems": ["checkout", "refund-flow", "global.setup"]},
    {"dir": "e2e/utils", "files": 1, "stems": ["clipboard"]},          # support dir: never a seed
    {"dir": "examples/demo/e2e", "files": 1, "stems": ["demo"]},        # noise path: never a seed
]}


def submit(doc, cid="s"):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "function": {"name": "submit_discovery", "arguments": json.dumps({"discovery": doc})}}]}}]}


def cov(**kw):
    return [dict({"seed": "S1"}, **kw)]


class TestStemAccounting(unittest.TestCase):
    """Live run 2: the gate passed with 25 of 27 seeds rejected and all 32 studio tests
    mapped to ONE journey (deploy_edge_function). Accounting is now per test name, checked
    against the validated journeys, with enumerated rejection categories."""

    def setUp(self):
        self.root = make_repo(REPO)

    def acct(self, doc, signals=SIGNALS):
        return ex.validate(doc, self.root, signals)[1]["accounting"]

    def test_seeds_exclude_support_dirs_noise_paths_and_setup_stems(self):
        sds = ex.sig.seeds(SIGNALS)
        self.assertEqual([(s["id"], s["dir"], s["stems"]) for s in sds],
                         [("S1", "e2e/billing", ["checkout", "refund-flow"])])

    def test_everything_covered_or_rejected_is_accounted(self):
        doc = {"journeys": [journey()], "seed_coverage": cov(
            covers={"checkout": ["checkout"]},
            rejected=[{"stems": ["refund-flow"], "category": "test_helper", "reason": "shared page helpers"}])}
        a = self.acct(doc)
        self.assertEqual((a["missing"], a["weak"], a["ok"], a["total"]), ([], [], 2, 2))

    def test_stem_dumped_on_an_unrelated_journey_is_weak_and_missing(self):
        doc = {"journeys": [journey()], "seed_coverage": cov(
            covers={"checkout": ["checkout", "refund-flow"]})}
        a = self.acct(doc)
        self.assertEqual(a["weak"], [{"seed": "S1", "stem": "refund-flow", "journey": "checkout"}])
        self.assertEqual(a["missing"][0]["stems"], ["refund-flow"])

    def test_star_is_only_allowed_for_small_seeds(self):
        big = {"e2e_groups": [{"dir": "e2e/app", "files": 4, "stems": ["checkout", "a1", "a2", "a3"]}]}
        doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": "*"})}
        self.assertEqual(len(self.acct(doc, big)["missing"][0]["stems"]), 4)
        small = {"e2e_groups": [{"dir": "e2e/app", "files": 1, "stems": ["checkout"]}]}
        self.assertEqual(self.acct(doc, small)["missing"], [])

    def test_unknown_category_or_thin_reason_is_not_a_rejection(self):
        for rej in ({"category": "meh", "reason": "not important enough"},
                    {"category": "tooling", "reason": "no"}):
            doc = {"journeys": [journey()], "seed_coverage": cov(
                covers={"checkout": ["checkout"]}, rejected=[dict({"stems": ["refund-flow"]}, **rej)])}
            self.assertEqual(self.acct(doc)["missing"][0]["stems"], ["refund-flow"])

    def test_not_consequence_bearing_needs_a_verifiable_citation(self):
        rej = {"stems": ["refund-flow"], "category": "not_consequence_bearing", "reason": "a UI preference toggle"}
        doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": ["checkout"]}, rejected=[rej])}
        self.assertEqual(self.acct(doc)["missing"][0]["stems"], ["refund-flow"])
        rej["evidence"] = {"file": "src/refund.ts", "line": 1, "symbol": "refundOrder"}
        self.assertEqual(self.acct(doc)["missing"], [])
        rej["evidence"] = {"file": "src/nope.ts"}
        self.assertEqual(self.acct(doc)["missing"][0]["stems"], ["refund-flow"])

    def test_citation_unrelated_to_the_rejected_stems_does_not_hold(self):
        """Live run 3: `assistant` and `filter-bar` were rejected 'not consequence-bearing'
        with a citation to SQLEditor.tsx, a file that exists and has nothing to do with them."""
        rej = {"stems": ["refund-flow"], "category": "not_consequence_bearing", "reason": "a UI preference toggle",
               "evidence": {"file": "src/cart.ts", "line": 1, "symbol": "addToCart"}}
        doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": ["checkout"]}, rejected=[rej])}
        a = self.acct(doc)
        self.assertEqual(a["missing"][0]["stems"], ["refund-flow"])
        self.assertIn("no vocabulary", a["bad_rejections"][0]["why"])

    def test_reason_admitting_it_was_not_read_is_not_a_reason(self):
        for reason in ("AI assistant panel. I did not trace it", "I did not find the components",
                       "could not verify this flow", "untraced helper stuff"):
            rej = {"stems": ["refund-flow"], "category": "tooling", "reason": reason}
            doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": ["checkout"]}, rejected=[rej])}
            a = self.acct(doc)
            self.assertEqual(a["missing"][0]["stems"], ["refund-flow"], reason)
            self.assertTrue(a["bad_rejections"], reason)

    def test_ordinary_negation_in_a_reason_is_fine(self):
        rej = {"stems": ["refund-flow"], "category": "tooling", "reason": "not a product flow, shared test harness"}
        doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": ["checkout"]}, rejected=[rej])}
        self.assertEqual(self.acct(doc)["missing"], [])

    def test_journey_over_the_stem_cap_is_flagged_overloaded(self):
        """MetaMask: send_funds held 41 stems, including Ledger and NFT sends."""
        stems = [f"checkout-{i}" for i in range(ex.MAX_STEMS_PER_JOURNEY + 3)]
        sigs = {"e2e_groups": [{"dir": "e2e/pay", "files": len(stems), "stems": stems}]}
        doc = {"journeys": [journey()], "seed_coverage": cov(covers={"checkout": stems})}
        a = self.acct(doc, sigs)
        self.assertEqual(a["missing"], [])
        self.assertEqual(a["overloaded"], [{"journey": "checkout", "stems": len(stems)}])

    def test_mapping_to_a_journey_the_validator_drops_does_not_count(self):
        """MetaMask: S29 was mapped to perps_close_position, which the validator then dropped
        for lacking two verifiable steps; the raw-submit gate still passed it."""
        thin = journey(steps=[{"id": "a", "evidence": [{"file": "src/cart.ts", "line": 1, "symbol": "addToCart"}]},
                              {"id": "b", "evidence": [{"file": "src/nope.ts"}]}])
        doc = {"journeys": [thin], "seed_coverage": cov(
            covers={"checkout": ["checkout"]},
            rejected=[{"stems": ["refund-flow"], "category": "test_helper", "reason": "shared page helpers"}])}
        a = self.acct(doc)
        self.assertEqual(a["missing"][0]["stems"], ["checkout"])

    def test_no_e2e_signals_means_nothing_to_account_for(self):
        self.assertEqual(self.acct({"journeys": []}, {"e2e_groups": []})["total"], 0)

    def test_report_shows_gaps(self):
        j, rep = ex.validate({"journeys": [journey()]}, self.root, SIGNALS)
        out = ex.render_report(j, rep, "r", "t")
        self.assertIn("0 of 2 test names accounted for", out)
        self.assertIn("refund-flow", out)


class TestTurnBudget(unittest.TestCase):
    """Live run 4: MetaMask used its 45 turns, the model was told 'out of budget', and that
    submit (with 53 test names unaccounted for, incl. buy-ramps) skipped the gate."""

    def test_auto_turns_scale_with_stems_and_are_capped(self):
        small = {"e2e_groups": [{"dir": "e2e/a", "files": 1, "stems": ["checkout"]}]}
        big = {"e2e_groups": [{"dir": f"e2e/g{i}", "files": 40, "stems": [f"s{i}-{k}" for k in range(40)]}
                              for i in range(60)]}
        self.assertEqual(ex.auto_turns({"e2e_groups": []}), ex.MAX_TURNS)
        self.assertEqual(ex.auto_turns(small), ex.MAX_TURNS)
        self.assertGreater(ex.auto_turns({"e2e_groups": big["e2e_groups"][:10]}), ex.MAX_TURNS)
        self.assertEqual(ex.auto_turns(big), 120)

    def test_a_bounce_buys_extra_turns(self):
        root = make_repo(REPO)
        n = {"i": 0}

        def chat(msgs):
            n["i"] += 1
            if n["i"] <= 2:   # two reads exhaust turns=2, then the model submits incompletely
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": str(n["i"]), "function": {"name": "read_file",
                                                     "arguments": json.dumps({"path": "src/cart.ts"})}}]}}]}
            if n["i"] == 3:
                return submit({"journeys": [journey()]}, cid="x")
            if n["i"] == 4:   # after the bounce the model must still be allowed to keep working
                return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "r", "function": {"name": "read_file",
                                             "arguments": json.dumps({"path": "src/pay.ts"})}}]}}]}
            return submit({"journeys": [journey()], "seed_coverage": cov(
                covers={"checkout": ["checkout"]},
                rejected=[{"stems": ["refund-flow"], "category": "test_helper",
                           "reason": "shared page helpers"}])}, cid="y")

        doc = ex.run_agent(root, "b", "m", "k", 5, chat=chat, turns=3, log=lambda *_: None, signals=SIGNALS)
        self.assertNotIn("_accepted_at_cap", doc)   # finished inside the extended budget
        self.assertIn("seed_coverage", doc)

    def test_cap_submit_is_marked_and_reported(self):
        root = make_repo(REPO)
        n = {"i": 0}

        def chat(msgs):
            n["i"] += 1
            if msgs[-1]["role"] == "user" and "Out of budget" in msgs[-1]["content"]:
                return submit({"journeys": [journey()]}, cid="z")
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": str(n["i"]), "function": {"name": "list_dir", "arguments": "{}"}}]}}]}

        doc = ex.run_agent(root, "b", "m", "k", 5, chat=chat, turns=2, log=lambda *_: None)
        self.assertTrue(doc["_accepted_at_cap"])
        out = Path(tempfile.mkdtemp())
        ex.ingest(doc, root, SIGNALS, out, "t")
        self.assertIn("Turn budget ran out", (out / "discovery-report.md").read_text())


class TestCostControl(unittest.TestCase):
    def test_old_tool_results_are_elided_but_stay_paired(self):
        msgs, labels = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}], {}
        for i in range(30):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": str(i)}]})
            msgs.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 5000})
            labels[str(i)] = f"read_file f{i}.ts"
        n = ex.elide_old_results(msgs, labels)
        tools = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual(n, 20)
        self.assertEqual(len(tools), 30)  # nothing removed: tool_call ids still pair up
        self.assertTrue(all(t["content"].startswith("[elided: read_file") for t in tools[:20]))
        self.assertTrue(all(len(t["content"]) == 5000 for t in tools[20:]))
        self.assertEqual(ex.elide_old_results(msgs, labels), 0)  # idempotent

    def test_few_results_are_left_alone(self):
        msgs = [{"role": "tool", "tool_call_id": "a", "content": "y" * 100}] * 5
        self.assertEqual(ex.elide_old_results(msgs, {"a": "grep"}), 0)

    def test_gate_message_is_never_elided(self):
        msgs = [{"role": "tool", "tool_call_id": "gate", "content": "Not accepted yet"}] * 40
        self.assertEqual(ex.elide_old_results(msgs, {}), 0)

    def test_request_size_grows_sublinearly_with_turns(self):
        """The quadratic input-token growth behind $6.39 on MetaMask: with elision, total
        characters sent over a 40-turn run stay far below the unelided sum."""
        root = make_repo(REPO)
        sent = []
        n = {"i": 0}

        def chat(msgs):
            sent.append(sum(len(str(m.get("content"))) for m in msgs))
            n["i"] += 1
            if n["i"] > 40:
                return {"choices": [{"message": {"role": "assistant", "content": '{"journeys": []}'}}]}
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": str(n["i"]), "function": {"name": "read_file",
                                                 "arguments": json.dumps({"path": "src/cart.ts"})}}]}}]}

        ex.run_agent(root, "b", "m", "k", 5, chat=chat, turns=50, log=lambda *_: None)
        self.assertLess(sent[-1], sent[17] * 1.8)  # flat after the window fills, not linear

    def test_anthropic_models_get_a_cacheable_prefix_others_do_not(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"},
                {"role": "assistant", "content": "a"}]
        out = ex.prepare_messages(msgs, "anthropic/claude-sonnet-5.5")
        self.assertEqual(out[1]["content"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(msgs[1]["content"], "task")  # input untouched
        self.assertIs(ex.prepare_messages(msgs, "openai/gpt-x"), msgs)


class TestSeedGate(unittest.TestCase):
    def full(self):
        return {"journeys": [journey()], "seed_coverage": cov(
            covers={"checkout": ["checkout"]},
            rejected=[{"stems": ["refund-flow"], "category": "test_helper", "reason": "shared page helpers"}])}

    def test_agent_bounces_incomplete_submit_then_accepts_complete_one(self):
        root = make_repo(REPO)
        replies = [submit({"journeys": [journey()]}), submit(self.full())]
        seen = []

        def chat(msgs):
            seen.append(msgs[-1])
            return replies[len(seen) - 1]

        doc = ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lambda *_: None, signals=SIGNALS)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["role"], "tool")
        self.assertIn("refund-flow", seen[1]["content"])
        self.assertIn("seed_coverage", doc)

    def test_gate_gives_up_after_two_bounces(self):
        root = make_repo(REPO)
        n = {"i": 0}

        def chat(msgs):
            n["i"] += 1
            return submit({"journeys": [journey()]}, cid=str(n["i"]))

        doc = ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lambda *_: None, signals=SIGNALS)
        self.assertEqual(n["i"], 3)          # two bounces, third accepted as is
        self.assertEqual(len(doc["journeys"]), 1)

    def test_bounce_names_journeys_the_validator_dropped(self):
        root = make_repo(REPO)
        thin = journey(steps=[{"id": "a", "evidence": [{"file": "src/nope.ts"}]}])
        replies = [submit({"journeys": [thin]}), submit(self.full())]
        seen = []

        def chat(msgs):
            seen.append(msgs[-1])
            return replies[len(seen) - 1]

        ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lambda *_: None, signals=SIGNALS)
        self.assertIn("dropped", seen[1]["content"])

    def test_token_usage_is_logged(self):
        root = make_repo(REPO)
        lines = []
        chat = lambda msgs: {"usage": {"prompt_tokens": 100, "completion_tokens": 7},
                             "choices": [{"message": {"role": "assistant", "content": '{"journeys": []}'}}]}
        ex.run_agent(root, "b", "m", "k", 5, chat=chat, log=lines.append)
        self.assertTrue(any("100 in / 7 out" in l for l in lines))


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
