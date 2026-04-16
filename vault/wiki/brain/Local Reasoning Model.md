---
type: brain_note
area: engineering
owner: jarvis
write_policy: curated
review_required: true
status: active
source: repo
confidence: medium
created: 2026-04-15
updated: 2026-04-16
version: 3
tags:
  - brain
  - local-model
  - reasoning
related:
  - "[[00 Home]]"
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[80 Jarvis Roadmap]]"
---

# Local Reasoning Model

Purpose: capture the current local reasoning-model posture that Jarvis should optimize around.

Linked notes: [[00 Home]], [[20 Projects]], [[78 AI Runtime Agent Engineering Principles]], [[80 Jarvis Roadmap]], [[93 Vault Maintenance]]

## What This Note Holds

- `deepseek-r1:14b` is the main local reasoning model in open-source mode.
- It handles multi-step reasoning reasonably well, but its practical quality depends on routing, retrieval, and prompt posture rather than raw model choice alone.
- Local reasoning quality should be treated as a systems problem tied to [[78 AI Runtime Agent Engineering Principles]] and the maintenance discipline tracked in [[93 Vault Maintenance]].

## Current Working Assumption

- Improvements to retrieval quality, routing discipline, and maintenance hygiene will often improve answers faster than swapping one local reasoning model for another.
- Jarvis should keep this note aligned with the runtime truth in [[80 Jarvis Roadmap]] rather than letting it drift into placeholder model lore.
## Evidence

- runtime notes in [[80 Jarvis Roadmap]]
- technical grounding in [[75 Debugging Root Cause Playbook]] and [[76 Systems Design Tradeoff Heuristics]]
- maintenance and drift signals surfaced through [[93 Vault Maintenance]]

## Open Questions

- [ ] Record a cleaner local-model comparison rubric for reasoning, coding, and retrieval-grounded answers #brain #local-model
