---
type: architecture_map
area: engineering
owner: jarvis
write_policy: curated
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - architecture
  - runtime
  - seams
related:
  - "[[70 Jarvis Decision Log]]"
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Coding Implementation Playbook]]"
  - "[[79C Verification Matrix]]"
  - "[[80 Jarvis Roadmap]]"
---

# Jarvis Architecture Runtime Seams

Purpose: preserve the real coding map of Jarvis so implementation work starts from the right seam.

Linked notes: [[70 Jarvis Decision Log]], [[78 AI Runtime Agent Engineering Principles]], [[79 Coding Implementation Playbook]], [[79C Verification Matrix]], [[80 Jarvis Roadmap]]

## Core Seams

- voice seam: `voice.py`, local STT/TTS runtime, packaged-app audio behavior
- routing seam: `router.py`, `orchestrator.py`, `model_router.py`
- specialist seam: `specialized_agents.py`, `specialized_agent_native.py`, `agents/*.md`
- vault seam: `vault.py`, `vault_edit.py`, `vault_capture.py`, `specialized_agent_native.py`
- packaged-app seam: `Jarvis.spec`, install script, `/Users/truthseeker/Applications/Jarvis.app`

## Default Rule

Change the seam that owns the behavior. Do not patch around it from a neighbor layer unless the owner layer is proven wrong for the job.

## Examples

- wrong role selection is a routing seam problem, not a voice problem
- protected vault mutation is a vault-edit policy problem, not a dashboard note problem
- packaged launch regressions are a packaging seam problem even if source tests pass
- a technical answer that sounds generic may be a model-grounding seam problem, not a skills problem
