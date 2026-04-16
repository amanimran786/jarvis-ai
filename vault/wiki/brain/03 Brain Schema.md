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
  - schema
  - metadata
  - dataview
related:
  - "[[00 Home]]"
  - "[[02 Brain Dashboard]]"
  - "[[04 Capture Workflow]]"
  - "[[70 Jarvis Decision Log]]"
  - "[[90 Task Hub]]"
---

# Brain Schema

Purpose: keep the Jarvis brain queryable, linkable, and durable without depending on any Obsidian plugin runtime.

Linked notes: [[00 Home]], [[02 Brain Dashboard]], [[04 Capture Workflow]], [[06 Vault Support Hub]], [[70 Jarvis Decision Log]], [[90 Task Hub]], [[93 Vault Maintenance]]

## Principle

Borrow the discipline from Dataview-style vaults, but keep the core brain usable as plain markdown.

That means:

- metadata should be predictable
- notes should still read well without any plugin
- tasks should still work as plain markdown checkboxes
- canvases should use the open `.canvas` format
- Jarvis should own the capture contract itself instead of relying on plugin scripting

## Recommended Frontmatter

Use concise YAML frontmatter on new operational notes, new templates, and any high-value curated notes we touch going forward.

Preferred fields:

- `id`: optional stable identifier for notes Jarvis may patch surgically later
- `type`: what kind of note this is
- `area`: product, career, engineering, security, ops, vault
- `owner`: `user`, `jarvis`, or `generated`
- `write_policy`: `curated`, `append_only`, `generated`, or `propose_only`
- `review_required`: `true` when Jarvis should stage or propose instead of silently updating canonical content
- `scope`: optional memory scope such as personal, project, or system
- `status`: active, draft, evergreen, archived
- `source`: repo, resume, claude_export, chatgpt_export, manual
- `confidence`: high, medium, low
- `created`: `YYYY-MM-DD`
- `updated`: `YYYY-MM-DD`
- `version`: integer revision marker when a note is actively maintained
- `tags`: short stable labels
- `related`: wikilinks to nearby notes
- `role_targets`: optional list for interview and career notes

## Type Suggestions

- `brain_meta`
- `identity`
- `project`
- `decision`
- `story`
- `playbook`
- `architecture_map`
- `verification`
- `roadmap`
- `task_hub`
- `capture_template`
- `investigation`

## Ownership Contract

- `owner: user`
  Canonical human-owned notes. Jarvis may read them freely, but write access should be narrow and intentional.
- `owner: jarvis`
  Operational notes Jarvis is allowed to maintain directly, usually append-first surfaces like inboxes or queues.
- `owner: generated`
  Deterministic build outputs, compiled notes, or indexes. Do not patch these manually through curator actions.

## Write Policy Contract

- `write_policy: curated`
  High-value canonical notes. Prefer explicit user-directed edits and bounded heading-level updates only when the target is clear.
  When `review_required: true`, native curator updates should stage into candidates instead of writing directly into canon.
- `write_policy: append_only`
  Safe operational notes. Jarvis may append tasks, updates, or queue items without rewriting the whole note.
- `write_policy: generated`
  Generated artifacts. Update the source or generator, not the output file.
- `write_policy: propose_only`
  Jarvis should route changes into [[92 Agent Inbox]] or `vault/wiki/candidates/` first, then promote them deliberately.

## Candidate Layer Contract

- `vault/wiki/candidates/` is the staging lane between inbox work and canon.
- Candidate notes are safe for Jarvis to create and append when a canonical note is `propose_only`.
- Candidate notes should point back to the canonical target clearly and keep proposed updates under a bounded heading.
- Promotion from candidate to canon should stay explicit and reviewable.
- Promotion should merge only the selected proposed update back into the canonical heading, then leave a durable promotion log in the candidate note.

## Task Contract

Use plain markdown tasks so the note still works without plugins:

- `- [ ] Ship mic fix 📅 2026-04-16 #jarvis #voice`
- `- [ ] Verify packaged app 📅 2026-04-16 #packaging`
- `- [x] Distill Anthropic variant #brain #career`

Rules:

- start with a verb
- add a due date only when real
- keep tags short and stable
- link target notes when the task changes a specific artifact

## Linking Contract

- use direct wikilinks for durable relationships
- prefer explicit links over hoping graph proximity will emerge
- link a note to the hub note that would logically surface it
- when a note changes product direction or memory policy, link it into [[70 Jarvis Decision Log]]

## Context-Bundle Contract

When Jarvis later builds note bundles, prefer a thin local bridge:

- read the note directly
- patch the smallest relevant heading or field
- pass explicit note refs instead of dumping giant context blobs
- prefer related-note bundles over raw top-k transcript fragments

## Change-Provenance Contract

Major brain changes should leave a durable trace in [[91 Vault Changelog]].

Update the changelog when:

- a new durable note family is added
- retrieval rules or memory hierarchy change
- a major import or distillation pass lands
- a new canvas or workflow meaningfully changes how the brain is navigated

## Plugin Boundary

Good optional accelerators:

- Dataview for querying metadata
- Tasks for vault-wide task views
- QuickAdd or Templater for human capture convenience

Do not make Jarvis depend on those plugins to read or write the brain correctly.

## Supporting Surfaces

- [[06 Vault Support Hub]] is the grouped support-layer anchor for these references.
- [Vault Overview](../overview.md) explains where schema applies across raw, compiled, and curated layers.
- [Source Map](../../indexes/source_map.md) is the provenance layer that should stay compatible with this schema.
- [Keyword Index](../../indexes/keyword_index.md) and [Topics Index](../../indexes/topics.md) are generated read surfaces, not schema owners.
- [Vault Guide](../../Vault%20Guide.md) is the human-readable vault contract that should stay aligned with this note.
