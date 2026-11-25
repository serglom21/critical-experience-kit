#!/usr/bin/env python3
"""
Tier-1 signals: language-agnostic facts about a repo that point at critical journeys.

`propose.py` guesses journeys from directory names and exported function names, in
two languages. That fails on real repos (Supabase: five "journeys" that were app
folders and icon components; MetaMask: `ui` and a bucket of mock builders). This
module replaces guessing with *evidence that does not depend on language*:

  e2e tests      A human already wrote the flows down. `e2e/studio/features/storage.spec.ts`
                 is a journey name and a test list. Strongest signal there is.
  analytics      `track('checkout_completed')` is the product team's own funnel.
  docs           README / docs headings name the product's capabilities.
  entry points   routes, controllers, message handlers, jobs, CLI commands, UI pages —
                 detected by cross-language hint patterns, counted per directory.
  sinks          money, identity, messaging, signing, storage writes. A directory that
                 reaches several is where consequences live. This is structural, never
                 volume: AGENTS.md invariant 3 stands.
  hot dirs       directories ranked by the above, each carrying its evidence.

Nothing here decides which journey matters (invariant 4). It produces a compact,
cited brief for the explorer — an LLM agent, or the customer's own — which proposes
journeys that must cite files this module (or its own reads) can verify.

Usage:
    python3 gap/signals.py --repo . --out ce-work/signals.json --brief ce-work/signals.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "coverage", "venv", ".venv",
    "__pycache__", "site-packages", ".tox", "target", "vendor", "third_party",
    "migrations", "generated", "__snapshots__", ".turbo", ".cache", "out", ".yarn",
    ".pnpm-store", "Pods", ".gradle", ".idea", ".vscode", "bower_components", ".aws",
    ".ssh", ".gnupg", ".secrets",
}
SECRET_NAMES = {".env", "credentials.json", "credentials.yml", "credentials.yaml",
                "id_rsa", "id_ed25519", "service-account.json"}
SECRET_SUFFIX = {".pem", ".p12", ".pfx", ".key"}
# Noise: files that describe code rather than being it. Journeys proposed from these
# were the baseline's worst failure (mock builders became "steps").
NOISE_PATH = re.compile(
    r"(^|/)(__mocks__|__fixtures__|fixtures|mocks?|stories|storybook|\.storybook|"
    r"design-system|ui-library|examples?|samples?|templates?|docs?-?examples?|"
    r"snapshots?|testing|test-utils|testutils|www|website|marketing|landing|blog|docs|kb|"
    r"learn|handbook|changelog)(/|$)", re.I)
# e2e *support* code (page objects, helpers, mocks) names things in the product's
# vocabulary but is not a suite; crediting it as coverage made every dir look covered.
E2E_SUPPORT = re.compile(r"(^|/)(page-objects?|pages|helpers?|utils?|scripts?|fixtures?|mocks?|"
                         r"benchmarks?|snaps|seeder|reporters?|common|shared|support|flows?)(/|$)", re.I)
TEST_FILE = re.compile(
    r"(\.(test|spec|e2e|cy|stories)\.[A-Za-z0-9]+$)|(^|/)test_[^/]*\.py$|_test\.(go|py|rb)$|"
    r"Test\.(java|kt)$|Tests?\.(cs|swift)$|\.feature$")
# `features/` is deliberately NOT here: it is a source-code convention in many apps
# (apps/docs/features/...), and counting it drowned the real e2e suites.
E2E_DIR = re.compile(r"(^|/)(e2e|end-to-end|playwright|cypress|integration-tests?|"
                     r"acceptance|journeys|scenarios)(/|$)", re.I)
I18N_DIR = re.compile(r"(^|/)(i18n|l10n|locales?|translations?)(/|$)", re.I)

CODE_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb", ".java", ".kt",
    ".swift", ".cs", ".php", ".rs", ".ex", ".exs", ".scala", ".dart", ".vue", ".svelte",
    ".sol", ".cpp", ".cc", ".c", ".m", ".mm",
}
MAX_BYTES = 400_000
MAX_FILES = 60_000

LANG_BY_EXT = {
    ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".py": "Python", ".go": "Go",
    ".rb": "Ruby", ".java": "Java", ".kt": "Kotlin", ".swift": "Swift", ".cs": "C#",
    ".php": "PHP", ".rs": "Rust", ".ex": "Elixir", ".exs": "Elixir", ".scala": "Scala",
    ".dart": "Dart", ".vue": "Vue", ".svelte": "Svelte", ".sol": "Solidity",
    ".cpp": "C++", ".cc": "C++", ".c": "C", ".m": "Objective-C", ".mm": "Objective-C",
}

# --- test titles (cross-language) ------------------------------------------
TEST_TITLE = [
    re.compile(r"""\b(?:test|it|describe|specify|scenario)(?:\.(?:only|skip|each))?\s*\(\s*['"`]([^'"`\n]{4,120})['"`]"""),
    re.compile(r"""^\s*(?:Scenario(?: Outline)?|Feature):\s*(.{4,120})$""", re.M),
    re.compile(r"""^\s*(?:async\s+)?def\s+(test_[a-z0-9_]{4,80})""", re.M),
    re.compile(r"""\bfunc\s+(Test[A-Z][A-Za-z0-9_]{3,80})\s*\("""),
    re.compile(r"""@Test[^\n]*\n\s*(?:public\s+)?void\s+([a-zA-Z0-9_]{4,80})"""),
    re.compile(r"""^\s*(?:it|scenario|feature|describe|context)\s+['"]([^'"\n]{4,120})['"]""", re.M),
]

# --- analytics events -------------------------------------------------------
ANALYTICS = re.compile(
    r"""\b(?:track|trackEvent|trackEventAsync|logEvent|capture|posthog\.capture|analytics\.track|"""
    r"""mixpanel\.track|amplitude\.track|segment\.track|sendEvent|emitEvent)\w*\s*\(\s*"""
    r"""['"`]([A-Za-z][A-Za-z0-9 _.:/\-]{3,80})['"`]""")
ANALYTICS_ENUM = re.compile(r"""\b([A-Z][A-Za-z0-9]*EventName|[A-Z][A-Za-z0-9]*Events?)\.([A-Z][A-Za-z0-9_]{3,60})\b""")

# --- entry points -----------------------------------------------------------
ENTRY = {
    "http_route": [
        re.compile(r"""\b(?:app|router|server|fastify|api|r|e|mux|route|routes)\s*\.\s*(?:get|post|put|patch|delete|all|handle|handlefunc|use)\s*\(\s*['"`/]"""),
        re.compile(r"""@\s*(?:\w+\.)?(?:get|post|put|patch|delete|route|api_view|app\.route|requestmapping|getmapping|postmapping|putmapping|deletemapping|controller|httpget|httppost|httpput|httpdelete)\b""", re.I),
        re.compile(r"""\b(?:urlpatterns|path\s*\(\s*['"]|re_path\s*\(|resources\s+:|Rails\.application\.routes|get\s+['"]/|post\s+['"]/|http\.HandleFunc|mux\.Handle|gin\.Default|echo\.New|chi\.NewRouter)"""),
    ],
    "message_handler": [
        re.compile(r"""\b(?:onMessage|onConnect|ipcMain\.(?:on|handle)|chrome\.runtime\.on\w+|browser\.runtime\.on\w+|addEventListener\(\s*['"]message|socket\.on|ws\.on|channel\.on|port\.on)\b"""),
        re.compile(r"""\b(?:handleRpcRequest|json-?rpc|createMethodMiddleware|registerActionHandler|messenger\.registerActionHandler)\b""", re.I),
    ],
    "job_or_consumer": [
        re.compile(r"""\b(?:@Processor|@Cron|@Scheduled|@shared_task|@celery\.task|@task\b|cron\.schedule|schedule\.every|queue\.process|new\s+Worker\(|kafka\.consumer|consumer\.subscribe|@EventListener|@RabbitListener|@KafkaListener|sidekiq)\b""", re.I),
    ],
    "cli_command": [
        re.compile(r"""\b(?:cobra\.Command|@click\.command|argparse\.ArgumentParser|yargs|commander|\.command\(\s*['"]|clap::|Thor\b)"""),
    ],
    "ui_route": [
        re.compile(r"""\b(?:createBrowserRouter|<Route\b|Routes\b|useRoutes|createRoutesFromElements|vue-router|createRouter\(|NavigationStack|NavHost|composable\()"""),
        re.compile(r"""\bpath\s*:\s*['"`]/[A-Za-z0-9_\-/:]+['"`]"""),
    ],
}

# --- sinks: where consequences live ----------------------------------------
SINKS = {
    "payments": re.compile(r"\b(stripe|paypal|adyen|braintree|checkout\.com|razorpay|mollie|klarna|invoice|subscription|billing|payment|refund|charge|chargeback|payout|coupon|credit[_ ]?card|spend[_ ]?cap|entitlement)\b", re.I),
    "identity": re.compile(r"\b(sign[_ -]?in|sign[_ -]?up|signin|signup|log[_ -]?in|log[_ -]?out|logout|oauth|saml|sso|jwt|password|passkey|totp|mfa|2fa|session[_ -]?token|refresh[_ -]?token|api[_ -]?key|invite|invitation|role[_ -]?assign|permission)\b", re.I),
    "signing_or_funds": re.compile(r"\b(signTransaction|signMessage|signTypedData|sendTransaction|personal_sign|eth_sendTransaction|private[_ ]?key|mnemonic|seed[_ ]?phrase|keyring|vault|wallet|swap|bridge|transfer|withdraw|deposit|approve)\b", re.I),
    "messaging": re.compile(r"\b(sendgrid|postmark|resend|twilio|mailgun|sendEmail|send_email|sendSms|push[_ ]?notification|webhook|notify|notification)\b", re.I),
    "data_write": re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|\.insert\(|\.upsert\(|\.save\(|\.create\(|\.destroy\(|\.delete\(|\.update\(|bulk_create|CREATE\s+TABLE|ALTER\s+TABLE)\b"),
    "storage_files": re.compile(r"\b(upload|bucket|s3\.|putObject|multipart|blob[_ ]?storage|file[_ ]?upload)\b", re.I),
    "provisioning": re.compile(r"\b(provision|create[_ ]?project|create[_ ]?org|deploy|migrate|backup|restore|onboard|onboarding|checkout)\b", re.I),
}

DEP_FILES = ("package.json", "requirements.txt", "pyproject.toml", "Pipfile", "go.mod",
             "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
             "Cargo.toml", "mix.exs", "pubspec.yaml", "Package.swift")
DEP_SIGNALS = {
    "stripe": "payments", "paypal": "payments", "adyen": "payments", "braintree": "payments",
    "razorpay": "payments", "mollie": "payments", "klarna": "payments",
    "auth0": "identity", "clerk": "identity", "okta": "identity", "passport": "identity",
    "next-auth": "identity", "keycloak": "identity", "firebase-auth": "identity",
    "twilio": "messaging", "sendgrid": "messaging", "postmark": "messaging", "resend": "messaging",
    "ethers": "signing_or_funds", "web3": "signing_or_funds", "viem": "signing_or_funds",
    "@solana": "signing_or_funds", "bitcoinjs": "signing_or_funds",
    "prisma": "data_write", "sequelize": "data_write", "typeorm": "data_write",
    "sqlalchemy": "data_write", "gorm": "data_write", "activerecord": "data_write",
    "aws-sdk": "storage_files", "@aws-sdk": "storage_files", "multer": "storage_files",
    "@sentry/": "observability", "sentry-sdk": "observability", "sentry-go": "observability",
}


def is_secret(p: Path) -> bool:
    return p.name in SECRET_NAMES or p.name.startswith(".env.") or p.suffix.lower() in SECRET_SUFFIX


def walk(root: Path):
    """Yield (path, rel) for readable source/doc/test files, skipping vendored and
    generated dirs and credential files. Bounded: huge monorepos must not hang."""
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if p.name in SKIP_DIRS:
                    continue
                stack.append(p)
            elif p.is_file():
                if is_secret(p):
                    continue
                if p.suffix.lower() not in CODE_EXT and p.suffix.lower() not in {".feature", ".md", ".mdx", ".yaml", ".yml", ".json"}:
                    continue
                n += 1
                if n > MAX_FILES:
                    return
                yield p, str(p.relative_to(root))


def read(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_BYTES:
            return ""
        return p.read_text(errors="replace")
    except OSError:
        return ""


def norm_tokens(s: str) -> list:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t]


def dir_key(rel: str, depth: int = 6) -> str:
    parts = rel.split("/")[:-1]
    return "/".join(parts[:depth]) or "."


def collect(root: Path) -> dict:
    langs: Counter = Counter()
    def _grp():
        return {"files": 0, "titles": [], "stems": [], "sample_files": []}

    e2e: dict = defaultdict(_grp)
    unit: dict = defaultdict(_grp)
    analytics: Counter = Counter()
    analytics_files: dict = defaultdict(set)
    entry: dict = defaultdict(Counter)
    sink: dict = defaultdict(Counter)
    entry_samples: dict = defaultdict(list)
    files_in_dir: Counter = Counter()
    docs: list = []
    deps: dict = {}
    manifests: list = []

    for p, rel in walk(root):
        ext = p.suffix.lower()
        base = p.name
        if base in DEP_FILES:
            manifests.append(rel)
            body = read(p).lower()
            for dep, kind in DEP_SIGNALS.items():
                if dep in body:
                    deps.setdefault(kind, set()).add(dep)
            continue
        if ext in {".md", ".mdx"}:
            if I18N_DIR.search(rel):
                continue
            if len(docs) < 12 and (base.lower().startswith("readme") and rel.count("/") <= 1
                                   or rel.lower().startswith("docs/") and rel.count("/") <= 2):
                heads = [m.group(1).strip() for m in re.finditer(r"^#{1,3}\s+(.{3,80})$", read(p), re.M)][:25]
                docs.append({"file": rel, "headings": heads})
            continue
        if ext not in CODE_EXT and ext != ".feature":
            continue
        langs[LANG_BY_EXT.get(ext, ext)] += 1
        text = read(p)
        if not text:
            continue
        is_test = bool(TEST_FILE.search(rel)) or bool(E2E_DIR.search(rel))
        key = dir_key(rel)
        files_in_dir[key] += 1

        if TEST_FILE.search(rel) or E2E_DIR.search(rel):
            titles = []
            for rx in TEST_TITLE:
                titles += [m.group(1).strip() for m in rx.finditer(text)]
            # Real e2e suites are the strongest signal; colocated unit tests are weak
            # (they describe components, not journeys) and must not outrank them.
            bucket = e2e if (E2E_DIR.search(rel) or ext == ".feature") else unit
            rec = bucket[key]
            rec["files"] += 1
            stem = re.sub(r"(\.(spec|test|e2e|cy|feature))?\.[A-Za-z0-9]+$", "", base)
            if stem not in rec["stems"] and len(rec["stems"]) < 40:
                rec["stems"].append(stem)
            for t in titles[:2]:
                if len(rec["titles"]) < 6 and t not in rec["titles"]:
                    rec["titles"].append(t)
            if len(rec["sample_files"]) < 6:
                rec["sample_files"].append(rel)
            continue  # tests are not entry points or sinks

        if NOISE_PATH.search(rel):
            continue

        for m in ANALYTICS.finditer(text):
            analytics[m.group(1)] += 1
            analytics_files[m.group(1)].add(rel)
        for m in ANALYTICS_ENUM.finditer(text):
            ev = m.group(1) + "." + m.group(2)
            analytics[ev] += 1
            analytics_files[ev].add(rel)

        for kind, rxs in ENTRY.items():
            hits = sum(len(rx.findall(text)) for rx in rxs)
            if hits:
                entry[key][kind] += hits
                if len(entry_samples[key]) < 3:
                    entry_samples[key].append(rel)
        for kind, rx in SINKS.items():
            hits = len(rx.findall(text))
            if hits:
                sink[key][kind] += min(hits, 25)  # cap: one giant file must not dominate

    # e2e coverage cross-reference: a source dir is "e2e-covered" if a test file or
    # test dir names its leaf. Name-token overlap, language-agnostic.
    e2e_tokens: dict = defaultdict(set)
    for k, rec in e2e.items():
        if E2E_SUPPORT.search(k + "/"):
            continue
        for stem in rec["stems"]:
            e2e_tokens[k].update(norm_tokens(stem))
        e2e_tokens[k].update(norm_tokens(k.split("/")[-1]))

    hot = []
    all_dirs = set(files_in_dir) | set(entry) | set(sink)
    for d in all_dirs:
        if NOISE_PATH.search(d + "/"):
            continue
        sk = sink.get(d, Counter())
        en = entry.get(d, Counter())
        leaf = set(norm_tokens(d.split("/")[-1]))
        covered = sorted({k for k, toks in e2e_tokens.items()
                          if leaf and (leaf & toks) and k != d})[:3]
        # Density, not presence: a regex word hit in a big directory is noise. A sink
        # kind counts only when it recurs relative to the directory's size.
        nfiles = max(files_in_dir.get(d, 0), 1)
        n_sink = len([k for k, v in sk.items() if v >= 4 and v / nfiles >= 0.6])
        score = n_sink * 2 + min(sum(en.values()), 20) / 4 + (3 if covered else 0)
        if score <= 0:
            continue
        hot.append({
            "dir": d,
            "files": files_in_dir.get(d, 0),
            "sinks": dict(sk.most_common(5)),
            "entry_points": dict(en),
            "entry_sample_files": entry_samples.get(d, []),
            "e2e_covered_by": covered,
            "score": round(score, 2),
        })
    hot.sort(key=lambda h: (-h["score"], h["dir"]))

    top_analytics = [{"event": ev, "count": c, "files": sorted(analytics_files[ev])[:3]}
                     for ev, c in analytics.most_common(60)]
    e2e_groups = sorted(
        ({"dir": k, "files": v["files"], "stems": v["stems"], "titles": v["titles"],
          "sample_files": v["sample_files"]} for k, v in e2e.items()),
        key=lambda g: (-g["files"], g["dir"]))[:80]
    unit_groups = sorted(
        ({"dir": k, "files": v["files"], "stems": v["stems"][:8]} for k, v in unit.items()),
        key=lambda g: (-g["files"], g["dir"]))[:60]

    return {
        "root": str(root),
        "languages": dict(langs.most_common(8)),
        "manifests": manifests[:40],
        "dependency_signals": {k: sorted(v) for k, v in deps.items()},
        "docs": docs,
        "e2e_groups": e2e_groups,
        "unit_test_dirs": unit_groups,
        "analytics_events": top_analytics,
        "hot_dirs": hot[:80],
        "_provenance": {
            "kind": "static, language-agnostic hints",
            "note": "Regex hints across languages. They locate where to read; they do not "
                    "prove a flow exists or matters. Counts are capped per file.",
        },
    }


def seeds(sig: dict, max_e2e: int = 40) -> list:
    """Numbered e2e groups the explorer must account for (journey or reasoned rejection).
    The ids are shown in the brief and checked mechanically by gap/explore.py."""
    return [{"id": f"S{i + 1}", "dir": g["dir"], "files": g["files"], "stems": g["stems"][:14]}
            for i, g in enumerate(sig.get("e2e_groups", [])[:max_e2e])]


def render_brief(sig: dict, max_hot: int = 45, max_e2e: int = 40, max_events: int = 30) -> str:
    L = ["# Repo signals", ""]
    L.append("Languages: " + ", ".join(f"{k} ({v})" for k, v in sig["languages"].items()))
    if sig["dependency_signals"]:
        L.append("Dependency signals: " + "; ".join(
            f"{k}: {', '.join(v)}" for k, v in sig["dependency_signals"].items()))
    L.append("")
    if sig["docs"]:
        L += ["## Docs headings (what the product says it does)"]
        for d in sig["docs"][:4]:
            L.append(f"- `{d['file']}`: " + " · ".join(d["headings"][:14]))
        L.append("")
    if sig["e2e_groups"]:
        L += ["## E2E / integration tests (humans already wrote these flows down)",
              "Each seed id below must appear in `seed_coverage` in your output."]
        for sd in seeds(sig, max_e2e):
            L.append(f"- **{sd['id']}** `{sd['dir']}` ({sd['files']} files): {', '.join(sd['stems'])}")
        L.append("")
    if sig["analytics_events"]:
        L += ["## Analytics events (the product team's own funnel names)"]
        for e in sig["analytics_events"][:max_events]:
            L.append(f"- `{e['event']}` ×{e['count']} — {e['files'][0]}")
        L.append("")
    L += ["## Hot directories (entry points + consequence-bearing code; NOT ranked by traffic)"]
    for h in sig["hot_dirs"][:max_hot]:
        bits = []
        if h["sinks"]:
            bits.append("sinks " + ",".join(h["sinks"]))
        if h["entry_points"]:
            bits.append("entry " + ",".join(h["entry_points"]))
        if h["e2e_covered_by"]:
            bits.append("e2e: " + h["e2e_covered_by"][0])
        L.append(f"- `{h['dir']}` ({h['files']} files) " + " · ".join(bits))
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", help="signals.json")
    ap.add_argument("--brief", help="signals.md (compact, for the explorer)")
    args = ap.parse_args(argv)
    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    sig = collect(root)
    if args.out:
        Path(args.out).write_text(json.dumps(sig, indent=2))
    brief = render_brief(sig)
    if args.brief:
        Path(args.brief).write_text(brief)
    if not args.out and not args.brief:
        print(brief)
    else:
        print(f"signals: {len(sig['e2e_groups'])} e2e group(s), {len(sig['analytics_events'])} "
              f"analytics event(s), {len(sig['hot_dirs'])} hot dir(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
