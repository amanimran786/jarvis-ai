---
type: brain_meta
area: vault
status: active
source: repo
confidence: high
created: 2026-04-15
updated: 2026-04-15
version: 1
tags:
  - obsidian
  - capture
  - workflow
  - quickadd
  - templater
related:
  - "[[03 Brain Schema]]"
  - "[[05 Source Inventory]]"
  - "[[70 Jarvis Decision Log]]"
  - "[[90 Task Hub]]"
  - "[[91 Vault Changelog]]"
---

# Capture Workflow

Purpose: imitate the best QuickAdd and Templater workflows without relying on plugin scripting for core Jarvis behavior.

Linked notes: [[03 Brain Schema]], [[05 Source Inventory]], [[07 Import Source Hub]], [[70 Jarvis Decision Log]], [[90 Task Hub]], [[91 Vault Changelog]]

## Capture Contract

Every durable capture should follow the same flow:

1. choose the smallest correct template
2. create or update the target note
3. add explicit wikilinks to the right hub notes
4. add or update tasks if follow-up work remains
5. add a changelog entry if the brain structure or memory policy changed

## Template Map

- [brain-note-template](../../templates/brain-note-template.md) for a new durable note
- [decision-template](../../templates/decision-template.md) for product or architecture decisions
- [story-template](../../templates/story-template.md) for interview and experience stories
- [project-capture-template](../../templates/project-capture-template.md) for project state and working notes
- [investigation-template](../../templates/investigation-template.md) for incidents, bugs, abuse patterns, or technical root-cause work

## Placement Rules

- raw evidence stays in `vault/raw/` or `vault/raw/imports/`
- durable curated notes live in `vault/wiki/brain/`
- staged candidate updates live in `vault/wiki/candidates/`
- generated visuals belong in `.canvas` files under `vault/wiki/brain/` when they are part of navigation
- outputs and ad hoc artifacts belong in `vault/outputs/`

## Support Surfaces

- [[06 Vault Support Hub]] groups the support-layer references that this workflow depends on.
- [[07 Import Source Hub]] groups the raw import entry points that feed this workflow.
- [Vault Overview](../overview.md) is the quickest explanation of how raw, compiled, and curated notes relate.
- [Source Map](../../indexes/source_map.md) should reflect every raw-to-compiled step that this workflow creates.
- [Topics Index](../../indexes/topics.md) and [Keyword Index](../../indexes/keyword_index.md) are downstream generated outputs from successful capture and wiki rebuilds.
- [Vault Guide](../../Vault%20Guide.md) should stay aligned with this workflow so the human-readable contract matches the actual vault behavior.

## Promotion Rules

Promote material into the curated brain only when it improves one of these:

- identity grounding
- project continuity
- reusable stories
- technical playbooks
- product decisions
- roadmap clarity

If it does not improve one of those, keep it in raw sources or a temporary working note.

## Mutation Rules

When Jarvis gains more direct vault tools later, prefer:

- heading-level edits over whole-file rewrites
- frontmatter patching over freeform metadata drift
- explicit note references over hidden context
- template-backed note creation over ad hoc structures
- [[92 Agent Inbox]] for open-ended background curation that should stay reviewable before it becomes canonical memory
- `append_only` notes for continuous agent maintenance work
- `propose_only` notes when Jarvis should gather candidate changes under `vault/wiki/candidates/` without directly rewriting the canon
- explicit promotion when a candidate update is accepted into the canonical note

## Major-Change Rule

If a capture changes the way Jarvis should reason, route, remember, or present itself, add an entry to [[91 Vault Changelog]] and link the affected note from [[70 Jarvis Decision Log]] or [[80 Jarvis Roadmap]].
