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
  - quality
  - grading
related:
  - "[[96 Learning Loop]]"
  - "[[95 Claude Shared Brain Contract]]"
  - "[[../patterns/README]]"
---

# Quality Ledger

Purpose: record the grade and decay state of every promoted pattern so retrieval can weight what gets injected into prompts.

Linked notes: [[96 Learning Loop]], [[95 Claude Shared Brain Contract]], [[../patterns/README]]

## How grading works

Each row in the table below tracks one pattern. A pattern must be in `vault/patterns/` before it appears here.

| Field | Meaning |
|---|---|
| `pattern` | Slug of the file under `vault/patterns/` (without extension) |
| `grade` | A, B, C, or D — see scale below |
| `evidence` | Short note + path to the eval/verification record |
| `last_validated` | YYYY-MM-DD of the most recent passing check |
| `decay_after` | YYYY-MM-DD when retrieval should drop or re-validate the pattern |
| `status` | `active`, `quarantined`, or `retired` |

## Grade scale

- **A** - measured win in evals or a verification command, plus a clean rollback path.
- **B** - repeated qualitative wins (user corrections that stuck, repeated successful applications) but no automated check yet.
- **C** - single observation; keep it but do not weight it heavily in retrieval.
- **D** - quarantined; pattern caused a regression or contradicted a higher-graded pattern.

## Ledger

| pattern | grade | evidence | last_validated | decay_after | status |
|---|---|---|---|---|---|
| _none yet_ | - | - | - | - | - |

## Rules

- Never raise a grade without a verifiable check. If a check does not exist, write the check before raising the grade.
- A `D`-grade pattern must be linked from [[92 Agent Inbox]] until it is either rehabilitated or retired.
- A retired pattern stays in this file with `status: retired` so we do not re-promote the same idea.
