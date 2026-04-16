# Jarvis Vault

The vault is Jarvis's local markdown knowledge layer.

- Put raw source material in `vault/raw/`
- Put cleaned topic pages in `vault/wiki/`
- Keep generated indexes in `vault/indexes/`
- Save generated reports or outputs in `vault/outputs/`
- Keep reusable templates in `vault/templates/`

Jarvis reads the vault through `vault.py`, indexes markdown locally, and only injects relevant snippets into the active request when needed.

Use [[02 Brain Dashboard]], [[05 Source Inventory]], [[91 Vault Changelog]], and [[93 Vault Maintenance]] to navigate the curated surface and keep upkeep visible.

## Obsidian Workflow

This vault is also safe to open directly in Obsidian.

- Keep raw exports and source dumps in `vault/raw/imports/`
- Distill durable knowledge into `vault/wiki/brain/`
- Use markdown as the source of truth so both you and Jarvis can read it
- Prefer short, factual notes over giant transcript dumps
- Keep plugins optional; the markdown should still stand on its own

Recommended flow:

1. Drop Claude, ChatGPT, or other exports into `vault/raw/imports/`
2. Review and distill them into clean notes in `vault/wiki/brain/`
3. Refresh the local vault index when you want Jarvis to pick up the new material
4. Send cleanup and upkeep work to [[93 Vault Maintenance]] instead of scattering it through durable notes.

## Brain Operating Layer

The vault now has a small operating layer under `vault/wiki/brain/`:

- `03 Brain Schema.md` defines the metadata, linking, and task contract
- `04 Capture Workflow.md` defines how Jarvis should create and promote durable notes
- `91 Vault Changelog.md` preserves major brain changes and provenance
- `93 Vault Maintenance` is the place for upkeep, cleanup, and vault-health tasks that should stay separate from the durable brain

This borrows the best patterns from Dataview, Tasks, QuickAdd, Templater, and JSON Canvas without making Jarvis depend on those plugins to function.
