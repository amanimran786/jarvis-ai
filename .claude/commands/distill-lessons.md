---
description: Distill candidate session lessons into pattern proposals.
argument-hint: [optional theme or trigger filter]
---

Read the running candidate lessons in `vault/sessions/lessons.md` and propose which ones are ripe for promotion into a pattern under `vault/patterns/`. This command never writes to `vault/patterns/` directly — it only stages a proposal for review.

## Steps

1. Read `vault/sessions/lessons.md` and group the candidate entries by their `Retrieval trigger` field. If `$ARGUMENTS` was provided, restrict to lessons whose trigger or title contains that string.
2. For each group with two or more entries, draft a single pattern proposal containing:
   - `Trigger` - the canonical phrase that should retrieve the pattern.
   - `Action` - what Jarvis or Claude should do when the trigger fires.
   - `Evidence` - bullet list of the underlying lessons (link by date + title from `vault/sessions/lessons.md`).
   - `Verification` - the concrete check (eval, test, command) that would confirm the pattern is helping.
   - `Rollback` - how to disable the pattern if it regresses.
3. Print the proposal block in chat. Do not edit `vault/patterns/`. Do not edit `vault/_meta/quality.md`.
4. Tell the human to grade the proposal with `/grade-pattern` before any pattern file is created.
5. Run `git status` to confirm no files were written, then stop.

## Rules

- Promote only patterns backed by repeated evidence (count of related candidate lessons >= 2 unless the user explicitly approves a single-source pattern).
- Never invent evidence — if the lesson does not record a source, surface that as a blocker instead of guessing.
- Do not consume more than ~8k tokens reading `vault/sessions/lessons.md` and related notes. Search before bulk-reading.
- Never delete lessons from `vault/sessions/lessons.md`; promotion does not remove the trail.
