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
  - code-review
  - regression
  - engineering
related:
  - "[[08 Coding Systems Hub]]"
  - "[[75 Debugging Root Cause Playbook]]"
  - "[[79 Coding Implementation Playbook]]"
  - "[[79C Verification Matrix]]"
---

# Code Review Regression Heuristics

Purpose: make Jarvis good at review, not just generation.

Linked notes: [[08 Coding Systems Hub]], [[75 Debugging Root Cause Playbook]], [[79 Coding Implementation Playbook]], [[79C Verification Matrix]]

## Review Order

1. behavioral regressions
2. unsafe assumptions
3. missing verification
4. maintainability only after the first three are clear

## Findings Rules

- findings first, summary second
- severity over style
- name the exact failure mode, not a vague concern
- mention the shortest proof step when the risk is inferential

## Jarvis-Specific Regression Classes

- packaged app path diverges from source-tree behavior
- local-first path silently falls back to cloud or generic runtime behavior
- voice, launch, or routing behavior regresses while tests stay green
- a native success message is returned even though the underlying action failed
- a generated or protected vault note is rewritten when it should have staged
- agent routing becomes broader but less deterministic

## Missing-Test Heuristic

- if the change alters routing, add routing coverage
- if the change alters native hooks, add focused native-path coverage
- if the change alters packaged behavior, require installed-app smoke or equivalent targeted proof
- if the change only affects vault/docs structure, prefer index refresh and structural checks over app rebuild theater
