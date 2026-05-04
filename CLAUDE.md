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

### Domain-Specific Rules

Detailed rules for specialized domains:

- **@.claude/skills/jarvis-voice.md** — Voice/STT/TTS/mic domain rules (verification checklist, common gotchas, runtime artifacts)
- **@.claude/skills/jarvis-packaging.md** — PyInstaller packaged app rules (when to test, build script, common failures, BrokenPipeError prevention)
- **@.claude/skills/jarvis-vault.md** — Obsidian brain/vault rules (write-only-when-approved, directory structure, brain schema, vault search)
- **@.claude/skills/jarvis-testing.md** — Test patterns and mock injection (narrowest tests, pytest commands, mock setup, PyQt6/sounddevice mocking)

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


## Communication Style For This Repo

When working in this codebase, prefer:

- short plans
- explicit assumptions
- exact file paths
- exact commands used for verification
- absolute timestamps when discussing builds or installed apps

If something is still uncertain, say exactly what is known and what is not.
