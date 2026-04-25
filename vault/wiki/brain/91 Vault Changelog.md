---
type: task_hub
area: vault
status: active
source: repo
confidence: high
created: 2026-04-15
updated: 2026-04-24
version: 6
tags:
  - vault
  - changelog
  - provenance
  - obsidian
related:
  - "[[03 Brain Schema]]"
  - "[[04 Capture Workflow]]"
  - "[[70 Jarvis Decision Log]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[96 Learning Loop]]"
---

# Vault Changelog

Purpose: preserve note-level provenance for major brain upgrades so Jarvis can compound knowledge without losing change history.

Linked notes: [[03 Brain Schema]], [[04 Capture Workflow]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]]

## 2026-04-24

### Messages router hardening

- fixed spoken message bodies like "message dad and ask him to bring chocalte milk" so the draft body becomes "bring chocalte milk" instead of "him to bring..."
- expanded deterministic Messages handling for "introduce yourself to dad", named relationship contacts, two-word contact-only prompts, and "send it" without an active draft
- added a UI response-generation guard so stale Open-Source worker replies cannot appear after newer deterministic Messages responses
- made the Jarvis console default to high effort and route one-shot natural effort commands locally before hitting the model
- verified the packaged app with safe live draft/cancel smoke tests against the running daemon; no confirmation sends were used
- replaced unsafe self-introduction claims with permission-gated wording and taught the router to safely forward the last Jarvis response, reject overclaiming message bodies, and treat lower-case two-word names like "fiza imran" as contacts rather than body text
- added verified Gemma 4, Qwen3.6, DeepSeek V4 Flash, and Llama 4 Maverick model-fleet candidates with local-first cautions, including strict tag matching so one Gemma tag does not falsely mark another tag installed

## 2026-04-15

vault_capture.py integrated and all intent tests pass
### Brain foundation from exports

- staged Claude and ChatGPT exports into `vault/raw/imports/`
- created the curated brain spine under `vault/wiki/brain/`
- established identity, projects, preferences, timeline, and synthesis as the durable core

### Career and technical companion expansion

- added role-targeting variants for Anthropic, OpenAI, Apple, YouTube, Meta, Google Play, security incident command, and LLNL technical credibility
- added technical playbooks for debugging, systems design, threat modeling, and AI runtime reasoning
- made Jarvis explicitly responsible for senior cybersecurity, AI, and software-engineering companionship
- made Jarvis explicitly responsible for broader universal engineering and problem-solving behavior

### Obsidian operating layer

- added [[03 Brain Schema]] as the plugin-optional metadata and task contract
- added [[04 Capture Workflow]] as the deterministic capture and promotion flow
- added reusable templates under `vault/templates/`
- promoted changelog-backed provenance as a first-class brain rule
- updated [[81 Jarvis Brain Map]] to include schema, capture workflow, and changelog navigation

### Runtime state correction

- updated [[80 Jarvis Roadmap]] with a current runtime snapshot instead of the older overstated "goal delivered" framing
- corrected the default local chat model from `gemma4:e4b` to `jarvis-local:latest`
- corrected the voice stack from `say`-only framing to `Kokoro -> say`
- recorded that semantic retrieval uses `ollama-embeddings` with `TF-IDF` fallback still in code
- recorded that local STT is configured for `faster-whisper`, but the current development shell audit showed an import/runtime gap, so packaged-app verification and shell verification should not be conflated

### Self-sustaining vault lane

- added [[92 Agent Inbox]] as the bounded queue for ongoing background brain-maintenance work
- taught `vault_curator` to append explicit inbox items instead of only mutating canonical notes
- added a background `vault` task lane so Jarvis can keep curating the brain while other work continues
- kept canonical note changes explicit by routing unresolved or open-ended curation into the inbox first
- added note ownership and `write_policy` metadata to the template/schema contract
- blocked `generated` and `propose_only` notes from direct curator append paths inside `vault_edit.py`

## 2026-04-15 (session 2)

### Obsidian write-back pipeline

- created `vault_capture.py` — natural-language capture pipeline that writes directly to brain notes without requiring wikilink syntax from the user
- `add_task()` → [[90 Task Hub]] under Incoming heading with Obsidian checkbox format and `📅 YYYY-MM-DD #brain` tag
- `log_decision()` → [[70 Jarvis Decision Log]] under Decisions with full ### heading and YAML-style fields
- `add_changelog_entry()` → this file, under a dated heading
- `capture_story()` → [[60 Interview Story Bank]] under Stories with full STAR format
- `update_projects()` → [[20 Projects]] under Recent Updates
- `save_to_brain()` → new brain notes via `brain-note-template` or appends to existing notes
- `append_to_note()` / `read_note()` — generic wikilink-addressed mutation and read
- all write functions call `_patch_frontmatter()` to bump `updated` date and increment `version`
- `detect_capture_intent()` — regex-based intent detection without LLM cost
- `handle_capture()` — single entry point for router; tasks, changelog entries, brain saves, project updates, and append/read commands resolved without LLM; decisions and stories fall through to vault_curator for structured field extraction

### Router integration

- wired `vault_capture` import and fast-path into `router.py` just before the vault search block
- capture commands now handled inline before orchestrator, keeping latency near zero for simple vault writes

### Vault curator agent spec expanded

- `agents/vault_curator.md` rewritten with explicit capture targets, heading formats, frontmatter rules, wikilink discipline, and extraction rules for decisions and stories
- decisions and stories now have field-by-field instructions so the LLM path produces consistent structured output

Affected: [[90 Task Hub]] · [[70 Jarvis Decision Log]] · [[60 Interview Story Bank]] · [[20 Projects]] · [[80 Jarvis Roadmap]]

## 2026-04-16

### Candidate staging lane

- added `vault/wiki/candidates/` as the explicit staging layer between inbox work and canonical notes
- taught `vault_curator` to stage writes for `propose_only` notes into candidate notes instead of failing dead-end
- kept `vault_edit.append_under_heading()` strict so canonical notes still fail closed unless the write policy allows direct mutation
- documented candidate-note promotion rules in [[03 Brain Schema]] and [[04 Capture Workflow]]
- added explicit candidate promotion so accepted staged updates can merge back into canon with a promotion log instead of copy-paste drift
- added stale-review helpers for candidate notes and [[92 Agent Inbox]] so the self-sustaining lane can surface review debt instead of only accumulating it
- added explicit next-step recommendations for stale candidate notes and stale inbox items so curator review surfaces `promote`, `requeue`, or `archive` style actions without taking them implicitly
- added explicit maintenance actions for candidate and inbox debt: archive candidate notes, close inbox items, requeue inbox items, and promote-plus-archive when a candidate is resolved
- tightened native curator behavior so `review_required: true` curated notes stage into candidates instead of appending directly
- tightened maintenance hygiene so archived candidate notes stop showing up as stale debt and resolved inbox items move into `Done` instead of cluttering active sections
- added `apply recommended action` support so stale review output can turn into a bounded maintenance command instead of only a suggestion
- added a vault maintenance status snapshot so Jarvis can report active, archived, stale, queued, in-review, and done counts without piecing them together manually
- added a bounded batch maintenance action for stale vault work so Jarvis can archive low-risk stale candidate drafts and close low-risk stale inbox items with a hard cap, while skipping anything that would promote into canon or otherwise needs manual review
- agent-led graph cleanup connected raw imports, compiled notes, templates, indexes, and support READMEs back into the curated brain so Obsidian graph view reflects the real vault structure instead of isolated leaf notes
- renamed the repeated vault `README.md` files into descriptive guide names and added [[06 Vault Support Hub]] plus [[07 Import Source Hub]] so the graph clusters around deliberate hubs instead of several ambiguous filename-only leaves
- renamed the old `readme-md` / `ingested-file-readme-md` bridge pair into [[product-surface-source|Product Surface Source]] plus `raw/Product Surface Source.md` so product-surface evidence reads like a deliberate knowledge node instead of an ingest artifact

## 2026-04-19

### Proposal-first local skill loop

- added [[79 Local Skill Loop]] as the canonical brain policy for Hermes-style local learning without unsafe self-modification
- added a dedicated `skill_builder` specialist role for drafting reusable skill proposals from local vault evidence
- added a non-mutating `/skills/propose` endpoint so Jarvis can validate skill payloads without writing files or changing `skills/index.json`
- added a managed `skill-builder` task lane so background skill work stays separate from chat routing and vault curation
- kept direct skill creation explicit and approval-sensitive instead of allowing background agents to silently mutate the skill registry
- refreshed generated vault indexes so the compiled product-surface source now resolves as [[product-surface-source|Product Surface Source]] instead of a stale bridge filename

### Context budget and local coding loop

- added [[82 Context Budget Discipline]] as the canonical brain policy for token/context quality during local coding-agent work
- added a native context-budget runtime surface so Jarvis can answer token-saver and local coding loop questions from live policy instead of hype-thread memory
- connected context discipline to [[78 AI Runtime Agent Engineering Principles]], [[79 Local Skill Loop]], and [[80 Jarvis Roadmap]]

### External agent pattern intake

- added [[83 External Agent Pattern Intake]] as the canonical adopt/adapt/watch/gate/defensive-only review layer for external agent repos
- added a native runtime pattern registry for GBrain, Multica, Claude Code Best Practice, OpenMythos, Scrapling, Browser Harness, and Decepticon-style signals
- tightened the skill-builder contract so proposed skills include "Do NOT use for" boundaries before promotion

### Skill precision and negative triggers

- added first-class `negative_triggers` metadata support so skill matching can suppress a skill before loading its full instructions
- updated generated skill proposals to carry negative-trigger metadata alongside the prose "Do NOT use for" section
- exposed negative triggers through skill registry detail so future skill debugging can inspect both positive and negative activation rules
- surfaced negative triggers inside orchestrator skill metadata so classification sees positive and negative activation rules together
- seeded negative triggers into the broadest curated skills: local knowledge, vault overview, personal context, eval self-improve, and self-improvement

### Frontier capability parity

- added [[84 Frontier Capability Parity]] as the measurable local-first scorecard for Claude/GPT/Codex/Gemini/Grok-style capability groups
- added a `/capability-parity` runtime endpoint and `/parity` console command so Jarvis can report ready, partial, and gap areas from live local state
- connected parity tracking to roadmap, context budget, and external pattern intake so the ambition stays grounded in verifiable product seams

### Defensive security ROE

- added [[85 Defensive Security ROE]] as the scoped defensive operating contract for cybersecurity tasks
- added `/security-roe` and `/security-roe <template>` console/API surfaces for authorization, threat model, code review, incident, AI misuse, and browser/source gates
- connected the security ROE skill to capability parity so Jarvis can report the cybersecurity companion surface as runtime-backed instead of persona-only

## 2026-04-21

### Claude shared brain bridge

- added [[95 Claude Shared Brain Contract]] so Claude Code and Jarvis can share the local Obsidian vault without unreviewed memory writes
- added `vault/indexes/Repo Map.md` as a cheap repo-orientation layer for Claude and Jarvis before opening source or vault files
- added `.claude/commands/search-shared-brain.md`, `.claude/commands/append-session-lesson.md`, `.claude/commands/propose-vault-update.md`, and `.claude/commands/token-discipline.md`
- appended the shared-brain rules to `CLAUDE.md`, including targeted search, proposal-first writes, and no-auto-commit rules for vault changes

### Capability eval harness

- added [[86 Capability Eval Harness]] to keep local capability claims tied to explicit regression cases
- added `/capability-evals` and `/capability-evals <group>` console/API surfaces for eval coverage and live golden commands
- added harder golden prompts for defensive security ROE and frontier eval coverage

### Production readiness contract

- added [[87 Production Readiness Contract]] so Jarvis answers production/free-use readiness questions from a live contract instead of ambition language
- added the `/production-readiness` API endpoint and console command to separate local daily-core readiness from full production go-live gates
- recorded that "100% free regardless of request" is not a valid claim because live sources, third-party accounts, permissions, local hardware limits, and safety boundaries still apply

### Coder workbench

- added [[88 Coder Workbench]] as the terminal-native coding loop for git state, changed files, and deterministic verification plans
- added `/coder/status`, `/coder/verify-plan`, `jarvis --code-status`, and `jarvis --verify-plan`
- added a coding-agent eval case so the workbench is treated as part of the local Claude/Codex parity path

### Local model fleet and free training lanes

- added [[89 Local Model Fleet]] as the canonical brain note for local LLM inventory, role-based model pulls, free training lanes, and self-learning promotion gates
- added `/local/model-fleet`, `jarvis --model-fleet`, and console `/model-fleet`
- recorded that Google Colab and Unsloth can be useful training labs for LoRA/SFT/GRPO experiments, but not reliable 24/7 Jarvis hosting
- recorded that Jarvis should not download every local model; it should install by role, measure, and promote through evals

### agentic-stack portable brain intake

- added agentic-stack to [[83 External Agent Pattern Intake]] as an `adapt` pattern for portable `.agent/` brain compatibility
- recorded the useful seams: four memory layers, host-agent review of candidate lessons, progressive-disclosure skills, typed permission protocols, and recall-before-action hooks
- kept Jarvis's canonical brain unchanged; `.agent/` should become an export/import compatibility surface only after safety review, not an installer-driven replacement for `AGENTS.md`, `CLAUDE.md`, or `skills/index.json`

## 2026-04-22

### Local-first upgrade loop

- guarded OpenAI and Anthropic clients so open-source mode can import cloud backends without API keys
- changed provider-priority helper calls to try local Ollama first and fail closed in open-source mode instead of falling through to cloud
- moved self-improve analysis and code generation onto the local-first provider path rather than directly importing Claude as the default teacher
- moved local training distillation and local model-eval judging onto the same free-first provider path so teacher and judge calls stay local in open-source mode
- changed local training, beta, automation, and eval request defaults from Claude model labels to the configured local reasoning model
- registered `LOCAL_CODER_RECOMMENDED` for the Qwen3-Coder 30B next-pull candidate while keeping the installed 7B coder as the active default until eval promotion
- added role-aware specialist-agent local timeouts so deep roles get enough time without making lightweight roles hang
- added a runnable coder verification loop behind `/coder/run-verify-plan`, console `/run-verify-plan`, and `jarvis --run-verify-plan`

### Learning loop scaffold

- added [[96 Learning Loop]] as the canonical capture, distill, retrieve, grade, and promote policy
- added `vault/sessions/lessons.md` as an append-only, indexed candidate lesson lane
- added `vault/templates/session-lesson-template.md` so future lesson captures stay structured and review-gated

## 2026-04-23

### Iron Man Jarvis foundation

- added `jarvis_core_brain.py` as the always-on identity snapshot module
  - loads `10 Identity.md`, `20 Projects.md`, `30 Preferences.md`, `80 Jarvis Roadmap.md` at import time
  - caches a compact (~1 800-char) combined snapshot with 5-minute TTL refresh
  - injected as the first layer of `system_extra` in `model_router.smart_stream()`
  - gives every local and cloud model the same always-on Aman identity and Jarvis north star that CLAUDE.md gives Codex
- added `jarvis_agents.py` as the parallel task dispatcher
  - calendar, tasks, vault, code, and research sub-agents run concurrently via ThreadPoolExecutor
  - `run_briefing()` → fan-out to calendar + tasks + vault, merge with escalation tags
  - `escalation_summary()` → only surface items flagged as urgent/overdue/blocked
  - `research_and_brief(topic)` → research + vault agents on a specific question
- wired Iron Man Jarvis fast-path into `router.py`
  - "brief me" / "morning briefing" / "give me an update" / "what's my status" → `run_briefing()`
  - "what needs my attention" / "anything urgent" / "what's blocking" → `escalation_summary()`
  - "run agents on X" / "research agent X" / "parallel research X" → `research_and_brief(X)`
- fixed pending message draft cancel/confirm bugs
  - added bare "cancel", "abort", "nevermind", "stop", "discard", "nvm" to `_is_message_cancel_query`
  - added `_META_BODY_BLOCKED` guard to body-replacement fallback so meta-commands never become message bodies
  - added 10 regression tests

Affected: [[10 Identity]] · [[20 Projects]] · [[30 Preferences]] · [[80 Jarvis Roadmap]]

### Proactive watcher, mem0 full wiring, model fleet update

- added `jarvis_watcher.py` — background watcher thread
  - scans calendar (events starting within 15 min) and task hub (urgent/overdue open tasks) every 5 min
  - delivers macOS banner notifications via osascript
  - speaks proactive alerts via registered TTS callback when not in quiet hours (22:00–08:00)
  - deduplicates alerts via `_notified_keys` set to prevent repeat notifications per session
  - configurable via env vars: `JARVIS_WATCHER_INTERVAL_SEC`, `JARVIS_WATCHER_QUIET_START/END`, `JARVIS_WATCHER_ENABLED`
  - wired into `main.py` and `ui.py` at startup; speak callback registered from voice path
- completed mem0 wiring
  - `record_turn()` wired into `main.py` headless loop (voice path)
  - `record_turn()` wired into `ui.py` voice UI and text UI paths
  - mem0 status verbal command handler completed in `router.py` (`_MEM0_STATUS_TRIGGERS`)
  - watcher status verbal command handler added (`_WATCHER_TRIGGERS`)
  - `/watcher` GET and `/watcher/notify` POST endpoints added to `api.py`
- updated `local_runtime/model_fleet.py`
  - added Devstral, Qwen3 4b/8b/30b-a3b, phi4-mini as `ModelCandidate` entries with pull commands
  - imports new constants from `config.py` (`LOCAL_QWEN3_FAST/MID/STRONG`, `LOCAL_DEVSTRAL`, `LOCAL_PHI4_MINI`)
- 17 new unit tests in `tests/test_jarvis_watcher.py` — all pass

Affected: router.py · main.py · ui.py · api.py · local_runtime/model_fleet.py

### LLM briefing synthesis, week-ahead agent, STT turbo default

- `jarvis_agents.py` — LLM synthesis layer for all briefing outputs
  - `_synthesise(raw, system)` pipes raw agent data through fastest available local model
  - 8s hard timeout — falls back to raw merged text if model is slow or unavailable
  - `_SYNTH_SYSTEM` persona: calm, direct, spoken-word, Iron Man Jarvis style
  - `run_briefing()`, `run_parallel()`, `escalation_summary()` all go through synthesis
  - added `week_ahead()` — calendar + tasks for next 7 days, LLM-synthesised
  - added `_agent_week()` sub-agent using `google_services.get_week_events()`
  - `_WEEK_AGENTS = ["week", "tasks"]` registry entry
- `google_services.py` — added `get_week_events(days=7) -> list[str]`
  - pulls next N days from Google Calendar with formatted day/time strings
- `router.py` — `_WEEK_TRIGGERS` fast-path
  - "this week" / "week ahead" / "what's coming up" / "next 7 days" → `week_ahead()`
- `config.py` — STT default upgraded from `base.en` to `large-v3-turbo`
  - 8x faster than large-v3 at near-identical accuracy; ~1.6GB download on first run
  - override via `JARVIS_FASTER_WHISPER_MODEL=small.en` for low-RAM machines

Affected: jarvis_agents.py · google_services.py · router.py · config.py

### Email urgency watcher, meeting prep agent

- `google_services.py`
  - `get_unread_email_subjects(max_results)` — structured list of dicts with sender/subject/snippet
  - `get_next_event()` — returns next upcoming calendar event as dict with title/start/attendees/description
- `jarvis_agents.py`
  - `_agent_email()` — scans unread inbox for urgency signals, escalates if found
  - `_agent_meeting_prep()` — pulls next event details + vault context about attendees/topic
  - `meeting_prep()` — public function, LLM-synthesised meeting brief in Iron Man Jarvis voice
  - `_BRIEFING_AGENTS` now includes email agent (4-way parallel: calendar+tasks+vault+email)
  - `_MEETING_PREP_AGENTS` registry entry
- `jarvis_watcher.py`
  - `_EMAIL_URGENT_PATTERNS` — regex for urgent/action required/deadline/follow-up etc.
  - `_check_emails()` — scans unread subjects+snippets, returns notifiable alerts
  - watcher loop now fans out to 3 checks: calendar + tasks + emails
- `router.py` — `_MEETING_PREP_TRIGGERS` fast-path
  - "prep me for my meeting" / "next meeting" / "who am i meeting" / "meeting brief" → `meeting_prep()`
- `tests/test_jarvis_watcher.py` — 6 new email urgency pattern tests (23 total, all pass)

Affected: jarvis_agents.py · jarvis_watcher.py · google_services.py · router.py

### Morning auto-brief, conversation fact extractor, mem0 search command

- `jarvis_watcher.py` — morning auto-brief
  - `_should_deliver_morning_brief()` — fires once per day inside an 8 AM ± 10-min window
  - `_deliver_morning_brief()` — runs `jarvis_agents.run_briefing()`, sends notification, speaks TTS
  - configurable via `JARVIS_MORNING_BRIEF_HOUR` env var (default 8)
  - `status()` now reports `morning_brief_hour` and `morning_brief_sent` date
- `jarvis_extractor.py` — new module: conversation fact extractor
  - `extract_async(user, reply)` — fire-and-forget after every turn
  - `extract(user, reply)` — synchronous; returns list of fact dicts
  - LLM pass using fastest local model with structured JSON output
  - routes facts to vault: tasks → [[90 Task Hub]], decisions → [[70 Jarvis Decision Log]], preferences → [[30 Preferences]]
  - all writes also go to mem0 for cross-session retrieval
  - wired into `record_turn()` in `router.py` — activates automatically after every response
  - `/extract` POST endpoint in `api.py` for external triggering
- `router.py` — mem0 search verbal command
  - "what do you remember about X" / "recall anything about X" / "do you remember X" → searches mem0 and returns formatted results
  - `_MEM0_SEARCH_PREFIXES` list covers natural phrasings
- `tests/test_jarvis_extractor.py` — 13 new tests (86 total, all pass)

Affected: jarvis_watcher.py · jarvis_extractor.py · router.py · api.py

## 2026-04-24 — Major Agentic Capability Sprint

### New Modules
- **jarvis_health.py** — concurrent component health checker (ollama/stt/tts/google/mem0/vault/watcher), 60s TTL cache, `spoken_summary()` for voice, `degraded()` list for watcher integration
- **jarvis_executor.py** — multi-step task executor: heuristic split → execute via route_stream → LLM synthesis; `is_multi_step()` used by router for compound requests
- **jarvis_extractor.py** — fire-and-forget fact extraction (tasks/decisions/preferences/entities) from every conversation turn → vault + mem0

### Router Fast-Paths Added
All route directly to local code, bypass cloud orchestrator:
- "health check" / "system status" → `jarvis_health.spoken_summary()`
- "message X and also do Y" → `jarvis_executor.run()`
- "what's on my screen" / "analyze my screen" → `camera.screenshot_and_describe()` [Vision label]
- "what should I work on" / "what's my priority" → `jarvis_agents.focus_advisor()` [Jarvis label]
- "create daily note" / "today's note" → `jarvis_agents.write_daily_note()` [Vault label]
- watcher status trigger updated with EOD hour

### Watcher Enhancements
- Email urgency scanning via `_EMAIL_URGENT_PATTERNS` regex
- Morning brief now auto-creates daily note in vault/daily/YYYY-MM-DD.md
- End-of-day summary at JARVIS_EOD_HOUR (default 18:00, configurable)
- Health monitoring: macOS notifications when components degrade
- `status()` exposes eod_hour, eod_sent, morning_brief_hour, morning_brief_sent

### New Agent Capabilities
- `focus_advisor()` — calendar + tasks + vault → ranked spoken priority recommendation
- `write_daily_note()` — creates vault/daily/YYYY-MM-DD.md with calendar, tasks, focus sections
- `week_ahead()`, `meeting_prep()`, `escalation_summary()` — all using parallel ThreadPoolExecutor dispatch

### API Endpoints Added
- GET /health, POST /execute, POST /extract
- GET /memory/mem0, POST /memory/mem0/search
- GET /watcher, POST /watcher/notify
- GET /daily-note, POST /daily-note

### Vault
- vault/templates/daily-note-template.md added
- vault/daily/ directory created by first morning brief

### Tests
- test_jarvis_health.py: 10 tests
- test_jarvis_executor.py: 19 tests (mock router/model_router to avoid PyQt6/PortAudio in CI)
- test_jarvis_watcher.py: 28 tests including EOD timing tests
- test_jarvis_extractor.py: 13 tests
- test_jarvis_new_fastpaths.py: 9 tests (2 skip cleanly in CI)
- Total new test suite: 77 passing, 2 skipped

### Runtime State (2026-04-24)
- STT: large-v3-turbo (upgraded from base.en — 8x faster, same accuracy)
- Models added: Qwen3 4b/8b/30b-a3b MoE, Devstral, phi4-mini
- Memory: mem0 + Qdrant (local) for cross-session episodic; fact extractor for vault
- Proactive layer: watcher (5min interval), morning brief (8am), EOD summary (6pm)
