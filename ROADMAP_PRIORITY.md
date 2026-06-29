# ROADMAP_PRIORITY.md

**Date:** 2026-06-25  
**Goal:** Close the gaps between Jarvis and Codex/Cursor/Claude in priority order.  
**Constraint:** Local models carry the load. Cloud only for tasks that genuinely need it.

---

## P1 — Local-First Operative Planner
**Closes:** Gap G1 (autonomous task execution)  
**Why first:** Everything else depends on a working planner. The operative is broken in local mode because `plan_task()` calls Sonnet. Fix this and the entire agentic layer becomes usable.

**What to build:**
1. In `task_planner.py`, add a local planning path using `ask_local_structured()` with `qwen3:30b-a3b`.
2. The schema is already defined (`_PLAN_SYSTEM` + tool summaries). Just wire the local model.
3. Add retry-on-failure: if a step fails, re-invoke the planner with `{failed_step, error_output}` and ask it to insert a corrective step.
4. Raise the step cap from 6 → 12.

**Local model:** `qwen3:30b-a3b` for planning (runs in background; ~8s/plan is fine for agentic)  
**Fast alternative:** `devstral` if the task is code-heavy  
**Estimated effort:** 1–2 days

**Verification:** `python -c "from task_planner import plan_task; print(plan_task('research Python async frameworks and write a summary'))"` — should produce 4–6 steps without touching cloud.

---

## P2 — Code Agent with Test-Run-Fix Loop
**Closes:** Gap G2 (code generation + execution)  
**Why second:** This is the single biggest gap vs Cursor. A working code loop unlocks Jarvis as a daily coding assistant.

**What to build:**
1. New function `coder_workbench.fix_loop(task, max_iterations=5)`:
   - Write file with `devstral`
   - Run `pytest` or the specified test command via `terminal.run_command()`
   - If exit code ≠ 0: feed stdout back to `devstral` with "Fix this failure:"
   - Repeat until pass or max_iterations
2. Wire into router: "implement X", "fix the failing test", "write a function that does Y" → `coder_workbench.fix_loop()`
3. Confinement: set `cwd` on subprocess to `workspace/` by default (already in `agent_dispatch.py` for `backend_engineer`)
4. Git write ops: add `git add -p`, `git commit -m`, `git checkout -b` as tool specs in `tool_registry.py`

**Local model:** `devstral` primary, `qwen2.5-coder:32b` for complex refactors (background)  
**Estimated effort:** 2–3 days

**Verification:** "Jarvis, write a Python function that sorts a list of dicts by key, add a pytest test, and make it pass." Should produce working file + passing test without cloud.

---

## P3 — Auto Memory Injection
**Closes:** Gap G3 (memory persistence)  
**Why third:** Without this, every session starts semi-cold. The data is all there in memory.json and mem0 — it just isn't being surfaced automatically.

**What to build:**
1. In `model_router.smart_stream()` (and all LLM call sites), prepend a `memory_header` to the system prompt:
   - Top-3 semantic memory hits for the current query (already returns from `memory_layer.runtime_context()`)
   - Last session's summary (1–2 sentences from `memory.conversation_history[-1]`)
   - Active projects list
2. Make this the default, not opt-in. Cap the header at 400 tokens to avoid bloating context.
3. Replace the hard 8-turn conversation history cap with a sliding window that summarizes older turns: when history exceeds 8 turns, summarize turns 1–4 into one entry using `glm-4.7-flash` (~0.5s).
4. At session end, write a session summary to `memory.conversation_history` via `memory.save_conversation()`.

**Local model:** `glm-4.7-flash` for history summarization (fast, cheap)  
**Estimated effort:** 1 day

**Verification:** Start a new session, ask "what am I working on?" — should surface active projects and recent topics without the user having to remind Jarvis.

---

## P4 — Harness Loop + Background Agent Queue
**Closes:** Gap G4 (multi-agent coordination)  
**Why fourth:** The code is ~70% written. The blocking issue is that `harness/loop.py` is never started and the event bus doesn't run. This is plumbing, not new logic.

**What to build:**
1. Start `harness/loop.py` as a background thread in `main.py` (same as voice loop). It already has the watchdog.
2. Replace the Redis event bus dependency with the existing SQLite task queue (`task_persistence.py`). `task_runtime.py` already has locking + semaphore.
3. Raise `JARVIS_MAX_CONCURRENT_TASKS` from 1 → 2 (safe on M-series with MoE models; avoid VRAM contention).
4. Wire `agent_dispatch.dispatch()` into `execution_engine.execute_step()` for steps with tool="specialized_agent".
5. Add a simple "task status" command to router: "what's the status of background tasks?"

**Local model:** `glm-4.7-flash` for coordinator; agents use their own model from `AGENT_ROSTER`  
**Estimated effort:** 1–2 days

**Verification:** `task_runtime.submit_task("research LLM routing strategies", kind="research")` → check SQLite has the entry → check it gets picked up and a result written.

---

## P5 — Local Intent Classifier Upgrade
**Closes:** Gap G5 (speed / routing quality in local mode)  
**Why fifth:** Quick win. The current local-mode classifier skips LLM classification for queries <20 words and falls back to "chat". This misses routing to the right tool for half of short voice commands.

**What to build:**
1. In `orchestrator._classify_with_local_structured()`, use `glm-4.7-flash` (not `LOCAL_DEFAULT`) — it's fast enough (~0.5–1s) to be a real intent classifier.
2. Add 15–20 few-shot examples to `_SYSTEM` prompt for common voice patterns ("set a timer", "what's on my calendar", "remind me to").
3. In `_local_short_query_classify()`, add 10 more patterns for common missed cases (file operations, research, task management).
4. Remove the `word_count < 20` short-circuit in `orchestrator.classify()` — let the fast local classifier handle everything.

**Local model:** `glm-4.7-flash` as intent classifier  
**Estimated effort:** 0.5 days

**Verification:** With `DEFAULT_MODE=local`, run 20 representative queries and check that >85% route to the correct tool (vs current ~60%).

---

## P6 — Workspace-Aware Coding Context
**Closes:** Gap G6 (file/workspace awareness)  
**Why sixth:** Relatively cheap, high leverage. Gives the coder agents actual repo context.

**What to build:**
1. `workspace_context.py` (new, ~80 lines):
   - `snapshot()` → returns: git status, last 5 commits, directory tree (2 levels), key file list
   - Cached for 60s so rapid queries don't re-run git
2. Inject snapshot into system prompt when routing to "terminal", "self_improve", or coder agents.
3. Add `ast`-based import graph for Python files: given a filename, list its imports and callers.
4. Wire git write ops from P2 into here.

**Local model:** devstral (receives the context; no model needed for the snapshot itself)  
**Estimated effort:** 1 day

---

## P7 — OAuth Auto-Refresh + Tool Circuit Breakers
**Closes:** Gap G7 (tool reliability)

**What to build:**
1. In `google_services.py`, implement token refresh on 401: try `credentials.refresh(Request())` before returning error.
2. Add a simple circuit breaker dict: `{tool_name: (fail_count, last_fail_ts)}`. After 3 failures in 5 minutes, skip the tool and return a cached/degraded response for 10 minutes.
3. Health check (`_jhealth`) should surface which tools are in circuit-open state.
4. Kokoro TTS: add a watchdog that restarts the subprocess if it hasn't responded in 3s.

**Local model:** not needed  
**Estimated effort:** 0.5–1 day

---

## P8 — Streaming Progress for Background Tasks
**Closes:** Gap G8 (no streaming output during long tasks)

**What to build:**
1. In `task_runtime.py`, add a `_task_events` dict that stores (task_id → list of progress strings).
2. Voice: "Working on step N of M..." utterance between steps (already have `on_progress` callback in operative).
3. PyQt6 UI: a task panel (collapsible tray) that polls `task_runtime.get_events(task_id)` every 2s.
4. For operative tasks routed from router, store task_id in session state so user can ask "how's that task going?"

**Local model:** not needed  
**Estimated effort:** 1 day

---

## Execution Schedule

| Week | Items | Outcome |
|---|---|---|
| Week 1 | P1 (local planner) + P5 (classifier) + P3 (memory injection) | Local-first agentic loop works; memory visible every session |
| Week 2 | P2 (code fix loop) + P6 (workspace context) | Cursor-style code iteration works locally |
| Week 3 | P4 (harness loop) + P7 (circuit breakers) | Background agents run; tools recover from failure |
| Week 4 | P8 (streaming progress) + integration testing | End-to-end: assign complex task → watch it complete |

---

## Local Model Strategy Summary

| Task | Model | Why |
|---|---|---|
| Interactive chat, routing | `glm-4.7-flash` | <2s; already default |
| Intent classification | `glm-4.7-flash` | Fast enough to replace regex for novel intents |
| Short voice commands | `phi4-mini` | Fallback if glm not available |
| Multi-step planning | `qwen3:30b-a3b` | Best local reasoning; background use only |
| Code generation + fix | `devstral` | Purpose-built coder; 3–8s acceptable for code |
| Complex code (>500L) | `qwen2.5-coder:32b` | Background only; too slow for interactive |
| Memory extraction | `glm-4.7-flash` | Fast; compress conversation to facts |
| Research synthesis | `qwen3:8b` | Good language quality; ~2–4s |
| Specialized agents | `glm-4.7-flash` | Parallel dispatch stays fast |

**Never use for interactive (<5s target):** `qwen3:30b-a3b`, `qwen2.5-coder:32b`, `devstral` as primary  
**Never use paid cloud for:** chat, routing, memory, code iteration (local is sufficient)  
**Reserve cloud for:** initial plan of novel complex tasks if local plan quality is poor (optional, flagged)
