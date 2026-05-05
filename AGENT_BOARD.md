# Jarvis Agent Board

Purpose: keep Codex and Claude from colliding while both are active in the same repo.

## Coordination Rules

- Claim a lane before editing shared files.
- Prefer disjoint files. If both agents need the same file, write a short handoff note here first.
- Verify narrowly and write the exact command used.
- Do not stage or commit another agent's unrelated dirty work.
- If Multica is running locally, mirror these items there. If not, this file is the source of truth.

## Active Lanes

### Codex Lane: Automation and Training Reliability

Owner: Codex

Scope:
- `local_runtime/local_finetune_scheduler.py`
- `training/dashboard_generator.py`
- `scripts/install_overnight_training.sh`
- `scripts/overnight_training_status.sh`
- `tests/test_overnight_training_pipeline.py`

Current objective:
Make overnight local fine-tuning observable, truthful, idempotent to install, and easy to verify after a run.

Latest status:
- `ai.jarvis.overnight-training` is loaded in launchd.
- Latest repaired training baseline is `312/313`.
- Next scheduled run is `2026-05-05T23:00:00`.

### Claude Lane: Product UX and Conversation Behavior

Owner: Claude

Suggested scope:
- `router.py`
- `ui.py`
- `jarvis_agents.py`
- `briefing.py`
- conversation/messaging UX tests

Current objective:
Improve Jarvis interaction quality and live app behavior while Codex stays out of the same files unless explicitly coordinated.

## Open Coordination Notes

- Multica API at `localhost:8080` is currently unavailable, so repo-file coordination is active.
- If Claude needs to touch the automation lane, add a note here before editing.
- If Codex needs to touch `router.py` again, keep it to a surgical hunk and stage only that hunk.
