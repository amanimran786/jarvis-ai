---
type: brain_meta
area: vault
owner: jarvis
write_policy: curated
review_required: true
scope: system
status: active
source: repo
confidence: high
created: 2026-04-22
updated: 2026-04-22
version: 1
tags:
  - learning-loop
  - patterns
  - retrieval
related:
  - "[[96 Learning Loop]]"
  - "[[95 Claude Shared Brain Contract]]"
  - "[[03 Brain Schema]]"
---

# Patterns

Purpose: store distilled, reusable patterns that have graduated from `vault/sessions/lessons.md` after passing the Learning Loop's grading step.

Linked notes: [[96 Learning Loop]], [[95 Claude Shared Brain Contract]], [[03 Brain Schema]]

## What lives here

A pattern is a small, named, retrievable behaviour rule that Jarvis (or Claude operating in this repo) should apply when its trigger fires. Each pattern file should be a single markdown note with:

- a precise trigger string (used by retrieval to decide when to inject the pattern into a prompt)
- the action the agent should take
- the evidence that justifies promoting the pattern (eval, verification command, repeated user correction)
- a rollback note describing how to disable the pattern if it regresses

## What does not live here

- Raw evidence — keep that in `vault/raw/` or the originating lesson under `vault/sessions/lessons.md`.
- One-off candidate lessons — those stay in `vault/sessions/lessons.md` until they meet the promotion bar.
- Skill code — proposals live under [[79 Local Skill Loop]] and the actual implementation in `agents/` or wherever Jarvis runtime lives.

## File naming

`pattern-<short-slug>.md`. Keep slugs descriptive and stable so retrieval indices do not need to be rewritten.

## Promotion path

1. Capture in `vault/sessions/lessons.md` via `vault/templates/session-lesson-template.md`.
2. Distill repeated lessons via `/distill-lessons`.
3. Grade via `/grade-pattern` (review evidence + rollback path) before a pattern note is created here.
4. Update `vault/_meta/quality.md` with the pattern's grade so retrieval can weight it.

## Related

- [[96 Learning Loop]] - the loop these patterns are part of.
- [[92 Agent Inbox]] - any pattern that touches production behaviour should be staged for human review here first.
- [[82 Context Budget Discipline]] - retrieval into prompts must stay within budget.
