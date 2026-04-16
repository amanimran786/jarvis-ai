# CLAUDE.md

This file gives Claude Code project-level instructions for working in this repository.

It is intentionally opinionated. Jarvis is a local-first macOS desktop app with a packaged runtime, persistent state, voice I/O, and a lot of easy ways for AI agents to make shallow changes that look correct but break the real product.

## Mission

Jarvis is not a generic chatbot repo.

It is a local-first desktop intelligence runtime for macOS with:

- a PyQt6 desktop app
- a local API/runtime
- local-first model routing
- voice, TTS, STT, meetings, memory, tools, and task execution
- a packaged macOS app that must behave correctly outside the repo checkout

The goal is to make Jarvis feel top-tier while staying local-first and operationally reliable.

## Core Principles

### 1. Think Before Coding

Do not silently pick an interpretation and run with it.

- State assumptions when they matter.
- If a task is ambiguous in a way that changes behavior, ask a focused question.
- If the repo already has a pattern, prefer that pattern over inventing a new one.
- If a simpler solution exists, prefer it and say so.

### 2. Simplicity First

Implement the smallest correct change.

- No speculative abstractions.
- No extra configuration unless the repo already uses configuration for that concern.
- No new dependency for a problem that can be solved with existing code.
- No “future-proofing” code that the user did not ask for.

The standard is: would a strong engineer describe this diff as tight and boring?

### 3. Surgical Changes

Touch only what the request requires.

- Do not refactor adjacent code unless the task requires it.
- Do not rewrite comments, rename symbols, or move code around just because you would prefer it differently.
- Clean up only the dead code or imports your own change created.
- If you notice unrelated problems, mention them separately instead of folding them into the task.

### 4. Goal-Driven Execution

Turn requests into verifiable outcomes.

For non-trivial work:

1. define the success condition
2. make the change
3. verify it with the narrowest meaningful check
4. if the repo has a packaged/runtime surface, verify there too

Do not stop at “code looks right”.

## Jarvis-Specific Rules

### Local-First Is The Default

Jarvis is open-source/local-first by default.

- `config.py` is the source of truth for runtime defaults.
- Assume `DEFAULT_MODE = "open-source"` is intentional unless the user explicitly wants cloud behavior.
- Do not reintroduce paid or cloud fallbacks into the core path casually.
- If a local path fails, fix the local path first instead of routing around it.

### The Packaged App Is A Real Product Surface

Source-only verification is not enough for desktop/runtime work.

If your change touches any of these areas:

- `voice.py`
- `ui.py`
- `main.py`
- `Jarvis.spec`
- anything under `local_runtime/`
- packaged permissions, assets, or app behavior

then you must treat the packaged app as part of the acceptance criteria.

Use:

```bash
/Users/truthseeker/jarvis-ai/scripts/install_jarvis_app.sh --applications-only
```

Then verify the installed bundle, not just `dist/`.

Important locations:

- `/Users/truthseeker/Applications/Jarvis.app`
- `/Users/truthseeker/Desktop/Jarvis.app`

The Desktop app is a symlink to the Applications bundle. Always verify timestamps and the real target if there is any doubt.

### Packaging Failures Are Common And Must Be Assumed

For packaged-app work, do not assume import success in the repo means bundle success.

Explicitly watch for:

- missing PyInstaller hidden imports
- missing package data files and assets
- missing ONNX/model/VAD assets
- macOS permission plist keys
- path differences between repo runtime and frozen runtime
- `BrokenPipeError` from print/logging in windowed app mode

If a packaged feature fails, inspect the packaged runtime evidence before guessing.

Important runtime artifacts:

- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_crash.log`
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_runtime.json`
- `/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_voice.log`

### Voice/STT/TTS Changes Need End-to-End Thinking

Voice bugs in Jarvis often come from the seams between:

- mic permissions
- mic device selection
- PortAudio / `speech_recognition`
- local STT model load
- packaged assets
- TTS timing and post-speech capture
- UI status clobbering

Do not assume the visible symptom points to the failing layer.

For voice work:

- verify whether the mic opened
- verify which input device was used
- verify whether audio was captured
- verify whether local STT returned text or an error
- verify whether packaged assets exist

Never claim “the mic is broken” or “STT is unavailable” without checking the runtime evidence.

### Status Surfaces Must Reflect Reality

Do not let generic UI/task status overwrite true voice/runtime status.

If a UI element represents live capability state, it must be driven by that capability’s real state, not by unrelated activity like text requests or background tasks.

### Preserve Current Product Direction

Jarvis is aiming for:

- local-first behavior
- zero-API-cost core path where feasible
- “Jarvis-like” desktop presence
- fast, reliable operator behavior

Changes should support that direction rather than drifting back toward a generic cloud chat app.

### Codex Coding Posture

When acting as Jarvis's coding or code-review core:

- default to security-first and local-first choices
- respect the repo seam boundaries between `local_runtime/`, `brains/`, `skills/`, routing layers, and vault layers
- preserve compatibility with `memory_layer.py`, `graph_context.py`, and the packaged app path when relevant
- do not suggest new external API dependencies for core behavior unless the user explicitly wants them or the repo already has that connector pattern
- prefer explicit permission gates, privacy boundaries, and data-redaction thinking for features touching browser, camera, microphone, meetings, screenshots, or stored memory

Be careful with framework-specific advice:

- use `Pydantic` where the repo is already using schema validation or API models
- do not force `Pydantic`, FastAPI dependency injection changes, or new abstraction layers into places that do not need them
- repository fit matters more than generic best-practice theater

### Use Context7 For External Library Docs, Not Repo Truth

When implementing or debugging third-party libraries, frameworks, SDKs, or APIs, prefer up-to-date source documentation through Context7 before relying on model memory.

Use Context7 for:

- library and API docs
- version-specific setup and configuration
- current code examples

Do not use Context7 as a substitute for:

- reading this repository's code
- preserving existing Jarvis patterns
- verifying packaged macOS app behavior

### Obsidian Brain Contract

The vault is not just a note dump. It is a shared operating surface for Jarvis and Obsidian.

When changing the brain:

- keep raw evidence in `vault/raw/` or `vault/raw/imports/`
- keep durable curated notes in `vault/wiki/brain/`
- follow `vault/wiki/brain/03 Brain Schema.md` for metadata, linking, and task style
- follow `vault/wiki/brain/04 Capture Workflow.md` for promotion and placement rules
- update `vault/wiki/brain/91 Vault Changelog.md` when a major brain change lands

Prefer:

- concise YAML frontmatter on new operational notes and templates
- plain markdown tasks that stay useful without plugins
- deterministic templates under `vault/templates/`
- `.canvas` files for visual maps instead of plugin-specific drawing formats

Do not turn the brain into plugin-dependent app logic. Borrow the good conventions from Obsidian Git, Dataview, Tasks, QuickAdd, Templater, JSON Canvas, and thin local bridge tools, but keep Jarvis able to read and write the vault correctly as plain markdown.

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

## Testing Expectations

Prefer the narrowest tests that prove the change.

Examples:

- logic/config change: targeted unit test
- UI status regression: targeted regression test
- packaged-app fix: targeted test plus packaged rebuild verification

Common targeted test commands:

```bash
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_voice_tts_regression.py -q
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_jarvis_regression_suite.py -k 'VoiceStatusUiRegressionTests or transcript_callback_forwards_to_live_bridge' -q
python3 -m pytest /Users/truthseeker/jarvis-ai/tests/test_unit_coverage.py -q
```

If you add a regression for a bug, keep it small and directly tied to the real failure mode.

## What To Avoid

- Do not add dependencies without strong justification.
- Do not bypass atomic write patterns in persistence modules.
- Do not change system prompts or model defaults casually.
- Do not trust repo runtime behavior as proof of packaged runtime behavior.
- Do not “fix” a local failure by silently enabling cloud fallback unless explicitly intended.
- Do not mix unrelated cleanup into bug-fix diffs.

## Good Change Pattern

For non-trivial Jarvis work, follow this shape:

1. identify the real failing layer
2. make the smallest fix there
3. add a regression test for that failure mode
4. rebuild or verify the packaged/runtime surface if relevant
5. report the concrete evidence, not just conclusions

## Communication Style For This Repo

When working in this codebase, prefer:

- short plans
- explicit assumptions
- exact file paths
- exact commands used for verification
- absolute timestamps when discussing builds or installed apps

If something is still uncertain, say exactly what is known and what is not.
