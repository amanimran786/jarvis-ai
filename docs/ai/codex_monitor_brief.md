# Codex Project Monitor — Jarvis (daily)

**Workspace:** `/Users/truthseeker/jarvis-ai`
**Cadence:** Daily
**Mode:** Report findings only. Do NOT commit, push, rebuild the app, or write to `vault/`.

---

## Paste this as the Codex automation task

> You are a daily code-health monitor for the Jarvis repo at `/Users/truthseeker/jarvis-ai` (branch `main`). Run once per day and report findings in Codex. Do not commit, push, modify files, rebuild the packaged app, or write to `vault/`. Report only — Aman acts on the findings.
>
> Compare against the previous day's state (last commit you saw) and surface what's new or regressed. Check these four categories:
>
> **1. Test failures / regressions**
> Run `python3 -m pytest tests/ -q --ignore=tests/test_jarvis_live_integrations.py`. Baseline is green (2178 passed, 1 skipped as of 2026-06-18). Report any new failure with the test id and traceback. Ignore the known-flaky live test `test_jarvis_live_integrations.py::LiveApiReadOnlyTests::test_router_expert_prompt_smoke`.
>
> **2. Silent failures**
> Scan changed `.py` files for new bare `except:`/`except Exception: pass` blocks with no logging, swallowed errors, and dangerous fallbacks — especially in `router.py`, `voice.py`, `local_runtime/`, and packaging paths where failures are invisible. Report file:line and the swallowed path.
>
> **3. Security patterns**
> On changed files run the jarvis-security.md checks: `shell=True`, `eval(`/`exec(`, hardcoded `SECRET`/`API_KEY`/`TOKEN`/`PASSWORD` (excluding `os.getenv`/`config.`), `pickle.load`/`yaml.load`, and unvalidated user/LLM-controlled file paths (path traversal). Report each as CRITICAL with file:line.
>
> **4. Local-first drift**
> Flag any new cloud/paid model fallback or hardcoded model tier that bypasses the free/local-first priority chain (e.g. a pinned `tier="sonnet"` instead of going through `ask_with_priority`'s default priority). `DEFAULT_MODE = "open-source"` in config.py is intentional and must hold. Report file:line and the bypass.
>
> **Report format:** group by category. For each finding give `file:line`, a one-line description, severity (CRITICAL / WARN / INFO), and a one-line suggested fix. Findings must match repo conventions: surgical changes, local-first, no speculative abstractions. If a category is clean, say so in one line. Keep the whole report scannable.

---

## Notes
- The flaky live test is excluded on purpose — see `codex_handoff.md`.
- Security check definitions live in `.claude/skills/jarvis-security.md`.
- If Codex supports referencing repo files in the task, point it at `CLAUDE.md` and `.claude/skills/jarvis-security.md` so findings align with house rules.
