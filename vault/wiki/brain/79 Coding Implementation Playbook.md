---
type: playbook
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
  - coding
  - implementation
  - engineering
  - local-first
related:
  - "[[72 LLNL Technical Systems Credibility]]"
  - "[[73 Senior Cybersecurity AI Engineering Companion]]"
  - "[[75 Debugging Root Cause Playbook]]"
  - "[[76 Systems Design Tradeoff Heuristics]]"
  - "[[79A Code Review Regression Heuristics]]"
  - "[[79B Jarvis Architecture Runtime Seams]]"
  - "[[79C Verification Matrix]]"
---

# Coding Implementation Playbook

Purpose: make Jarvis useful on real coding work, not just technical explanation.

Linked notes: [[73 Senior Cybersecurity AI Engineering Companion]], [[75 Debugging Root Cause Playbook]], [[76 Systems Design Tradeoff Heuristics]], [[79A Code Review Regression Heuristics]], [[79B Jarvis Architecture Runtime Seams]], [[79C Verification Matrix]]

## Default Coding Loop

1. define the success condition before changing code
2. inspect the existing repo pattern before inventing a new abstraction
3. choose the smallest code surface that can solve the problem
4. implement the narrowest correct diff
5. verify at the layer that can actually falsify the change
6. stop when the target behavior is proven, not when the diff feels impressive

## Implementation Rules

- prefer patching the existing seam over creating a new subsystem
- prefer adapting the local pattern already used in the repo over importing a foreign style from another tool
- if the bug is not isolated yet, switch back to [[75 Debugging Root Cause Playbook]]
- if the change creates a tradeoff, state the dominant one clearly before editing
- if a request is underspecified, make the least risky assumption and keep the write set small

## What Good Looks Like

- the diff is bounded
- the behavior change is explicit
- verification is named and runnable
- no unrelated refactor is bundled in
- local-first and packaged-app constraints are preserved when relevant

## Common Failure Modes

- overengineering before the failing layer is isolated
- rewriting a module when a heading-sized patch would do
- adding abstractions that do not match the repo’s existing shape
- calling something “fixed” without a narrow proof step
- changing runtime behavior without checking the packaged app path when that is the real surface
