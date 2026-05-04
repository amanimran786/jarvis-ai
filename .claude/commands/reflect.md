---
description: Analyze diary entries and propose CLAUDE.md improvements.
argument-hint: (no arguments)
---

Reflect on recent diary entries and identify patterns worth documenting in CLAUDE.md.

## Steps

1. Read the last 10 entries from `/Users/truthseeker/jarvis-ai/vault/raw/claude_diary.md` (or all if fewer).
2. Identify patterns:
   - Rules violated multiple times
   - Missing guidance
   - Wrong defaults
   - Ambiguities in existing rules
3. For each pattern, propose a specific 1-line imperative bullet for CLAUDE.md or a domain skill file.
4. Show proposed additions as a diff, with exact file paths and line numbers.
5. Ask: "Should I add these to CLAUDE.md or a domain skill file?"
6. Wait for explicit approval or feedback. Do not auto-apply.

## Rules for Proposals

- Only propose additions that are specific and actionable.
- Never propose removing existing rules without strong evidence (multiple violations, documented as harmful).
- Proposed bullets must start with an imperative verb: Never, Always, Prefer, Avoid, Check, Verify, etc.
- Show the exact section of CLAUDE.md or domain file where each bullet would go.
- If proposing a new bullet in a domain file, show context from that file.

## What Reflection Should NOT Do

- Do not merge multiple diary entries into vague guidance.
- Do not invent rules from speculation (only from observed facts in diary).
- Do not rewrite existing rules.
- Do not propose removing documentation without multiple documented failures.

## Example Proposal

If diary entries show "Nearly missed: forgot to check packaged assets twice this week":

Propose adding to `@.claude/skills/jarvis-packaging.md` under "Common Packaging Failures":
```
+ Always verify packaged resources exist in /Users/truthseeker/Applications/Jarvis.app/Contents/Resources/ after rebuild.
```
