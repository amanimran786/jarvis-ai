---
type: brain_meta
area: vault
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - vault
  - candidates
related:
  - "[[03 Brain Schema]]"
  - "[[04 Capture Workflow]]"
  - "[[92 Agent Inbox]]"
---

# Candidate Layer

Purpose: hold staged note updates when Jarvis should propose changes without writing directly into a canonical note.

Use [[02 Brain Dashboard]] for the curated brain surface, [[03 Brain Schema]] and [[04 Capture Workflow]] for the note contract, [[91 Vault Changelog]] for provenance, and [[93 Vault Maintenance]] for cleanup and promotion debt.

Linked notes: [[03 Brain Schema]], [[04 Capture Workflow]], [[92 Agent Inbox]]

## Contract

- Candidate notes sit between inbox work and canon.
- Jarvis may create and append these notes automatically for `propose_only` targets.
- Each candidate note should point back to the canonical target it is proposing to change.
- Promotion into the canonical note should stay explicit and reviewable.
- Stale candidate notes should be surfaced for review instead of lingering indefinitely.
- Archived candidate notes should stop counting as active review debt.
