---
type: brain_meta
area: vault
owner: jarvis
write_policy: curated
review_required: true
status: active
source: repo
confidence: high
created: 2026-04-21
updated: 2026-04-21
version: 1
tags:
  - index
  - repo-map
  - claude
  - jarvis
related:
  - "../wiki/brain/95 Claude Shared Brain Contract.md"
  - "../wiki/brain/82 Context Budget Discipline.md"
---

# Repo Map

Purpose: give Claude Code and Jarvis a cheap orientation layer before reading source or vault notes.

## Repo Root

`/Users/truthseeker/jarvis-ai/`

| Path | Purpose |
|---|---|
| `AGENTS.md` | Cross-agent operating contract. Read first. |
| `CLAUDE.md` | Claude Code repo rules and shared-brain contract. |
| `main.py` | Desktop/headless runtime entry point. |
| `api.py` | FastAPI daemon surface used by desktop and console. |
| `jarvis_cli.py` | Terminal console and Claude-style CLI surface. |
| `router.py` | Intent/tool routing before LLM use. |
| `model_router.py` | Local/cloud/open-source model routing policy. |
| `config.py` | Runtime defaults and local model identifiers. |
| `ui.py` | PyQt6 desktop app. |
| `desktop/` | Desktop overlay, bridge, hotkeys, screen capture, device panel. |
| `local_runtime/` | Local STT/TTS/model training/eval/fleet code. |
| `task_runtime.py` | Managed agent/task lifecycle. |
| `semantic_memory.py`, `memory_layer.py`, `memory/` | Local memory and retrieval. |
| `vault.py`, `vault_capture.py`, `vault_edit.py`, `wiki_builder.py` | Obsidian vault read/write/build path. |
| `tests/` | Regression, unit, live, packaged-app smoke tests. |
| `scripts/install_jarvis_app.sh` | Real packaged-app install path. |

## Vault Layout

| Path | Purpose | Write rule |
|---|---|---|
| `vault/wiki/brain/` | Curated durable Jarvis brain | schema-gated |
| `vault/raw/` | Raw local evidence | append/import only |
| `vault/raw/imports/` | Imported Claude/OpenAI/ChatGPT exports | do not mutate casually |
| `vault/wiki/candidates/` | Review/staging lane | explicit promotion only |
| `vault/templates/` | Markdown templates | deliberate edits |
| `vault/indexes/` | Generated and curated indexes | prefer generator for generated files |
| `vault/.obsidian/` | Obsidian UI state/config | avoid unless user asks |

## Verification Shortcuts

```bash
python3 -m pytest tests/test_unit_coverage.py -q
python3 -m pytest tests/test_jarvis_regression_suite.py -q
scripts/install_jarvis_app.sh --applications-only
JARVIS_RUN_PACKAGED_SMOKE=1 python3 -m pytest tests/test_jarvis_live_integrations.py -k packaged_app -q
```

## Current Product Surface

- Real app: `/Users/truthseeker/Applications/Jarvis.app`
- Desktop shortcut: `/Users/truthseeker/Desktop/Jarvis.app -> /Users/truthseeker/Applications/Jarvis.app`
- Terminal console: `jarvis`
- Local daemon: FastAPI on discovered localhost port, usually `127.0.0.1:8765`
