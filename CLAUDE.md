# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running Jarvis

```bash
# GUI mode (default)
python main.py

# Terminal-only / headless mode
python main.py --no-ui
```

Requires a `.env` file with `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. Google Calendar/Gmail credentials live in `credentials.json` and `token.json` (OAuth2 flow from `google_services.py`).

## Dependencies

```bash
pip install -r requirements.txt
```

Ollama must be running separately for local model support (`ollama serve`). Pull models with e.g. `ollama pull llama3.1:8b`.

## Architecture

Jarvis is a personal voice+text AI assistant for macOS. All requests flow through a two-layer routing system:

**Layer 1 — `router.py` (intent routing):** Regex/keyword matching dispatches specific intents (timers, volume, file ops, calendar, camera, self-improvement, etc.) to the appropriate tool or module before any LLM is called. Falls through to Layer 2 for open-ended AI responses.

**Layer 2 — `model_router.py` (model selection):** Classifies task complexity and routes to the cheapest viable model: Local (Ollama) → GPT-mini → Haiku → Sonnet → Opus. Three modes: `auto` (default, local-first), `cloud`, `local`. Mode is runtime-switchable via natural language.

**LLM backends:**
- `brain.py` — OpenAI GPT via streaming
- `brain_claude.py` — Anthropic Claude via streaming
- `brain_ollama.py` — local Ollama models via streaming

**Memory & learning:**
- `memory.py` — persistent JSON store (`memory.json`) for facts, preferences, projects, conversation summaries, and topic frequency. Thread-safe with atomic writes.
- `learner.py` — auto-extracts facts from conversations using Haiku; runs a background web-search feed every 4 hours on user-interest topics; daily reflection synthesizes user profile. Stores to `knowledge.json`.

**UI:**
- `ui.py` — PyQt6 dark-mode chat window. `VoiceWorker` and `TextWorker` are background `QThread`s that emit signals to update the UI. The window stays on top and is made screen-share invisible via `stealth.py`.
- `main.py --no-ui` runs the same voice loop without Qt.

**Other modules:**
- `voice.py` — OpenAI TTS (`tts-1`, voice `onyx`) + `SpeechRecognition` for STT; streams TTS sentence-by-sentence
- `tools.py` — web search (DuckDuckGo), system control (volume, brightness, screenshot, lock), timers, weather, app launcher
- `terminal.py` — file read/write, shell command execution, clipboard
- `camera.py` — webcam capture + screenshot analysis via Claude vision
- `meeting_listener.py` — taps meeting audio via BlackHole virtual audio device; generates real-time suggestions
- `hotkeys.py` — global macOS hotkeys (Cmd+Shift+J/K/L/; and Cmd+Shift+M)
- `stealth.py` — hides the window from screen share using macOS private APIs (`pyobjc-framework-Cocoa`)
- `self_improve.py` — Jarvis reads its own source, asks Opus to rewrite a file, backs up the original to `.jarvis_backups/`, applies the change, then restarts

**Persistent files:**
- `memory.json` — user facts, preferences, conversation history
- `knowledge.json` — auto-learned user profile, insights, news feed
- `last_session.json` — last briefing timestamp
- `.jarvis_backups/` — timestamped backups created by `self_improve.py`

## Key design constraints

- All AI responses are read aloud — no markdown, bullets, or headers in LLM output. The system prompt enforces plain spoken language.
- `config.py` contains the system prompt and all model identifiers. Change models there, not inline.
- The self-improve pipeline uses Opus exclusively and backs up before every write. Always say "restart yourself" after an improvement to reload changed modules.
- `memory.py` uses a write-to-temp-then-rename pattern to prevent corruption — don't bypass this.
