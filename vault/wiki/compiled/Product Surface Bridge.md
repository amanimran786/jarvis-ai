# Product Surface Bridge

Source file: `raw/Product Surface Source.md`

Connected notes: [[80 Jarvis Roadmap]], [[78 AI Runtime Agent Engineering Principles]], [[70 Jarvis Decision Log]], [[04 Capture Workflow]], [[91 Vault Changelog]]

## Summary
Product Surface Source. Source path: /Users/truthseeker/jarvis-ai/README.md. Jarvis AI is a personal voice + text AI assistant for macOS. It combines local-first inference, persistent memory, self-learning, live browser and system control, and a PyQt6 desktop UI.

## Key Terms
local, jarvis, vault, screen, can, mode, context, skill, prompt, self, skills, wiki

## Citation Map
- Product Surface Source at line 1: Source path: /Users/truthseeker/jarvis-ai/README.md
- Jarvis AI at line 5: A personal voice + text AI assistant for macOS.
- Features at line 9: - **Voice + text interface** — speech-to-text input, ElevenLabs TTS output, and a desktop chat UI - **Local-first model routing** — Ollama handles private everyday requests first, with GPT-mini, Haiku, Sonnet, or Opus used only when the task warrants the extra cost - **Persistent memory** — remembers facts, preferences, projects, and recent context from local JSON stores - **Local skills** — lightweight skill metadata stays cheap to load, while full SKILL.md instructions and references load only for the active request - **Task-scoped conversation context** — Jarvis keeps only the active task in prompt history, rotates between unrelated requests, and compacts older turns into a short carry-over summary - **Local markdown vault** — indexed markdown files in vault/ can be searched and selectively injected into the current request before Jarvis grows prompt context or escalates outward - **Wiki compiler** — raw markdown in vault/raw/ can be compiled into cleaned topic pages and cross-topic indexes for cheaper local retrieval - **Self-learning** — background knowledge feed, fact extraction, and daily reflection - **Live browser control** — open sites, search, summarize the current page, navigate back and forward, reload, and click visible links or buttons - **System control** — volume, brightness, screenshots, app launch, lock screen, clipboard readout, and shell commands - **Admin command path** — can run a terminal command through the native macOS administrator prompt when explicitly asked - **Google integration** — Calendar and Gmail read/create/send via OAuth2 - **Meeting overlay** — floating HUD during calls with live transcript, real-time AI suggestions, and screen scan; invisible to screen share - **Webcam + screen vision** — image and screen analysis from the camera and desktop - **Self-improvement** — Jarvis can inspect and rewrite parts of its own source, validate generated Python, back up originals, and apply changes atomically - **Stealth mode** — windows hidden from screen share using macOS APIs
- Requirements at line 28: - macOS - Python 3.12+ - Ollama for local model support - BlackHole 2ch (optional) for capturing call audio in meeting mode
- Setup at line 35: 1.
- Running at line 79: ```bash
- GUI mode (default) at line 82: ./run.sh
- Headless / terminal-only at line 85: ./run.sh --no-ui `` Jarvis exposes a local API while running: - GET /status — current mode and local-model availability - POST /chat — chat with Jarvis - GET /context — inspect current prompt/session footprint and recent request context stats - GET /vault — inspect the current local vault index status - POST /vault/build — compile raw markdown into wiki pages and rebuild the vault indexes - GET /memory — inspect saved memory - POST /mode — switch local, cloud, or auto`
- Skills at line 99: Jarvis now supports a local skill layer under skills/: - skills/index.json is the L1 metadata index used for cheap relevance checks - skills/<skill_id>/SKILL.md holds the full L2 skill instructions - skills/<skill_id>/references/ holds L3 reference files that load only when that skill is active The first built-in skills are: - browser_execution for live browser navigation and page-action recovery - personal_context for Aman-specific answers grounded in memory and eval signals - self_improvement for evidence-gated self-editing behavior This keeps the baseline prompt smaller while still letting Jarvis load deeper guidance when a request actually needs it.
- Hotkeys at line 115: All hotkeys use Cmd + Option and work system-wide, even during screen share.
- Configuration at line 154: All model identifiers and the system prompt live in config.py.
- Vault at line 174: Jarvis now includes a local markdown vault: - vault/raw/ for raw source material - vault/wiki/ for cleaned topic pages - vault/indexes/ for generated indexes - vault/outputs/ for generated reports and artifacts Use phrases like refresh the vault index, search the vault for X, or what's in your local knowledge base.

## Brain Use

- use this page as product-surface evidence, not as the canonical roadmap; the canonical layer is [[80 Jarvis Roadmap]]
- use it with [[78 AI Runtime Agent Engineering Principles]] when README claims need to be checked against real runtime behavior
- use it with [[70 Jarvis Decision Log]] and [[91 Vault Changelog]] when public-facing product claims change
- keep vault-structure interpretation aligned with [[04 Capture Workflow]] so source docs do not become the brain directly
