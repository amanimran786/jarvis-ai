# Orchestrator Shared File Schema

Defines the contract between all active Jarvis dev sessions and the
coordination layer (`session_orchestrator.py` / `python orchestrator.py`).

All files live at the **project root**. Every session can read any file; each
session writes only its own status entry in `ORCHESTRATOR_STATUS.json` and
appends tasks via the CLI. Never truncate these files — they are append-friendly
by design.

---

## `ORCHESTRATOR_STATUS.json`

Written by each dev session. The dashboard reads this to display live health.

**Update frequency:** after every meaningful action (task start, task complete,
blocking event). At minimum update `last_active` every 5 minutes while the
session is running — otherwise the orchestrator marks you STALLED.

```json
{
  "sessions": {
    "<session-name>": {
      "last_active": "2026-06-24T07:00:00Z",
      "current_task": "Brief description of what the session is doing right now",
      "completed_tasks": [
        "Short description of each completed task (append, never remove)"
      ],
      "next_task": "What will happen after current_task is done",
      "status": "active | idle | stalled | offline | error"
    }
  }
}
```

### Field rules

| Field | Type | Required | Notes |
|---|---|---|---|
| `last_active` | ISO-8601 UTC string | yes | Must include timezone (`Z` or `+00:00`). Age >5 min → STALLED |
| `current_task` | string | yes | One line. Empty string is valid when idle |
| `completed_tasks` | string[] | yes | Append-only. Keep the last 20 max to avoid bloat |
| `next_task` | string | no | Empty string or omit when unknown |
| `status` | enum | yes | `active` while running, `idle` between tasks, `offline` when shutting down |

### Write pattern (Python)

```python
import json
from datetime import datetime, timezone
from pathlib import Path

STATUS_FILE = Path("ORCHESTRATOR_STATUS.json")
SESSION_NAME = "jarvis-board"   # must match SESSIONS.json name

def _update_status(current_task: str, next_task: str = "", status: str = "active") -> None:
    try:
        data = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
    except json.JSONDecodeError:
        data = {}
    sessions = data.get("sessions", {})
    entry = sessions.get(SESSION_NAME, {"completed_tasks": []})
    entry.update({
        "last_active": datetime.now(timezone.utc).isoformat(),
        "current_task": current_task,
        "next_task": next_task,
        "status": status,
    })
    sessions[SESSION_NAME] = entry
    data["sessions"] = sessions
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATUS_FILE)

def _complete_task(task_description: str) -> None:
    try:
        data = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
    except json.JSONDecodeError:
        data = {}
    sessions = data.get("sessions", {})
    entry = sessions.get(SESSION_NAME, {"completed_tasks": []})
    completed = entry.get("completed_tasks", [])
    completed.append(task_description)
    entry["completed_tasks"] = completed[-20:]  # keep last 20
    entry["last_active"] = datetime.now(timezone.utc).isoformat()
    sessions[SESSION_NAME] = entry
    data["sessions"] = sessions
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATUS_FILE)
```

---

## `WORK_QUEUE.json`

Prioritized task list across all sessions. Any session can read this to
discover its next task. Only the orchestrator CLI writes new entries; sessions
mark their own tasks `in_progress` or `done`.

```json
[
  {
    "session_name": "jarvis-board",
    "task": "One-line description of the task",
    "priority": 1,
    "status": "queued | in_progress | done | blocked | cancelled",
    "assigned_at": "2026-06-24T07:00:00Z or null",
    "completed_at": "2026-06-24T08:00:00Z or null",
    "created_at": "2026-06-24T00:00:00Z"
  }
]
```

### Priority convention

| Priority | Meaning |
|---|---|
| 1 | Blocking — must ship before anything else in this session |
| 2 | High — current sprint focus |
| 3 | Normal — queued, not urgent |
| 4+ | Backlog / nice-to-have |

### CLI to add a task

```bash
python orchestrator.py add-task "session-name" "task description" <priority>
# Example:
python orchestrator.py add-task "jarvis-board" "Fix _awaiting_msg_recipient bypass" 1
```

### Session pick-up pattern (Python)

```python
import json
from pathlib import Path

QUEUE_FILE = Path("WORK_QUEUE.json")

def _pick_next_task(session_name: str) -> dict | None:
    try:
        queue = json.loads(QUEUE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    candidates = [
        t for t in queue
        if t.get("session_name") == session_name and t.get("status") == "queued"
    ]
    return min(candidates, key=lambda t: t.get("priority", 99), default=None)
```

---

## `SESSIONS.json`

Registry of all known dev sessions. Sessions register themselves here once;
the orchestrator uses this for cross-reference and health checks.

```json
{
  "version": 1,
  "sessions": [
    {
      "name": "jarvis-board",
      "purpose": "Short human-readable description of what this session does",
      "status": "active | idle | offline",
      "registered_at": "2026-06-24T00:00:00Z",
      "owner": "Claude (Cowork) | Codex | User",
      "notes": "Optional coordination notes — lane boundaries, file ownership, etc."
    }
  ]
}
```

Sessions should update their own `status` field here when going offline so
other sessions know not to assign them work.

---

## `MASTER_LOG.md`

Append-only event log. Written by `session_orchestrator.py` automatically
whenever:
- A task is added to WORK_QUEUE
- A STALL is first detected for a session
- The dashboard starts or stops
- A session marks itself offline

**Format (strict):**
```
[YYYY-MM-DD HH:MM:SS] [SESSION-NAME] event description in plain English
```

Sessions may also append directly:

```python
from pathlib import Path
from datetime import datetime

def _log(session: str, event: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("MASTER_LOG.md", "a") as fh:
        fh.write(f"[{ts}] [{session}] {event}\n")
```

Never rewrite or truncate this file. It is the audit trail.

---

## Dashboard entry point

```bash
python orchestrator.py              # live dashboard, refresh every 30s
python orchestrator.py status       # one-shot snapshot and exit
python orchestrator.py add-task "session" "task" <priority>
python orchestrator.py history      # print MASTER_LOG.md
```

`orchestrator.py` delegates the coordination UI to `session_orchestrator.py`
when run as `__main__`. When imported as a module (`from orchestrator import classify`)
it behaves as the LLM intent classifier — the `__main__` guard is never triggered.

---

## Stall detection

A session is STALLED when `last_active` is more than **5 minutes** old and its
`status` is not `idle` or `offline`. The orchestrator logs each new stall to
`logs/orchestrator.log` and appends a line to `MASTER_LOG.md`. It does not
re-log the same session's stall every refresh cycle — only on the first detection.

Sessions should set `status: "idle"` when between tasks and `status: "offline"`
when shutting down to avoid false stall alerts.
