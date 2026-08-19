#!/usr/bin/env python3
"""
Tests for the `ce` entry point.

The important ones are the `ce local` cases: that command is the whole answer to
"can I run this against a service", and its failure diagnostics matter more than
its happy path — a silent zero-envelope run is the trap people actually hit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent
CLI = KIT / "cli.py"


def ce(*args: str, cwd: Path | None = None, env: dict | None = None,
       timeout: float = 180) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True,
                          text=True, cwd=cwd, env=e, timeout=timeout)


class TestDispatch(unittest.TestCase):
    def test_help_lists_every_stage(self):
        r = ce()
        self.assertEqual(r.returncode, 0)
        for stage in ("intake", "gap", "spec", "registry", "diff", "grade",
                      "runtime", "doctor", "init", "local", "profile", "snapshot",
                      "discover", "review", "report"):
            self.assertIn(stage, r.stdout, stage)
        self.assertNotIn("bundle", r.stdout)

    def test_version(self):
        self.assertIn("0.1.0", ce("--version").stdout)

    def test_unknown_command_is_an_error_not_a_traceback(self):
        r = ce("nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown command", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_stage_help_passes_through_to_the_stage(self):
        r = ce("gap", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--resolved", r.stdout)

    def test_stage_exit_code_propagates(self):
        """A stage's own exit code must survive dispatch, or CI gating breaks."""
        r = ce("gap", "--resolved", "/nonexistent.json", "--observed", "/nope.json")
        self.assertEqual(r.returncode, 1)

    def test_works_from_an_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ce("doctor", cwd=Path(d)).returncode, 0)


class TestDoctor(unittest.TestCase):
    def test_reports_python_and_core_deps(self):
        r = ce("doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for item in ("python", "PyYAML", "node", "SENTRY_AUTH_TOKEN", "weaver",
                     "stage scripts", "ce on PATH"):
            self.assertIn(item, r.stdout)

    def test_json_mode_is_machine_readable(self):
        d = json.loads(ce("doctor", "--json").stdout)
        self.assertIn("ready", d)
        self.assertTrue(all({"name", "status", "needed_for"} <= set(c)
                            for c in d["checks"]))

    def test_optional_items_do_not_block(self):
        """A missing token or weaver must not read as 'broken' — they scope to one
        command each."""
        d = json.loads(ce("doctor", "--json").stdout)
        blocking = [c for c in d["checks"] if c["status"] == "missing"]
        self.assertEqual(blocking, [], f"unexpected blockers: {blocking}")


class TestInit(unittest.TestCase):
    def test_scaffolds_a_usable_journey_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "work"
            r = ce("init", "--out", str(out), "--journey-id", "signup",
                   "--journey-name", "Signup")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((out / "journeys.yaml").exists())
            self.assertTrue((out / "RUNBOOK.md").exists())

            # The scaffold must survive the real resolver, not just look plausible.
            resolved = out / "resolved.json"
            r2 = ce("intake", "--declared", str(out / "journeys.yaml"),
                    "--out-json", str(resolved))
            self.assertEqual(r2.returncode, 0, r2.stderr)
            doc = json.loads(resolved.read_text())
            j = doc["journeys"][0]
            self.assertEqual(j["id"], "signup")
            self.assertTrue(j["spec_ready"], j["blockers"])

    def test_does_not_clobber_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "w"
            ce("init", "--out", str(out))
            (out / "journeys.yaml").write_text("# edited by hand\n")
            ce("init", "--out", str(out))
            self.assertEqual((out / "journeys.yaml").read_text(), "# edited by hand\n")
            ce("init", "--out", str(out), "--force")
            self.assertIn("journeys:", (out / "journeys.yaml").read_text())

    def test_gitignores_the_workdir_when_run_from_the_service_root(self):
        """Customer-run: ce init from the service folder must not leave
        ce-work/ as an untracked surprise. An --out outside the repo must not
        edit that repo's .gitignore — that is a different test."""
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            r = ce("init", "--out", "ce-work", cwd=service)
            self.assertEqual(r.returncode, 0, r.stderr)
            gi = (service / ".gitignore").read_text()
            self.assertIn("ce-work/", gi)
            # Second init must not duplicate the entry.
            ce("init", "--out", "ce-work", cwd=service)
            self.assertEqual(gi.count("ce-work/"),
                             (service / ".gitignore").read_text().count("ce-work/"))


class TestLocal(unittest.TestCase):
    """`ce local` is the answer to 'run it against my service'."""

    def _work(self, d: Path, journey_id: str = "signup") -> Path:
        ce("init", "--out", str(d / "ce"), "--journey-id", journey_id)
        ce("intake", "--declared", str(d / "ce" / "journeys.yaml"),
           "--out-json", str(d / "ce" / "resolved.json"))
        return d / "ce" / "resolved.json"

    def test_missing_resolved_is_a_clear_error(self):
        r = ce("local", "--resolved", "/nope.json", "--drive", "true")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ce intake", r.stderr)

    def test_zero_envelope_run_names_the_python_orphan_span_trap(self):
        """The confusing case: the app exits 0 and sends nothing. A bare
        start_span with no transaction is dropped silently by the Python SDK."""
        with tempfile.TemporaryDirectory() as d:
            resolved = self._work(Path(d))
            r = ce("local", "--resolved", str(resolved), "--drive", "true", cwd=Path(d))
            self.assertEqual(r.returncode, 1)
            self.assertIn("no envelopes reached the collector", r.stderr)
            self.assertIn("start_transaction", r.stderr)

    def test_exports_the_dsn_into_the_drive_command(self):
        with tempfile.TemporaryDirectory() as d:
            resolved = self._work(Path(d))
            r = ce("local", "--resolved", str(resolved),
                   "--drive", 'sh -c "echo DSN=$SENTRY_DSN >&2"', cwd=Path(d))
            self.assertIn("DSN=http://publickey@127.0.0.1:", r.stderr)

    def test_never_writes_into_the_current_directory(self):
        """`ce local` from inside the service repo must not drop `.ce-observed.json`
        next to src/. A failed run (no envelopes) writes nothing. A successful
        run without --out-* writes only under ce-work/ (see TestLocalWorkdir)."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            resolved = self._work(work)
            service = work / "service"
            service.mkdir()
            before = set(p.name for p in service.iterdir())
            ce("local", "--resolved", str(resolved), "--drive", "true", cwd=service)
            after = set(p.name for p in service.iterdir())
            self.assertEqual(before, after, f"litter left behind: {after - before}")

    def test_observed_lands_next_to_the_chosen_output(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            resolved = self._work(work)
            out = work / "reports" / "gap.json"
            ce("local", "--resolved", str(resolved), "--drive", "true",
               "--out-json", str(out), cwd=work)
            # The run fails (no spans) before writing gap.json, but the observed
            # path must still be derived from --out-json, never from cwd.
            self.assertFalse((work / ".ce-observed.json").exists())

    def test_custom_dsn_env_var_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            resolved = self._work(Path(d))
            r = ce("local", "--resolved", str(resolved), "--dsn-env", "MY_DSN",
                   "--drive", 'sh -c "echo GOT=$MY_DSN >&2"', cwd=Path(d))
            self.assertIn("GOT=http://publickey@", r.stderr)

    @unittest.skipUnless(
        subprocess.run([sys.executable, "-c", "import sentry_sdk"],
                       capture_output=True).returncode == 0,
        "needs sentry-sdk installed")
    def test_end_to_end_against_a_python_service(self):
        """Language-agnostic by construction: the DSN is the only contract."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            resolved = self._work(work)
            (work / "app.py").write_text(textwrap.dedent("""
                import os, sentry_sdk
                sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0)
                for i in range(4):
                    with sentry_sdk.start_transaction(op="ui.action", name="signup") as t:
                        t.set_data("signup.id", f"su_{i}")
                        t.set_data("signup.step", "started")
                        t.set_data("signup.value", 49.0 + i)
                        t.set_data("signup.outcome", "completed")
                        t.set_data("user.plan_tier", "pro")
                        with sentry_sdk.start_span(op="function", name="signup.submitted") as s:
                            s.set_data("signup.step", "submitted")
                        with sentry_sdk.start_span(op="ui.action", name="signup.confirmed") as s:
                            s.set_data("signup.step", "confirmed")
                sentry_sdk.flush(timeout=8)
            """))
            gap = work / "gap.json"
            r = ce("local", "--resolved", str(resolved), "--journey", "signup",
                   "--drive", f"{sys.executable} app.py", "--out-json", str(gap),
                   cwd=work, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("captured", r.stderr)

            doc = json.loads(gap.read_text())
            j = next(x for x in doc["journeys"] if x["id"] == "signup")
            self.assertEqual(j["coverage_state"], "complete")
            self.assertEqual(j["steps_instrumented"], j["steps_total"])

            # The real value type must survive to the finding — this is the thing
            # static analysis could not decide.
            numeric = next(f for f in j["findings"]
                           if f["rule"] == "CE-010" and "signup.value" in f["detail"])
            self.assertTrue(numeric["passed"])
            self.assertIn("number", numeric["detail"])


class TestPackaging(unittest.TestCase):
    def test_pyproject_declares_entry_point_and_a_python_floor(self):
        """The floor's *value* is owned by test_py39_compat.py, which verifies it
        rather than asserting a literal. Hardcoding the version in two places is
        what broke this test when the floor legitimately moved to 3.9."""
        txt = (KIT / "pyproject.toml").read_text()
        self.assertIn('ce = "cli:main"', txt)
        self.assertRegex(txt, r'requires-python = ">=3\.\d+"')

    def test_requirements_file_present(self):
        self.assertTrue((KIT / "requirements.txt").exists())

    def test_only_pyyaml_is_a_hard_dependency(self):
        """The kit must run on a locked-down machine. jsonschema is optional and
        degrades; everything else is stdlib."""
        txt = (KIT / "pyproject.toml").read_text()
        deps = txt.split("dependencies = [")[1].split("]")[0]
        self.assertIn("pyyaml", deps.lower())
        for pkg in ("requests", "pydantic", "click", "jsonschema"):
            self.assertNotIn(pkg, deps.lower(), f"{pkg} should not be required")

    def test_non_editable_install_ships_stage_scripts(self):
        """A non-editable pip install used to ship only cli.py, so every stage
        404'd. That is what made this unlistable as a customer package."""
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "site"
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--target", str(dest),
                 "--no-deps", str(KIT)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            self.assertTrue((dest / "cli.py").exists())
            self.assertTrue((dest / "gap" / "propose.py").exists(),
                            f"stages missing: {list(dest.iterdir())}")
            self.assertTrue((dest / "intake" / "resolve.py").exists())
            self.assertTrue((dest / "spec" / "generate.py").exists())
            self.assertTrue(
                (dest / "warden_skill" / "sentry-critical-experience" / "SKILL.md").exists()
                or (dest / "warden-skill" / "sentry-critical-experience" / "SKILL.md").exists(),
                "Warden skill missing from the wheel",
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = str(dest) + os.pathsep + env.get("PYTHONPATH", "")
            r2 = subprocess.run(
                [sys.executable, "-c",
                 "import cli, sys; sys.exit(cli.main(['doctor']))"],
                capture_output=True, text=True, env=env, cwd=d)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn("stage scripts", r2.stdout)


class TestDiscover(unittest.TestCase):
    """Customer-run path: working files under ce-work/; specs published to .agents/."""

    def _checkout_service(self, service: Path) -> None:
        (service / "src" / "checkout").mkdir(parents=True)
        (service / "src" / "checkout" / "routes.ts").write_text(textwrap.dedent("""
            import { Router } from "express";
            export const router = Router();
            router.post("/api/checkout/session", submit);
            router.post("/api/checkout", confirm);
        """))

    def _apply_keep_first(self, service: Path, impact: str = "critical") -> None:
        """The old path was 'edit the YAML comment'. Tests go through `ce review
        --apply` so report cannot spec unreviewed junk."""
        import yaml  # type: ignore
        doc = yaml.safe_load((service / "ce-work" / "journeys.yaml").read_text())
        decisions = {"journeys": []}
        for i, j in enumerate(doc["journeys"]):
            if i == 0:
                decisions["journeys"].append({
                    "id": j["id"], "keep": True, "business_impact": impact,
                })
            else:
                decisions["journeys"].append({"id": j["id"], "keep": False})
        payload = service / "ce-work" / "decisions.json"
        payload.write_text(json.dumps(decisions))
        r = ce("review", "--apply", str(payload), "--work", "ce-work", cwd=service)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])

    def test_writes_only_under_ce_work(self):
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            self._checkout_service(service)
            before = {p.name for p in service.iterdir()}
            r = ce("discover", "--repo", str(service), "--out", "ce-work", cwd=service)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            after = {p.name for p in service.iterdir()}
            self.assertEqual(after - before, {"ce-work", ".gitignore"})
            self.assertTrue((service / "ce-work" / "journeys.yaml").exists())
            self.assertTrue((service / "ce-work" / "observed.json").exists())
            self.assertTrue((service / "ce-work" / "resolved.json").exists())
            self.assertTrue((service / "ce-work" / "REVIEW.md").exists())
            self.assertIn("ce-work/", (service / ".gitignore").read_text())
            self.assertFalse((service / "journeys.yaml").exists())
            self.assertFalse((service / ".agents").exists())

    def test_report_refuses_without_review(self):
        """`ce report` used to spec every proposed journey, including `web`.
        The stamp is the gate that does not invent business_impact."""
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            self._checkout_service(service)
            self.assertEqual(ce("discover", cwd=service).returncode, 0)
            r = ce("report", cwd=service)
            self.assertEqual(r.returncode, 1, r.stderr[-1500:])
            self.assertIn("ce review", r.stderr)
            self.assertFalse((service / ".agents").exists())

    def test_report_refuses_stamp_without_impact(self):
        """Forging `.reviewed` without assigning impact used to let report
        proceed and leave business_impact unset in the spec."""
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            self._checkout_service(service)
            self.assertEqual(ce("discover", cwd=service).returncode, 0)
            (service / "ce-work" / ".reviewed").write_text("{}\n")
            r = ce("report", cwd=service)
            self.assertEqual(r.returncode, 1, r.stderr[-1500:])
            self.assertIn("business_impact", r.stderr)

    def test_report_publishes_tracked_specs_not_why(self):
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            self._checkout_service(service)
            self.assertEqual(ce("discover", cwd=service).returncode, 0)
            self._apply_keep_first(service)
            r = ce("report", cwd=service)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            specs = list((service / "ce-work" / "specs").glob("*-SPEC.md"))
            self.assertTrue(specs, "expected a spec for the uninstrumented journey")
            text = specs[0].read_text()
            # State B, JS source, no SDK: §5 must be JS, not Python.
            self.assertIn("Sentry.startSpan", text)
            self.assertNotIn("start_transaction", text.split("## 6.")[0])
            published = list((service / ".agents" / "journeys").glob("*-SPEC.md"))
            self.assertEqual(len(published), 1)
            self.assertFalse(list((service / ".agents" / "journeys").glob("*-WHY.md")))
            self.assertTrue((service / "warden.toml").exists())
            self.assertIn("failOn = \"off\"", (service / "warden.toml").read_text())
            self.assertTrue(
                (service / ".agents" / "skills" / "sentry-critical-experience"
                 / "SKILL.md").is_file()
            )
            self.assertIn("ce:sentry-journeys", (service / "AGENTS.md").read_text())
            self.assertTrue((service / "ce-work" / "EXPLORE.md").exists())
            self.assertFalse((service / "ce-work.zip").exists())

    def test_unsupported_language_exits_2(self):
        """A Go-only tree used to produce no candidates and a confusing empty
        ce-work/. Fail loud and point at `ce init`."""
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            (service / "main.go").write_text("package main\nfunc main() {}\n")
            r = ce("discover", cwd=service)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("ce init", r.stderr)
            self.assertFalse((service / "ce-work" / "journeys.yaml").exists())

    def test_sentry_without_token_writes_mcp_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            service = Path(d)
            self._checkout_service(service)
            env = {"SENTRY_AUTH_TOKEN": ""}
            r = ce("discover", "--sentry", cwd=service, env=env)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            self.assertTrue((service / "ce-work" / "SENTRY-MCP.md").exists())
            self.assertIn("mcp_auth", (service / "ce-work" / "SENTRY-MCP.md").read_text())


class TestLocalWorkdir(unittest.TestCase):
    def test_allow_empty_writes_only_under_ce_work(self):
        """Successful local with no --out-* used to dump a temp file or litter
        cwd. It must land in ce-work/ and nowhere else next to source."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            ce("init", "--out", str(work / "ce"), "--journey-id", "signup")
            ce("intake", "--declared", str(work / "ce" / "journeys.yaml"),
               "--out-json", str(work / "ce" / "resolved.json"))
            service = work / "service"
            service.mkdir()
            r = ce("local", "--resolved", str(work / "ce" / "resolved.json"),
                   "--drive", "true", "--allow-empty", cwd=service)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
            leftover = {p.name for p in service.iterdir()} - {"ce-work", ".gitignore"}
            self.assertEqual(leftover, set(), f"litter: {leftover}")
            self.assertTrue((service / "ce-work" / "observed.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
