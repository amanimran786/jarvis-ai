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

### ✅ CODEX-5: Wire the autonomous loop scheduler (HIGH)

Completed: connected durable handoffs to isolated local `task_runtime` execution,
polling, approval states, timeout cancellation, completion harvesting, and
classified retry failures. Retired three false active sessions and blocked
untyped legacy rows from autonomous execution.

The loop harness is built (`orchestrator_loop.py`, `LAUNCH_QUEUE.json`). What's missing is the
companion script that actually fires Cowork sessions and harvests completions.

Build `harness/cowork_launcher.py`:
- Read `LAUNCH_QUEUE.json`; for each entry with `status == "pending"`:
  - Append to `PENDING_SESSIONS.json` with the full prompt and metadata
  - Mark the entry `"handoff_ready"` and write back to `LAUNCH_QUEUE.json`
- Call `orchestrator_loop.run_loop()` to harvest completions and enqueue new work
- Script must be idempotent — safe to call every 5 minutes even when nothing is pending

Wire as a launchd job on macOS:
- Write `scripts/com.jarvis.loop.plist` (canonical plist for the repo)
- Deploy to `~/Library/LaunchAgents/com.jarvis.loop.plist` for activation
- ProgramArguments: `/usr/bin/python3 /Users/truthseeker/jarvis-ai/harness/cowork_launcher.py`
- Interval: 300 seconds
- Log stdout → `logs/launchd.log`, stderr → `logs/launchd_error.log`
- Create `LAUNCHD_SETUP.md` with install/uninstall instructions:
  - Install: `launchctl load ~/Library/LaunchAgents/com.jarvis.loop.plist`
  - Uninstall: `launchctl unload ~/Library/LaunchAgents/com.jarvis.loop.plist`

Commit: `[CODEX] feat(harness): cowork_launcher + launchd plist for 5-min loop`

Current truth: `handoff_ready` records are submitted through local `task_runtime`
with isolated worktrees. The scheduler persists runtime correlation, polls across
invocations, and only hands successful terminal work to the completion verifier.

### CODEX-6: /history command with Rich CLI (MEDIUM) ✅

Build the `/history` command and upgrade the REPL's visual chrome using the `rich` library.

`/history` command (in `main.py` or `harness/repl.py`):
- Default: last 10 turns; accept optional `N` argument (`/history 20`)
- Read turns from `usage_log.jsonl`
- Each turn: timestamp (dim), role label (bold cyan for user / bold green for assistant), content truncated to 120 chars
- Use `rich.table.Table` or `rich.panel.Panel` for layout

REPL prompt upgrades:
- Color the `Jarvis>` prompt (bold magenta)
- Show budget status (tokens used / limit) in the right margin using `rich.live` or prompt suffix
- Color-code response types: model output (white), tool calls (cyan), errors (red), memory ops (dim)
- Add a spinner (`rich.progress` or `rich.status`) during long operations — wire into `operative.py`'s streaming callback

Add `rich` to `requirements.txt` if not already present (check first).
If not installed in the environment: `pip install rich`

Commit: `[CODEX] feat(cli): /history command + Rich REPL chrome`

### CODEX-7: Plugin system foundation (MEDIUM) ✅

Build a plugin loader in `harness/plugin_loader.py`. Check `plugins/` for any existing files first
before writing anything — don't clobber.

`harness/plugin_loader.py`:
- `scan_plugins(plugins_dir: str) -> list[ModuleType]` — imports every `*.py` in `plugins/`
- `load_all(router) -> int` — calls `plugin.register(router)` on each; returns count loaded
- Failures in one plugin must not abort the others — catch, log, continue
- Log each loaded plugin at INFO level

Plugin contract:
- Each plugin exports `register(router)` where `router` is the Jarvis command router
- `register` calls `router.add_command(name, handler, help_text)`

Example plugin — `plugins/echo_plugin.py`:
- Adds `/echo <text>` command
- Returns the input text prefixed with `Echo: `
- Demonstrates minimal register(router) contract with no external dependencies

Wire into `main.py` startup — call `plugin_loader.load_all(router)` before the REPL loop.

Commit: `[CODEX] feat(plugins): plugin_loader + weather example plugin`

## How to update this file when done
Add ✅ next to the task name and write one line describing what you built.
Append to `MASTER_LOG.md`:
```
[2026-06-XX HH:MM UTC] [CODEX] Completed: <task name> — <commit hash>
```
