# Jarvis AI

A personal voice + text AI assistant for macOS. Jarvis combines local-first inference, cloud escalation when needed, persistent memory, self-learning, live browser and system control, and a PyQt6 desktop UI.

## Features

- **Voice + text interface** — speech-to-text input, ElevenLabs TTS output, and a desktop chat UI
- **Local-first model routing** — Ollama handles private everyday requests first, with GPT-mini, Haiku, Sonnet, or Opus used only when the task warrants the extra cost
- **Persistent memory** — remembers facts, preferences, projects, and recent context from local JSON stores
- **Self-learning** — background knowledge feed, fact extraction, and daily reflection
- **Live browser control** — open sites, search, summarize the current page, navigate back and forward, reload, and click visible links or buttons
- **System control** — volume, brightness, screenshots, app launch, lock screen, clipboard readout, and shell commands
- **Admin command path** — can run a terminal command through the native macOS administrator prompt when explicitly asked
- **Google integration** — Calendar and Gmail read/create/send via OAuth2
- **Meeting overlay** — floating HUD during calls with live transcript, real-time AI suggestions, and screen scan; invisible to screen share
- **Webcam + screen vision** — image and screen analysis from the camera and desktop
- **Self-improvement** — Jarvis can inspect and rewrite parts of its own source, validate generated Python, back up originals, and apply changes atomically
- **Stealth mode** — windows hidden from screen share using macOS APIs

## Requirements

- macOS
- Python 3.12+
- [Ollama](https://ollama.com) for local model support
- [BlackHole 2ch](https://existential.audio/blackhole/) (optional) for capturing call audio in meeting mode

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/amanimran786/jarvis-ai.git
   cd jarvis-ai
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create a `.env` file in the project root:
   ```
   OPENAI_API_KEY=sk-...
   ANTHROPIC_API_KEY=sk-ant-...
   ELEVENLABS_API_KEY=...
   ELEVENLABS_VOICE_ID=...   # optional, defaults to George
   ```

5. Optional: add `credentials.json` from Google Cloud Console for Calendar/Gmail OAuth.

6. Install and pull the recommended local models:
   ```bash
   brew install ollama
   brew services start ollama
   ollama pull llama3.1:8b
   ollama pull qwen2.5-coder:7b
   ollama pull mistral
   ```

7. Grant macOS permissions when prompted:
   - Accessibility, so global hotkeys and input automation can work
   - Contacts, so first-run iMessage contact lookup can succeed
   - Microphone, camera, and screen recording as needed for voice, webcam, and screen analysis
   - Automation permissions for controlling apps like Safari, Messages, and Terminal

## Running

```bash
# GUI mode (default)
./run.sh

# Headless / terminal-only
./run.sh --no-ui
```

Jarvis exposes a local API while running:

- `GET /status` — current mode and local-model availability
- `POST /chat` — chat with Jarvis
- `GET /memory` — inspect saved memory
- `POST /mode` — switch `local`, `cloud`, or `auto`

## Hotkeys

All hotkeys use `Cmd + Option` and work system-wide, even during screen share.

| Hotkey | Action |
|---|---|
| `⌘⌥J` | Capture screen → Jarvis analyzes it |
| `⌘⌥K` | Capture webcam frame → Jarvis analyzes it |
| `⌘⌥L` | Read clipboard → Jarvis responds |
| `⌘⌥M` | Toggle Smart Listen (meeting audio) |
| `⌘⌥O` | Toggle Meeting Overlay HUD |
| `⌘⌥;` | Toggle Jarvis window visibility |

## Architecture

```text
main.py              # Starts UI, API, hotkeys, learner, agents
api.py               # Local HTTP API
ui.py                # PyQt6 desktop interface
router.py            # Layer 1 routing: fast-path commands, hardware, orchestrator fallback
orchestrator.py      # Layer 2 intent classification into tool decisions
model_router.py      # Layer 3 model selection: Local -> GPT-mini -> Haiku -> Sonnet -> Opus
brain.py             # OpenAI backend
brain_claude.py      # Anthropic backend
brain_ollama.py      # Ollama backend
browser.py           # Safari/Chrome control and current-page summarization
terminal.py          # Shell, file, and admin-command helpers
memory.py            # Persistent JSON store
learner.py           # Fact extraction, knowledge feed, reflection
overlay.py           # Floating meeting HUD
meeting_listener.py  # BlackHole audio capture and Whisper transcription
self_improve.py      # Self-rewriting pipeline with backup and syntax validation
```

## Configuration

All model identifiers and the system prompt live in `config.py`. Change models there, not inline.

Model routing mode can be switched at runtime via natural language:

- *"switch to cloud mode"*
- *"switch to local mode"*
- *"switch to auto mode"*

Current recommended local defaults:

- `llama3.1:8b` for general local conversation
- `qwen2.5-coder:7b` for coding tasks
- `mistral` for stronger local reasoning

`auto` mode is the default and is intended to keep API usage low without forcing everything through weaker local models.

## Self-Improve Safety

Jarvis can modify parts of its own source, but the apply path is guarded:

- generated Python is syntax-validated before any file is changed
- the original file is backed up to `.jarvis_backups/`
- writes go through a temporary file and atomic replace
- Jarvis tells you to restart after a successful self-improve run so modules reload cleanly

## Security

- API keys are loaded from `.env` and never hardcoded
- `credentials.json` and `token.json` are gitignored
- memory, knowledge, backup, and session files stay local
- meeting overlay is invisible to screen share via `NSWindowSharingNone`
