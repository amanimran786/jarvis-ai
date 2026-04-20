# Jarvis Roadmap

Purpose: keep Jarvis development pointed at the long-term product, not just the bug of the week.

Linked notes: [[20 Projects]], [[50 Synthesis]], [[70 Jarvis Decision Log]], [[90 Task Hub]], [[81 Jarvis Brain Map]]

This note should answer:

- what Jarvis is becoming
- what must be true for it to feel genuinely top-tier
- what we build next
- what we deliberately avoid for now

## North Star

Jarvis should feel like a local-first operating companion: capable, grounded, proactive when useful, calm in tone, and strong at taking action across the Mac without losing trust.

The goal is not just to imitate a cloud chatbot offline. The goal is to build a system that feels more present, more personal, and more operationally useful than a generic assistant.

Jarvis should also be credible as a senior cybersecurity, AI, and software-engineering companion: able to pair on debugging, architecture, risk reasoning, and systems decisions without losing technical grounding.

Jarvis should also feel like a universal engineer and problem solver: able to move across product, systems, AI, security, and operations problems from first principles instead of getting trapped in one specialty lens.

## Non-Negotiables

- local-first by default
- zero or near-zero API dependence in the main experience
- strong packaged macOS app reliability
- memory grounded in curated notes, not transcript sprawl
- truthful self-knowledge about runtime state
- no regressions in voice, launch, or core system actions

## Current Runtime Snapshot

### 2026-04-15

- mode: `open-source` by default
- reasoning model: `deepseek-r1:14b`
- coder model: `qwen2.5-coder:7b`
- default local chat model: `jarvis-local:latest`
- local vision model: `llava:7b`
- local embedding model: `nomic-embed-text:latest`
- semantic retrieval backend: `ollama-embeddings` when available, with `TF-IDF` fallback still present in code
- TTS priority: `Kokoro subprocess` first, `say` fallback second
- local `say` voice currently resolves to `Reed (English (US))` at `175` WPM
- STT backend configuration: `faster-whisper` only, model `small.en`
- reasoning boost: enabled for non-trivial local prompts
- Ollama timeout: `600s` general read timeout, `30s` vision timeout

### What This Means

Jarvis is much closer to the intended local-first shape than the older project summary suggested, but the honest claim is narrower than "goal delivered."

- The core open-source path is local for reasoning, coding, vision, embeddings, and semantic retrieval.
- The default local chat model is no longer `gemma4:e4b`; it is `jarvis-local:latest`, with other local models still available.
- Voice is no longer accurately described as `say` only. The current design is `Kokoro -> say`, with Kokoro carrying the more human voice path and `say` acting as fallback.
- Cloud and paid fallback code still exists in the repo, so "zero cloud dependencies remaining" is not the right statement. The correct statement is that open-source mode is designed to keep the main experience on local models and local runtime paths.
- STT is configured for local-only `faster-whisper`, but that backend was not importable in the current development shell during the audit. That means we should describe STT as the intended local path with a current environment/runtime caveat unless we are explicitly talking about a verified packaged-app run.

## Strategic Layers

### 1. Runtime Reliability

- launch cleanly every time
- keep voice input and TTS stable in the packaged app
- preserve correct mic, STT, and TTS behavior
- keep routing and tool execution predictable

### 2. Brain and Memory

- Obsidian-compatible markdown vault as the durable source of truth
- distilled notes outrank raw imports
- reusable interview stories and decision logs compound over time
- stable frontmatter, task, template, and provenance rules keep the vault queryable as it grows
- future additions should strengthen identity, project context, and decisions instead of just increasing data volume

### 3. Action Competence

- messaging, calendar, browser, system control, notes, and file tasks should be reliable
- short spoken commands should still route to tools when appropriate
- Jarvis should increasingly act like an operator, not a passive responder

### 4. Technical Companion Depth

- Jarvis should be useful on cybersecurity, AI-runtime, backend, observability, and debugging questions
- it should reason like a strong senior technical partner, not a generic assistant with engineering vocabulary
- retrieval and memory should reinforce technical fluency, not just product or career context
- reusable playbooks for debugging, systems design, threat modeling, and AI runtime should compound into better answers over time

### 5. Universal Problem-Solving Depth

- Jarvis should be able to reason across product, systems, AI, security, operations, and UX together when the problem spans layers
- it should default to first-principles diagnosis, smallest-correct-fix thinking, and real verification
- the brain should reinforce broad technical judgment, not only domain-specific phrasing

### 6. Presence and Experience

- voice should sound polished and human
- UI should move toward command-center clarity and intentionality
- Jarvis should feel calm, competent, and aware rather than noisy or generic

## Near-Term Priorities

### Reliability

- keep fixing real packaged-app regressions before adding too much surface area
- harden message sending, voice continuity, and wake/listen flow
- continue verifying source and packaged behavior together

### Memory Quality

- distill more interview and operations stories into the story bank
- capture Jarvis architecture and product decisions in the decision log
- turn major exports into curated notes instead of relying on giant raw archives
- keep the vault aligned to [[03 Brain Schema]], [[04 Capture Workflow]], and [[91 Vault Changelog]]
- keep evidence gaps explicit rather than filling them with smooth but weak summaries
- keep building reusable technical playbooks instead of only storing technical identity as a vague role claim

### Product Intelligence

- improve tool routing for short natural requests
- strengthen self-knowledge and runtime-grounded answers
- keep semantic memory and vault retrieval serving current priorities rather than stale context
- strengthen the technical-companion layer so Jarvis can help with cybersecurity, AI runtime, and software engineering work more deliberately
- strengthen the universal-engineer layer so Jarvis can reason clearly even when the problem crosses product, runtime, and systems boundaries
- start wiring the new technical playbooks into more runtime answer paths so the brain affects live engineering behavior, not just vault structure
- grow reusable local skills through [[79 Local Skill Loop]] so repeated work becomes reviewable capability instead of hidden self-mutation

## Medium-Term Priorities

- stronger proactive routines
- better meeting support and live context awareness
- richer local vision and screen understanding
- more polished command-center UI and overlays
- clearer user snapshot injection and preference-aware behavior

## Quarterly Milestones

### Q2 2026

- stabilize the packaged macOS app so launch, voice, messaging, and core actions stay reliable
- finish the first durable brain layer: identity, projects, synthesis, story bank, decision log, roadmap, task hub
- tighten short-query tool routing so Jarvis behaves more like an operator in normal spoken use
- keep voice grounded in the better local TTS path without regressing to brittle packaging

### Q3 2026

- add stronger proactive follow-through for active tasks, reminders, and obvious continuity cases
- deepen meeting support, screen understanding, and live local context handling
- push the UI toward a clearer command-center feel without outrunning runtime reliability
- strengthen retrieval so curated notes, current runtime state, and active project context shape answers more consistently

## Things To Avoid

- adding large quantities of raw memory without curation
- broad rewrites that destabilize the packaged app
- cloud-first shortcuts that dilute the core local-first direction
- cosmetic UI work that outruns runtime usefulness

## Success Markers

- Jarvis remembers stable identity and project direction without needing re-explanation
- Jarvis can handle frequent real tasks reliably in the packaged app
- Jarvis feels more human and more operational over time
- the vault becomes a compounding asset rather than a storage bin
- every new layer improves coherence instead of adding noise
