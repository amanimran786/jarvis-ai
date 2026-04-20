---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-19
updated: 2026-04-19
version: 1
tags:
  - jarvis
  - skills
  - agents
  - local-first
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[82 Context Budget Discipline]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[70 Jarvis Decision Log]]"
  - "[[92 Agent Inbox]]"
  - "[[93 Vault Maintenance]]"
---

# Local Skill Loop

Purpose: define how Jarvis learns reusable local skills without unsafe self-modification or graph noise.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[82 Context Budget Discipline]], [[80 Jarvis Roadmap]], [[70 Jarvis Decision Log]], [[92 Agent Inbox]], [[93 Vault Maintenance]]

Use this note when Jarvis needs to decide whether a repeated task, failure pattern, or workflow should become a reusable local skill.

## Principle

Self-improvement must be evidence-backed, local-first, reviewable, and reversible.

The goal is not automatic mutation. The goal is compounding capability with a clear audit trail.

## Skill Lifecycle

1. Observe a repeated task, eval failure, or workflow gap.
2. Propose a skill from local evidence.
3. Stage the skill as a dry-run payload or candidate.
4. Verify the skill with a narrow local test.
5. Promote deliberately through an explicit create/promote command.
6. Log provenance in [[91 Vault Changelog]] or [[70 Jarvis Decision Log]] when the behavior changes.

## What Counts As A Skill

A skill is reusable procedural knowledge. It should explain how to do something, not merely remember that something exists.

Good skill candidates include:

- recurring terminal or repo workflows
- coding verification helpers
- context-budget and log-compression workflows
- vault maintenance procedures
- routing and debugging playbooks
- agent profile instructions
- safe escalation procedures for repeated failure modes

Facts, preferences, and stable identity belong in memory or durable brain notes. Procedures belong in skills.

## Guardrails

- no silent canonical rewrites
- no autonomous code mutation without approval
- no registry writes from background agents by default
- no cloud dependency for the skill loop
- no skill promotion without a repeated need, eval signal, or local vault source
- no large skill that mixes unrelated workflows just because the theme sounds useful

## Runtime Contract

The `skill_builder` specialist should draft and validate skill proposals first.

Direct skill creation remains explicit. Background/self-improving work should default to proposal mode and only promote after review.

This mirrors the useful part of Hermes-style local agents while preserving Jarvis's safety model: local memory, local skills, approval-gated writes, and provenance in the vault.

## Vault Placement Rules

Skill ideas go to [[92 Agent Inbox]] or candidate notes first.

Stable skill policy lives here.

Product priority lives in [[80 Jarvis Roadmap]].

Architecture decisions live in [[70 Jarvis Decision Log]].

Maintenance state lives in [[93 Vault Maintenance]].

## Implementation Backlog

- [x] Add a proposal-first `skill_builder` specialist #jarvis #skills
- [x] Add a non-mutating skill proposal endpoint #jarvis #skills
- [x] Add a managed skill-builder task lane #jarvis #agents
- [ ] Add a local skill proposal template #brain #skills
- [ ] Add a promotion review checklist before skill registry writes #jarvis #safety
