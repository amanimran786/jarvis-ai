# Orchestrator Shared File Schema

Defines the contract between all active Jarvis dev sessions and the
coordination layer (`session_orchestrator.py` / `python orchestrator.py`).

All files live at the **project root**. Every session can read any file. Sessions
write their own status entry via `harness/audit.py` (which owns the write path).
Tasks are added via `python orchestrator.py add-task`. Never truncate these files.

---

## `ORCHESTRATOR_STATUS.json`

**Writer:** `harness/audit.py` — called automatically on `query_received`,
`response_sent`, `session_start`, `session_end` events.

**Reader:** `session_orchestrator.py` dashboard (every 30s).

`sessions` is a **list of dicts** — one entry per active process. When a process
starts it appends its entry; when it ends it sets `status: "offline"`. The
orchestrator loop purges ghost entries (status=active, last_active >6h old).

```json
{
  "sessions": [
    {
      "session_id": "uuid4",
      "name": "jarvis-board",
      "last_active": "2026-06-24T07:00:00Z",
      "current_task": "What the session is doing right now",
      "completed_tasks": [
        {"task": "description", "completed_at": "2026-06-24T06:00:00Z"}
      ],
      "next_task": "What happens after current_task",
      "status": "active | idle | stalled | offline | error"
    }
  ]
}
```

### Field rules

| Field | Type | Required | Notes |
|---|---|---|---|
| `session_id` | UUID string | yes | Set once at process start by `harness/audit.py` |
| `name` | string | yes | Must match a `name` in `SESSIONS.json`. Set via `harness.audit.start_session(name)` |
| `last_active` | ISO-8601 UTC | yes | Updated on every event. Age >5 min → STALLED on dashboard |
| `current_task` | string\|null | yes | Set to `null` when idle |
| `completed_tasks` | list of `{task, completed_at}` | yes | Capped at 50 by audit.py |
| `next_task` | string\|null | no | Optional hint for the dashboard |
| `status` | enum | yes | `active` → working; `idle` → between tasks; `offline` → clean shutdown |

### How to register your session

Call `harness.audit.start_session(name)` at process start — it snapshots memory,
appends your entry to `sessions[]`, and registers an atexit handler for clean shutdown.

```python
import harness.audit as audit
audit.start_session("jarvis-board")   # name must match SESSIONS.json
# ... do work ...
audit.end_session()                   # marks status=offline, writes ops ledger
```

The `name` field is what `session_orchestrator.py` displays. Use the short IDs
from `SESSIONS.json` so the dashboard can match tasks in `WORK_QUEUE.json`.

### Registering without code changes

Two options for sessions that don't call `harness.audit` directly:

**Option A — env var** (for processes that call `start_session()` internally):
```bash
JARVIS_SESSION_NAME=jarvis-board python main.py
```
`start_session()` checks `JARVIS_SESSION_NAME` first, then uses its `name` arg.

**Option B — CLI register** (for coordination sessions that don't run main.py):
```bash
python orchestrator.py register jarvis-board
# With explicit next task:
python orchestrator.py register jarvis-board "Review and close AGENT_BOARD items"
```
This writes a live `status=active` entry directly to `ORCHESTRATOR_STATUS.json`
and auto-fills `next_task` from the highest-priority queued task in WORK_QUEUE
if none is specified.

---

## `WORK_QUEUE.json`

Prioritized task list across all sessions. Any session reads this to find its
next task. The orchestrator loop writes new entries; sessions update `status`.

```json
[
  {
    "session_name": "jarvis-board",
    "task": "One-line task description",
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
| 1 | Blocking — must complete before anything else in this session |
| 2 | High — current sprint focus |
| 3 | Normal — queued but not urgent |
| 4+ | Backlog |

### CLI to add a task

```bash
python orchestrator.py add-task "jarvis-board" "Fix _awaiting_msg_recipient bypass" 1
```

### Session pick-up pattern

```python
import json
from pathlib import Path

def pick_next(session: str) -> dict | None:
    try:
        queue = json.loads(Path("WORK_QUEUE.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    candidates = [t for t in queue if t.get("session_name") == session and t.get("status") == "queued"]
    return min(candidates, key=lambda t: t.get("priority", 99), default=None)
```

---

## `SESSIONS.json`

Registry of all known dev sessions. Write once when a session is created;
update `status` to `offline` when the session winds down.

```json
{
  "version": 2,
  "sessions": [
    {
      "name": "jarvis-board",
      "display": "Human-readable display name",
      "purpose": "What this session works on",
      "status": "active | idle | offline",
      "registered_at": "2026-06-24T00:00:00Z",
      "owner": "Claude (Cowork) | Codex | User",
      "files_owned": ["list of files this session may edit"],
      "notes": "Coordination notes — lane boundaries, dependencies, etc."
    }
  ]
}
```

The `name` field is the short ID used in `ORCHESTRATOR_STATUS.json` and
`WORK_QUEUE.json`. Keep it kebab-case, ≤20 chars.

---

## `MASTER_LOG.md`

Append-only human-readable audit trail. Written by the orchestrator loop on
every coordination round. Sessions may also append directly.

**Format (strict):**
```
[YYYY-MM-DD HH:MM:SS] [SESSION-NAME] event in plain English
```

```python
from pathlib import Path
from datetime import datetime

def log(session: str, event: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("MASTER_LOG.md", "a") as fh:
        fh.write(f"[{ts}] [{session}] {event}\n")
```

Never rewrite or truncate. It is the audit trail.

---

## Stall detection

A session is **STALLED** when `last_active` is >5 minutes old AND `status` is
not `idle` or `offline`. The dashboard flags it with ⚠ and logs a single warning
per stall event (not every refresh cycle).

Sessions set `status: "idle"` between tasks and `status: "offline"` on shutdown
to suppress false stall alerts. Seed entries with `last_active: null` are shown
as `idle` / `never` — not stalled.

---

## Dashboard entry point

```bash
python orchestrator.py              # live dashboard, refresh every 30s
python orchestrator.py status       # one-shot snapshot and exit
python orchestrator.py add-task "session" "task" <priority>
python orchestrator.py history      # print MASTER_LOG.md
```

`orchestrator.py` is the LLM intent classifier when imported as a module.
When run directly it delegates to `session_orchestrator.py` via `__main__`.
