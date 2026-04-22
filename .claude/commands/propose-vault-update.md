---
description: Propose a shared-brain update without applying it.
argument-hint: <target path or "new note">
---

Use this when the vault should probably change but the human has not explicitly approved the write.

## Steps

1. Locate the target under `/Users/truthseeker/jarvis-ai/vault`.
2. Check `vault/wiki/brain/95 Claude Shared Brain Contract.md` and `vault/wiki/brain/03 Brain Schema.md`.
3. Render exactly:

   ```text
   PROPOSED VAULT UPDATE
   Path:   vault/<path>
   Action: create | append | edit
   Reason: <25 words max>
   Risk:   low | medium | high
   ```

4. Show a small unified diff.
5. End with `Approve? (y/n)` and wait.
6. On explicit `y`, apply the shown change only, update frontmatter, run `git status`, and stop.
7. On anything else, discard.

## Rules

- Never apply without explicit `y`.
- Never rename, delete, or move notes.
- Never modify `vault/raw/imports/**`, `vault/wiki/candidates/**`, archived notes, or generated indexes without explicit instruction.
- Keep diffs under 50 lines. Split larger changes.
