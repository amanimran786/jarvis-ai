---
description: Grade a distilled pattern proposal and stage it for promotion.
argument-hint: <pattern-slug>
---

Apply the Learning Loop's grading step to a pattern proposal produced by `/distill-lessons`. This command never auto-creates files in `vault/patterns/` or auto-edits `vault/_meta/quality.md` — it only assembles the grading record so the human can approve.

## Steps

1. Resolve `$ARGUMENTS` to a pattern slug. If empty, ask the human which proposal to grade and stop.
2. Confirm the proposal text is in the current chat or in `vault/wiki/candidates/`. If it is not, ask the human to paste or stage it and stop.
3. Read the supporting candidate lessons in `vault/sessions/lessons.md` (use `rg` first, then targeted reads — stay under ~8k tokens).
4. Score the proposal against the grade scale in `vault/_meta/quality.md`:
   - `A` requires a passing eval or a verification command + clean rollback.
   - `B` requires repeated qualitative wins backed by source links.
   - `C` requires at least one recorded observation.
   - `D` is for proposals that contradict an existing higher-graded pattern or failed a check.
5. Print a grading block in chat with these fields:
   ```
   pattern: <slug>
   grade: <A|B|C|D>
   evidence: <short note + paths>
   verification: <command or eval id>
   rollback: <how to disable>
   recommendation: <create file | quarantine | reject>
   ```
6. Tell the human exactly which two writes would happen on approval:
   - create `vault/patterns/pattern-<slug>.md`
   - append a row to the ledger in `vault/_meta/quality.md`
7. Wait for the human to say `approve`. If approved, perform only those two writes, update the `updated:` and `version:` fields in `vault/_meta/quality.md`, run `git status`, and stop. Do not commit.

## Rules

- Never raise a grade above what the evidence supports.
- Never write to `vault/patterns/` or `vault/_meta/quality.md` without an explicit `approve`.
- If the proposal lacks a rollback path, downgrade it to `C` or reject it — do not fabricate a rollback.
- Do not delete the originating entries in `vault/sessions/lessons.md`.
- A `D` grade requires also linking the pattern from [[92 Agent Inbox]]; surface that follow-up to the human.
