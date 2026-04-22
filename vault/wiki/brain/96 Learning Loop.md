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
  - memory
  - self-improvement
related:
  - "[[03 Brain Schema]]"
  - "[[79 Local Skill Loop]]"
  - "[[82 Context Budget Discipline]]"
  - "[[92 Agent Inbox]]"
  - "[[95 Claude Shared Brain Contract]]"
---

# Learning Loop

Purpose: define the safe path for Jarvis to learn from sessions without silently mutating production behavior.

Linked notes: [[03 Brain Schema]], [[79 Local Skill Loop]], [[82 Context Budget Discipline]], [[92 Agent Inbox]], [[95 Claude Shared Brain Contract]]

## Loop

1. Capture a short candidate lesson in `vault/sessions/lessons.md`.
2. Distill repeated lessons into a candidate note, skill proposal, or local training example.
3. Retrieve only the relevant top-k lesson context when a future task matches its trigger.
4. Grade the result with evals, user correction, or a concrete verification command.
5. Promote only after review, passing evidence, and a rollback path.

## Timing Policy

- Run a weekly synthesis pass so the vault does not drift.
- Also synthesize when repeated candidate lessons cross a threshold before the weekly pass.
- Keep both paths review-gated. Weekly and threshold synthesis should stage proposals; neither should rewrite canonical notes unattended.

## Promotion Targets

- Stable preference or operating rule: curated brain note.
- Repeated task pattern: [[79 Local Skill Loop]] proposal.
- Model behavior correction: local training teacher example or preference pair.
- Unclear or high-impact change: [[92 Agent Inbox]] first.

## Non-Goals

- Do not treat one-off conversation noise as durable memory.
- Do not train on secrets, credentials, private raw logs, jailbreak payloads, or unreviewed hostile text.
- Do not claim self-learning has improved production behavior until evals or verification show it.
