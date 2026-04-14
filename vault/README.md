# Jarvis Vault

The vault is Jarvis's local markdown knowledge layer.

- Put raw source material in `vault/raw/`
- Put cleaned topic pages in `vault/wiki/`
- Keep generated indexes in `vault/indexes/`
- Save generated reports or outputs in `vault/outputs/`

Jarvis reads the vault through `vault.py`, indexes markdown locally, and only injects relevant snippets into the active request when needed.

## Obsidian Workflow

This vault is also safe to open directly in Obsidian.

- Keep raw exports and source dumps in `vault/raw/imports/`
- Distill durable knowledge into `vault/wiki/brain/`
- Use markdown as the source of truth so both you and Jarvis can read it
- Prefer short, factual notes over giant transcript dumps

Recommended flow:

1. Drop Claude, ChatGPT, or other exports into `vault/raw/imports/`
2. Review and distill them into clean notes in `vault/wiki/brain/`
3. Refresh the local vault index when you want Jarvis to pick up the new material
