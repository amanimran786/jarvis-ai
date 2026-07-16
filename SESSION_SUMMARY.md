# Jarvis AI — Session Summary
**Date:** July 1–2, 2026
**Branch:** `improve/local-artifact-and-dashboard`
**Commits ahead of origin:** 10

---

## What Was Worked On

This session covered two tracks: **dashboard fixes** and a **local-LLM audit + fixes** to reduce cloud API usage and prevent rate-limit hits.

---

## Track 1 — Dashboard Fixes

### Problem
The Jarvis AI Dashboard at `http://localhost:7842` was showing `[?] ?` in the Work Queue table and all stat counters were showing 0 even though `WORK_QUEUE.json` had real data.

### Root Cause
`jarvis_dashboard.py` was using wrong JSON field names (`id`, `title`, `assigned_ai`) and wrong status values (`pending`, `queued`, `failed`) — none of which exist in the actual file.

### Actual WORK_QUEUE.json schema (confirmed by inspection)
```
session_name, task, priority, status, assigned_at, completed_at, created_at, notes
status values: in_progress | done | blocked
```

### Changes Made
**File:** `jarvis_dashboard.py` — commit `339e1a6`
- `_work_queue_table()`: field names fixed (`id→session_name`, `title→task`, `assigned_ai→priority`)
- Stats counters: fixed to count `in_progress`, `done`, `blocked` statuses
- Stat cards: labels updated from "Pending"/"Failed" → "In Progress"/"Blocked"
- `_badge()`: added color mappings for `in_progress` (#f0c040 yellow) and `blocked` (#ef5350 red)

---

## Track 2 — CODEX Task Board Updates

### Changes Made
**File:** `CODEX_TASKS.md` — commit `b95f56f`
- Marked **CODEX-6** (`/history` command with Rich CLI) as ✅
- Marked **CODEX-7** (Plugin system foundation) as ✅

Both were implemented in prior sessions but the board hadn't been updated.

---

## Track 3 — Local LLM Audit & Fixes

### Goal
Ensure Jarvis uses local LLMs (Ollama/GLM, Apple Foundation) for the vast majority of requests so cloud API rate limits don't interrupt operation.

### Audit Findings
Running a deep audit of `model_router.py`, `orchestrator.py`, `specialized_agents.py`, `execution_engine.py`, `research.py`, and `usage_log.jsonl` revealed:

**Good news:** 97% of traffic (194/200 recent entries in `usage_log.jsonl`) was already hitting local models. The core `smart_stream` routing path works correctly.

**The 3% problem — 5 specific bypass points:**

| # | File | Problem |
|---|------|---------|
| 1 | `.env` | `OLLAMA_TIMEOUT_SECONDS=45` too short — cold model loads take 20–40s, leaving no headroom for generation. Requests silently fell through: `ollama → ollama_cloud → gpt-4o-mini` |
| 2 | `orchestrator.py:151` | Auto-mode intent classification called Claude Haiku on **every turn**, bypassing the local structured classifier that already existed at line 426 |
| 3 | `specialized_agents.py:549` | Specialist roles (security, science, technical) went cloud-first even with `LOCAL_STRICT_FIRST=1` — the flag was only checked for open-source mode |
| 4 | `execution_engine.py:293` | Chat-step fallback hardcoded `ask_claude(SONNET)` — most expensive tier, bypassed the entire priority chain |
| 5 | `research.py:39,118,230` | Deep research has **zero local path** — query gen, synthesis, and voice summary all unconditionally call cloud *(not fixed yet — needs design decision)* |

### Fixes Applied

#### Fix #1 — Ollama Timeout (`.env`, no commit — gitignored)
```
OLLAMA_TIMEOUT_SECONDS=45  →  OLLAMA_TIMEOUT_SECONDS=120
```
The 45s timeout was the #1 cause of silent cloud fallthrough. Now there's room for cold loads + generation.

#### Fix #2 — Local Classifier First (`orchestrator.py`, commit `799da83`)
In auto-mode intent classification, the local structured classifier now runs first. Claude Haiku is only called if local returns `None`, fails, or times out (3s timeout). Eliminates the highest-frequency per-turn cloud call.

#### Fix #3 — Specialists Respect LOCAL_STRICT_FIRST (`specialized_agents.py`, commit `55450b4`)
The gate for specialist agent local routing was widened:
```python
# Before:
if is_open_source_mode():
    # use local

# After:
if is_open_source_mode() or (LOCAL_STRICT_FIRST and get_mode() != "cloud"):
    # use local
```
In local-strict mode, specialists try local first and only fall to cloud on failure. Explicit `cloud` mode still overrides as expected.

#### Fix #4 — Execution Engine Fallback (`execution_engine.py`, commit `5f774e0`)
```python
# Before:
ask_claude(prompt, model=SONNET, ...)  # most expensive, no priority chain

# After:
ask_with_priority(prompt, tier="strong", ...)  # local → ollama_cloud → paid, in order
```
Also removed the now-orphaned `ask_claude`/`SONNET` imports from this file.

### Test Results
```
3080 passed, 45 failed, 36 skipped
```
The 45 failures are **all pre-existing** (Python 3.10 vs 3.11 datetime.UTC incompatibilities, missing local services, etc.) — confirmed by diff against pre-fix baseline. **Zero new regressions.**

---

## What's Still Open

### 1. `research.py` — Deep Research Local Path
All three research stages (query generation, report synthesis, voice summary) unconditionally call cloud APIs. To fix, we need a decision:
- **Option A:** Use local model for query gen + voice summary; keep Sonnet for synthesis (hybrid)
- **Option B:** Full local path with `ask_with_priority` everywhere; cloud only on failure
- **Option C:** Leave deep research cloud-only (it's infrequent and quality-sensitive)

### 2. FINNHUB_API_KEY
The WorldView markets panel needs a key. Placeholder is already in `.env`:
```
FINNHUB_API_KEY=
```
Get a free key at https://finnhub.io/register and paste it in.

### 3. `core/manager.py` Test Failures (10 tests)
`_decompose_via_llm` calls `re.sub` on a MagicMock in tests — real test bug, not related to the LLM work. Pre-existing but worth cleaning up.

### 4. Force Quit Dialog (DELL Monitor)
A macOS Force Quit confirmation dialog is still on the DELL display. Either:
- Click "Force Quit" on the monitor, or
- Run `killall -9 TextEdit` in Terminal

This doesn't block any Jarvis work since we now use direct filesystem access via `request_cowork_directory`, but it should be cleared.

### 5. MASTER_LOG.md
Has uncommitted changes (CODEX-6 and CODEX-7 completion entries). Should be committed separately.

---

## Files Changed This Session

| File | Change | Commit |
|------|--------|--------|
| `CODEX_TASKS.md` | CODEX-6 and CODEX-7 marked ✅ | `b95f56f` |
| `jarvis_dashboard.py` | Field names, status values, card labels, badge colors | `339e1a6` |
| `orchestrator.py` | Local classifier before Haiku in auto mode | `799da83` |
| `specialized_agents.py` | LOCAL_STRICT_FIRST gate widened | `55450b4` |
| `execution_engine.py` | Fallback through priority chain | `5f774e0` |
| `.env` | Ollama timeout 45→120 | *(gitignored)* |

---

## Branch State
```
improve/local-artifact-and-dashboard
10 commits ahead of origin
Latest: 5f774e0 [JARVIS] execution_engine: route chat-step fallback through provider priority chain
```
