# CLAUDE.md

Lean core instructions for the Jarvis codebase. Domain-specific rules are in `.claude/skills/`.

Jarvis is a local-first macOS desktop intelligence runtime with voice, TTS, STT, meetings, memory, tools, and task execution. It must work both in-repo and as a packaged app.

## Core Principles

### Think Before Coding
- State assumptions when they matter.
- If a task is ambiguous, ask a focused question.
- If the repo has a pattern, prefer it over inventing new ones.

### Simplicity First
- Implement the smallest correct change. Would a strong engineer call it tight and boring?
- No speculative abstractions, extra config, or “future-proofing”.

### Surgical Changes
- Touch only what the request requires.
- Do not refactor adjacent code or rename symbols for preference.
- Clean up only dead code your change created.

### Goal-Driven Execution
- Define success condition, make change, verify with narrowest check.
- For packaged/runtime work, verify the packaged app too.
- Do not stop at “code looks right”.

## Jarvis-Specific Rules

### Local-First Is The Default

- `config.py` is the source of truth for runtime defaults.
- Assume `DEFAULT_MODE = “open-source”` is intentional.
- Do not reintroduce paid or cloud fallbacks casually.
- If a local path fails, fix the local path first.

### Use Context7 For External Library Docs, Not Repo Truth

When implementing third-party libraries, prefer up-to-date source documentation through Context7.

Do not use Context7 as a substitute for reading this repository’s code or preserving Jarvis patterns.

### Pre-Commit Gate (mandatory)

Before every commit, run **`REVIEW.md`** in full: security scan, `py_compile`, affected
tests, and the git plumbing commit pattern. No exceptions.

### Cross-Agent Queue

Claude and Codex coordinate queue work through `harness.agent_coordinator`.
Before autonomous `WORK_QUEUE.json` work, claim exactly one lease with the
appropriate `--agent` value and `--takeover-cooling`. Do not edit queue status
directly. Renew long-running work with `heartbeat`, and submit clean committed
work through `finish` so the loop-owned verifier decides completion. See
`CROSS_AGENT_ORCHESTRATION.md`.

### Domain-Specific Rules

Detailed rules for specialized domains:

Read the relevant file on demand (no longer auto-imported, to save per-turn context):
- `.claude/skills/jarvis-voice.md` — Voice/STT/TTS/mic domain rules. Read when touching `voice.py`, `local_runtime/**`, `meeting_listener.py`.
- `.claude/skills/jarvis-packaging.md` — PyInstaller packaged app rules. Read when touching `Jarvis.spec`, `main.py`, `ui.py`, `local_runtime/**`.
- `.claude/skills/jarvis-vault.md` — Obsidian brain/vault rules. Read when touching `vault/**`.
- `.claude/skills/jarvis-testing.md` — Test patterns and mock injection. Read when touching `tests/**`.

## Repo Facts To Preserve

### Runtime / Entry Points

```bash
# GUI mode
python main.py

# Headless mode
python main.py --no-ui
```

### Main Routing Layers

- `router.py`: intent/tool routing before LLM use
- `model_router.py`: model selection and mode behavior
- `orchestrator.py`: request/runtime coordination

### Important Runtime Modules

- `voice.py`: voice loop, wake/listen/TTS behavior
- `ui.py`: PyQt6 desktop app and status surfaces
- `local_runtime/local_stt.py`: local speech-to-text
- `local_runtime/local_tts.py`: macOS `say` fallback TTS
- `local_runtime/local_kokoro_tts.py`: Kokoro local TTS path
- `meeting_listener.py`: meeting audio and transcript logic
- `runtime_state.py`: packaged/runtime metadata
- `Jarvis.spec`: packaged app build definition

### Configuration

- `config.py` holds runtime defaults, model identifiers, STT/TTS configuration, and system behavior defaults.
- Change defaults there instead of hardcoding them inline.


## Task Delegation and Model Selection

When spawning subagents, pick the cheapest model that can handle the job:

- **Haiku**: bulk mechanical tasks — file reading, grep, format conversion, no judgment needed. Never spawns further subagents.
- **Sonnet**: scoped research, code exploration, synthesis, writing. Default for most tasks.
- **Opus**: only when real planning or architectural tradeoffs are required.

Max spawn depth: 2 (parent → subagent → one more tier max). If a subagent needs a smarter model, it returns to the parent instead of self-escalating.

Preferred tool order: WebFetch first → agent-browser CLI for dynamic pages → pdftotext for PDFs.


## Specialized Agents

Invoke these for targeted review work:

- **python-reviewer** — PEP 8, type hints, security, Jarvis patterns (any `.py` change)
- **security-reviewer** — subprocess, path traversal, secrets, LLM output safety
- **tdd-guide** — write-tests-first, pytest red-green-refactor, AAA pattern
- **silent-failure-hunter** — swallowed exceptions, missing logging, bad fallbacks
- **build-error-resolver** — pytest failures, PyInstaller errors, import issues

Security rules: **@.claude/skills/jarvis-security.md**

## Communication Style For This Repo

When working in this codebase, prefer:

- short plans
- explicit assumptions
- exact file paths
- exact commands used for verification
- absolute timestamps when discussing builds or installed apps

If something is still uncertain, say exactly what is known and what is not.

# Compact instructions

When compacting this conversation, keep: code changes and their file paths, test/CI
failures and their root causes, architectural decisions, and the current next step.
Drop: raw CI/test log dumps, full-file reads, exploratory dead ends, and verbose tool
output. Prefer pushing verbose investigation (log reading, multi-file search, test runs)
into subagents so only a short summary returns to the main thread.
