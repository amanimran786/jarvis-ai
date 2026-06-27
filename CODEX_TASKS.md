# Codex Task Board
> You are OpenAI Codex working on Jarvis AI at /Users/truthseeker/jarvis-ai
> Read AI_AGENTS_COORDINATION.md first. Commit with prefix [CODEX].
> Local-first: Ollama handles routing; only call cloud APIs when truly needed.

## Context
Jarvis is a Python macOS AI runtime. Main entry: `main.py`. Core modules in `harness/`.
Routing layers: `router.py` → `orchestrator.py` → `operative.py` → `execution_engine.py`.
Model routing: `model_router.py`. UI: `ui.py` (PyQt6). Voice: `voice.py`.
See `AI_AGENTS_COORDINATION.md` for full ownership map and project state.

Run `python -m pytest tests/ -x -q` to verify your work before committing.

## Your tasks (priority order)

### ✅ CODEX-1: GLM 5.2 eval findings (HIGH — blocks Claude)
Completed: published `CODEX_GLM_EVAL.md`; readiness tests pass, but five live query attempts were subscription-blocked, so GLM 5.2 must not become the default.
This task is **blocking** a Claude work item. Complete it first.

- Read `RECOMMENDED_MODELS.md` and `model_router.py` to understand the current GLM routing setup
- Read `evals.json` and `logs/self_eval.jsonl` to understand current eval scores by model
- Run `python -m pytest tests/test_glm52_readiness.py -v` and report pass/fail
- Evaluate GLM 5.2 on 5 representative Jarvis queries (code, planning, QA, voice command, tool routing)
- Write findings to: `GEMINI_GLM_EVAL.md` (or `CODEX_GLM_EVAL.md`)
- Decision: should `model_router.py` be updated to set GLM 5.2 as a default? State yes/no with evidence.
- Commit: `[CODEX] docs(eval): GLM 5.2 routing readiness findings`

### ✅ CODEX-2: Voice TTS per operative step (HIGH)
Completed: added opt-in operative step announcements through the existing macOS `say` backend, with non-fatal failure logging and regression coverage.
- File: `harness/tts.py` (create)
- When Jarvis completes a task step in `operative.py`, speak it aloud using macOS `say` command
- Add config flag: `VOICE_ENABLED` (bool, default False) to `config.py`
- Wire into `operative.py`'s `on_progress` callback — only speak if `VOICE_ENABLED=True`
- Fallback: if TTS fails, log via `logging.exception()` and continue — never crash Jarvis
- The existing TTS path lives in `voice.py` — check it before reinventing (reuse if possible)
- Test: `tests/test_voice_tts_regression.py` already exists — extend it, don't break it
- Commit: `[CODEX] feat(tts): voice utterance per operative step`

### ✅ CODEX-3: PyQt6 system tray panel (HIGH)
Completed: added a standalone PyQt6 tray with live green/yellow/red status, Jarvis launch, latest-task details, and quit actions.
- File: `ui/tray.py` (create, or extend `ui.py` if that's cleaner — check first)
- A minimal macOS system tray icon showing Jarvis status
- Menu items: "Open Jarvis", "Last task status", "Quit"
- Status indicator: green (idle), yellow (running task), red (error)
- Reads `ORCHESTRATOR_STATUS.json` every 5s to update status
- Launch with: `python ui/tray.py &`
- PyQt6 is already a dependency — check `requirements.txt` before adding anything
- Commit: `[CODEX] feat(ui): PyQt6 system tray status panel`

### CODEX-4: CLI UX improvements (MEDIUM)
- Add rich-formatted output to `main.py` responses (use `rich` library — add to requirements.txt if missing)
- Color-code output types: model responses (white), tool calls (cyan), errors (red), memory (dim)
- Add a progress spinner during long operations (wire into `operative.py` streaming output)
- Add `/history` command showing last 10 interactions with timestamps (read from `usage_log.jsonl`)
- Commit: `[CODEX] feat(cli): rich formatting + /history command`

### CODEX-5: Plugin system scaffold (MEDIUM)
- Create `harness/plugins.py` — a simple plugin loader
- Plugins live in `plugins/` directory (already exists — check its contents first)
- Each plugin is a Python file with a `register(jarvis)` function
- On startup, `main.py` auto-loads all plugins in `plugins/`
- Write an example plugin: `plugins/pomodoro.py` — `/pomodoro` command that runs a 25min timer
- Commit: `[CODEX] feat(plugins): plugin loader + pomodoro example`

## How to update this file when done
Add ✅ next to the task name and write one line describing what you built.
Append to `MASTER_LOG.md`:
```
[2026-06-XX HH:MM UTC] [CODEX] Completed: <task name> — <commit hash>
```
