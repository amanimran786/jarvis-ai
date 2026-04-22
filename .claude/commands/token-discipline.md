---
description: Apply Jarvis token/context discipline before a Claude Code task.
argument-hint: <task>
---

Use this before large Jarvis tasks to reduce context waste.

## Steps

1. Read only:
   - `AGENTS.md`
   - `CLAUDE.md`
   - `vault/wiki/brain/82 Context Budget Discipline.md`
   - targeted files found with `rg`
2. State the smallest context set needed for `<task>`.
3. Avoid dumping logs. Use `tail`, `rg`, focused test output, and file ranges.
4. Prefer symbol/path search before opening full files.
5. If output is long, summarize and store exact command/file references instead of pasting everything.

## Rules

- No broad `cat`.
- No full-vault reads.
- No generated-cache commits.
- Default to terse implementation notes and evidence.
