---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-20
updated: 2026-04-20
version: 1
tags:
  - jarvis
  - local-first
  - capability
  - roadmap
  - agents
related:
  - "[[80 Jarvis Roadmap]]"
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[82 Context Budget Discipline]]"
  - "[[83 External Agent Pattern Intake]]"
---

# Frontier Capability Parity

Purpose: keep Jarvis's "as capable as Claude, GPT, Codex, Gemini, and Grok locally" goal measurable.

Linked notes: [[80 Jarvis Roadmap]], [[78 AI Runtime Agent Engineering Principles]], [[82 Context Budget Discipline]], [[83 External Agent Pattern Intake]]

Use this note when deciding what to build next for the local-first product.

## Principle

The goal is not brand imitation. The goal is local capability parity across product classes:

- frontier chat and reasoning
- coding-agent implementation loop
- local vision and screen understanding
- persistent memory and brain retrieval
- voice input and output
- managed agents and task lifecycle
- portable reusable skills
- browser and tool execution
- defensive cybersecurity engineering support

## Runtime Surface

Jarvis exposes the current scorecard through `/capability-parity` and the console command `/parity`.

The scorecard should answer:

- what local equivalent exists
- whether the capability is ready, partial, or a gap
- what evidence supports that state
- what the next engineering seam is

## Guardrails

- do not claim parity just because a model name exists
- do not treat cloud fallback as local parity
- do not hide partial voice, vision, browser, or security gaps behind generic "AI assistant" language
- do not add offensive automation to satisfy a "Grok/Codex-like" ambition

## Current Direction

The next useful moves should close real product gaps:

- repo-map and verification helpers for coding-agent work
- packaged voice end-to-end reliability
- richer screenshot and UI-understanding smoke tests
- portable skill export/import compatibility
- gated browser/source ingestion that respects privacy and ToS

This note should make the ambition sharper, not grander.
