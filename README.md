# Jarvis AI

A personal voice + text AI assistant for macOS. Multi-model routing, persistent memory, self-learning, meeting overlay, and full system control — all running locally with cloud AI backends.

## Features

- **Voice I/O** — OpenAI Whisper for speech-to-text, ElevenLabs TTS (JARVIS voice)
- **Dual brain** — Claude (Anthropic) and GPT (OpenAI) with automatic model routing based on task complexity
- **Local models** — Ollama support for fully private, offline responses
- **Persistent memory** — remembers facts, preferences, projects, and conversation history across sessions
- **Self-learning** — auto-extracts insights from conversations, runs a background knowledge feed, daily reflection
- **Meeting overlay** — floating HUD during calls with live transcript, real-time AI suggestions, screen scan; invisible to screen share
- **System control** — volume, brightness, screenshots, app launcher, lock screen
- **Google integration** — Calendar and Gmail read/create/send via OAuth2
- **Webcam + screen vision** — Claude vision analyzes what's on screen or through the camera
- **Self-improvement** — Jarvis reads its own source, rewrites files using Opus, backs up originals, and restarts
- **Stealth mode** — window hidden from screen share using macOS private APIs

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.12+
- [Ollama](https://ollama.ai) running locally for local model support
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

5. (Optional) Add `credentials.json` from Google Cloud Console for Calendar/Gmail OAuth.

## Running

```bash
# GUI mode (default)
./run.sh

# Headless / terminal-only
./run.sh --no-ui
```

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

```
main.py
  └── router.py          # Layer 1: intent routing (regex/keyword)
        └── model_router.py  # Layer 2: model selection (Local → Mini → Haiku → Sonnet → Opus)
              ├── brain.py         # OpenAI GPT
              ├── brain_claude.py  # Anthropic Claude
              └── brain_ollama.py  # Local Ollama

memory.py       # Persistent JSON store (facts, preferences, projects, history)
learner.py      # Auto-extracts facts, background knowledge feed, daily reflection
overlay.py      # Meeting HUD (floating, screen-share invisible)
meeting_listener.py  # BlackHole audio capture + Whisper transcription
self_improve.py # Self-rewriting pipeline using Opus
```

## Configuration

All model identifiers and the system prompt live in `config.py`. Change models there, not inline.

Model routing mode can be switched at runtime via natural language:
- *"switch to cloud mode"*
- *"switch to local mode"*
- *"switch to auto mode"*

## Security

- API keys are loaded from `.env` — never hardcoded
- `credentials.json` and `token.json` are gitignored
- Memory and knowledge files are gitignored (stay local)
- Meeting overlay is invisible to screen share via `NSWindowSharingNone`
