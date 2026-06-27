# Jarvis AI — Multi-Agent Coordination Hub
> Last updated: 2026-06-26
> Active AI agents: Claude (Cowork), Codex (OpenAI), Gemini (Google)

## How to use this file
Each AI reads this file at the start of a session, picks up its assigned tasks from the section below,
works in its own branch or directly on main, commits with a prefix tag ([CLAUDE], [CODEX], [GEMINI]),
and updates MASTER_LOG.md when done.

## Project state summary

Jarvis is a local-first macOS AI runtime (Python) with voice, TTS, STT, meetings, memory, tools,
and agentic task execution. Main entry: `main.py`. Core routing in `router.py` / `orchestrator.py`.

### What has been built (31 orchestrator rounds, P1–P8 roadmap complete)

- **P1–P3**: Intent classifier, local model routing (devstral/qwen3:30b), Ollama cloud tier
- **P4**: SQLite event bus (Redis replaced), parallel agent execution (cpu_count threads), task_runtime
- **P5**: Short-query classifier fix (~40% misroute eliminated), fix_loop/code_task routable tool
- **P6**: workspace_context snapshot injected into all orchestrate calls (git status, tree, 60s cache)
- **P7**: Circuit breakers for local model routing (google_services + specialist models)
- **P8**: Operative streaming — progress tokens stream to UI in real-time via thread+queue
- **Memory**: JSON + SQLite dual persistence, mem0 hybrid layer, cross-session memory
- **Self-eval**: Scoring loop, /reflect, /diagnose, LLM improvement notes pipeline (auto every 100)
- **Reflection**: Corrective replanning on step failure (replan_after_failure in task_planner.py)
- **Audit**: Full audit trail (query_received, route_decision, memory_write, reflection_run), hardened audit_log()
- **Silent-failure sweep**: 90+ bare exceptions converted across 13 files
- **Tests**: 130+ test files; SQLite event bus tests, P7 circuit breaker tests, workspace_context tests

### What is open / in progress

- AGENT_BOARD items 11 and 13 still open
- Final baseline suite run (lift freeze)
- audit.jsonl end-to-end verification (memory_write + route_decision)
- run_id threading through operative.py → execution_engine.py
- audit_errors.log rotation + alert threshold
- eval_trace_score surface in briefing
- GLM 5.2 eval handoff (blocked on Codex lane findings)

### Recent commits (last 5)
```
e2fe27a feat(orchestrator): loop round 31 — 2nd reset, signal refreshed, offline confirmed
4a98198 feat(orchestrator): loop round 30 — sessions offline, cadence extended to 20:00 UTC
ca61cf1 feat(orchestrator): loop round 29 — signal unanswered 5m post-reset
0c60da7 feat(orchestrator): loop round 28 — hourly reset 19:00 UTC, resume signal written
e24dc02 feat(orchestrator): loop round 27 — quiet (3rd), stall pattern emerging
```

## Ownership map
| Domain | Owner | Status |
|--------|-------|--------|
| Intent classifier (local, fast-path) | Claude | ✅ Done |
| Local model routing (devstral/qwen3) | Claude | ✅ Done |
| SQLite event bus (P4) | Claude | ✅ Done |
| Parallel agent execution (P4) | Claude | ✅ Done |
| Task runtime + harness heartbeat (P4) | Claude | ✅ Done |
| workspace_context injection (P6) | Claude | ✅ Done |
| Circuit breakers for local routing (P7) | Claude | ✅ Done |
| Operative streaming — live progress (P8) | Claude | ✅ Done |
| Memory auto-injection (JSON + SQLite) | Claude | ✅ Done |
| Audit logging (full event trail) | Claude | ✅ Done |
| Reflection pipeline + /reflect /diagnose | Claude | ✅ Done |
| Self-eval scoring loop | Claude | ✅ Done |
| Corrective replanning on step failure | Claude | ✅ Done |
| Silent-failure sweep (90+ exceptions) | Claude | ✅ Done |
| Fix_loop / code_task as routable tool | Claude | ✅ Done |
| coder_workbench write-test-patch loop | Claude | ✅ Done |
| Web search tool | Claude | 🔄 In progress |
| /task command (user-facing) | Claude | 🔄 In progress |
| audit.jsonl end-to-end verification | Claude | 🔄 In progress |
| run_id threading (operative → engine) | Claude | 🔄 Queued |
| AGENT_BOARD items 11 + 13 | Claude | 🔄 Queued |
| Final baseline suite (lift freeze) | Claude | 🔄 Queued |
| Voice TTS per operative step | **Codex** | 📋 Queued |
| PyQt6 system tray UI | **Codex** | 📋 Queued |
| CLI UX improvements (rich output) | **Codex** | 📋 Queued |
| Plugin system scaffold | **Codex** | 📋 Queued |
| GLM 5.2 eval findings | **Codex** | 📋 Queued (blocks Claude) |
| Full architecture review | **Gemini** | 📋 Queued |
| Test coverage audit | **Gemini** | 📋 Queued |
| Security review | **Gemini** | 📋 Queued |
| Prompt quality analysis | **Gemini** | 📋 Queued |

## Coordination rules
1. Each agent picks up only its assigned tasks from this file and the agent-specific task board.
2. Commit prefix is mandatory: `[CLAUDE]`, `[CODEX]`, or `[GEMINI]`.
3. When a task is done, update the task board file (`CODEX_TASKS.md` or `GEMINI_TASKS.md`) and append to `MASTER_LOG.md`.
4. Never touch another agent's in-progress files without announcing it here first.
5. `WORK_QUEUE.json` is the authoritative task state — update `assigned_to` when picking up a task.
6. Local-first always: Ollama/local models for routing, cloud API only when truly needed.
7. Run `python -m pytest tests/ -x -q` before committing anything that touches Python.
