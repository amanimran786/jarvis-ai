---
type: task_hub
area: vault
scope: system
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-15
updated: 2026-04-15
version: 1
tags:
  - brain
  - obsidian
  - agents
  - inbox
related:
  - "[[03 Brain Schema]]"
  - "[[04 Capture Workflow]]"
  - "[[70 Jarvis Decision Log]]"
  - "[[90 Task Hub]]"
  - "[[91 Vault Changelog]]"
---

# Agent Inbox

Purpose: give Jarvis a bounded place to queue ongoing brain-maintenance work without silently mutating canonical notes.

Linked notes: [[03 Brain Schema]], [[04 Capture Workflow]], [[70 Jarvis Decision Log]], [[90 Task Hub]], [[91 Vault Changelog]]

## Contract

- agents may continuously queue work here
- agents may promote work from here only when the target note and heading are explicit
- canonical note rewrites should not happen implicitly from background loops
- unresolved ambiguity should stay visible here or in a disambiguation note
- stale inbox work should be reviewed explicitly instead of silently piling up
- resolved inbox items should move into `Done` so the queued sections stay operational instead of becoming a mixed archive

## Queued

- [ ] Distill recurring vault-maintenance patterns into tighter curator actions #brain #agents #agent-inbox

## In Review

- [ ] Decide which inbox items should graduate into [[90 Task Hub]] versus direct canonical note updates #brain #agent-inbox

## Done

- [x] Established a dedicated inbox lane so smart agents can keep the vault moving without freeform self-editing #brain #agents
