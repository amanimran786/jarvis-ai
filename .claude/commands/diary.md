---
description: Log a session observation to the shared diary for future reference.
argument-hint: <observation>
---

A diary entry captures something you learned or observed this session that future Claude sessions should remember.

## Steps

1. Confirm with the human: `Log this observation to vault/raw/claude_diary.md? (y/n): <observation>`
2. Wait for explicit `y`. Anything else cancels.
3. Append a dated entry to `/Users/truthseeker/jarvis-ai/vault/raw/claude_diary.md`.
4. If the file does not exist, create it with header:
   ```markdown
   # Claude Diary
   
   Session observations and debugging evidence for future reference.
   ```
5. Entry format: `## YYYY-MM-DD HH:MM\n<observation>` (100 words max)
6. Run `git status` and stop. Do not commit.

## What to Log

- Rules in CLAUDE.md you nearly violated or found ambiguous
- Patterns that worked unexpectedly well or poorly
- Recurring errors or gotchas that aren't documented
- New Jarvis behaviors discovered during testing
- Seams between components that caused confusion

## What NOT to Log

- PII, secrets, or credentials
- Full conversation content (summarize instead)
- Speculative "this might be broken" (only observed facts)
- Personal frustration or venting

## Example

```
## 2026-05-04 14:23
Found that voice.py status updates can be clobbered by unrelated UI task spinners. 
The UI has a generic "loading" state that overwrites the real mic status. 
Fixed by checking voice capability state before generic task status. 
See test_voice_status_ui_regression.py for verification.
```
