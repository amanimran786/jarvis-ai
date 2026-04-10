# Project Audit — 2026-04-09

## Goal

Jarvis should become a local-first AI operating system:

- primary path runs 100% local
- cloud providers remain optional fallback, not the foundation
- product effort stays focused on assistant capability, not side projects

## Keep

These subsystems are directly relevant to the goal and are still wired into the runtime:

- local model routing and provider policy
- voice, meeting assist, browser control, system control
- memory, vault, Graphify grounding, skills, and specialized agents
- daemon, managed task runtime, CLI, and API
- local training, local evals, and beta replay loops
- interview and personal-context packs

## Removed In This Pass

These pieces were removed because they were dead, duplicate, or off-mission:

- `jarvis_beta.py`
  - legacy GPT-4o beta harness
  - not part of the current daemon/UI/API path
  - pointed away from the local-first goal

- `private_vault.py`
  - unused encrypted-vault stub
  - not integrated into runtime state, memory routing, or UI

- `skills/whatsapp_automation/`
  - unrelated sidecar automation surface
  - not part of the core Jarvis runtime

- `scripts/whatsapp_local_parser.py`
  - only supported the removed WhatsApp sidecar

- `test_router.py`
  - ad hoc debug script, not a real test

- `test_tcc.applescript`
- `test_tcc_term.applescript`
  - local permission-debug artifacts, not product code

- `attachment.png`
  - stray workspace artifact

- `routing_log.jsonl`
  - generated runtime artifact

## Remaining Cleanup Candidates

These still deserve review, but were not removed in this pass because they are actively wired or need a more careful replacement:

- `stealth.py`
  - currently used by `ui.py` and `overlay.py`
  - adds product risk and is not part of the local-model goal
  - should be removed only with a coordinated UI cleanup

- `google_services.py`
  - still wired into runtime and assistant flows
  - relevant to personal-assistant capability, but not required for local-first core

- `local_model_benchmark.py`
  - useful for local-model quality work
  - could eventually move under `training/` if we want a cleaner root

- `memory_layer.py`
  - thin compatibility shim
  - still referenced by `router.py`

## Recommended Next Cut

If we continue the cleanup, the next highest-value pass should be:

1. remove stealth/screen-share-evasion behavior from the UI stack
2. move benchmarking and training helpers under a tighter `training/` surface
3. collapse thin wrapper modules where they only preserve old naming
4. tighten README so it reflects the actual local-first architecture instead of historical features
