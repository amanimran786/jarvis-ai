from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import conversation_context as ctx
import evals
from router import route_stream
import task_persistence
import usage_tracker
import worktree_manager


_LOCK = threading.RLock()
_EXECUTION_LOCK = threading.Lock()

_AGENTS: dict[str, dict[str, Any]] = {}
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_EVENTS: dict[str, list[dict[str, Any]]] = {}
_TASK_THREADS: dict[str, threading.Thread] = {}

_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}

_TERSE_PREFIXES = {
    "lite": "CAVEMAN LITE",
    "full": "CAVEMAN FULL",
    "ultra": "CAVEMAN ULTRA",
}

_BOOTSTRAPPED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _default_agents() -> list[dict[str, Any]]:
    return [
        {
            "id": "chat-router",
            "label": "Chat Router",
            "kind": "system",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["chat", "routing", "reasoning"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "router", "mode": "daemon"},
        },
        {
            "id": "meeting-assist",
            "label": "Meeting Assist",
            "kind": "system",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["transcript", "suggestion", "call_assist"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "meeting_listener", "mode": "background"},
        },
        {
            "id": "knowledge-vault",
            "label": "Knowledge Vault",
            "kind": "system",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["vault", "memory", "grounding"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "vault", "mode": "daemon"},
        },
        {
            "id": "bridge",
            "label": "Bridge",
            "kind": "system",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["bridge", "devices", "remote_access"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "hardware", "mode": "daemon"},
        },
    ]


def _drain_task_threads(timeout: float = 0.4) -> None:
    with _LOCK:
        threads = list(_TASK_THREADS.values())
        _TASK_THREADS.clear()
    current = threading.current_thread()
    for thread in threads:
        if not thread or thread is current:
            continue
        if thread.is_alive():
            try:
                thread.join(timeout=timeout)
            except Exception:
                pass


def bootstrap(force_reset: bool = False) -> None:
    global _BOOTSTRAPPED
    if force_reset:
        _drain_task_threads()
    with _LOCK:
        if force_reset:
            _AGENTS.clear()
            _TASKS.clear()
            _TASK_EVENTS.clear()
            _BOOTSTRAPPED = False
            task_persistence.reset_for_tests()
        if _BOOTSTRAPPED:
            return
        for agent in _default_agents():
            _AGENTS[agent["id"]] = agent
        persisted = task_persistence.load_snapshot()
        for task in persisted.get("tasks", []):
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            _TASKS[task_id] = task
            _TASK_EVENTS[task_id] = [_copy(event) for event in persisted.get("events", {}).get(task_id, [])]

        for task_id, task in list(_TASKS.items()):
            if task.get("status") in _TERMINAL_TASK_STATUSES:
                continue
            task.update(
                status="failed",
                error="daemon_restart",
                finished_at=_now(),
                updated_at=_now(),
            )
            _persist_task(task_id)
            _append_event(task_id, "error", status="failed", error="daemon_restart", reason="daemon_restart")
            agent_id = task.get("assigned_agent_id", "")
            if agent_id:
                _touch_agent(agent_id, status="idle", current_task_id="", last_error="daemon_restart")
        _BOOTSTRAPPED = True


def reset_for_tests() -> None:
    bootstrap(force_reset=True)


def _append_event(task_id: str, event_type: str, **payload: Any) -> dict[str, Any]:
    events = _TASK_EVENTS.setdefault(task_id, [])
    event = {
        "task_id": task_id,
        "type": event_type,
        "ts": _now(),
        **payload,
    }
    events.append(event)
    task_persistence.append_event(event, len(events) - 1)
    return event


def _persist_task(task_id: str) -> None:
    task = _TASKS.get(task_id)
    if not task:
        return
    task_persistence.upsert_task(_copy(task))


def _touch_agent(agent_id: str, *, status: str | None = None, current_task_id: str | None = None, last_error: str | None = None) -> None:
    agent = _AGENTS.get(agent_id)
    if not agent:
        return
    if status is not None:
        agent["status"] = status
    if current_task_id is not None:
        agent["current_task_id"] = current_task_id
    if last_error is not None:
        agent["last_error"] = last_error
    agent["last_heartbeat_at"] = _now()


def _choose_agent(kind: str, requested_agent_id: str = "") -> str:
    if requested_agent_id and requested_agent_id in _AGENTS:
        return requested_agent_id
    normalized = (kind or "chat").strip().lower()
    if normalized == "meeting":
        return "meeting-assist"
    if normalized in {"knowledge", "memory", "vault"}:
        return "knowledge-vault"
    if normalized in {"bridge", "devices"}:
        return "bridge"
    return "chat-router"


def _normalize_terse_mode(value: str = "") -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"", "off", "none", "false"}:
        return ""
    if normalized in {"terse", "default", "on", "true"}:
        return "full"
    if normalized in _TERSE_PREFIXES:
        return normalized
    return ""


def _task_prompt(prompt: str, terse_mode: str = "") -> str:
    normalized = _normalize_terse_mode(terse_mode)
    if not normalized:
        return prompt
    prefix = _TERSE_PREFIXES[normalized]
    return f"{prefix}: {prompt}"


def _should_isolate_workspace(kind: str, isolated_workspace: bool | None) -> bool:
    if isolated_workspace is not None:
        return bool(isolated_workspace)
    normalized = (kind or "").strip().lower()
    return normalized in {"code", "coding", "review", "refactor", "fix", "implementation"}


def list_agents() -> list[dict[str, Any]]:
    bootstrap()
    with _LOCK:
        return [_copy(agent) for agent in _AGENTS.values()]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    bootstrap()
    with _LOCK:
        agent = _AGENTS.get(agent_id)
        return _copy(agent) if agent else None


def list_tasks(limit: int = 25, status: str = "") -> list[dict[str, Any]]:
    bootstrap()
    with _LOCK:
        tasks = list(_TASKS.values())
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return [_copy(task) for task in tasks[: max(limit, 1)]]


def get_task(task_id: str) -> dict[str, Any] | None:
    bootstrap()
    with _LOCK:
        task = _TASKS.get(task_id)
        return _copy(task) if task else None


def get_task_events(task_id: str) -> list[dict[str, Any]]:
    bootstrap()
    with _LOCK:
        return [_copy(event) for event in _TASK_EVENTS.get(task_id, [])]


def _set_task_status(task_id: str, status: str, **updates: Any) -> None:
    task = _TASKS.get(task_id)
    if not task:
        return
    task["status"] = status
    task.update(updates)
    task["updated_at"] = _now()
    _append_event(task_id, "status", status=status, updates=updates)
    _persist_task(task_id)


def _complete_task(task_id: str, *, response: str, model: str, usage: dict[str, Any], interaction_id: str, source: str) -> None:
    task = _TASKS[task_id]
    task.update(
        status="succeeded",
        result=response,
        model=model,
        error="",
        interaction_id=interaction_id,
        usage=_copy(usage),
        finished_at=_now(),
        updated_at=_now(),
        source=source,
    )
    _append_event(
        task_id,
        "meta",
        status="succeeded",
        model=model,
        interaction_id=interaction_id,
        usage=_copy(usage),
    )
    _persist_task(task_id)


def _fail_task(task_id: str, error: str) -> None:
    task = _TASKS[task_id]
    task.update(
        status="failed",
        error=error,
        finished_at=_now(),
        updated_at=_now(),
    )
    _append_event(task_id, "error", status="failed", error=error)
    _persist_task(task_id)


def _run_task(task_id: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        agent_id = task["assigned_agent_id"]
        _set_task_status(task_id, "assigned", assigned_at=_now())
        _touch_agent(agent_id, status="busy", current_task_id=task_id, last_error="")

    try:
        with _EXECUTION_LOCK:
            with _LOCK:
                task = _TASKS[task_id]
                if task.get("cancel_requested"):
                    _set_task_status(task_id, "cancelled", finished_at=_now())
                    return
                _set_task_status(task_id, "running", started_at=_now())
                prompt = task.get("effective_prompt") or task["prompt"]
                original_prompt = task["prompt"]
                source = task["source"]

            start_seq = usage_tracker.current_seq()
            stream, model = route_stream(prompt)
            chunks: list[str] = []
            for index, chunk in enumerate(stream):
                with _LOCK:
                    task = _TASKS[task_id]
                    if task.get("cancel_requested"):
                        _set_task_status(task_id, "cancelled", finished_at=_now())
                        return
                    if task["status"] != "streaming":
                        _set_task_status(task_id, "streaming")
                chunks.append(chunk)
                _append_event(task_id, "chunk", chunk=chunk, index=index, model=model)

            response = "".join(chunks)
            usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
            context_stats = ctx.record_request_stats(model, source=f"task:{source}")
            interaction = evals.log_interaction(original_prompt, response, model, source=f"task:{source}", context=context_stats)
            evals.maybe_log_automatic_failure(interaction)

            with _LOCK:
                _complete_task(
                    task_id,
                    response=response,
                    model=model,
                    usage=usage,
                    interaction_id=interaction["id"],
                    source=source,
                )
    except Exception as exc:
        with _LOCK:
            _fail_task(task_id, str(exc))
    finally:
        with _LOCK:
            task = _TASKS.get(task_id, {})
            agent_id = task.get("assigned_agent_id", "")
            if agent_id:
                error = task.get("error", "") if task.get("status") == "failed" else ""
                _touch_agent(agent_id, status="idle", current_task_id="", last_error=error)
            _TASK_THREADS.pop(task_id, None)


def submit_task(
    prompt: str,
    *,
    kind: str = "chat",
    source: str = "api",
    assigned_agent_id: str = "",
    terse_mode: str = "",
    isolated_workspace: bool | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bootstrap()
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    chosen_agent_id = _choose_agent(kind, assigned_agent_id)
    created_at = _now()
    normalized_kind = (kind or "chat").strip().lower() or "chat"
    normalized_terse_mode = _normalize_terse_mode(terse_mode)
    workspace = worktree_manager.prepare_isolated_workspace(
        task_id,
        prompt,
        enabled=_should_isolate_workspace(normalized_kind, isolated_workspace),
    )
    task = {
        "id": task_id,
        "kind": normalized_kind,
        "source": source or "api",
        "status": "queued",
        "prompt": prompt,
        "effective_prompt": _task_prompt(prompt, normalized_terse_mode),
        "assigned_agent_id": chosen_agent_id,
        "created_at": created_at,
        "assigned_at": "",
        "started_at": "",
        "finished_at": "",
        "updated_at": created_at,
        "result": "",
        "model": "",
        "error": "",
        "interaction_id": "",
        "usage": {},
        "cancel_requested": False,
        "terse_mode": normalized_terse_mode,
        "workspace": _copy(workspace),
        "meta": _copy(meta or {}),
    }
    with _LOCK:
        _TASKS[task_id] = task
        _TASK_EVENTS[task_id] = []
        _persist_task(task_id)
        _append_event(
            task_id,
            "status",
            status="queued",
            agent_id=chosen_agent_id,
            kind=task["kind"],
            source=task["source"],
            terse_mode=normalized_terse_mode,
        )
        if workspace.get("enabled"):
            _append_event(task_id, "workspace", workspace=_copy(workspace))
        thread = threading.Thread(target=_run_task, args=(task_id,), daemon=True, name=f"JarvisTask-{task_id}")
        _TASK_THREADS[task_id] = thread
        thread.start()
        return _copy(task)


def cancel_task(task_id: str) -> dict[str, Any] | None:
    bootstrap()
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        if task["status"] in _TERMINAL_TASK_STATUSES:
            return _copy(task)
        task["cancel_requested"] = True
        _append_event(task_id, "status", status="cancel_requested")
        _persist_task(task_id)
        if task["status"] == "queued":
            _set_task_status(task_id, "cancelled", finished_at=_now())
            agent_id = task.get("assigned_agent_id", "")
            if agent_id:
                _touch_agent(agent_id, status="idle", current_task_id="")
        return _copy(task)


def wait_for_task(task_id: str, timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_task(task_id)
        if not task:
            return None
        if task.get("status") in _TERMINAL_TASK_STATUSES:
            return task
        time.sleep(0.05)
    return get_task(task_id)


def stream_task_events(task_id: str, poll_interval: float = 0.1):
    seen = 0
    while True:
        with _LOCK:
            events = list(_TASK_EVENTS.get(task_id, []))
            task = _TASKS.get(task_id)
        if task is None:
            yield {"task_id": task_id, "type": "error", "error": "task_not_found", "ts": _now()}
            return
        while seen < len(events):
            yield _copy(events[seen])
            seen += 1
        if task.get("status") in _TERMINAL_TASK_STATUSES and seen >= len(events):
            yield {"task_id": task_id, "type": "done", "status": task.get("status"), "ts": _now()}
            return
        time.sleep(poll_interval)


def runtime_snapshot() -> dict[str, Any]:
    bootstrap()
    with _LOCK:
        tasks = list(_TASKS.values())
        active_tasks = [
            _copy(task)
            for task in tasks
            if task.get("status") not in _TERMINAL_TASK_STATUSES
        ]
        return {
            "agents": [_copy(agent) for agent in _AGENTS.values()],
            "task_counts": {
                "total": len(tasks),
                "queued": sum(1 for task in tasks if task.get("status") == "queued"),
                "running": sum(1 for task in tasks if task.get("status") in {"assigned", "running", "streaming"}),
                "succeeded": sum(1 for task in tasks if task.get("status") == "succeeded"),
                "failed": sum(1 for task in tasks if task.get("status") == "failed"),
                "cancelled": sum(1 for task in tasks if task.get("status") == "cancelled"),
            },
            "isolated_workspace_count": sum(
                1
                for task in tasks
                if (task.get("workspace") or {}).get("enabled")
            ),
            "active_tasks": active_tasks,
            "recent_tasks": [_copy(task) for task in sorted(tasks, key=lambda item: item.get("created_at", ""), reverse=True)[:10]],
        }


bootstrap()
