"""
Jarvis Event Bus — SQLite backend.

Drop-in HTTP API replacement for infra/event_bus.py.
Uses task_persistence (SQLite) instead of redis.asyncio — no external broker needed.

Stream layout mirrored in SQLite:
  tasks table, status='queued'      → submitted tasks
  tasks table, status='assigned'    → claimed by scheduler, routed to agent queue
  tasks table, status='succeeded'   → completed
  tasks table, status='waiting_approval' → needs human review
  tasks table, status='cancelled'   → dismissed

A background asyncio task (the scheduler) claims queued tasks and
pushes them into per-agent in-memory asyncio.Queues. The inbox SSE
endpoint long-polls its queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

import task_persistence

log = logging.getLogger("jarvis.event_bus_sqlite")

# ── Per-agent in-memory delivery queues ───────────────────────────────────────
# Scheduler → pushes tasks here. Inbox endpoint ← dequeues them.
_AGENT_QUEUES: dict[str, asyncio.Queue] = {}


def _agent_queue(name: str) -> asyncio.Queue:
    if name not in _AGENT_QUEUES:
        _AGENT_QUEUES[name] = asyncio.Queue(maxsize=256)
    return _AGENT_QUEUES[name]


# ── Think-tag stripping ────────────────────────────────────────────────────────
_THINK_RE = re.compile(
    r"<think>.*?</think>|<thinking>.*?</thinking>|<\|thinking\|>.*?<\|/thinking\|>",
    re.DOTALL | re.IGNORECASE,
)


def strip_think_tags(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _sanitize_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return strip_think_tags(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_recursive(i) for i in obj]
    return obj


class ThinkTagMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "application/json" not in ct:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            data = json.loads(body)
            data = _sanitize_recursive(data)
            body = json.dumps(data).encode()
        except (json.JSONDecodeError, ValueError):
            pass
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=ct,
        )


# ── Background scheduler ───────────────────────────────────────────────────────
_SCHEDULER_POLL = float(os.getenv("JARVIS_SCHEDULER_POLL_INTERVAL", "0.25"))
_scheduler_running = False


async def _scheduler_loop() -> None:
    global _scheduler_running
    _scheduler_running = True
    log.info("[SQLite bus] Scheduler started (poll=%.2fs)", _SCHEDULER_POLL)
    while _scheduler_running:
        try:
            task = await asyncio.to_thread(task_persistence.claim_oldest_queued_task)
            if task:
                agent_name = str(task.get("agent") or "backend_engineer")
                queue = _agent_queue(agent_name)
                if not queue.full():
                    await queue.put(task)
                    log.debug("[SQLite bus] Routed task %s → agent:%s", task.get("id"), agent_name)
                else:
                    # Queue full — un-claim (put back to queued) so it is retried
                    task["status"] = "queued"
                    await asyncio.to_thread(task_persistence.upsert_task, task)
            else:
                await asyncio.sleep(_SCHEDULER_POLL)
        except Exception:
            log.exception("[SQLite bus] Scheduler loop error")
            await asyncio.sleep(1)


# ── App lifecycle ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    asyncio.create_task(_scheduler_loop())
    yield
    global _scheduler_running
    _scheduler_running = False


app = FastAPI(title="Jarvis Event Bus (SQLite)", lifespan=lifespan)
app.add_middleware(ThinkTagMiddleware)


# ── Pydantic models ────────────────────────────────────────────────────────────
class TaskRequest(BaseModel):
    title: str
    description: str = ""
    agent: str = "backend_engineer"
    priority: int = Field(default=5, ge=1, le=10)
    context: dict = Field(default_factory=dict)


class TaskResult(BaseModel):
    task_id: str
    output: str
    needs_review: bool = False


class ApprovalDecision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str = ""


# ── Security helpers ───────────────────────────────────────────────────────────
_RISKY_KEYWORDS: frozenset[str] = frozenset({
    "shell", "deploy", "push", "delete", "drop", "rm", "exec",
})


def _inline_threat_screen(req: TaskRequest) -> str | None:
    payload_text = f"{req.title}\n{req.description}"
    try:
        from infra.threat_screen import screen_payload
        result = screen_payload(payload_text)
        if result.blocked:
            reasons = "; ".join(f["description"] for f in result.findings[:3])
            return reasons or "threat screen blocked"
    except ImportError:
        pass
    source = (req.context.get("source") or "").lower()
    if source in {"webhook", "external"}:
        lower = payload_text.lower()
        for kw in _RISKY_KEYWORDS:
            if kw in lower:
                return f"risky keyword '{kw}' in external task payload"
    return None


def _approval_token_authorized(request: Request) -> bool:
    expected = os.getenv("JARVIS_EVENT_BUS_APPROVAL_TOKEN", "").strip()
    if not expected:
        log.warning(
            "JARVIS_EVENT_BUS_APPROVAL_TOKEN not set — all approval requests rejected"
        )
        return False
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        supplied = bearer[7:].strip()
    else:
        supplied = request.headers.get("X-Jarvis-Token", "").strip()
    return bool(supplied) and supplied == expected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    db_ok = task_persistence._ensure_schema()
    return {"status": "ok" if db_ok else "degraded", "backend": "sqlite"}


@app.post("/tasks", status_code=202)
async def create_task(req: TaskRequest):
    block_reason = _inline_threat_screen(req)
    if block_reason:
        log.warning("[SQLite bus] Task blocked by threat screen: %s", block_reason)
        return JSONResponse(status_code=202, content={
            "queued": False,
            "status": "waiting_approval",
            "reason": block_reason,
        })

    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = _now()
    task = {
        "id": task_id,
        "status": "queued",
        "agent": req.agent,
        "title": req.title,
        "description": req.description,
        "priority": req.priority,
        "context": req.context,
        "created_at": now,
        "updated_at": now,
        "finished_at": "",
        "result": "",
    }
    await asyncio.to_thread(task_persistence.upsert_task, task)
    log.info("[SQLite bus] Queued task %s → agent:%s", task_id, req.agent)
    return {"task_id": task_id, "agent": req.agent, "status": "queued"}


@app.get("/tasks/{task_id}/status")
async def task_status(task_id: str):
    snapshot = await asyncio.to_thread(task_persistence.load_snapshot, 500)
    task = next((t for t in snapshot.get("tasks", []) if t.get("id") == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="task_id not found in recent history")
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "agent": task.get("agent"),
    }


@app.post("/results")
async def post_result(res: TaskResult):
    output = strip_think_tags(res.output)
    new_status = "waiting_approval" if res.needs_review else "succeeded"
    await asyncio.to_thread(
        task_persistence.update_task_status, res.task_id, new_status, result=output
    )
    return {"status": "queued"}


@app.get("/approvals/pending")
async def pending_approvals():
    tasks = await asyncio.to_thread(
        task_persistence.list_tasks_with_status, "waiting_approval"
    )
    return tasks


@app.get("/approvals/{task_id}")
async def get_approval(task_id: str):
    tasks = await asyncio.to_thread(
        task_persistence.list_tasks_with_status, "waiting_approval"
    )
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="approval not found")
    return task


@app.post("/approvals/{task_id}")
async def decide_approval(task_id: str, body: ApprovalDecision, request: Request):
    if not _approval_token_authorized(request):
        raise HTTPException(status_code=401, detail="approval endpoint requires authentication")
    new_status = "queued" if body.decision == "approve" else "failed"
    await asyncio.to_thread(task_persistence.update_task_status, task_id, new_status)
    log.info("[SQLite bus] Approval %s for task %s: %s", body.decision, task_id, body.reason)
    return {"status": body.decision, "task_id": task_id}


@app.delete("/approvals/{task_id}")
async def dismiss_approval(task_id: str, request: Request):
    if not _approval_token_authorized(request):
        raise HTTPException(status_code=401, detail="approval endpoint requires authentication")
    await asyncio.to_thread(task_persistence.update_task_status, task_id, "cancelled")
    return {"status": "dismissed"}


@app.get("/agent/{agent_name}/inbox")
async def agent_inbox_stream(agent_name: str, timeout_ms: int = 5000):
    async def generate() -> AsyncGenerator[bytes, None]:
        queue = _agent_queue(agent_name)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            try:
                task = queue.get_nowait()
                payload = json.dumps({
                    "type": "task",
                    "task_id": task.get("id"),
                    "task": _sanitize_recursive(task),
                })
                yield f"data: {payload}\n\n".encode()
                return
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
        yield b'data: {"type":"heartbeat"}\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/metrics")
async def metrics():
    snapshot = await asyncio.to_thread(task_persistence.load_snapshot, 500)
    counts: dict[str, int] = {}
    for task in snapshot.get("tasks", []):
        s = str(task.get("status", "unknown"))
        counts[s] = counts.get(s, 0) + 1
    return {"task_counts": counts, "backend": "sqlite"}
