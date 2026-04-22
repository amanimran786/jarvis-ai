---
description: Append an approved lesson to the shared Jarvis brain.
argument-hint: <lesson text>
---

A session lesson is a small durable observation worth carrying forward.

## Steps

1. Confirm the exact lesson with the human:
   `Append this lesson to vault/wiki/brain/94 Claude Session Lessons.md? (y/n): <lesson text>`
2. Wait for explicit `y`. Anything else cancels.
3. Append a dated entry to `/Users/truthseeker/jarvis-ai/vault/wiki/brain/94 Claude Session Lessons.md`.
4. If the note does not exist, create it with Jarvis brain-schema frontmatter:
   ```yaml
   ---
   type: task_hub
   area: vault
   owner: claude
   write_policy: append_only
   review_required: false
   status: active
   source: claude
   confidence: high
   created: YYYY-MM-DD
   updated: YYYY-MM-DD
   version: 1
   tags:
     - claude
     - lessons
     - shared-brain
   related:
     - "[[03 Brain Schema]]"
     - "[[95 Claude Shared Brain Contract]]"
   ---
   ```
5. Update `updated:` and increment `version`.
6. Run `git status` and stop. Do not commit.

## Rules

- Never invent a lesson.
- One lesson per invocation.
- Keep the lesson to 1-2 sentences.
- Do not write to `vault/raw/`, `vault/wiki/candidates/`, or archived notes from this command.
