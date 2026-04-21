---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-21
updated: 2026-04-21
version: 1
tags:
  - jarvis
  - coding
  - terminal
  - local-first
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[82 Context Budget Discipline]]"
  - "[[84 Frontier Capability Parity]]"
  - "[[86 Capability Eval Harness]]"
  - "[[87 Production Readiness Contract]]"
---

# Coder Workbench

Purpose: make Jarvis's terminal coding loop more like a local Claude/Codex console without losing repo grounding.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[82 Context Budget Discipline]], [[84 Frontier Capability Parity]], [[86 Capability Eval Harness]], [[87 Production Readiness Contract]]

Jarvis now exposes a repo-aware coding workbench through:

- `/coder/status`
- `/coder/verify-plan`
- `jarvis --code-status`
- `jarvis --verify-plan`
- console `/code-status`
- console `/verify-plan`

## Contract

The coder workbench should answer:

- what branch and commit Jarvis is working on
- whether the worktree is clean
- which files changed
- which verification commands match the current diff
- whether the packaged app must be rebuilt

## Rule

Jarvis should not ask the model to invent a verification strategy when git state can generate one deterministically.

The local coding loop is:

1. inspect repo state
2. make the smallest correct diff
3. run the generated verify plan
4. rebuild and smoke the packaged app when runtime surfaces changed
5. commit and push only after verification passes
