# Codex Handoff — Jarvis AI

**Date:** 2026-06-18  
**Branch:** `main`  
**Last commit:** `fe707d8 feat(artifact): Claude Code Artifact generation tool`

---

## What was just done (do not redo)

### 1. Local Artifact feature (`fe707d8`, renamed + corrected this session — uncommitted)
- `orchestrator.py`: Added `artifact` fast-path in `_fast_classify()` — matches "create/make/build/generate … artifact/diagram/dashboard/visualization/walkthrough/shareable page" (unchanged)
- `router.py`: artifact handler writes a self-contained HTML file to `~/Desktop` and logs to notes. Label is now `"Local Artifact"`. **Fixed:** the call was `ask_with_priority(prompt, tier="sonnet", …)` — `"sonnet"` is NOT a valid tier (valid: `cheap`/`strong`/`deep`), so it silently resolved to `LOCAL_DEFAULT` locally and the *cheap* cloud plan otherwise. Now `tier="strong"` → `LOCAL_REASONING` locally, proper strong cloud plan only when local-first is off. Respects `DEFAULT_MODE = "open-source"`.
- `tool_registry.py`: `"artifact"` description reworded to "Local Artifact … created on-device" (tool key unchanged)
- `config.py`: SYSTEM_PROMPT paragraph renamed "Claude Code Artifacts" → "Local Artifacts" and corrected to describe an on-device HTML file saved to Desktop (not a hosted/session artifact)
- `tests/test_artifact_routing.py`: 11 tests, all green. Label assertions updated to `"Local Artifact"`. Clean sys.modules isolation via setUpClass/tearDownClass snapshot.

**Why the rename:** the original called its output a "Claude Code Artifact," but it produces a local HTML file via an API call — no hosted link, no versioning, not Anthropic-hosted. Truth-in-labeling + local-first. Note: the handler only ever sent `user_input` (the request line) to the model — it never read or transmitted vault, transcript, source, or secrets.

#### Future (DO NOT build until verified): native cloud artifact lane
A "publish to a hosted Claude artifact with a private link" lane is **not** built and should not be, because:
- There is no known public API for a third-party app to publish a hosted Claude artifact and get back a private link. Verify the mechanism exists first.
- Per Anthropic's sharing docs, Pro-tier artifact sharing is **public**; private org sharing is Team/Enterprise. So "genuinely private link for Pro" is unconfirmed and likely false.
- If ever built, it must transmit only an explicitly-approved compact brief — never vault, chat history, source, or secrets — behind a separate user approval.

### 2. Dashboard KPI improvements (`api.py` — uncommitted, Jarvis restarted to serve)
- Added 5th KPI tile: **Task success rate** (completed/total as %, colored: ≥70% green, 50–69% yellow, <50% red)
- Fixed `mc-active` KPI: was showing only `active` count; now shows `active + awaiting_approval` to match its label
- Fixed `mc-nav-pipeline` counter to use `totalInFlight` (same fix)
- Enriched Manager Console narrative: now shows "X succeeded, Y failed of Z total" and per-agent success rates
- Responsive grid: 5 → 3 cols at ≤1400px

### 3. sys.modules test isolation fix
`tests/test_artifact_routing.py` previously contaminated later test files by setting PyQt6 stubs at module level. Fix: all Apple framework stubs scoped to `ArtifactHandlerTests.setUpClass/tearDownClass` with a pre-test snapshot. `orchestrator` imports cleanly at module level with zero stubs.

---

## Immediate TODO (do these first)

### A. Commit the uncommitted changes (two logical commits)
Verify first:
```bash
python3 -m pytest tests/ -q --ignore=tests/test_jarvis_live_integrations.py
```
Expected: all pass (baseline 2178 passed, 1 skipped as of 2026-06-18).

Then commit in two slices:
```bash
# 1. Dashboard KPIs (api.py)
git add api.py
git commit -m "feat(dashboard): task success rate KPI, fix active count, richer narrative"

# 2. Local Artifact rename + local-first fix
git add config.py router.py tool_registry.py tests/test_artifact_routing.py docs/ai/
git commit -m "fix(artifact): rename to Local Artifact, route through strong local-first tier"
```

### B. Rebuild the packaged Jarvis.app
`config.py` SYSTEM_PROMPT changes (artifact feature) and other committed changes are NOT in the installed app yet.

```bash
/Users/truthseeker/jarvis-ai/scripts/install_jarvis_app.sh --applications-only
```

Verify bundle timestamp:
```bash
ls -la /Users/truthseeker/Applications/Jarvis.app
```

---

## Self-improvement loop — queued items (priority order)

These are from the autonomous improvement loop. Pick in order. Test after each, commit separately.

> **Status (updated 2026-06-18):** #1 and #3 are DONE; #4 is Won't-do. Remaining for Codex: #2 (agent_dispatch tests), #5 (flaky live test), #6 (capability investigation). Full status in the loop memory `project_self_improve_loop.md`.

### 1. `router.py` — silent failure audit ✅ DONE
Converted 4 bare `except: pass` → `logging.debug(..., exc_info=True)` (email-from-memory fallback, teacher-capture of beta failure, local reply-draft, background fact extraction). No `except: continue/return` swallows remain; the queue note's "many instances" was overstated (4 total).

### 2. `agent_dispatch.py` — missing test coverage (DO THIS)
No tests for context-parameter forwarding and custom-system-prompt agents. Add narrowest meaningful tests. File: `tests/test_agent_dispatch.py` (check if exists first — may need to create).

Use the mock injection pattern from `jarvis-testing.md`:
```python
sys.modules['PyQt6'] = MagicMock()
sys.modules['sounddevice'] = MagicMock()
```

### 3. `_bg_agents.py` — timeout guard ✅ ALREADY DONE
Already implemented: threaded run + `thread.join(timeout=_RUN_TIMEOUT)`, logs + skips on timeout. A traceback-logging bug (`logging.exception` outside the except block) was fixed this session → `logging.error(..., exc_info=exc_box[0])`.

### 4. `model_router.py` — context cache ❌ WON'T-DO
Redundancy already mitigated by the always-on identity snapshot (replaces per-query vault search), the `skip_dynamic_context` flag for tool-loop continuations, and parallel ThreadPool context assembly. A TTL cache would risk stale vault/semantic context for marginal gain — against "no speculative abstractions."

### 5. Flaky live test — make assertion fuzzy (DO THIS)
`tests/test_jarvis_live_integrations.py::LiveApiReadOnlyTests::test_router_expert_prompt_smoke`
Asserts `label == "Specialized Agents"` but LLM sometimes classifies differently. Either:
- Change assertion to `label in {"Specialized Agents", "Open-Source"}`, or
- Mark `@pytest.mark.skip(reason="live LLM: label non-deterministic")`

### 6. Capability investigation — ZERO_CALL_FAIL / 43% task failure (LOOP-IN FIRST)
Live dashboard shows 32/75 tasks failed and repeated `ZERO_CALL_FAIL` (agents returning answers with zero tool calls, scored 0.0; seen for qa-tester, backend-engineer, researcher). Determine whether the zero-call gate correctly fails tool-less tasks or false-negatives pure-reasoning tasks. Touches agent prompts/dispatch — confirm with Aman before changing behavior.

---

## Key file map

| File | Role |
|------|------|
| `router.py` | Intent/tool routing before LLM |
| `model_router.py` | Model selection and mode behavior |
| `orchestrator.py` | Request/runtime coordination |
| `agent_dispatch.py` | Agent spawning and task dispatch |
| `_bg_agents.py` | Background agent tick loop |
| `api.py` | FastAPI server + dashboard HTML (inline template, line ~4546) |
| `config.py` | Runtime defaults, SYSTEM_PROMPT |
| `tool_registry.py` | TOOLS dict |
| `voice.py` | Voice loop, wake/listen/TTS |
| `ui.py` | PyQt6 desktop UI |
| `tests/` | pytest suite — run with `python3 -m pytest tests/ -q --ignore=tests/test_jarvis_live_integrations.py` |

## Security checklist (run before any commit)

```bash
grep -n "shell=True" <file>.py
grep -n "eval\|exec(" <file>.py
grep -n "SECRET\|API_KEY\|TOKEN\|PASSWORD" <file>.py | grep -v "os.getenv\|config\."
grep -n "pickle.load\|yaml.load" <file>.py
```

---

## Do NOT

- Reintroduce cloud or paid model fallbacks (local-first is intentional)
- Mock PyQt6 or sounddevice at module level in tests (causes sys.modules contamination across the suite)
- Add print() statements to voice.py or ui.py (BrokenPipeError in packaged windowed mode)
- Touch voice.py, ui.py, or Jarvis.spec without also verifying the packaged app
- Auto-commit vault changes (propose diffs for review only)
