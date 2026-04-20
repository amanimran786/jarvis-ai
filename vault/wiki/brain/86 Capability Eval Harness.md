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
  - evals
  - local-first
  - quality
related:
  - "[[84 Frontier Capability Parity]]"
  - "[[85 Defensive Security ROE]]"
  - "[[82 Context Budget Discipline]]"
---

# Capability Eval Harness

Purpose: make Jarvis prove capability claims through explicit local regression cases.

Linked notes: [[84 Frontier Capability Parity]], [[85 Defensive Security ROE]], [[82 Context Budget Discipline]]

Jarvis now exposes eval coverage through `/capability-evals` and the console command `/capability-evals`.

The point is simple: a capability group is not mature just because the surface exists. It needs regression cases.

## Covered Groups

The eval catalog tracks these local capability groups:

- chat reasoning
- coding-agent implementation loop
- vision
- memory brain
- voice
- agents
- skills
- browser tools
- security

## Live Golden Command

Use this command for expensive live golden cases:

```bash
JARVIS_RUN_GOLDEN_CASES=1 python3 -m pytest tests/test_jarvis_golden_cases.py -q
```

Use the faster catalog command during normal iteration:

```bash
jarvis --capability-evals
```

## Rule

When Jarvis gains a new major ability, add one of:

- a live golden case
- a unit-level capability case
- a packaged-app smoke case
- a console smoke case

This keeps the local-first goal honest. Surface parity is not intelligence parity until it survives eval pressure.
