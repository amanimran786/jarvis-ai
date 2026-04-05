# Jarvis Vault

The vault is Jarvis's local markdown knowledge layer.

- Put raw source material in `vault/raw/`
- Put cleaned topic pages in `vault/wiki/`
- Keep generated indexes in `vault/indexes/`
- Save generated reports or outputs in `vault/outputs/`

Jarvis reads the vault through `vault.py`, indexes markdown locally, and only injects relevant snippets into the active request when needed.
