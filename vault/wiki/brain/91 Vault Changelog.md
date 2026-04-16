---
type: task_hub
area: vault
status: active
source: repo
confidence: high
created: 2026-04-15
updated: 2026-04-15
version: 3
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
