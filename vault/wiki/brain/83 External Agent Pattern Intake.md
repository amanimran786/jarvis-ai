---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo_research
confidence: high
created: 2026-04-20
updated: 2026-04-21
version: 2
tags:
  - jarvis
  - agents
  - local-first
  - security
  - skills
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Local Skill Loop]]"
  - "[[82 Context Budget Discipline]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[77 Threat Modeling Security Thinking]]"
---

# External Agent Pattern Intake

Purpose: turn external agent repos into Jarvis-native decisions without blindly installing unsafe dependencies.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[79 Local Skill Loop]], [[82 Context Budget Discipline]], [[80 Jarvis Roadmap]], [[77 Threat Modeling Security Thinking]]

Use this note when a new repo, hype thread, or agent architecture claim appears and Jarvis needs to decide what to adopt, adapt, gate, watch, or reject.

## Intake Rule

Borrow operating patterns first. Install dependencies only after a local safety and product-fit review.

Jarvis should classify external repos into:

- adopt: safe, small, and directly compatible
- adapt: useful pattern, but implement Jarvis-native
- watch: interesting research signal, not production-ready
- gate: useful only behind explicit permissions and policy checks
- defensive-only: dual-use or offensive pattern that must not become autonomous action

## Current Repo Intake

### agentic-stack

Verdict: adapt.

Useful for: portable `.agent/` bridge folders, shared memory across Claude Code, OpenClaw, Hermes, Cursor, OpenCode, and other coding harnesses, four memory layers with retention policies, host-agent review tools, and recall-before-action hooks.

Jarvis path: treat `.agent/` as an export and compatibility layer before making it canonical. Jarvis already has the core primitives: `AGENTS.md`, semantic and episodic memory, progressive-disclosure skills, candidate staging, and review-gated promotion. The missing seam is a Jarvis-native `.agent/` exporter/importer that maps Jarvis memory and skills into a portable folder without letting a foreign installer overwrite `AGENTS.md`, `CLAUDE.md`, or `skills/index.json`.

Do not integrate: unattended semantic-memory mutation, installer-driven overwrite of project instructions, or self-rewrite hooks that bypass Jarvis review gates.

### GBrain

Verdict: adapt.

Useful for: dream-cycle consolidation, entity enrichment, citation repair, and separating stable memory from procedural skills.

Jarvis path: strengthen vault maintenance, semantic memory, and agent inbox loops without replacing the plain-markdown vault contract.

### Multica

Verdict: adapt.

Useful for: agents as assignees, runtime inventory, progress streaming, blocker reporting, and skill compounding from completed work.

Jarvis path: improve `task_runtime.py`, `jarvis_cli.py`, and specialist profiles so agents feel like teammates while staying local-first.

### Claude Code Best Practice

Verdict: adapt.

Useful for: session memory, command packs, workflow hooks, and explicit coding-agent loops.

Jarvis path: reinforce [[82 Context Budget Discipline]] and [[79 Local Skill Loop]] instead of copying prompt packs wholesale.

### OpenMythos

Verdict: watch.

Useful for: recurrent-depth reasoning as a local model evaluation idea.

Jarvis path: evaluate as research through `local_runtime/local_model_eval.py` and benchmark notes, not as a desktop runtime dependency.

### Scrapling

Verdict: gate.

Useful for: adaptive selectors, static-first extraction, and structured scrape output.

Jarvis path: possible browser/source-ingest helper only with ToS, privacy, robots.txt, and user-consent gates. Stealth/bot-evasion modes are not default Jarvis behavior.

### Browser Harness

Verdict: gate.

Useful for: thin CDP harnesses, generated helpers, and domain skills.

Jarvis path: borrow deterministic browser-action primitives, but do not let agents self-edit helpers while controlling logged-in browser sessions without review.

### Decepticon

Verdict: defensive-only.

Useful for: rules of engagement, OPPLAN artifacts, sandbox isolation, persistent terminal sessions, phase-specific agents, and findings-to-defense loops.

Jarvis path: strengthen security-review and defensive validation. Do not integrate autonomous exploitation, credential harvesting, C2, or kill-chain execution.

## Skill Builder Implication

Every proposed skill should include "Do NOT use for" boundaries. Negative triggers matter because a useful skill should activate narrowly and stay quiet on unrelated requests.

Jarvis now supports this as metadata through `negative_triggers`, so the matcher can suppress a skill before loading its full instructions.

## Default Safety Posture

- memory and skill patterns: adapt
- portable agent brain bridge patterns: adapt
- teammate lifecycle patterns: adapt
- recurrent model architecture: watch
- browser/scraping: gate
- autonomous offensive security: defensive-only
