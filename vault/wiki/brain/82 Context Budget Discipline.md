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
  - coding
  - agents
  - local-first
  - context
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Local Skill Loop]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[73 Senior Cybersecurity AI Engineering Companion]]"
  - "[[74 Universal Engineer Thinker Problem Solver]]"
---

# Context Budget Discipline

Purpose: define how Jarvis should preserve context quality while acting like a local coding agent.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[79 Local Skill Loop]], [[80 Jarvis Roadmap]], [[73 Senior Cybersecurity AI Engineering Companion]], [[74 Universal Engineer Thinker Problem Solver]]

Use this note when Jarvis is doing implementation, code review, long terminal work, repo analysis, or multi-agent coordination.

## Principle

The constraint is not just token count. The constraint is context quality.

Jarvis should spend context on facts that change the decision, not on raw logs, repeated boilerplate, or entire files read "just in case."

## Runtime Contract

Jarvis should prefer these lanes:

- `/code <prompt>` for normal isolated implementation
- `/code-lite <prompt>` for small focused code changes
- `/code-ultra <prompt>` for large repos, long logs, or repeated agent work
- `/task <prompt>` for default managed non-code tasks
- `/task-ultra <prompt>` for non-code tasks that need hard compression
- `/context-budget` or `/tokens` to inspect the live policy

The default coding path should use the local coder model when available and run through an isolated workspace where the managed task runtime supports it.

## What To Compress

- terminal logs
- dependency install output
- repeated stack traces
- full-file dumps when a symbol or function is enough
- copied social posts or hype threads
- broad repo inventories after the relevant seam is known

## What Not To Compress Away

- exact error messages
- file paths and line numbers
- command names and exit statuses
- security assumptions
- package/app verification evidence
- user intent and non-negotiables

## Skill Loop Interaction

If the same context-saving pattern recurs, Jarvis should route it through [[79 Local Skill Loop]] as a proposal-first skill candidate.

Examples:

- a repo map/index workflow
- a log summarizer workflow
- a code review graph workflow
- a safe terminal-output filter
- a local model readiness check

## Guardrails

- no random token-saver repo installation without review
- no proxying sensitive local code through unknown external tools
- no replacing real verification with shorter answers
- no cloud-first fallback to save local runtime effort
- no losing traceability when summarizing logs

## Current Implementation

Jarvis exposes this contract through `context_budget.py`, `/context-budget`, `/tokens`, and terse console aliases for task and code lanes.

The useful lesson from external tools like Claude Code through Ollama, `gh skill`, APM, and token-efficient prompt files is not that Jarvis should copy every tool. The lesson is that local agents need explicit packageable skills, context gates, and repo-grounded implementation loops.
