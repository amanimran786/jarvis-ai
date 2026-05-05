# Obsidian Brain / Vault Domain Rules

The vault is a shared operating surface between Jarvis and Obsidian. It is not a note dump.

## Core Principle

Write to the vault only when Aman explicitly says "remember this", "log this", "save to brain", "append session lesson", or approves a proposed diff. Otherwise propose changes and stop.

## Directory Structure

- `vault/raw/` or `vault/raw/imports/` — raw evidence, captures, imports
- `vault/wiki/brain/` — durable curated operational notes
- `vault/templates/` — deterministic templates (not plugin-specific)
- `vault/indexes/` — read-only reference indexes (e.g., Repo Map)

## Brain Schema and Workflow

When changing the brain, follow:

- `vault/wiki/brain/03 Brain Schema.md` — metadata, linking, task style
- `vault/wiki/brain/04 Capture Workflow.md` — promotion and placement rules
- `vault/wiki/brain/91 Vault Changelog.md` — log major brain changes here

## Preferred Formats

- Concise YAML frontmatter on new operational notes and templates
- Plain markdown tasks (stay useful without plugins)
- Deterministic templates under `vault/templates/`
- `.canvas` files for visual maps (not plugin-specific drawing formats)

## Forbidden Patterns

Do not turn the brain into plugin-dependent app logic.

You may borrow conventions from: Obsidian Git, Dataview, Tasks, QuickAdd, Templater, JSON Canvas, and thin local bridge tools.

But keep Jarvis able to read and write the vault correctly as plain markdown, without those plugins.

## Claude Commands for Vault Work

- `/search-shared-brain <query>` — read-only targeted search (do not bulk-load)
- `/append-session-lesson <lesson>` — approval-gated append to brain
- `/propose-vault-update <path|new note>` — proposal-first update (never auto-apply)
- `/token-discipline <task>` — context-saving preflight for large Jarvis work

## Vault Paths

- Root: `/Users/truthseeker/jarvis-ai/vault`
- Claude Brain Contract: `vault/wiki/brain/95 Claude Shared Brain Contract.md`
- Repo Map: `vault/indexes/Repo Map.md`
- Context Policy: `vault/wiki/brain/82 Context Budget Discipline.md`

## Search Pattern

For Jarvis/project/memory questions, search targeted:

```bash
rg -n --hidden -g '!.git' "QUERY" vault/
```

Read only the targeted files. Do not bulk-load vault, raw imports, generated indexes, or giant logs.

## Never Auto-Commit

Do not auto-commit vault changes. Stage or show diffs for review.
