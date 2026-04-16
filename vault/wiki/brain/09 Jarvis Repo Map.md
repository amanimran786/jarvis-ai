---
type: map
area: engineering
owner: jarvis
write_policy: curated
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - repo
  - coding
  - runtime
  - verification
related:
  - "[[08 Coding Systems Hub]]"
  - "[[75 Debugging Root Cause Playbook]]"
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Coding Implementation Playbook]]"
  - "[[79A Code Review Regression Heuristics]]"
  - "[[79B Jarvis Architecture Runtime Seams]]"
  - "[[79C Verification Matrix]]"
---

# Jarvis Repo Map

Purpose: give Jarvis one fast retrieval surface for where behavior actually lives and which verification command proves a change.

## Main Runtime Spine

- `router.py` handles fast paths, user intent routing, and tool dispatch before deeper model work.
- `orchestrator.py` classifies requests into tools and specialist-agent paths.
- `model_router.py` decides local/cloud behavior, engineering grounding, and prompt shaping.
- `specialized_agents.py` coordinates scoped specialist roles.
- `specialized_agent_native.py` handles narrow native specialist shortcuts that should bypass model calls.

## Product Surfaces

- `main.py` starts the GUI or headless runtime.
- `ui.py` owns the main PyQt desktop surface.
- `desktop/overlay.py` owns the overlay surface.
- `voice.py` owns the voice loop and TTS/STT handoff behavior.
- `meeting_listener.py` owns meeting capture and live assist behavior.
- `Jarvis.spec` plus `scripts/install_jarvis_app.sh` define the packaged macOS app path.

## Local Runtime Layer

- `local_runtime/local_stt.py` is the main local STT seam.
- `local_runtime/local_tts.py` is the macOS fallback TTS seam.
- `local_runtime/local_kokoro_tts.py` and related subprocess/runtime files are the higher-quality local voice path.
- `local_runtime/` is where packaging drift often shows up first.

## Brain And Memory Layer

- `vault.py` handles vault indexing, search, and context building.
- `vault_edit.py` handles bounded note mutation.
- `vault_capture.py` handles structured natural-language brain writes.
- `memory.py`, `memory_layer.py`, `semantic_memory.py`, and `graph_context.py` shape durable memory and retrieval.

## Where To Verify

- routing change: targeted `tests/test_jarvis_regression_suite.py`
- bounded vault mutation: targeted `tests/test_unit_coverage.py`
- packaged surface change: rebuild with `scripts/install_jarvis_app.sh --applications-only`
- installed app smoke: `tests/test_jarvis_live_integrations.py`
- syntax-only Python patch: `python3 -m py_compile ...`

## Narrow Verification Commands

- `python3 -m pytest tests/test_jarvis_regression_suite.py -k '<targeted_case>' -q`
- `python3 -m pytest tests/test_unit_coverage.py -k '<targeted_case>' -q`
- `python3 -m py_compile <files>`
- `JARVIS_RUN_PACKAGED_SMOKE=1 python3 -m pytest tests/test_jarvis_live_integrations.py -k 'packaged_app_starts_and_serves_status' -q`

## Default Coding Rule

Start from [[08 Coding Systems Hub]], then pull the smallest specific playbook and the narrowest proof command for the seam you are touching.
