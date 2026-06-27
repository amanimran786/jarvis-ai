# Gemini Task Board
> You are Google Gemini working on Jarvis AI at /Users/truthseeker/jarvis-ai
> Read AI_AGENTS_COORDINATION.md first. Commit docs/analysis with prefix [GEMINI].
> Your superpower: 1M token context window. Use it to read the ENTIRE codebase at once.

## Context
Jarvis is a Python macOS AI runtime with voice, STT, TTS, meetings, memory, tools, and agentic
task execution. ~130+ test files. P1–P8 roadmap complete. Now in stabilization + audit phase.

Core files: `main.py`, `router.py`, `orchestrator.py`, `operative.py`, `execution_engine.py`,
`model_router.py`, `voice.py`, `ui.py`, `harness/*.py`, `local_runtime/*.py`, `tests/*.py`.

Docs: `00_ARCHITECTURE.md` (if exists), `AGENT_BOARD.md`, `ROADMAP_PRIORITY.md`, `CAPABILITIES_AUDIT.md`,
`GAP_ANALYSIS.md`, `AI_AGENTS_COORDINATION.md`.

## Your tasks (priority order)

### GEMINI-1: Full architecture review (HIGH)
Load the entire codebase into your 1M context window and do a single-pass review.

Read ALL of: `main.py`, `router.py`, `orchestrator.py`, `operative.py`, `execution_engine.py`,
`model_router.py`, `task_planner.py`, `task_runtime.py`, `harness/*.py`, `local_runtime/*.py`,
`voice.py`, `ui.py`, `memory.py`, `memory_layer.py`, `semantic_memory.py`, `mem0_layer.py`,
all `agents/*.py` and `brains/*.py`, plus the existing docs (`AGENT_BOARD.md`, `ROADMAP_PRIORITY.md`,
`CAPABILITIES_AUDIT.md`, `GAP_ANALYSIS.md`).

Write findings to `GEMINI_ARCHITECTURE_REVIEW.md`. For each finding include:
- File name + line number
- What is wrong or inconsistent
- Recommended fix (concrete, not vague)

Categories to cover:
- (a) Architectural inconsistencies — layers bypassing each other, duplicate routing logic
- (b) Dead code — unreachable paths, commented-out blocks that have drifted from reality
- (c) Missing error handling — calls that can throw but aren't caught anywhere upstream
- (d) Security concerns — API key exposure in logs, path traversal, subprocess with user input
- (e) Performance bottlenecks — blocking calls on the main thread, missing timeouts, N+1 patterns
- (f) Contract mismatches — function signatures that callers use wrong, missing type hints on public APIs

Commit: `[GEMINI] docs(review): full architecture analysis`

### GEMINI-2: Test coverage audit (HIGH)
- Run `python -m pytest tests/ --co -q 2>/dev/null | head -300` to list all collected tests
- Cross-reference against every public function in `harness/*.py`, `router.py`, `orchestrator.py`,
  `operative.py`, `execution_engine.py`, `task_planner.py`, `model_router.py`
- Find every function NOT covered by any test
- Write prioritized list to `GEMINI_TEST_GAPS.md`: which untested paths are highest risk and why
- Write the 10 highest-priority missing tests directly in `tests/test_gemini_coverage.py`
  (use pytest, follow AAA pattern, mock external calls — no real Ollama/API calls in unit tests)
- Commit: `[GEMINI] test: coverage audit + high-priority gap tests`

### GEMINI-3: Security review (MEDIUM)
Scan the entire codebase for:
- Hardcoded API keys, secrets, or tokens (grep: `SECRET|API_KEY|TOKEN|PASSWORD` not behind `os.getenv`)
- Shell injection: `subprocess` calls with `shell=True` or string concatenation with user input
- Path traversal: file opens using user-controlled strings without `Path.resolve()` + prefix check
- Insecure deserialization: `pickle.load`, `yaml.load` without `Loader=yaml.SafeLoader`
- LLM output reaching `eval()` or `exec()` directly
- Unvalidated STT/voice output flowing into tool dispatch (voice input is untrusted — see `CLAUDE.md`)

Write findings to `GEMINI_SECURITY_REVIEW.md` with severity (HIGH / MED / LOW) and a specific fix.
Fix any HIGH severity issues directly in the source files.

Commit: `[GEMINI] fix(security): remediate high-severity findings`
Then: `[GEMINI] docs(security): full security review report`

### GEMINI-4: Prompt quality analysis (MEDIUM)
- Read `logs/self_eval.jsonl` (all entries — your 1M window can hold them all)
- Read `kb/self_improvement_log.md` and `harness/reflection.py`
- Read the system prompt construction in `orchestrator.py` and `prompt_modifiers.py`
- Identify:
  - Which query types consistently score below 0.7
  - What patterns predict low scores (query length? routing tag? model used?)
  - Which system prompt sections correlate with failures
  - Whether /reflect suggestions are actually being actioned in prompt changes
- Write a detailed improvement plan to `GEMINI_PROMPT_ANALYSIS.md`
  (be specific: which prompt section to change, what to say instead, why it will help)
- Commit: `[GEMINI] docs(analysis): prompt quality deep-dive + improvement plan`

## How to update this file when done
Add ✅ next to the task name.
Append to `MASTER_LOG.md`:
```
[2026-06-XX HH:MM UTC] [GEMINI] Completed: <task name> — <commit hash>
```
