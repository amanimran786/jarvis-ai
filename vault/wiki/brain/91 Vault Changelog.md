---
type: task_hub
area: vault
status: active
source: repo
confidence: high
created: 2026-04-15
updated: 2026-04-19
version: 5
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
---

# Vault Changelog

Purpose: preserve note-level provenance for major brain upgrades so Jarvis can compound knowledge without losing change history.

Linked notes: [[03 Brain Schema]], [[04 Capture Workflow]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]]

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
