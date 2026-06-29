# GAP_ANALYSIS.md

**Date:** 2026-06-25  
**Goal:** What separates Jarvis from a true autonomous assistant (Codex/Cursor/Claude)?

---

## Gap 1 — Autonomous Task Execution

**Dimension:** Can Jarvis take a multi-step goal and complete it without hand-holding?

### Current state
`operative.py` + `task_planner.py` + `execution_engine.py` form a real pipeline:
- `plan_task(goal)` → calls **Sonnet** to decompose into ≤6 steps as JSON
- `execution_engine.execute_step()` runs each step against the tool registry
- `$step_N_result` placeholders chain outputs across steps
- A verifier checks each step's output before proceeding

Problems:
1. **Planning requires cloud Sonnet.** In local/open-source mode `plan_task` falls through to a single "chat" step, making the operative useless.
2. **No loop-back on failure.** When a step fails (`ok=False`), the engine logs it and continues — it never retries with a corrected approach.
3. **No persistent task queue.** `task_runtime.py` has SQLite + a threading watchdog, but the semaphore cap is 1 and no harness loop is running continuously.
4. **Approval gate is binary.** It blocks on confidence < 0.74 but has no way to ask a clarifying question mid-task.
5. **Six-step hard cap.** Real coding or research tasks need 10–20 steps.

### Target state
- Plan with a local model (qwen3:30b-a3b or devstral) for any goal
- Retry failed steps with error context before escalating
- Persistent task queue that survives restarts
- Dynamic step insertion when a plan proves insufficient
- Mid-task clarification ("I need the DB schema to proceed — can you share it?")

### Effort: High
### Local model: qwen3:30b-a3b for planning (strong reasoning), glm-4.7-flash for step dispatch

---

## Gap 2 — Code Generation & Execution (Cursor/Codex parity)

**Dimension:** Can Jarvis write code, run it, read the error, fix it, and repeat until it passes?

### Current state
- `terminal.run_command()` executes arbitrary shell commands
- `tools/fs_tools.py` reads/writes files
- `self_improve.py` patches Jarvis's own source (single file, diff + backup + approval gate)
- `coder_workbench.py` reads `git status/diff` to surface what changed
- No sandbox — all execution is on the host

What's missing:
- **No iterative fix loop.** Jarvis can write a file and run a command, but there's no code-agent that reads the test failure, patches the file, and re-runs automatically.
- **No multi-file code awareness.** Can't traverse a repo, understand call graphs, or make coordinated changes across files.
- **No git operations.** No `git commit`, `git branch`, `git push`, no PR creation. Read-only (`git diff`/`git status`).
- **No sandbox.** Arbitrary shell on the host is dangerous for generated code.
- **No test-run-fix cycle.** `execution_engine` can call a "terminal" step, but it doesn't feed stdout back into a "fix" step intelligently.

### Target state
- Code agent that: reads context → writes file → runs tests → reads failure → patches → re-runs (up to N retries)
- Git commit + branch + (optionally) PR creation
- Lightweight sandbox: `subprocess` with `timeout` + working-directory confinement is enough for most tasks
- Multi-file edit: parse imports, find definitions, make coordinated changes

### Effort: High
### Local model: devstral (primary coder), qwen2.5-coder:32b (fallback for complex refactors)

---

## Gap 3 — Memory Across Sessions (True Persistence)

**Dimension:** Does context truly persist, or does each session start cold?

### Current state
Three tiers exist in code:

| Tier | Mechanism | Reality |
|---|---|---|
| Working memory | `memory.json` (facts/prefs/projects) | ✅ Persists — loaded every session |
| Episodic memory | mem0 async writes after each turn | ⚠️ Writes happen but recall is not automatic in context |
| Semantic memory | TF-IDF in-process index | ⚠️ Must be manually queried; not prepended to every LLM call |

Problems:
1. **Episodic recall is opt-in.** `memory_layer.runtime_context()` is called in some paths but not all. The model often doesn't "know" what happened in prior sessions unless the user asks explicitly.
2. **Conversation history caps at 8 turns.** Real tasks span many more turns; prior turns are dropped.
3. **No automatic context injection.** Unlike Claude's persistent memory, Jarvis doesn't automatically surface relevant past facts before answering.
4. **mem0 quality depends on local extraction.** The async write compresses the turn — quality of what gets stored is inconsistent.
5. **No cross-session task state.** A task started in one session has no mechanism to be resumed in the next.

### Target state
- Every LLM call gets a memory header: top-3 relevant facts + last-session summary
- Conversation history stored in rolling vector index, not a capped list
- Task state that survives process restart (already partially done via SQLite)
- Automatic memory consolidation at session end

### Effort: Medium
### Local model: glm-4.7-flash for extraction; no model needed for retrieval (TF-IDF/vector)

---

## Gap 4 — Multi-Agent Coordination

**Dimension:** Can Jarvis decompose work across parallel agents?

### Current state
The architecture has everything designed:
- `AGENT_ROSTER` defines 13 specialist agents in `config.py`
- `agent_dispatch.py` routes tasks to Ollama tool-calling loop
- `agent_worker.py` implements an event-bus polling worker
- `task_runtime.py` has per-agent execution locks
- `docker-compose.yml` defines Redis + PostgreSQL
- `session_orchestrator.py` coordinates named dev sessions via JSON files

What's actually running: **nothing parallel.** The threading semaphore is set to 1 (`JARVIS_MAX_CONCURRENT_TASKS=1`). The event bus at `localhost:8766` has no server. Redis is not running. Agent workers are never started.

`specialized_agents.py` runs planner → executor → reviewer **sequentially**, 3–5 LLM calls in serial. With local models (glm-4.7-flash ~2s/call) that's 10–15 seconds. With qwen3:30b it's 2–5 minutes.

### Target state
- Start the harness loop (`harness/loop.py`) as a background service
- Redis-free option: use SQLite task queue + threading (already partially coded)
- Parallel agent fan-out: planner assigns subtasks → 2–3 workers run concurrently
- Result aggregation: reviewer synthesizes outputs

### Effort: Medium (the code is ~70% written; infrastructure not wired)
### Local model: glm-4.7-flash for coordinator dispatch (fast); qwen3:30b for complex planning

---

## Gap 5 — Speed on Local Models

**Dimension:** Which local models are fast enough for interactive use?

### Current state
From config.py defaults and code:

| Model | Role | Speed on M-series | Interactive? |
|---|---|---|---|
| `glm-4.7-flash` | Default chat | ~1–2s first token | ✅ Yes |
| `qwen3:4b` | Fast chat | ~0.5–1s | ✅ Yes |
| `qwen3:8b` | Balanced | ~2–4s | ✅ Marginal |
| `phi4-mini` | Fast instruction | ~1–2s | ✅ Yes |
| `qwen3:30b-a3b` (MoE) | Planning/reasoning | ~5–15s | ⚠️ Too slow for interactive; fine for background |
| `devstral` | Coding | ~3–8s | ⚠️ Marginal for interactive |
| `qwen2.5-coder:32b` | Heavy coding | ~15–30s | ❌ Background only |

The prior session's OpenClaw problem was using a large dense model where a fast MoE or small model would suffice. The router already uses glm-4.7-flash as default, which is the right call.

**The gap:** For the orchestrator's intent classifier in local mode, the fallback is to skip LLM classification entirely for short queries (<20 words). This means many queries get routed as "chat" instead of being properly dispatched. The fast-path regex covers common cases but misses anything novel.

### Target state
- Use `glm-4.7-flash` or `phi4-mini` as the local intent classifier (fast enough to not feel like a local-path penalty)
- Reserve `qwen3:30b-a3b` for planning and complex multi-step reasoning only
- Never block the UI thread on a >5s model call

### Effort: Low
### Local model: glm-4.7-flash (classifier), phi4-mini (backup)

---

## Gap 6 — File/Workspace Awareness

**Dimension:** Can Jarvis read/write the user's actual files with awareness of repo structure?

### Current state
- `terminal.run_command()` can run any shell command including `find`, `cat`, `grep`
- `tools/fs_tools.py` has explicit `read_file` / `write_file`
- `coder_workbench.py` reads `git status` and `git diff`
- No directory tree traversal built into any agent system prompt
- No call-graph or import-graph understanding
- Agents are instructed to confine to `workspace/` but this isn't enforced at the OS level

### Target state
- Workspace-aware context: before any coding task, snapshot `git status`, recent diffs, top-level directory tree
- File index (list of all files + sizes) injected into coder agent context
- Confinement via `subprocess` cwd restriction (cheap, already possible)
- Import/dependency graph for Python projects (via `ast` — no external tool needed)

### Effort: Low–Medium
### Local model: devstral (code tasks with workspace context)

---

## Gap 7 — Tool Reliability Under Degradation

**Dimension:** When a tool fails (auth expired, API down, network timeout), does Jarvis recover gracefully?

### Current state
Individual tools have try/except with error strings returned, but:
- Google OAuth tokens expire silently — error message says "re-run `python google_services.py --reauth`" (not actionable from voice)
- CDP browser connection fails silently → falls back to `subprocess open`
- Kokoro TTS subprocess sometimes crashes → `voice.py` does have fallback ordering in `TTS_BACKENDS`
- `operative.py` continues on step failure but doesn't try alternatives
- No circuit-breaker: if weather API times out, it fails every time for 20 minutes

### Target state
- OAuth refresh is automatic (token rotation)
- Tool failures in operative trigger a retry with alternative tool
- Circuit breaker per tool: back off on repeated failures
- Health check proactively identifies which tools need attention

### Effort: Medium
### Local model: not needed (infrastructure change)

---

## Gap 8 — No Streaming Agent Output

**Dimension:** Does Jarvis stream partial results during long agentic tasks?

### Current state
- Single-turn responses stream via generator pattern (`_s()` / `smart_stream()`) ✅
- `operative.run_task()` has an `on_progress` callback but it only prints to console
- Background tasks have no streaming output to the UI
- The user sees nothing while operative is planning + executing

### Target state
- Live task panel in PyQt6 UI showing: current step, last step result, overall progress
- SSE stream from `api.py` for web HUD
- Voice: "Working on step 2 of 4 — searching the web now."

### Effort: Medium
### Local model: not needed (UI change)

---

## Gap Summary Table

| Gap | Severity | Effort | Key Blocker |
|---|---|---|---|
| G1: Autonomous task execution | 🔴 Critical | High | Planner needs local model; no failure-retry loop |
| G2: Code generation + execution loop | 🔴 Critical | High | No iterative fix cycle; no git write ops |
| G3: Memory persistence | 🟠 High | Medium | Recall not auto-injected; history capped at 8 |
| G4: Multi-agent coordination | 🟠 High | Medium | Infra designed but not running |
| G5: Local model speed | 🟡 Medium | Low | Classifier path misses novel intents in local mode |
| G6: Workspace/file awareness | 🟡 Medium | Low | No repo context injection; no git write |
| G7: Tool reliability | 🟡 Medium | Medium | Auth expiry, no circuit-breaker |
| G8: Streaming agent output | 🟢 Low | Medium | No progress UI during background tasks |
