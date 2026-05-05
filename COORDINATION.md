# Jarvis Agent Coordination Board

Shared task board between **Claude (Cowork)** and **Codex**. Both agents read this at startup.

- `[CLAUDE]` — Cowork handles this (architecture, config, orchestration, UI, dashboards)
- `[CODEX]` — Codex handles this (code fixes, test loops, refactoring, targeted patches)
- `[BOTH]` — coordinate, one agent proposes, other reviews
- `[DONE]` — completed, timestamp noted
- `[BLOCKED]` — waiting on user input or external dependency

Update this file when picking up or finishing a task. Commit the update so the other agent sees it on next pull.

---

## Active Tasks

### [CODEX] Voice / PaMacCore AUHAL mic fix
**File:** `voice.py`, `local_runtime/local_stt.py`
**Goal:** Fix PaMacCore AUHAL errors causing mic input to fail
**Log:** `~/Library/Application Support/Jarvis/.jarvis_voice.log`
**Acceptance:** `test_voice_tts_regression.py` passes, mic opens without AUHAL error in log
**Status:** In progress

### [CODEX] mem0 / Qdrant end-to-end verification
**File:** `local_runtime/local_mem0.py`, `tests/test_mem0_layer.py`
**Goal:** Verify Qdrant vector store is reachable and mem0 reads/writes correctly
**Acceptance:** `test_mem0_layer.py` all pass with live Qdrant
**Status:** Open

### [CODEX] Resolve qwen3 Ollama model tag
**Goal:** Run `ollama list` and confirm actual tag — `qwen3:35b` vs `qwen3:35b-a3b`
**File:** `config.py` LOCAL_DEFAULT, `brains/brain_ollama.py`
**Acceptance:** Config tag matches `ollama list` output, keepalive starts without 404
**Status:** Open

### [CLAUDE] Benchmark run — all 7 categories
**Goal:** Run full category benchmark now that all test files are confirmed present
**File:** `training/benchmark_tracker.py`, `training/benchmarks.jsonl`
**Acceptance:** `benchmarks.jsonl` has a record with no skipped categories
**Status:** Open — run `python3 training/benchmark_tracker.py` after overnight cycle

### [CLAUDE] iMessage Full Disk Access
**Goal:** Grant Full Disk Access to Terminal in System Settings so iMessage tools work
**Action:** User must do this manually: System Settings → Privacy → Full Disk Access → Terminal ✓
**Status:** Blocked (needs user)

### [DONE] Training pack quality — richer examples
**Completed by Claude 2026-05-05**
`build_training_pack()` now has 4 ranked sources:
1. Teacher examples from `training/teacher_examples/*.jsonl` (11 curated)
2. Real verbatim conversations from `memory/conversations/verbatim.jsonl` (up to 80 real pairs)
3. 21 handcrafted synthetic examples (calendar, terminal, voice, iMessage, memory, system control)
4. Legacy memory.json summaries as low-quality fallback (only if total < 30)
Pack size: 74 examples (up from 30 generic summaries). All in messages format for mlx_lm ≥0.31.x.
**Helpers added:** `_collect_teacher_examples()`, `_collect_verbatim_examples()`,
`_build_synthetic_examples()`, `_collect_legacy_summary_examples()`

---

## Automation Already Running

| System | Schedule | Owner | Status |
|--------|----------|-------|--------|
| Overnight MLX training | 11pm–7am daily | launchd | ✅ Live |
| Brain daemon (5 agents) | 30m/2h/4h/8h/24h | main.py | ✅ Live |
| Dashboard regeneration | After each training run | scheduler | ✅ Live |
| Ollama keepalive | Continuous | main.py deferred | ✅ Live |
| Knowledge feed refresh | Every 4hrs | brain_daemon | ✅ Live |

---

## Parallel Automation Targets

### [DONE] Auto-commit training artifacts
**Completed by Claude 2026-05-05**
`_auto_commit_artifacts()` added to `local_finetune_scheduler.py` — Stage 6.
Commits overnight_log.jsonl, benchmarks.jsonl, dashboard.html, overnight_state.json
after each training run. Commit message format: `chore(training): overnight artifacts YYYY-MM-DD [promoted|no-promote]`

### [DONE] Notification when training completes
**Completed by Claude 2026-05-05**
`_notify_training_complete()` added to `local_finetune_scheduler.py` — Stage 7.
Sends macOS notification via `osascript` with promoted status and "Glass" sound.

### [CODEX] Auto-pull before each Codex session
Add to Codex startup: `git pull origin main` before reading repo state.
Ensures Codex always has Claude's latest changes.

---

## Handoff Notes

**Last Claude session (2026-05-05):** Added auto-commit (Stage 6) and macOS notification (Stage 7)
to `local_finetune_scheduler.py`. Benchmark run shows 99.33% (597/601 tests). Dashboard updated.
COORDINATION.md updated with completed tasks.

**For Codex next session:** Pull latest main. Voice AUHAL fix + mem0 Qdrant verification +
qwen3 model tag still open. See `.claude/skills/jarvis-voice.md` for voice checklist.
