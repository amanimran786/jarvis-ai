# Jarvis Production Roadmap

One item at a time. Nothing moves forward until the current item is bug-free and
production-ready. Definition of done is listed under each item.

---

## ✅ Completed

- Contract system (TaskContract, approval workflow, gate_pre_commit enforcement)
- REVIEW.md pre-commit gate + harness/pre_commit_check.py
- Overnight autonomous dispatch (scheduled task, 10PM–5AM)
- Cross-session shared memory (harness/shared_memory.py backed by vault)
- Eval trace scorer (eval_trace_score.py --trace-score, observe-only)
- Orchestrator runbook (ORCHESTRATOR_RUNBOOK.md)
- Prompt quality analysis (GEMINI_PROMPT_ANALYSIS.md)

---

## ✅ Item 1 — CI Green (DONE)

**Fixed:** `3035 passed, 3 skipped` — full suite green on Ubuntu (commit `82b7804`).

Fixes applied:
- Moved `ask_local_structured` import to module level in `core/manager.py`
- Fixed `patch("infra.X.Y")` contamination from `test_memory.py`'s `sys.modules` deletion — switched to `patch.object()` throughout event bus tests
- Fixed `test_backend_engineer.py` stale binding via `importlib.import_module` + `patch.object`
- Fixed `infra/checkpointer.py` Python 3.10 compat (`datetime.UTC` → `timezone.utc`)
- Added `_AGENT_QUEUES.clear()` to sqlite event bus fixture; relaxed scheduler-race assertions
- Added CI ignores for 4 test files needing macOS/local services

---

## 🟡 Item 2 — Dashboard Always Running

**Problem:** `jarvis_dashboard.py` at port 7842 is the only UI for approving the
5 gated tasks and reviewing queue state. It's currently offline. The launchd
plist (`scripts/com.jarvis.dashboard.plist`) keeps failing to bootstrap (error 5).

**Fix:**
1. Diagnose the launchd error 5 — likely wrong Python path or missing log dir
2. Update plist to use conda Python (`which python3` on the user's machine)
3. `mkdir -p ~/jarvis-ai/logs` and `chmod 644` the plist
4. Use `launchctl bootstrap gui/$(id -u)` (not deprecated `launchctl load`)
5. Verify dashboard is reachable: `curl -s http://localhost:7842/health`
6. Verify it survives a reboot (or at least survives login)

**Done when:** `curl http://localhost:7842` returns 200 after a fresh login with
no manual intervention.

---

## 🟡 Item 3 — Orchestrator Self-Healing

**Problem:** `orchestrator_loop.py` has no process-level watchdog. If it crashes,
the queue stalls silently. The launchd plist (`scripts/com.jarvis.loop.plist`)
exists but isn't installed.

**Fix:**
1. Update `com.jarvis.loop.plist` to use correct conda Python path
2. Install and bootstrap it via launchd with KeepAlive=true
3. Verify it restarts after a kill: `kill <pid>` → process comes back within 30s
4. Wire `_ensure_dashboard_running()` call into the loop startup so dashboard
   and loop start together

**Done when:** `kill $(pgrep -f orchestrator_loop)` → process restarts automatically.

---

## 🟡 Item 4 — Wire run_checks() Into Orchestrator Loop (Boris Cherny Gap)

**Problem:** `gate_pre_commit=true` is in every contract but the loop never
verifies that a session actually ran the checker before marking a task done.
The gate is defined everywhere, enforced nowhere at runtime.

**Fix:**
1. In `orchestrator_loop.py`, after a session completes, call
   `harness.pre_commit_check.run_checks()` on any `.py` files in the session's
   commit (get them from `git diff HEAD~1 --name-only -- '*.py'`)
2. If findings > 0: set task status to `needs_review` instead of `done`,
   log findings to `logs/pre_commit_violations.log`
3. Add `needs_review` as a valid status to WORK_QUEUE and dashboard
4. Test: commit a file with a `shell=True` line, verify loop catches it

**Done when:** A task that commits `shell=True` code never reaches `status=done`
without a manual override.

---

## 🟡 Item 5 — Specialist Model Routing

**Problem:** devstral and qwen3:30b-a3b are not installed in Ollama.
`jarvis-local-llm-routing-verify` keeps aborting. Routing falls back to defaults.

**Fix:**
1. `ollama pull devstral` and `ollama pull qwen3:30b-a3b` on the user's machine
2. Re-run `jarvis-local-llm-routing-verify` — verify specialist model wins in logs
3. Add a startup check in `config.py` that warns if expected specialist models
   are missing (don't fail, but log clearly)

**Done when:** `ollama list` shows both models, and routing tests pass.

---

## 🟡 Item 6 — Security Review (GEMINI-3)

**Problem:** Full codebase security scan hasn't run. Unknown HIGH severity issues
may exist. Requires approval before running.

**Fix:**
1. Approve `gemini-lane-security-review` via dashboard (or manually)
2. Session writes `GEMINI_SECURITY_REVIEW.md` and fixes HIGH severity issues
3. Verify all fixes pass `python -m harness.pre_commit_check` and CI

**Done when:** `GEMINI_SECURITY_REVIEW.md` exists, no CRITICAL/HIGH findings
remain, CI still green.

---

## 🟡 Item 7 — Test Coverage Hardening

**Problem:** Many harness modules have no test coverage. `GEMINI_TEST_GAPS.md`
(in progress) will surface the worst gaps.

**Fix:**
1. Wait for GEMINI-2 session to complete and review `GEMINI_TEST_GAPS.md`
2. Ensure `tests/test_gemini_coverage.py` passes in CI
3. Add any additional gap tests surfaced in the audit
4. Target: every harness module has at least one import-smoke test

**Done when:** `CI=true python -m pytest tests/ -q` exits 0 with coverage for
every harness module.

---

## 🟡 Item 8 — Voice Pipeline Production Ready

**Problem:** Local STT/TTS pipeline (Kokoro, Whisper, macOS `say` fallback) has
not been verified end-to-end in the current codebase state.

**Fix:**
1. Run `python main.py --no-ui` and confirm wake word detection works
2. Verify STT: speak → transcript appears in logs
3. Verify TTS: response is spoken via Kokoro or `say` fallback
4. Verify mic lifecycle: no zombie processes after session end
5. All voice tests pass: `pytest tests/ -k voice -q`

**Done when:** Wake → listen → respond → TTS cycle completes without errors,
reproducible across 3 consecutive runs.

---

## 🟡 Item 9 — Full 24/7 Autonomous Operation

**Problem:** All individual pieces work but the full autonomous loop (queue
dispatch → session runs → pre-commit gate enforced → task done → next task
dispatched) hasn't been verified end-to-end.

**Fix:**
1. All Items 1–8 complete
2. Run a 24-hour unattended test: add 5 synthetic tasks to WORK_QUEUE, let the
   overnight orchestrator process them, verify all 5 reach `status=done` with
   correct commits
3. Dashboard shows accurate real-time state throughout
4. Only approval-gated tasks require human action

**Done when:** 5 tasks processed overnight with zero manual intervention,
dashboard accurately reflects final state.

---

---

## 🔴 Item 1.5 — Fix Self-Learning Pipeline (NEXT PRIORITY)

**Problem (from production audit, July 2026):** Jarvis generates training
artifacts but never successfully promotes a model. Eight broken subsystems:

1. **Fake benchmarks** — `585/685` baseline written to `BENCHMARK.md` but pytest
   suite actually passes 685/685. Promotion condition compares equal numbers.
2. **Fusion base-model mismatch** — Overnight trainer trains a Qwen3-8B adapter,
   but `local_finetune_scheduler.py:957` hardcodes `Qwen/Qwen2.5-Coder-7B-Instruct`
   as the fusion base. Shape mismatch → every promotion fails.
3. **Promotion condition invalid** — `current_passed >= baseline_passed` allows
   promotion when scores are equal (no improvement). Should be `> baseline_passed`.
4. **Voice UI self-edit bypass** — `ui.py:1907` legacy voice path calls
   self-improvement method directly, bypassing approval gate.
5. **Test telemetry contamination** — `MockModel`, `UnitTestModel`, `task:test`
   interactions pollute production learning telemetry.
6. **Non-atomic memory writer** — `knowledge.json` writes race-crashed July 13;
   needs atomic write (write to `.tmp`, then `rename`).
7. **Upgrade scout idle** — One cycle, three tickets, zero decisions, no
   activity since June 13. Needs connection to coordinator.
8. **Packaged app stale** — Built July 12, not running. (Lower priority.)

**Fix order:**
1. Fix fusion base model in `local_finetune_scheduler.py:957` (Qwen3-8B)
2. Fix promotion condition: `>` not `>=`
3. Fix benchmark to use real test count (or compare pass-rate, not raw count)
4. Remove voice self-edit bypass in `ui.py:1907`
5. Add telemetry source tag; filter test interactions from production metrics
6. Atomic `knowledge.json` write
7. Connect upgrade scout to coordinator queue

**Done when:** A model trained overnight gets a real evaluation, passes the gate,
and is fused + loaded without a crash.

---

## Current Item

**→ Item 1.5: Self-Learning Pipeline Fix**

Item 1 (CI Green) is complete as of commit `82b7804`.
Both Claude and Codex can now work on Items 2–9 in parallel — see
`CROSS_AGENT_ORCHESTRATION.md` for lane assignments.
