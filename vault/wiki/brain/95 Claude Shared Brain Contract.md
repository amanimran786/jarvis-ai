---
type: brain_meta
area: vault
owner: user
write_policy: curated
review_required: true
status: active
source: claude_bridge
confidence: high
created: 2026-04-21
updated: 2026-04-21
version: 1
tags:
  - claude
  - jarvis
  - shared-brain
  - obsidian
related:
  - "[[03 Brain Schema]]"
  - "[[04 Capture Workflow]]"
  - "[[82 Context Budget Discipline]]"
  - "[[83 External Agent Pattern Intake]]"
---

# Claude Shared Brain Contract

Purpose: let Claude Code and Jarvis share one local Obsidian brain without turning the vault into an unreviewed scratchpad.

Linked notes: [[03 Brain Schema]], [[04 Capture Workflow]], [[82 Context Budget Discipline]], [[83 External Agent Pattern Intake]]

## Scope

- Vault root: `/Users/truthseeker/jarvis-ai/vault`
- Curated brain: `vault/wiki/brain/`
- Raw evidence: `vault/raw/` and `vault/raw/imports/`
- Indexes: `vault/indexes/`
- Staging lane: `vault/wiki/candidates/`

Claude Code may read the vault for Jarvis context. Claude Code may write only through explicit human-approved workflows.

## Read Protocol

1. Start with `vault/indexes/Repo Map.md` for repo orientation.
2. Use `rg` before opening files.
3. Prefer curated notes under `vault/wiki/brain/` before raw imports.
4. Read the smallest file ranges that answer the question.
5. Cite `vault/...` paths when using vault evidence.

## Write Protocol

Claude may write only when Aman explicitly says one of:

- `remember this`
- `log this`
- `save to brain`
- `append session lesson`
- `approve`

If approval is missing, use `/propose-vault-update` and stop.

## Do Not Mutate

- `vault/raw/imports/**`
- `vault/wiki/candidates/**` unless asked to stage a candidate
- `vault/.obsidian/**`
- generated indexes except through the existing generator
- note filenames, moves, or deletes
- frontmatter provenance except append-only updates

## Token Discipline

- Search first, read second.
- Keep vault reads under roughly 8k tokens per turn unless Aman asks for a deep dive.
- Never load the whole vault.
- Never paste giant logs or generated files into chat.
- Use [[82 Context Budget Discipline]] before large coding tasks.

## Commit Rule

Claude may commit code/config changes only when asked. Claude should not auto-commit vault changes; stage or show diffs for review.
