# Jarvis AI

A personal voice + text AI assistant for macOS. Jarvis combines local-first inference, cloud escalation when needed, persistent memory, self-learning, live browser and system control, and a PyQt6 desktop UI.

## Features

- **Voice + text interface** — speech-to-text input, ElevenLabs TTS output, and a desktop chat UI
- **Local-first model routing** — Ollama handles private everyday requests first, with GPT-mini, Haiku, Sonnet, or Opus used only when the task warrants the extra cost
- **Local-model improvement loop** — Jarvis can export strong interaction datasets, distill repeated failures into better teacher answers, and generate tuned Ollama model targets so the local path gets stronger over time
- **Persistent memory** — remembers facts, preferences, projects, and recent context from local JSON stores
- **Local skills** — lightweight skill metadata stays cheap to load, while full `SKILL.md` instructions and references load only for the active request
- **Specialist skill bench** — Jarvis can stack a small set of specialist skills per request for planning, coding, debugging, review, architecture, writing, research, and source-grounded answers
- **Specialized agents** — Jarvis can run isolated role passes such as planner, executor, reviewer, science expert, security reviewer, and self-improve critic through the local `agents/` layer
- **Scoped prompt modifiers** — request-local shorthands like `ELI5:`, `/BRIEFLY`, `TONE formal:`, or `ROLE: ... TASK: ... FORMAT: ...` shape only the current answer instead of bloating the global system prompt
- **Task-scoped conversation context** — Jarvis keeps only the active task in prompt history, rotates between unrelated requests, and compacts older turns into a short carry-over summary
- **Local markdown vault** — indexed markdown files in `vault/` can be searched and selectively injected into the current request before Jarvis grows prompt context or escalates outward, with citations to exact local files and headings
- **Graphify repo grounding** — a generated code graph under `graphify-out/` can be injected as supporting context for repo and codebase questions, so Jarvis can answer from the project structure instead of rereading raw files every time
- **Wiki compiler** — raw markdown in `vault/raw/` can be compiled into cleaned topic pages and cross-topic indexes for cheaper local retrieval
- **Structured source ingestion** — PDFs keep page boundaries, PowerPoint decks keep slide boundaries, and Google Drive sources can be pulled into the vault before indexing
- **Self-learning** — background knowledge feed, fact extraction, and daily reflection
- **Live browser control** — open sites, search, summarize the current page, navigate back and forward, reload, and click visible links or buttons
- **System control** — volume, brightness, screenshots, app launch, lock screen, clipboard readout, and shell commands
- **Admin command path** — can run a terminal command through the native macOS administrator prompt when explicitly asked
- **Behavior gates** — deterministic pre/post hooks guard shell commands, admin commands, file writes, and self-improve entry before risky actions execute
- **Cost policy** — Jarvis now uses a deterministic policy layer to keep simple chat local, escalate higher-stakes requests to cloud, and block distill or train cycles until repeated eval failures justify the spend
- **Google integration** — Calendar and Gmail read/create/send via OAuth2, plus Google Drive document ingestion into the local vault
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

   # Free-first routing (defaults shown)
   JARVIS_FREE_FIRST_ENABLED=1
   JARVIS_PAID_FALLBACK_ENABLED=1
   JARVIS_LOCAL_STRICT_FIRST=1
   JARVIS_PROVIDER_PRIORITY_MINI=openai,gemini,anthropic
   JARVIS_PROVIDER_PRIORITY_HAIKU=gemini,openai,anthropic
   JARVIS_PROVIDER_PRIORITY_SONNET=openai,gemini,anthropic
   JARVIS_PROVIDER_PRIORITY_OPUS=gemini,openai,anthropic

   # Optional: max-permissive local profile (keeps only hard-stop gates)
   JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE=0
   JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES=0
   ```

5. Optional: add `credentials.json` from Google Cloud Console for Calendar/Gmail/Drive OAuth.

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

8. Optional: build Graphify repo artifacts for stronger codebase grounding:
   ```bash
   venv/bin/python -m pip install graphifyy
   venv/bin/python scripts/build_graphify_repo.py
   ```
   This writes `graphify-out/graph.json`, `graphify-out/GRAPH_REPORT.md`, and `graphify-out/analysis.json`.

9. Optional: generate callable tool signatures and benchmark local models:
   ```bash
   venv/bin/python scripts/generate_tool_signatures.py
   venv/bin/python scripts/benchmark_local_models.py --repeats 1
   ```
   If you have a malware detection API running locally, set:
   `JARVIS_MALWARE_API_BASE=http://127.0.0.1:<port>`

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
- `GET /context` — inspect current prompt/session footprint and recent request context stats
- `GET /usage` — inspect provider call counts, token totals, local-vs-cloud split, and estimated cloud cost
- `GET /cost-policy` — inspect the current routing and local-model-improvement policy state
- `GET /hooks/status` — inspect behavior-gate activity and recent blocked actions
- `GET /vault` — inspect the current local vault index status
- `POST /vault/build` — compile raw markdown into wiki pages and rebuild the vault indexes
- `GET /local/training/status` — inspect exported datasets, distilled examples, and Modelfiles for local tuning
- `GET /local/evals/status` — inspect local-model benchmark runs and current promoted local model
- `GET /local/automation/status` — inspect automated local-model training and eval cycles
- `GET /local/beta/status` — inspect safe beta-test runs that replay Jarvis goldens and log failures into evals
- `POST /local/training/export` — export successful interaction examples into JSONL for local SFT
- `POST /local/training/distill` — ask a stronger teacher model to rewrite failed cases into better training targets
- `POST /local/training/modelfile` — generate an Ollama Modelfile for the tuned Jarvis local model target
- `POST /local/training/run` — run the full export + distill + pack + Modelfile pipeline in one call
- `POST /local/training/handoff` — build offline Unsloth and Axolotl fine-tune handoff folders from the latest training pack
- `POST /local/evals/run` — compare a candidate local Ollama model against the current baseline on Jarvis-specific benchmark prompts
- `POST /local/evals/promote` — promote a local model only if the benchmark result clears the configured thresholds
- `POST /local/automation/run` — run the full automated cycle: build pack, create candidate model, evaluate it, and promote only if it wins
- `POST /local/beta/run` — run a safe beta suite against Jarvis, log failures into evals, and optionally build a fresh training pack from that new evidence
- `GET /memory` — inspect saved memory
- `GET /memory/status` — inspect tiered-memory readiness, working-memory contents, and the durable user profile summary
- `POST /memory/consolidate` — rebuild the working-memory and long-term-profile tiers from saved facts, projects, preferences, and recent conversations
- `POST /mode` — switch `local`, `cloud`, `auto`, or `open-source`

## Skills

Jarvis now supports a local skill layer under `skills/`:

- `skills/index.json` is the L1 metadata index used for cheap relevance checks
- `skills/<skill_id>/SKILL.md` holds the full L2 skill instructions
- `skills/<skill_id>/references/` holds L3 reference files that load only when that skill is active

The current built-in skills include:

- `browser_execution` for live browser navigation and page-action recovery
- `planning_execution` for ordered multi-step planning and finish conditions
- `code_implementation` for focused code changes and implementation guidance
- `debugging_diagnostics` for ranked root-cause analysis and narrowing steps
- `code_review` for senior-style review focused on bugs and regressions
- `architecture_design` for system design and tradeoff questions
- `engineering_reasoning` for technical software-engineering answers
- `writing_editor` for rewrite, tone, and concision requests
- `research_synthesis` for source comparison and grounded research summaries
- `local_knowledge` and `source_grounding` for vault-first, citation-aware answers
- `local_model_tuning` for dataset export, selective distillation, and tuned Ollama targets
- `personal_context` for Aman-specific answers grounded in memory and eval signals
- `self_improvement` for evidence-gated self-editing behavior

Jarvis now loads a small relevant skill stack per request instead of relying on one giant baseline prompt.

## Agents

Jarvis now supports an `agents/` layer for isolated role passes:

- `planner`
- `executor`
- `reviewer`
- `science_expert`
- `security_reviewer`
- `self_improve_critic`

Use phrases like `use specialized agents`, `use a science expert`, or `use planner executor reviewer on this`.

## Prompt Modifiers

Jarvis now supports request-scoped prompt modifiers at the start of a message. These change only the current answer and are stripped before tool routing and memory tracking.

Examples:

- `ELI5: explain TCP congestion control`
- `/BRIEFLY summarize this page`
- `TONE formal: rewrite this email`
- `COMPARE: Postgres vs SQLite for a local desktop app`
- `FIRST PRINCIPLES: should I use a vector database here?`
- `ROLE: security reviewer TASK: review this auth flow FORMAT: JSON`

Supported modifiers include concise-answer, explanation-style, comparison, role, audience, tone, format, self-evaluation, first-principles, pitfall, and developer-style controls.

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
skills.py            # Local skill registry, matching, and on-demand SKILL.md loading
conversation_context.py  # Shared task-scoped chat session manager and prompt compaction
vault.py                 # Local markdown vault indexing, citation-aware search, and snippet loading
wiki_builder.py          # Deterministic wiki compiler with section metadata
source_ingest.py         # Structured ingest for files, PDFs, slides, URLs, and Google Drive
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
skills/              # Local skill packages: metadata index, SKILL.md files, references
vault/               # Local markdown knowledge layer: raw, wiki, indexes, outputs
```

## Configuration

All model identifiers and the system prompt live in `config.py`. Change models there, not inline.

Model routing mode can be switched at runtime via natural language:

- *"switch to cloud mode"*
- *"switch to local mode"*
- *"switch to auto mode"*
- *"switch to open-source mode"*

Current recommended local defaults:

- `jarvis-local` as the preferred tuned general model target once you create it in Ollama
- `llama3.1:8b` for general local conversation
- `qwen2.5-coder:7b` for coding tasks
- `mistral` for stronger local reasoning

`open-source` mode is now the default. It keeps Jarvis on local models and local runtime logic first, so the product stays usable even when closed-model credits are unavailable. `auto` mode is still available when you want pragmatic cloud escalation without forcing everything through local models.

Jarvis now also includes a local-model improvement loop. The intended low-cost order is:

- export strong successful cloud answers into SFT-style JSONL
- distill only repeated failures or weak local answers with a stronger teacher
- distill a small curated set of expert technology and science answers so the local model learns the tone and structure of high-quality advanced explanations
- build and register a tuned Ollama target such as `jarvis-local`
- let general local routing prefer that tuned model automatically once it exists

The one-shot path is now available too: Jarvis can build a merged local training pack that combines exported strong examples with distilled failure corrections and writes a manifest plus Modelfile for the target model.

Jarvis also has a local beta loop now. Safe golden-style beta runs can replay core product prompts, log any misses into `evals.json`, and feed those standalone failures back into the distillation path even when they were not produced by a normal `/chat` interaction. This is the fastest non-GPU path for improving the local model and product behavior together.

The beta runner now supports a focused engineering suite too, so you can stress just the SWE-heavy prompts without mixing them with every other product behavior check.

Jarvis can also emit offline fine-tune handoff folders for `llama3.1:8b` and `qwen2.5-coder:7b`. Each handoff contains:

- train and validation JSONL splits in conversation format
- train and validation JSONL splits in instruction format
- a baseline Unsloth training script
- a baseline Axolotl QLoRA config
- a per-target manifest and handoff README

Jarvis now also has a local model eval gate. Candidate Ollama models are compared against the current local baseline on Jarvis-specific prompts such as technical reasoning, self-improve policy, personalization, and source-grounded summarization. Promotion is refused unless the candidate improves average score and clears a minimum pass rate.

On top of that, Jarvis now has an automated local-model cycle. It can generate a fresh training pack, build a candidate Ollama model, benchmark it against the baseline, and only promote it if the benchmark clears the gate. This keeps the local model path improving without blindly overwriting the current best model.

Jarvis now also tracks prompt footprint per request. `/chat` responses include a `context` object with the active session id, carried summary size, prompt-size estimate, and rotation count so you can see when context is growing.

Jarvis now also keeps a real provider usage ledger for its text-model calls. `/chat` responses include a `usage` object for the work done during that request, and `/usage` returns a rolling summary across OpenAI, Anthropic, and Ollama calls. Local calls are tracked separately from cloud calls so you can see whether the system is actually staying local-first.

Cloud cost is still an estimate, not an invoice. The ledger uses blended per-million-token assumptions in [usage_tracker.py](/Users/truthseeker/jarvis-ai/usage_tracker.py) so you can see directional spend even when providers return different usage formats.

## Open-Source Mode

Jarvis now has an explicit `open-source` runtime mode. In this mode, Jarvis keeps the answer path on local models and local runtime logic instead of depending on closed-model APIs for core behavior.

- `smart_stream` stays on Ollama when a local model is available
- formatting helpers avoid GPT fallback and stay local
- orchestrator classification skips the Claude classifier and relies on the existing rule and heuristic paths
- specialized-agent and answer fallbacks still keep the product usable when closed APIs are unavailable

This is the right mode when you want Jarvis to behave like an open-source-heavy system rather than a cloud-first assistant with local fallback.

## Local-First Workflow

The clean day-to-day workflow now looks like this:

- leave Jarvis in `open-source` mode for normal use
- use `beta test jarvis` to replay the safe golden cases
- use `beta test engineering` when you want a tighter SWE-focused pass
- use `coach local model` when you want to refresh the local training pack from the latest failures and strong examples
- use `coach engineering model` when you want the same loop but only from the engineering beta subset
- inspect `GET /local/beta/status`, `GET /local/training/status`, and `GET /memory/status` when you want to see whether the local loop is actually improving
- switch to `auto` or `cloud` only when you intentionally want stronger paid models involved

That keeps the harness, memory, evals, and runtime behavior open-source heavy by default while still leaving optional cloud paths available when you deliberately choose them.

## Tiered Memory

Jarvis now consolidates memory into two tiers instead of only appending raw facts:

- `working_memory` keeps the current active projects, recent conversational focus, recurring topics, and assistive preferences that should shape near-term behavior
- `long_term_profile` keeps a compact durable summary plus stable facts and recurring project/topic signals that belong in the broader user model

These tiers are rebuilt automatically whenever Jarvis saves new facts, preferences, projects, or conversation summaries. You can also inspect or refresh them directly through `GET /memory/status` and `POST /memory/consolidate`.

## Testing

Jarvis now has two test layers:

- deterministic regression coverage in [tests/test_jarvis_regression_suite.py](/Users/truthseeker/jarvis-ai/tests/test_jarvis_regression_suite.py)
- opt-in live integration coverage in [tests/test_jarvis_live_integrations.py](/Users/truthseeker/jarvis-ai/tests/test_jarvis_live_integrations.py)
- safe local beta coverage through [tests/jarvis_golden_cases.py](/Users/truthseeker/jarvis-ai/tests/jarvis_golden_cases.py) and `POST /local/beta/run`

Run the regression suite with:

```bash
venv/bin/python -m unittest tests.test_jarvis_regression_suite -v
```

Run the live integration suite only when you explicitly want to hit real services:

```bash
JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 venv/bin/python -m unittest tests.test_jarvis_live_integrations -v
```

For side-effecting tests such as sending an iMessage, opt in separately:

```bash
JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 \
JARVIS_ALLOW_SIDE_EFFECTS=1 \
JARVIS_TEST_IMESSAGE_RECIPIENT="Name or number" \
venv/bin/python -m unittest tests.test_jarvis_live_integrations -v
```

## Vault

Jarvis now includes a local markdown vault:

- `vault/raw/` for raw source material
- `vault/wiki/` for cleaned topic pages
- `vault/indexes/` for generated indexes
- `vault/outputs/` for generated reports and artifacts

Use phrases like `refresh the vault index`, `search the vault for X`, or `what's in your local knowledge base`. Jarvis also searches the vault automatically for knowledge-seeking requests and injects only the most relevant snippets into the active request.

You can also say `build the vault wiki` or `compile the wiki`. That runs the deterministic `wiki_builder.py` pipeline, which turns files in `vault/raw/` into compiled pages under `vault/wiki/compiled/` and refreshes the topic and keyword indexes in `vault/indexes/`.

Vault search is citation-aware. Jarvis can now point to the exact local file and heading it used, and ingested PDFs/slides preserve page or slide boundaries instead of flattening everything into one text block.

You can ingest local files, repositories, notes, normal URLs, and Google Drive links through the API:

- `POST /vault/ingest` with `source_type` set to `auto`, `google_drive`, `directory`, `url`, or `notes`

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
