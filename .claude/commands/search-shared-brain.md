---
description: Search the shared Jarvis vault and return targeted matches with paths.
argument-hint: <query>
---

You are searching the shared brain at `/Users/truthseeker/jarvis-ai/vault`.

## Steps

1. Run a targeted search first:
   ```bash
   rg -n --hidden -g '!.git' "$ARGUMENTS" /Users/truthseeker/jarvis-ai/vault
   ```
2. If no hits, retry case-insensitive with fewer terms.
3. Read only the smallest useful ranges from at most 5 matching files.
4. Return:
   - note title
   - `vault/...` path
   - 1-3 relevant line references
   - one short synthesis sentence

## Rules

- Do not bulk-read the vault.
- Do not write to the vault from this command.
- Prefer curated notes under `vault/wiki/brain/` before raw imports.
- If results exceed 5 files, list remaining paths only.
