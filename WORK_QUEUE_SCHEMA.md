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
  "files_hint": [
    "harness/web_search.py",
    "harness/adaptive_router.py",
    "tests/test_web_search.py"
  ],
  "acceptance_criteria": [
    "fetch_page retries 3× on 429 with exponential backoff",
    "on final failure raises WebFetchError (not swallows)",
    "all existing tests still pass",
    "new unit tests: mock 429 response × 3 → success on 4th; mock 429 × 4 → raises"
  ],
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
| `files_hint` | string[] | ✅ | Relative paths of files the agent should read first. Improves prompt relevance significantly. |
| `acceptance_criteria` | string[] | ✅ | Concrete, verifiable conditions that define "done". Used verbatim in the generated prompt checklist. |
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

## Tips for good prompts

The `prompt_generator` uses `description`, `files_hint`, and `acceptance_criteria`
to ask qwen3:30b-a3b to write the actual session prompt.  Quality degrades if:

- `description` is vague ("fix stuff" → bad; "fetch_page must retry on 429 using urllib.error.HTTPError, with jitter" → good)
- `files_hint` is empty (the LLM can't reference specific code)
- `acceptance_criteria` has no verifiable conditions ("works correctly" → bad; "raises WebFetchError after 3 failed retries" → good)
