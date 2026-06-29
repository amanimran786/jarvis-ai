# WORK_QUEUE.json — Enriched Task Spec Schema

`WORK_QUEUE.json` is the single source of truth for all pending, active, and
completed work.  The orchestrator loop reads it every iteration; the Cowork
companion reads `LAUNCH_QUEUE.json` to fire sessions.

---

## Full task object

```json
{
  "id": "TASK-042",
  "title": "Add rate limiting to web_fetch",
  "description": "harness/web_search.py calls fetch_page() with no retry logic. On 429 or transient network errors the whole search fails silently. Add exponential backoff (3 retries, base 1s) and surface the error clearly when all retries are exhausted.",
  "allowed_files": [
    "harness/web_search.py",
    "harness/adaptive_router.py",
    "tests/test_web_search.py"
  ],
  "forbidden_files": [".env", "config/credentials.json"],
  "acceptance_criteria": [
    "fetch_page retries 3× on 429 with exponential backoff",
    "on final failure raises WebFetchError (not swallows)",
    "all existing tests still pass",
    "new unit tests: mock 429 response × 3 → success on 4th; mock 429 × 4 → raises"
  ],
  "verification_commands": [
    "python -m pytest tests/test_web_search.py -q",
    "python -m ruff check harness/web_search.py tests/test_web_search.py"
  ],
  "constraints": {
    "local_first": true,
    "network": false
  },
  "budget": {
    "max_attempts": 3,
    "wall_time_seconds": 1800,
    "tool_calls": 40
  },
  "domain": "harness",
  "assigned_ai": "claude",
  "priority": 1,
  "status": "queued",
  "created_at": "2026-06-28T00:00:00Z",
  "assigned_to": null,
  "assigned_at": null,
  "completed_at": null,
  "result_summary": null
}
```

---

## Field reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | ✅ | Unique identifier. Format: `TASK-NNN` (zero-padded, e.g. `TASK-042`). |
| `title` | string | ✅ | Short imperative phrase (≤ 60 chars). Used as the session heading. |
| `description` | string | ✅ | Full description of what needs to be done. Include the *why*, the *what*, and any known edge cases. The more detail here, the better the generated prompt. |
| `allowed_files` | string[] | ✅ | Relative paths the execution agent may modify. Enforced by the loop contract. Legacy `files_hint` is accepted as an adapter only. |
| `forbidden_files` | string[] | — | Relative paths that must never be modified, even if they appear in context. |
| `acceptance_criteria` | string[] | ✅ | Concrete, verifiable conditions that define "done". Used verbatim in the generated prompt checklist. |
| `verification_commands` | string[] | ✅ for code | Deterministic commands the loop runs and records. Agent claims do not satisfy this field. |
| `constraints` | object | — | Machine-readable policy such as `local_first`, network access, packaging, or security review requirements. |
| `budget` | object | — | Loop-owned `max_attempts`, `wall_time_seconds`, and `tool_calls` limits. |
| `domain` | string | ✅ | Coarse area of the codebase. Used for session naming and prompt routing. Values: `harness`, `brains`, `ui`, `tests`, `infra`, `orchestration`, `general`. |
| `assigned_ai` | string | ✅ | Which agent should pick this up. Values: `claude`, `codex`, `gemini`, `local`. |
| `priority` | int | — | 1 = highest. Lower numbers run first. Default 99 if omitted. |
| `status` | string | ✅ | Lifecycle state (see below). |
| `created_at` | ISO 8601 | — | Set automatically by `_enqueue_task`. |
| `assigned_to` | string\|null | — | Session ID that claimed the task (`jarvis-board`, etc.). Set by the orchestrator on launch. |
| `assigned_at` | ISO 8601\|null | — | Timestamp when the task was claimed. |
| `completed_at` | ISO 8601\|null | — | Timestamp when `status` moved to `done`. |
| `result_summary` | string\|null | — | Free-text summary written by the completing session. Harvested by the orchestrator. |

---

## Status lifecycle

```
queued
  │
  ▼ (orchestrator picks up, writes to LAUNCH_QUEUE, Cowork fires start_task)
in_progress
  │
  ▼ (session calls SessionTracker.complete())
[harvested by orchestrator]
  │
  ▼
done
```

Possible statuses:

| Status | Meaning |
|--------|---------|
| `queued` | Waiting to be picked up by the orchestrator |
| `in_progress` | A session has been launched for this task |
| `done` | Completed and harvested |
| `blocked` | Cannot proceed (dependency not met). Orchestrator skips blocked tasks. |
| `cancelled` | Dropped intentionally. Stays in file for audit trail. |

---

## LAUNCH_QUEUE.json format

Written by `orchestrator_loop.py`; read and cleared by the Cowork scheduled task.

```json
[
  {
    "task_id":    "TASK-042",
    "session_id": "jarvis-harness-claude-task042",
    "attempt_id": "attempt_9e7e5c...",
    "contract_sha256": "...",
    "task_spec":  {"...": "validated contract snapshot"},
    "prompt":     "...<full generated prompt>...",
    "queued_at":  "2026-06-28T01:00:00+00:00",
    "status":     "pending",
    "domain":     "harness",
    "assigned_ai": "claude"
  }
]
```

The Cowork companion:
1. Reads `LAUNCH_QUEUE.json`
2. For each record with `status == "pending"`: calls `start_task(session_id, prompt)`
3. Updates `status` to `"fired"` in the file
4. On error: sets `status` to `"error"` with an `error` field

---

## Contract authoring rules

`harness.task_contract.TaskSpec` validates the queue row and
`harness.prompt_generator` deterministically renders the agent packet. The model
does not write or reinterpret the contract. A launch is blocked when the contract
is invalid or its dispatch checkpoint cannot be persisted.

- Write an observable goal, not implementation theater.
- Declare allowed and forbidden paths; do not rely on "touch no other file" prose.
- Pair every acceptance criterion with loop-owned verification evidence.
- Set explicit budgets for expensive or risky work.
- Treat the generated prompt as a view of the contract, never the source of truth.

Every non-dry launch appends an `AttemptRecord` checkpoint to
`~/Library/Application Support/Jarvis/orchestrator/attempts.jsonl` before the
session is claimed.
