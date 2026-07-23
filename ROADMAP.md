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

## ✅ Item 2 — Dashboard Always Running (DONE)

**Problem:** `jarvis_dashboard.py` at port 7842 is the only UI for approving the
5 gated tasks and reviewing queue state. It was offline. The launchd plist
(`scripts/com.jarvis.dashboard.plist`) kept failing to bootstrap (error 5).

**Root cause:** two separate bugs, both in the repo's source of truth (the
live `~/Library/LaunchAgents` copy had been hand-patched at some point and no
longer matched what the repo would install):
1. `scripts/com.jarvis.dashboard.plist` pointed at `/usr/bin/python3`, which
   lacks the project's dependencies (fastapi/uvicorn) — the process died
   immediately after launch.
2. `scripts/install_launchd.py` was stale: it generated a plist inline
   (ignoring the checked-in one), pointed at a nonexistent entry point
   (`kill_and_start.py` wasn't referenced correctly), used the deprecated
   `launchctl load`/`unload`, and blocked on `input()` — unusable
   non-interactively. It also called `bootout` immediately followed by
   `bootstrap` with no wait; `bootout` is asynchronous, so bootstrapping
   before launchd finished releasing the label reproduced the exact
   `Bootstrap failed: 5: Input/output error` from the bug report.

**Fix:**
1. `scripts/com.jarvis.dashboard.plist` → `/opt/anaconda3/bin/python3` (this
   machine's `which python3`)
2. `scripts/install_launchd.py` rewritten to copy the checked-in plist
   (single source of truth, no more inline duplicate), poll after `bootout`
   until the label is actually gone before re-`bootstrap`ing, use
   `launchctl bootstrap gui/$(id -u)` (not deprecated `load`), and drop the
   blocking `input()`
3. Verified with 3 consecutive clean reinstalls — `launchctl print` shows
   `state = running`, `curl http://localhost:7842/` returns 200/401
   (401 = dashboard's own auth gate, confirms the process is alive and
   serving)

**Done when:** `curl http://localhost:7842` responds after a fresh
`python3 scripts/install_launchd.py` with no manual intervention. ✓

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

## ✅ Item 4 — Wire run_checks() Into Orchestrator Loop (Boris Cherny Gap) (DONE)

**Problem:** `gate_pre_commit=true` is in every contract but the loop never
verified that a session actually ran the checker before marking a task done.
The gate was defined everywhere, enforced nowhere at runtime.

**Fix:**
1. `harness.completion_verifier.verify_completion()` is now the single
   completion boundary used by both the orchestrator harvest path and
   `harness.agent_coordinator.finish()`.
2. `harness.commit_review_gate` resolves the lease base and completion SHAs,
   requires a clean checkout whose HEAD still matches the completion commit,
   and scans Python blobs from the immutable Git tree instead of mutable
   working-tree files.
3. The gate rejects non-ancestor ranges, Python symlinks/unsupported modes,
   syntax errors, security findings, hard process exits/replacement, native FFI,
   and newly introduced `# pre-commit-ok` suppressions. It runs before
   repository verification code. Persisted reasons and owner-only logs contain
   rule/path/line metadata, never the matching source line or secret value.
4. Gate violations become `needs_review`; gate infrastructure failures remain
   unverified/retryable. Queue persistence now raises on failure so completed
   sessions are not purged after a failed state transition.
5. The coordinator validates unexpired lease ownership and contract digests
   before and after verification, then writes advisory agent state before the
   authoritative queue commit so a state-write failure cannot persist `done`.
6. `needs_review` is a recognized WORK_QUEUE status: `jarvis_dashboard.py`
   badges it (amber), adds a "Needs Review" stat card, and gives it the same
   Requeue action as `blocked`/`stalled`/`failed`.
7. Verification commands run in a default-deny macOS Seatbelt profile with no
   network, ambient credentials, external reads, or external writes. Pytest
   disables repository `conftest.py` and must report normal structured
   completion for every collected test.
8. Session/queue locks live outside Git in an owner-only directory, reject
   symlinks, and protect selective completion purges. Stale completions are
   quarantined without overwriting the current task owner or jamming later
   harvests.
9. Tests cover both completion paths, real unsafe/clean commits, dirty-tree
   replacement, symlink and suppression bypasses, test-result forgery, secret
   redaction, lease expiry, and queue/state write failures.

**Done when:** A task that commits `shell=True` code never reaches `status=done`
through either supported completion path. ✓

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

## ✅ Item 1.5 — Fix Self-Learning Pipeline (DONE)

**Fixed commit `ea1fca4` (July 18 2026).** Five of eight broken subsystems:

1. ✅ **Fusion base-model mismatch** — `promote_if_better()` now derives
   `model_hf_id` from `MLX_MODEL_PRESETS[config.MLX_TRAINING_MODEL]["hf_id"]`
   instead of hardcoding `Qwen/Qwen2.5-Coder-7B-Instruct`. Fusion base always
   matches the trained adapter (Qwen3-8B with default config).
2. ✅ **Promotion condition** — Changed `>=` to `>` (strict improvement required).
   Equal eval scores no longer promote. Test updated accordingly.
3. ✅ **Voice UI self-edit bypass** — `ui.py` now queues a `POST /tasks` to the
   event bus instead of calling `si.self_improve()` directly.
4. ✅ **Test telemetry contamination** — `evals.log_interaction()` skips
   persist + scoring for MockModel / UnitTestModel / test-prefixed sources.
5. ✅ **Non-atomic knowledge.json write** — `learner._save_knowledge()` now
   uses atomic write (`.tmp` + `os.replace()`). Crash-safe since July 18.

Still open (lower priority):
- Upgrade scout idle — needs coordinator queue connection
- Packaged app stale — rebuild after all code fixes merge

**Done when:** A model trained overnight gets a real evaluation, passes the gate,
and is fused + loaded without a crash. ✓ Pipeline unblocked.

---

## Current Item

Items 1, 1.5, 2, and 4 are complete. Item 3 (orchestrator self-healing) is in
progress. Both Claude and Codex can work on Items 5–9 in parallel — see
`CROSS_AGENT_ORCHESTRATION.md` for lane assignments.
