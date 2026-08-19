# Run `ce` on your service

This kit reads your source, proposes the business journeys it can see, and writes
a Sentry instrumentation spec for **your** coding agent. It does **not** modify
application code, upload source, or call an LLM.

## What leaves the machine

Nothing, on the default path (`discover` / `review` / `report`). Live Sentry
fetch is opt-in.

| Command | Reads | Writes | Network |
| --- | --- | --- | --- |
| `ce discover` | your source (`node_modules`, `.git`, `dist`, `venv`, secrets skipped) | `ce-work/` only | none unless `--sentry` |
| `ce review` | `ce-work/journeys.yaml` | that YAML + `.reviewed` | localhost only |
| `ce report` | `ce-work/` | `ce-work/` plus tracked `.agents/journeys/` | none |
| `ce snapshot` | Sentry API | `ce-work/` | Sentry (needs `SENTRY_AUTH_TOKEN`) |
| `ce local` | drives *your* command | `ce-work/` | localhost collector only |

## Install

Python **3.9+**, installed from [python.org](https://www.python.org/downloads/)
(not Docker — `ce` is a local CLI). From the wheel your SE sent:

```bash
pip install critical_experience_kit-0.1.0-py3-none-any.whl
cd /path/to/your-service
ce doctor
```

If `ce` is not found after pip, Python's scripts directory is not on `PATH`.
Open a new shell, or run `python3 -m pip show -f critical-experience-kit` and
add the directory that contains `ce` to `PATH`.

## 1. Discover

```bash
ce discover
```

This writes `ce-work/` (gitignored). If the tree has no JS/TS or Python, it
exits and tells you to `ce init` instead of proposing junk.

On a TTY it asks whether to fetch **live** telemetry from Sentry:

- **Token present** (`SENTRY_AUTH_TOKEN`) plus `--org`: documented attributes
  API, plus span names from `--from-mcp` if you saved MCP JSON.
- **No token:** it writes `ce-work/SENTRY-MCP.md`. In Cursor, authenticate the
  Sentry MCP (`mcp_auth`), run `search_events` (dataset `spans`), save the JSON
  to `ce-work/mcp-spans.json`, then:

```bash
ce snapshot --org YOUR_ORG --from-mcp ce-work/mcp-spans.json --out ce-work/observed.json
```

Skip with `--no-sentry` (CI / scripts). Never uses undocumented span query
endpoints.

## 2. Review journeys

```bash
ce review
```

Opens a local page (stdlib server on 127.0.0.1). Keep 2–3 flows, set impact
(`critical` = we would page / `important` = same day / `normal` = visible, not
a pager), Save. Each card is Keep/Impact plus a Today / After this spec sketch
(structure, not latency). Illustrated errors are labelled; they are not
recorded incidents. With no Sentry org, “today” is sketched from routes. With
a saved `ce-work/mcp-trace.json`, “today” uses that tree. Names like `web`
default to drop. The page does **not** patch your app. The tool will **not**
set impact for you.

If you already edited `ce-work/journeys.yaml` by hand:

```bash
ce review --stamp
```

`ce report` refuses to run until this step exists (`.reviewed`).

## 3. Generate the spec

```bash
ce report
```

Writes working files under `ce-work/` (gap, profile, `*-SPEC.md`, `*-WHY.md`,
`EXPLORE.md`) and copies **only** `*-SPEC.md` to **`.agents/journeys/`**
(tracked). Also scaffolds, if missing:

- a short pointer in `AGENTS.md`
- `.cursor/rules/sentry-journeys.mdc`
- `.agents/skills/sentry-critical-experience/` (Warden skill)
- `warden.toml` with `failOn = "off"` (comments on the PR, does not block merge)

Do not implement from `*-WHY.md`. Do not commit `ce-work/`.

## 4. Point your coding agent at the spec

Do this **in this service repo**, in a new branch. One journey per session.

**Cursor**

1. `File → Open Folder` on this service.
2. New chat. `@.agents/journeys/checkout-SPEC.md` (use the real filename).
3. Prompt:

```
Implement the Sentry instrumentation spec in @.agents/journeys/checkout-SPEC.md
against this repository.

Rules:
- Read the whole spec first. Implement only the numbered requirements in §4.
- Do not touch spans or attributes the spec marks as present.
- Use exactly the span names and attribute keys in the spec. Do not invent names.
- Use only the Sentry APIs in §5. Never the APIs in §6. A requirement may also ask for a Sentry issue, structured log, or Application Metric — those are companions to a missing span attribute, not a replacement.
- Do not read or implement from *-WHY.md or ce-work/.
- If a required span has no clear location in this repo, stop and ask. Do not guess.
- When done, list each §4 requirement with file:line where you implemented it.
```

**Claude Code / Codex / other CLI agents**

```bash
claude -p "Implement .agents/journeys/checkout-SPEC.md against this repo. Only §4. Exact names. APIs from §5 only. Stop and ask if a span has no clear location."
```

**What not to do**

- Do not point the agent at `ce-work/` or `*-WHY.md`.
- Do not ask it to “add Sentry tracing” in general — that ignores the contract.
- Do not implement more than one journey in the same session.
- The change you merge is the **instrumentation**, opened as a PR the way you
  already ship code. The spec in `.agents/journeys/` is the sidecar Warden reads.

## 5. Warden on the implementation PR

Once, in this repo:

```bash
npm install -g @sentry/warden
# warden.toml is already scaffolded; the skill is vendored under .agents/skills/
```

CI needs a model provider key (`WARDEN_ANTHROPIC_API_KEY` or
`WARDEN_OPENAI_API_KEY`). No Sentry token. `failOn = "off"` means findings are
comments, not a red X. Flip to `"high"` after the first green PR if you want it
to gate.

The check no-ops unless `.agents/journeys/` exists — that is why `ce report`
copies the spec there.

## 6. After it lands

- Paste the queries in `ce-work/EXPLORE.md` (same as spec §7) into Trace Explorer.
  `ce` does not create dashboards.
- JS/TS: `ce grade --rubric ce-work/specs/<id>-RUBRIC.json --repo .`
- Any language: `ce local --resolved ce-work/resolved.json --drive '<cmd that exercises the journey>'`
- Re-run `ce snapshot` / `ce gap` later and `ce diff` against the baseline
  `ce-work/gap.json`. Regressions lead that report.

Span names and keys must match the spec literally — a typo produces no compiler
error and no data.
