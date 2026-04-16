# Jarvis Local Vault Overview

This vault exists so Jarvis can rely on indexed local markdown knowledge before growing prompts or depending on long chat carry-over.

The current design goal is simple:

Keep source material organized. Build small searchable indexes. Load only the relevant snippets for the current request.

## Core Connections

- [[04 Capture Workflow]] defines how raw evidence should move into durable notes
- [[70 Jarvis Decision Log]] holds the retrieval, memory, and product-policy decisions behind the vault
- [[78 AI Runtime Agent Engineering Principles]] explains how the runtime should use the vault without turning it into a giant prompt dump
- [[80 Jarvis Roadmap]] keeps the vault tied to Jarvis product direction rather than treating it as a side system
- [[91 Vault Changelog]] records major brain and vault changes over time
- [[93 Vault Maintenance]] is the maintenance surface for keeping this layer self-sustaining

## How To Read This Layer

- `vault/raw/` is source evidence and import material
- `vault/wiki/compiled/` is the deterministic bridge from raw material into searchable summaries
- `vault/wiki/brain/` is the curated canonical layer
- `vault/indexes/` is the retrieval infrastructure, not the source of truth

Useful entry points from here:

- [[jarvis-vault-strategy]]
- [[eval-pattern-self-improve]]
- [[ingested-file-readme-md]]
