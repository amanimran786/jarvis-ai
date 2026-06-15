"""project_manager.py — Assign tasks/projects to agents and monitor them.

A project is a named goal with an ordered list of tasks, pinned to a specific
agent. Once dispatched, the project executor thread works through each task
sequentially, records results, emits structured events, and updates the
project's status — no human intervention needed.

Monitoring surfaces:
  CLI:  python3 project_manager.py [create|list|dispatch|cancel|monitor|status]
  API:  GET /projects, POST /projects, GET /projects/{id}/events (SSE)

Execution model:
  - Each project gets one daemon thread.
  - Tasks run via task_runtime.submit_task(), polled until terminal.
  - All activity written to project_events table (append-only, not rewritten).
  - Heartbeat event every HEARTBEAT_INTERVAL seconds while running.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.project_manager")

_REPO_ROOT = Path(__file__).resolve().parent
_LOCK = threading.RLock()

# How often a running project emits a heartbeat event.
HEARTBEAT_INTERVAL = int(os.getenv("JARVIS_PROJECT_HEARTBEAT", "30"))

# How often the executor polls task_runtime for task completion.
_POLL_INTERVAL = float(os.getenv("JARVIS_PROJECT_POLL_INTERVAL", "3"))

# Max time (seconds) to wait for a single task before declaring it timed out.
_TASK_TIMEOUT = float(os.getenv("JARVIS_PROJECT_TASK_TIMEOUT", "1800"))

# Max tasks from a single project running concurrently. task_runtime's own
# model semaphore still bounds total concurrent model calls across all projects;
# this just caps how wide one project's dependency wave can fan out.
_MAX_PARALLEL = max(1, int(os.getenv("JARVIS_PROJECT_MAX_PARALLEL", "6")))

_PROJECT_STATUSES = ("pending", "running", "done", "failed", "cancelled")
_TERMINAL = {"done", "failed", "cancelled"}
_TASK_TERMINAL = {"succeeded", "failed", "cancelled"}


# ── DB setup ──────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    override = os.getenv("JARVIS_PROJECT_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    # Reuse the task-persistence DB so foreign-key joins are possible.
    from task_persistence import db_path as _task_db_path
    return _task_db_path()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA_INITIALIZED = False


def _ensure_schema() -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    with _LOCK:
        if _SCHEMA_INITIALIZED:
            return
        with _connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    agent_id    TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    meta_json   TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_projects_status
                ON projects(status);

                CREATE TABLE IF NOT EXISTS project_tasks (
                    id         TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    seq        INTEGER NOT NULL,
                    title      TEXT NOT NULL,
                    prompt     TEXT NOT NULL,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    task_id    TEXT NOT NULL DEFAULT '',
                    result     TEXT NOT NULL DEFAULT '',
                    depends_on TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_project_tasks_project_id
                ON project_tasks(project_id, seq);

                CREATE TABLE IF NOT EXISTS project_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    task_seq   INTEGER,
                    ts         TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_project_events_project_id
                ON project_events(project_id, id);
            """)
            # Idempotent migration: add depends_on for pre-Build1 tables.
            try:
                conn.execute(
                    "ALTER TABLE project_tasks ADD COLUMN depends_on TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                pass  # Column already exists.
        _SCHEMA_INITIALIZED = True


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _emit(project_id: str, event_type: str, task_seq: int | None = None, **payload: Any) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO project_events (project_id, task_seq, ts, event_type, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, task_seq, _now(), event_type, _dumps(payload)),
            )
    except Exception:
        log.exception("project_manager: failed to emit event %s for %s", event_type, project_id)


def _set_project_status(project_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE id=?",
            (status, _now(), project_id),
        )
    _emit(project_id, "status_change", status=status)


def _set_ptask_status(ptask_id: str, status: str, task_id: str = "", result: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE project_tasks SET status=?, task_id=?, result=?, updated_at=? WHERE id=?",
            (status, task_id, result[:8000], _now(), ptask_id),
        )


# ── Project executor thread ───────────────────────────────────────────────────

# Registry of running executor threads, keyed by project_id.
_EXECUTORS: dict[str, threading.Thread] = {}


def _run_project(project_id: str) -> None:
    """Daemon thread: execute project tasks respecting declared dependencies.

    Dependency model
    ----------------
    Each task's ``depends_on`` column encodes its blocking dependencies as a
    JSON list of seq integers (e.g. ``[0, 2]``).  An empty string means the
    implicit sequential default: depends on seq-1 only (preserving the original
    ordered behaviour for all existing projects and templates).  Passing
    ``depends_on=[]`` marks a task as unconditionally independent so it fans out
    immediately alongside other ready tasks.

    Up to ``_MAX_PARALLEL`` tasks from the same project run concurrently; the
    task_runtime model semaphore still caps total cross-project model calls.
    Fail-fast: any task failure (or timeout) immediately marks the project
    failed and returns.
    """
    import task_runtime

    try:
        _set_project_status(project_id, "running")
        _emit(project_id, "project_started")

        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_tasks WHERE project_id=? ORDER BY seq",
                (project_id,),
            ).fetchall()
            agent_row = conn.execute(
                "SELECT agent_id FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()

        agent_id = agent_row["agent_id"] if agent_row else ""
        tasks_by_seq: dict[int, sqlite3.Row] = {r["seq"]: r for r in rows}

        def _deps(seq: int) -> set[int]:
            try:
                raw = tasks_by_seq[seq]["depends_on"]
            except (IndexError, KeyError):
                raw = ""
            if not raw:
                return {seq - 1} if seq > 0 else set()
            try:
                return set(json.loads(raw))
            except Exception:
                return {seq - 1} if seq > 0 else set()

        # completed: seqs that have reached a terminal state (done or pre-existing failed).
        # submitted: seqs already dispatched or skipped — never re-submitted.
        completed: set[int] = set()
        submitted: set[int] = set()
        for r in rows:
            if r["status"] in ("done", "failed"):
                completed.add(r["seq"])
                submitted.add(r["seq"])

        # in_flight: seq -> {ptask_id, submitted_id, title, deadline}
        in_flight: dict[int, dict] = {}

        def _cancel_in_flight() -> None:
            """Request cancellation of all currently in-flight task_runtime tasks."""
            for info in in_flight.values():
                try:
                    task_runtime.cancel_task(info["submitted_id"])
                except Exception:
                    pass

        last_heartbeat = time.monotonic()

        while True:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                _emit(project_id, "heartbeat")
                last_heartbeat = now

            # Cancellation check.
            with _connect() as conn:
                proj = conn.execute(
                    "SELECT status FROM projects WHERE id=?", (project_id,)
                ).fetchone()
            if proj and proj["status"] == "cancelled":
                _cancel_in_flight()
                _emit(project_id, "project_cancelled", reason="cancel_requested")
                return

            # Find tasks whose deps are all satisfied and that haven't been submitted yet.
            ready = [
                seq for seq in sorted(tasks_by_seq)
                if seq not in submitted and _deps(seq).issubset(completed)
            ]

            # Submit up to available parallelism slots.
            for seq in ready[: max(0, _MAX_PARALLEL - len(in_flight))]:
                r = tasks_by_seq[seq]
                ptask_id = r["id"]
                title = r["title"]
                prompt = r["prompt"]

                # Inject completed dep results into synthesis/aggregation tasks.
                # Only injects for explicit deps (stored as non-empty JSON list).
                _raw_deps = tasks_by_seq[seq]["depends_on"]
                try:
                    _explicit_dep_seqs = json.loads(_raw_deps) if _raw_deps else []
                except Exception:
                    _explicit_dep_seqs = []
                if _explicit_dep_seqs:
                    _dep_blocks: list[str] = []
                    with _connect() as _dconn:
                        for _dep_seq in sorted(_explicit_dep_seqs):
                            _dep_row = tasks_by_seq.get(_dep_seq)
                            if _dep_row is None:
                                continue
                            _res_row = _dconn.execute(
                                "SELECT result FROM project_tasks WHERE id=?",
                                (_dep_row["id"],),
                            ).fetchone()
                            _res_text = (_res_row["result"] or "") if _res_row else ""
                            if _res_text:
                                _dep_title = _dep_row["title"] or f"Task {_dep_seq}"
                                _dep_blocks.append(
                                    f"=== {_dep_title} ===\n{_res_text[:2000]}"
                                )
                    if _dep_blocks:
                        prompt = (
                            prompt
                            + "\n\n== Results from prerequisite tasks ==\n"
                            + "\n\n".join(_dep_blocks)
                        )

                _emit(project_id, "task_started", task_seq=seq, title=title)
                _set_ptask_status(ptask_id, "running")
                try:
                    task_result = task_runtime.submit_task(
                        prompt,
                        kind="code" if _looks_like_code_task(prompt) else "chat",
                        source="project",
                        assigned_agent_id=agent_id,
                        meta={
                            "project_id": project_id,
                            "project_task_seq": seq,
                            # Bypass content-scan for orchestrator-generated tasks.
                            # The prompt is machine-authored, not user voice/text input.
                            "confidence_score": 0.9,
                        },
                    )
                    submitted_id = task_result["id"]
                except Exception as exc:
                    log.exception("project %s task %d submit failed", project_id, seq)
                    _set_ptask_status(ptask_id, "failed")
                    _emit(project_id, "task_failed", task_seq=seq, error=str(exc))
                    _set_project_status(project_id, "failed")
                    return
                submitted.add(seq)
                in_flight[seq] = {
                    "ptask_id": ptask_id,
                    "submitted_id": submitted_id,
                    "title": title,
                    "deadline": time.monotonic() + _TASK_TIMEOUT,
                }

            # Termination: nothing in flight after submitting all ready tasks.
            if not in_flight:
                unrunnable = set(tasks_by_seq) - submitted
                if unrunnable:
                    # Dependency graph has tasks that can never become ready.
                    _emit(project_id, "project_error",
                          error=f"Tasks with unsatisfiable deps: {sorted(unrunnable)}")
                    _set_project_status(project_id, "failed")
                    return
                break

            # Poll all in-flight tasks.
            time.sleep(_POLL_INTERVAL)

            failed_this_round: list[int] = []

            for seq, info in list(in_flight.items()):
                task_snapshot = task_runtime.get_task(info["submitted_id"])
                timed_out = time.monotonic() > info["deadline"]

                if task_snapshot is None and not timed_out:
                    continue  # Not ready yet.

                if timed_out and (
                    task_snapshot is None
                    or task_snapshot.get("status") not in _TASK_TERMINAL
                ):
                    _set_ptask_status(info["ptask_id"], "failed", info["submitted_id"])
                    _emit(project_id, "task_timeout", task_seq=seq, timeout_s=_TASK_TIMEOUT)
                    failed_this_round.append(seq)
                    del in_flight[seq]
                    continue

                t_status = task_snapshot.get("status", "") if task_snapshot else ""
                if t_status in _TASK_TERMINAL:
                    result_text = task_snapshot.get("result", "") or ""
                    if t_status == "succeeded":
                        _set_ptask_status(info["ptask_id"], "done", info["submitted_id"], result_text)
                        _emit(project_id, "task_done", task_seq=seq, title=info["title"],
                              result_excerpt=result_text[:400])
                        completed.add(seq)
                    else:
                        _set_ptask_status(info["ptask_id"], "failed", info["submitted_id"], result_text)
                        _emit(project_id, "task_failed", task_seq=seq, title=info["title"])
                        failed_this_round.append(seq)
                    del in_flight[seq]

            # Fail-fast: any failure terminates the project; cancel remaining in-flight work.
            if failed_this_round:
                _cancel_in_flight()
                _set_project_status(project_id, "failed")
                return

        _set_project_status(project_id, "done")
        _emit(project_id, "project_done")

    except Exception:
        log.exception("project_manager: unhandled error in executor for %s", project_id)
        try:
            _set_project_status(project_id, "failed")
            _emit(project_id, "project_error", error="unhandled exception in executor")
        except Exception:
            pass
    finally:
        with _LOCK:
            _EXECUTORS.pop(project_id, None)


# ── Project templates ─────────────────────────────────────────────────────────

_TEMPLATES: dict[str, dict] = {
    "security-audit": {
        "title": "Security audit: {target}",
        "agent": "security-reviewer",
        "description": "Automated security review of {target}",
        "tasks": [
            # Scans 0-3 are independent — fan out in parallel.
            {"title": "subprocess scan", "prompt": "Search {target} for subprocess calls with shell=True. For each match report: file, line number, the command string, and whether it processes user-controlled data. Output a bullet list.", "depends_on": []},
            {"title": "eval/exec scan", "prompt": "Search {target} for eval(), exec(), pickle.load(), and yaml.load() usage. For each match report: file, line number, what data flows into it. Output a bullet list.", "depends_on": []},
            {"title": "secrets scan", "prompt": "Search {target} for hardcoded secrets: patterns matching API_KEY, TOKEN, PASSWORD, SECRET not wrapped in os.getenv(). Report file, line, and the variable name. Output a bullet list.", "depends_on": []},
            {"title": "path traversal scan", "prompt": "Check all user-controlled input paths in {target} for path traversal risk. Look for file opens, path joins, or directory listings that use untrusted input without Path.resolve() + prefix validation. Output a bullet list.", "depends_on": []},
            # Summary task depends on all four scans.
            {"title": "summarize findings", "prompt": "Read the project task results for the four parallel security scans just completed on {target}. Consolidate into a final report: (1) Critical findings (2) Medium findings (3) Informational. Add a recommended fix for each Critical item.", "depends_on": [0, 1, 2, 3]},
        ],
    },
    "test-coverage": {
        "title": "Test coverage: {target}",
        "agent": "qa-tester",
        "description": "Add missing test coverage for {target}",
        "tasks": [
            "Read {target} and list all public functions/methods with no corresponding tests",
            "Write pytest unit tests for the top 3 most-used untested functions in {target}",
            "Write edge-case tests: empty input, None values, type errors for {target}",
            "Run the new tests and confirm they all pass",
        ],
    },
    "api-review": {
        "title": "API review: {target}",
        "agent": "backend-engineer",
        "description": "Review and harden API endpoints in {target}",
        "tasks": [
            # Task 0 first: enumerate endpoints so checks 1-3 have a concrete list.
            {"title": "enumerate endpoints", "prompt": "List all API endpoints in {target} with their HTTP methods, route paths, and auth requirements (authenticated vs. public). Output as a markdown table.", "depends_on": []},
            # Checks 1-3 fan out in parallel after the endpoint list is ready.
            {"title": "auth/rate-limit check", "prompt": "Using the endpoint list from the previous task on {target}: identify every endpoint missing authentication or rate limiting. Note the risk level for each.", "depends_on": [0]},
            {"title": "input validation check", "prompt": "Using the endpoint list from the previous task on {target}: check all request body parsing for missing schema validation. Flag endpoints that accept arbitrary JSON or missing required fields.", "depends_on": [0]},
            {"title": "error response check", "prompt": "Using the endpoint list from the previous task on {target}: review error responses. Identify any that leak stack traces, internal paths, or exception messages to the caller.", "depends_on": [0]},
            # Summary depends on all three checks.
            {"title": "summarize recommendations", "prompt": "Synthesize the auth/rate-limit, input-validation, and error-response findings for {target} into a single prioritized recommendation report. Group by severity: Critical / Medium / Low.", "depends_on": [1, 2, 3]},
        ],
    },
    "refactor": {
        "title": "Refactor: {target}",
        "agent": "backend-engineer",
        "description": "Clean up and refactor {target}",
        "tasks": [
            "Read {target} and identify dead code, unused imports, and duplicated logic",
            "List all functions longer than 40 lines that could be split",
            "Identify missing type annotations on public functions",
            "Propose specific refactors with before/after for the top 3 issues",
        ],
    },
    "research": {
        "title": "Research: {target}",
        "agent": "researcher",
        "description": "Research and summarize {target}",
        "tasks": [
            "Research the topic: {target}. Collect key facts, approaches, and tradeoffs.",
            "Compare the top 3 approaches for {target} with pros/cons",
            "Recommend the best approach for the Jarvis codebase and explain why",
        ],
    },
}


def list_templates() -> list[str]:
    return sorted(_TEMPLATES.keys())


def create_from_template(
    template_name: str,
    target: str = "",
    *,
    title_override: str = "",
    agent_override: str = "",
) -> dict:
    """Create a project from a named template, substituting {target} in all strings."""
    if template_name not in _TEMPLATES:
        raise ValueError(f"Unknown template {template_name!r}. Available: {list_templates()}")
    tpl = _TEMPLATES[template_name]

    def _sub(s: str) -> str:
        return s.replace("{target}", target or "the codebase")

    def _sub_task(t: str | dict) -> str | dict:
        if isinstance(t, str):
            return _sub(t)
        return {k: (_sub(v) if isinstance(v, str) else v) for k, v in t.items()}

    title = title_override or _sub(tpl["title"])
    agent = agent_override or tpl["agent"]
    tasks = [_sub_task(t) for t in tpl["tasks"]]
    return create_project(
        title=title,
        description=_sub(tpl["description"]),
        agent_id=agent,
        tasks=tasks,
    )


# ── Routines (scheduled recurring projects) ───────────────────────────────────

def _ensure_routines_schema() -> None:
    _ensure_schema()
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS project_routines (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                template    TEXT NOT NULL DEFAULT '',
                target      TEXT NOT NULL DEFAULT '',
                agent_id    TEXT NOT NULL DEFAULT '',
                tasks_json  TEXT NOT NULL DEFAULT '[]',
                schedule    TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                last_fired_at TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );
        """)


def create_routine(
    name: str,
    schedule: str,
    *,
    template: str = "",
    target: str = "",
    agent_id: str = "",
    tasks: list[str] | None = None,
) -> dict:
    """Create a named recurring project routine.

    schedule: cron expression, e.g. '0 9 * * 1' (Mon 9am) or shorthand
              'daily', 'weekly', 'hourly'.
    Either supply template+target or explicit tasks.
    """
    _ensure_routines_schema()
    if not template and not tasks:
        raise ValueError("Provide --template or --tasks")
    routine_id = f"rt_{uuid.uuid4().hex[:10]}"
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO project_routines (id, name, template, target, agent_id, tasks_json, schedule, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (routine_id, name, template, target, agent_id, json.dumps(tasks or []), schedule, now),
        )
    return get_routine(routine_id)  # type: ignore[return-value]


def get_routine(routine_id: str) -> dict | None:
    _ensure_routines_schema()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM project_routines WHERE id=?", (routine_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["tasks"] = json.loads(d.pop("tasks_json", "[]") or "[]")
    except Exception:
        d["tasks"] = []
    return d


def list_routines() -> list[dict]:
    _ensure_routines_schema()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM project_routines ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["tasks"] = json.loads(d.pop("tasks_json", "[]") or "[]")
        except Exception:
            d["tasks"] = []
        result.append(d)
    return result


def fire_routine(routine_id: str) -> dict:
    """Execute a routine now: create and dispatch a project from it."""
    routine = get_routine(routine_id)
    if routine is None:
        raise ValueError(f"Routine not found: {routine_id}")
    if routine.get("template"):
        proj = create_from_template(
            routine["template"],
            target=routine.get("target", ""),
            agent_override=routine.get("agent_id", ""),
        )
    else:
        proj = create_project(
            title=f"{routine['name']} — {_now()[:10]}",
            agent_id=routine.get("agent_id", "backend-engineer"),
            tasks=routine["tasks"],
        )
    dispatch_project(proj["id"])
    with _connect() as conn:
        conn.execute(
            "UPDATE project_routines SET last_fired_at=? WHERE id=?",
            (_now(), routine_id),
        )
    return proj


def _looks_like_code_task(prompt: str) -> bool:
    lower = prompt.lower()
    code_words = ("implement", "write", "create", "add", "fix", "refactor",
                  "function", "class", "endpoint", "api", "test", "module")
    return any(w in lower for w in code_words)


# ── Public API ────────────────────────────────────────────────────────────────

def create_project(
    title: str,
    description: str = "",
    agent_id: str = "backend-engineer",
    tasks: list[str | dict] | None = None,
    *,
    meta: dict | None = None,
) -> dict:
    """Create a project and its task list. Does not start execution.

    `tasks` is a list of either plain prompt strings or dicts with keys:
      title (optional), prompt (required).

    Returns the full project dict.
    """
    _ensure_schema()
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    now = _now()
    meta_json = _dumps(meta or {})

    normalized_tasks: list[dict] = []
    for i, t in enumerate(tasks or []):
        if isinstance(t, str):
            normalized_tasks.append({"title": f"Task {i + 1}", "prompt": t, "depends_on": ""})
        else:
            raw_deps = t.get("depends_on")
            normalized_tasks.append({
                "title": t.get("title", f"Task {i + 1}"),
                "prompt": t.get("prompt", str(t)),
                # "" = implicit sequential (seq-1); explicit list serialised as JSON.
                "depends_on": json.dumps([int(d) for d in raw_deps]) if raw_deps is not None else "",
            })

    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, description, agent_id, status, created_at, updated_at, meta_json) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (project_id, title, description, agent_id, now, now, meta_json),
        )
        for seq, task in enumerate(normalized_tasks):
            ptask_id = f"pt_{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO project_tasks "
                "(id, project_id, seq, title, prompt, depends_on, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (ptask_id, project_id, seq, task["title"], task["prompt"],
                 task["depends_on"], now, now),
            )

    _emit(project_id, "project_created", title=title, agent_id=agent_id, task_count=len(normalized_tasks))
    return get_project(project_id)  # type: ignore[return-value]


def dispatch_project(project_id: str) -> bool:
    """Start autonomous execution of a project. Idempotent — no-ops if already running/done."""
    _ensure_schema()
    with _LOCK:
        if project_id in _EXECUTORS:
            return False  # Already running.

    with _connect() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"Project not found: {project_id}")
    if row["status"] in _TERMINAL:
        raise ValueError(f"Project is already {row['status']}")

    with _LOCK:
        if project_id in _EXECUTORS:
            return False
        thread = threading.Thread(
            target=_run_project,
            args=(project_id,),
            daemon=True,
            name=f"ProjExec-{project_id}",
        )
        _EXECUTORS[project_id] = thread
    thread.start()
    return True


def cancel_project(project_id: str) -> bool:
    """Request cancellation. The executor thread checks this between tasks."""
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"Project not found: {project_id}")
    if row["status"] in _TERMINAL:
        return False
    with _connect() as conn:
        conn.execute(
            "UPDATE projects SET status='cancelled', updated_at=? WHERE id=?",
            (_now(), project_id),
        )
    _emit(project_id, "cancel_requested")
    return True


def retry_project(project_id: str) -> dict:
    """Re-queue all failed/timed-out tasks in a failed project and re-dispatch.

    Resets the project status to 'pending' and flips any 'failed' project_tasks
    back to 'pending' so the wave scheduler will re-run them.  Tasks that
    already succeeded are left intact and their seqs remain in the 'completed'
    set when the executor resumes.

    Returns the updated project dict.
    Raises ValueError if the project is not found or not in a failed/cancelled state.
    """
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"Project not found: {project_id}")
    if row["status"] not in ("failed", "cancelled"):
        raise ValueError(
            f"retry is only valid for failed or cancelled projects (current status: {row['status']})"
        )
    if project_id in _EXECUTORS:
        raise ValueError("Project executor is still running — cannot retry while in flight")

    with _connect() as conn:
        # Reset failed tasks to pending; leave 'done' tasks untouched.
        conn.execute(
            "UPDATE project_tasks SET status='pending', task_id='', result='', updated_at=? "
            "WHERE project_id=? AND status='failed'",
            (_now(), project_id),
        )
        # Reset project to pending so dispatch_project will accept it.
        conn.execute(
            "UPDATE projects SET status='pending', updated_at=? WHERE id=?",
            (_now(), project_id),
        )
    _emit(project_id, "project_retry")
    dispatch_project(project_id)
    proj = get_project(project_id)
    assert proj is not None
    return proj


def get_project(project_id: str, include_events: int = 0) -> dict | None:
    """Return project dict with tasks and optional recent events.

    Args:
        include_events: If > 0, include the last N events in the result.
    """
    _ensure_schema()
    with _connect() as conn:
        proj_row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if proj_row is None:
            return None
        task_rows = conn.execute(
            "SELECT * FROM project_tasks WHERE project_id=? ORDER BY seq",
            (project_id,),
        ).fetchall()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM project_events WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        recent_events: list[dict] = []
        if include_events > 0:
            ev_rows = conn.execute(
                "SELECT * FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, include_events),
            ).fetchall()
            recent_events = list(reversed([dict(r) for r in ev_rows]))

    tasks = [dict(r) for r in task_rows]
    proj = dict(proj_row)
    try:
        proj["meta"] = json.loads(proj.pop("meta_json", "{}") or "{}")
    except Exception:
        proj["meta"] = {}
    proj["tasks"] = tasks
    proj["event_count"] = event_count
    proj["events"] = recent_events
    proj["running"] = project_id in _EXECUTORS
    return proj


def list_projects(status: str = "", limit: int = 50) -> list[dict]:
    _ensure_schema()
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.pop("meta_json", "{}") or "{}")
        except Exception:
            d["meta"] = {}
        d["running"] = d["id"] in _EXECUTORS
        results.append(d)
    return results


def tail_events(
    project_id: str,
    since_id: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Return events for a project with id > since_id (for SSE polling)."""
    _ensure_schema()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM project_events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
            (project_id, since_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def collect_status() -> list[dict]:
    """Return a monitoring table row per project (for CLI and /dashboard/state)."""
    projects = list_projects(limit=100)
    rows = []
    for p in projects:
        with _connect() as conn:
            counts = conn.execute(
                "SELECT status, COUNT(*) as n FROM project_tasks WHERE project_id=? GROUP BY status",
                (p["id"],),
            ).fetchall()
        count_map: dict[str, int] = {r["status"]: r["n"] for r in counts}
        total = sum(count_map.values())
        done = count_map.get("done", 0)
        rows.append({
            "id": p["id"],
            "title": p["title"][:40],
            "agent": p["agent_id"],
            "status": p["status"],
            "running": p["running"],
            "tasks_total": total,
            "tasks_done": done,
            "tasks_failed": count_map.get("failed", 0),
            "created_at": p["created_at"][:19],
            "updated_at": p["updated_at"][:19],
        })
    return rows


def render_status(rows: list[dict]) -> str:
    if not rows:
        return "[projects] No projects yet. Create one:\n  python3 project_manager.py create 'Title' --agent backend-engineer --tasks 'step 1' 'step 2'"
    header = f"{'ID':<18} {'TITLE':<42} {'AGENT':<22} {'STATUS':<11} {'LIVE':<5} {'PROGRESS':<12} {'UPDATED'}"
    sep = "-" * 130
    lines = [header, sep]
    for r in rows:
        live = "●" if r["running"] else "○"
        prog = f"{r['tasks_done']}/{r['tasks_total']}" if r["tasks_total"] else "0/0"
        if r["tasks_failed"]:
            prog += f" ({r['tasks_failed']}✗)"
        lines.append(
            f"{r['id']:<18} {r['title']:<42} {r['agent']:<22} {r['status']:<11} {live:<5} {prog:<12} {r['updated_at']}"
        )
    active = [r for r in rows if r["status"] not in ("done", "cancelled")]
    if active:
        lines += ["", f"Active: {len(active)}   →  python3 project_manager.py monitor <id>"]
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli_create(args: argparse.Namespace) -> None:
    # --tasks-json takes precedence; it's a JSON array of task dicts that may
    # include 'depends_on' for parallel fan-out.  Falls back to plain --tasks.
    if getattr(args, "tasks_json", None):
        try:
            tasks = json.loads(args.tasks_json)
            if not isinstance(tasks, list) or not tasks:
                raise ValueError("must be a non-empty JSON array")
        except Exception as exc:
            print(f"[error] --tasks-json: {exc}")
            sys.exit(1)
    else:
        tasks = list(args.tasks or [])
        if not tasks:
            print("[error] Provide --tasks or --tasks-json.")
            sys.exit(1)
    proj = create_project(
        title=args.title,
        description=args.description or "",
        agent_id=args.agent,
        tasks=tasks,
    )
    print(f"[created] {proj['id']}  title={proj['title']!r}  agent={proj['agent_id']}  tasks={len(proj['tasks'])}")
    if args.dispatch or getattr(args, "monitor", False):
        dispatch_project(proj["id"])
        print(f"[dispatched] project {proj['id']} is now running.")
    if getattr(args, "monitor", False):
        args.project_id = proj["id"]
        _cli_monitor(args)


def _cli_list(args: argparse.Namespace) -> None:
    print(render_status(collect_status()))


def _cli_dispatch(args: argparse.Namespace) -> None:
    if getattr(args, "all_pending", False):
        rows = list_projects(status="pending")
        if not rows:
            print("[dispatch] No pending projects.")
            return
        for p in rows:
            try:
                started = dispatch_project(p["id"])
                print(f"[{'dispatched' if started else 'no-op'}] {p['id']}  {p['title']!r}")
            except ValueError as exc:
                print(f"[error] {p['id']}: {exc}")
        return
    if not args.project_id:
        print("[error] Provide a project_id or --all-pending.")
        sys.exit(1)
    try:
        started = dispatch_project(args.project_id)
        if started:
            print(f"[dispatched] {args.project_id} — executor thread started.")
        else:
            print(f"[no-op] {args.project_id} is already running or in a terminal state.")
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)
    if getattr(args, "monitor", False):
        _cli_monitor(args)


def _cli_agents(_args: argparse.Namespace) -> None:
    """Print available agent IDs from task_runtime."""
    try:
        import task_runtime
        task_runtime.bootstrap()
        agents = task_runtime.list_agents()
        print(f"{'ID':<30} {'KIND':<12} STATUS")
        print("─" * 60)
        for a in agents:
            print(f"  {a['id']:<28} {a.get('kind','?'):<12} {a.get('status','?')}")
    except Exception as exc:
        print(f"[error] Could not load agents: {exc}")


def _cli_cancel(args: argparse.Namespace) -> None:
    try:
        ok = cancel_project(args.project_id)
        print(f"[{'cancelled' if ok else 'no-op'}] {args.project_id}")
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)


def _cli_retry(args: argparse.Namespace) -> None:
    try:
        proj = retry_project(args.project_id)
        failed_count = sum(1 for t in proj["tasks"] if t["status"] == "failed")
        done_count = sum(1 for t in proj["tasks"] if t["status"] == "done")
        print(f"[retry] {args.project_id} — re-queued {failed_count} failed tasks "
              f"({done_count} already done, kept)")
        if getattr(args, "monitor", False):
            _cli_monitor(args)
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)


def _cli_show(args: argparse.Namespace) -> None:
    proj = get_project(args.project_id, include_events=50)
    if proj is None:
        print(f"[error] Project not found: {args.project_id}")
        sys.exit(1)
    print(json.dumps(proj, indent=2, default=str))


def _cli_results(args: argparse.Namespace) -> None:
    """Print each task's result in human-readable form."""
    proj = get_project(args.project_id)
    if proj is None:
        print(f"[error] Project not found: {args.project_id}")
        sys.exit(1)
    print(f"Project: {proj['id']}  {proj['title']!r}  status={proj['status']}")
    print("─" * 80)
    for t in proj.get("tasks", []):
        seq = t.get("seq", "?")
        title = t.get("title") or f"Task {seq}"
        status = t.get("status", "?")
        result = t.get("result") or ""
        status_icon = {"done": "✓", "failed": "✗", "running": "▶", "pending": "○"}.get(status, "?")
        print(f"\n[{seq}] {status_icon} {title}  ({status})")
        if result:
            # Print full result or truncated if --short flag set.
            limit = getattr(args, "chars", 0) or 0
            if limit and len(result) > limit:
                print(result[:limit] + f"\n  … ({len(result) - limit} chars truncated)")
            else:
                print(result)
        else:
            print("  (no result)")


def _cli_monitor(args: argparse.Namespace) -> None:
    """Live tail of project events. Polls every 2s. Ctrl-C to stop."""
    project_id = args.project_id
    proj = get_project(project_id)
    if proj is None:
        print(f"[error] Project not found: {project_id}")
        sys.exit(1)

    print(f"[monitor] {project_id}  {proj['title']!r}  agent={proj['agent_id']}  status={proj['status']}")
    print("─" * 80)

    since_id = 0
    try:
        while True:
            events = tail_events(project_id, since_id=since_id)
            for ev in events:
                payload = json.loads(ev.get("payload_json") or "{}")
                ts = (ev.get("ts") or "")[:19]
                seq = f"[task {ev['task_seq']}]" if ev.get("task_seq") is not None else ""
                et = ev["event_type"]
                # Render nicely by event type.
                if et == "task_started":
                    print(f"  {ts} {seq} ▶  {payload.get('title', '')}")
                elif et == "task_done":
                    excerpt = payload.get("result_excerpt", "")[:120]
                    print(f"  {ts} {seq} ✓  {payload.get('title', '')}  →  {excerpt}")
                elif et == "task_failed":
                    print(f"  {ts} {seq} ✗  task_failed  {payload.get('error', '')}")
                elif et == "task_timeout":
                    print(f"  {ts} {seq} ⏱  task_timeout  after {payload.get('timeout_s', '?')}s")
                elif et == "project_done":
                    print(f"  {ts} ★  PROJECT DONE")
                elif et == "project_error":
                    print(f"  {ts} ✗  PROJECT ERROR: {payload.get('error', '')}")
                elif et == "project_cancelled":
                    print(f"  {ts} ⊘  PROJECT CANCELLED")
                elif et == "project_retry":
                    print(f"  {ts} ↺  PROJECT RETRY — re-queuing failed tasks")
                elif et == "heartbeat":
                    pass  # suppress heartbeats from monitor output
                elif et == "status_change":
                    print(f"  {ts} →  status={payload.get('status', '')}")
                else:
                    print(f"  {ts} {et}  {json.dumps(payload)[:100]}")
                since_id = max(since_id, ev["id"])

            # Stop tailing if terminal.
            proj_now = get_project(project_id)
            if proj_now and proj_now["status"] in _TERMINAL and not events:
                print(f"\n[monitor] Project is {proj_now['status']}. Exiting.")
                break

            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[monitor] Detached.")


def _cli_status(args: argparse.Namespace) -> None:
    """Alias for list."""
    _cli_list(args)


def _cli_from_template(args: argparse.Namespace) -> None:
    try:
        proj = create_from_template(
            args.template,
            target=args.target or "",
            title_override=args.title or "",
            agent_override=args.agent or "",
        )
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)
    print(f"[created] {proj['id']}  title={proj['title']!r}  agent={proj['agent_id']}  tasks={len(proj['tasks'])}")
    if args.dispatch or getattr(args, "monitor", False):
        dispatch_project(proj["id"])
        print(f"[dispatched] {proj['id']}")
    if getattr(args, "monitor", False):
        args.project_id = proj["id"]
        _cli_monitor(args)


def _cli_templates(_args: argparse.Namespace) -> None:
    print("Available templates:")
    for name, tpl in sorted(_TEMPLATES.items()):
        print(f"  {name:<20} agent={tpl['agent']}  tasks={len(tpl['tasks'])}")
        print(f"    {tpl['description'].replace('{target}', '<target>')}")


def _cli_routine_create(args: argparse.Namespace) -> None:
    try:
        r = create_routine(
            name=args.name,
            schedule=args.schedule,
            template=args.template or "",
            target=args.target or "",
            agent_id=args.agent or "",
            tasks=list(args.tasks or []),
        )
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)
    print(f"[routine created] {r['id']}  name={r['name']!r}  schedule={r['schedule']!r}")


def _cli_routine_list(_args: argparse.Namespace) -> None:
    routines = list_routines()
    if not routines:
        print("[routines] No routines. Create one:\n  python3 project_manager.py routine create 'Daily scan' --schedule daily --template security-audit --target api.py")
        return
    print(f"{'ID':<14} {'NAME':<30} {'TEMPLATE':<18} {'SCHEDULE':<14} {'EN':<3} LAST FIRED")
    print("─" * 95)
    for r in routines:
        en = "✓" if r["enabled"] else "✗"
        fired = (r.get("last_fired_at") or "never")[:19]
        print(f"  {r['id']:<12} {r['name']:<30} {r.get('template','custom'):<18} {r['schedule']:<14} {en:<3} {fired}")


def _cli_routine_fire(args: argparse.Namespace) -> None:
    try:
        proj = fire_routine(args.routine_id)
        print(f"[fired] {args.routine_id} → project {proj['id']}  dispatched")
    except ValueError as exc:
        print(f"[error] {exc}")
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="project_manager", description="Assign tasks/projects to agents and monitor them.")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    c = sub.add_parser("create", help="Create a project")
    c.add_argument("title")
    c.add_argument("--description", default="")
    c.add_argument("--agent", default="backend-engineer",
                   help="Agent ID from task_runtime (e.g. backend-engineer, security-reviewer)")
    c.add_argument("--tasks", nargs="*", metavar="PROMPT",
                   help="One or more task prompts (in order)")
    c.add_argument("--tasks-json", default="", metavar="JSON",
                   help='JSON array of task dicts with optional depends_on for parallel fan-out, '
                        'e.g. \'[{"prompt":"...","depends_on":[]},{"prompt":"...","depends_on":[]}]\'')
    c.add_argument("--dispatch", action="store_true", help="Start execution immediately after creation")
    c.add_argument("--monitor", action="store_true", help="Tail events until project completes (implies --dispatch)")

    sub.add_parser("list", help="List all projects (status table)")
    sub.add_parser("status", help="Alias for list")

    sh = sub.add_parser("show", help="Show project detail (JSON)")
    sh.add_argument("project_id")

    rs = sub.add_parser("results", help="Show task results in human-readable format")
    rs.add_argument("project_id")
    rs.add_argument("--chars", type=int, default=0, metavar="N",
                    help="Truncate each result to N chars (default: show full)")

    d = sub.add_parser("dispatch", help="Start autonomous execution of a project (or all pending)")
    d.add_argument("project_id", nargs="?", default=None)
    d.add_argument("--all-pending", action="store_true", help="Dispatch all projects in pending state")
    d.add_argument("--monitor", action="store_true", help="Keep process alive and tail events until project completes")

    ca = sub.add_parser("cancel", help="Cancel a running project")
    ca.add_argument("project_id")

    re_p = sub.add_parser("retry", help="Re-queue failed tasks in a failed/cancelled project and re-dispatch")
    re_p.add_argument("project_id")
    re_p.add_argument("--monitor", action="store_true", help="Tail events until project completes")

    m = sub.add_parser("monitor", help="Live tail of project events")
    m.add_argument("project_id")

    sub.add_parser("agents", help="List available agent IDs")
    sub.add_parser("templates", help="List available project templates")

    ft = sub.add_parser("from-template", help="Create a project from a template")
    ft.add_argument("template", choices=list(_TEMPLATES.keys()))
    ft.add_argument("--target", default="", help="Target file, module, or topic to substitute into prompts")
    ft.add_argument("--title", default="", help="Override project title")
    ft.add_argument("--agent", default="", help="Override agent ID")
    ft.add_argument("--dispatch", action="store_true", help="Dispatch immediately after creation")
    ft.add_argument("--monitor", action="store_true", help="Tail events until project completes (implies --dispatch)")

    # Routines subcommand group.
    rt = sub.add_parser("routine", help="Manage recurring scheduled projects")
    rt_sub = rt.add_subparsers(dest="routine_command")

    rtc = rt_sub.add_parser("create", help="Create a routine")
    rtc.add_argument("name")
    rtc.add_argument("--schedule", required=True,
                     help="Cron expression or 'daily'/'weekly'/'hourly'")
    rtc.add_argument("--template", default="", help="Template name (e.g. security-audit)")
    rtc.add_argument("--target", default="", help="Target for template substitution")
    rtc.add_argument("--agent", default="", help="Override agent ID")
    rtc.add_argument("--tasks", nargs="+", metavar="PROMPT")

    rt_sub.add_parser("list", help="List all routines")

    rtf = rt_sub.add_parser("fire", help="Fire a routine immediately")
    rtf.add_argument("routine_id")

    args = p.parse_args(argv)

    _ensure_schema()

    dispatch_table = {
        "create": _cli_create,
        "list": _cli_list,
        "status": _cli_status,
        "show": _cli_show,
        "dispatch": _cli_dispatch,
        "cancel": _cli_cancel,
        "retry": _cli_retry,
        "monitor": _cli_monitor,
        "agents": _cli_agents,
        "templates": _cli_templates,
        "from-template": _cli_from_template,
        "results": _cli_results,
    }

    # Routine subcommands.
    if args.command == "routine":
        rt_dispatch = {
            "create": _cli_routine_create,
            "list": _cli_routine_list,
            "fire": _cli_routine_fire,
        }
        fn = rt_dispatch.get(getattr(args, "routine_command", None) or "")
        if fn is None:
            rt.print_help()
            sys.exit(1)
        fn(args)
        return
    fn = dispatch_table.get(args.command)
    if fn is None:
        p.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
