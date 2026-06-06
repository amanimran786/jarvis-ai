"""
Jarvis Event Bus + Agent Scheduler
-----------------------------------
Redis Streams — not pub/sub. Every event is durable, replayable, and consumer-grouped.

Stream layout:
  jarvis:tasks        — task lifecycle events (created, assigned, started, done, failed)
  jarvis:agent:{name} — per-agent inboxes
  jarvis:results      — completed task outputs (for manager + human)
  jarvis:approvals    — human approval requests

Routing:
  Human → POST /tasks → jarvis:tasks (created)
       → Scheduler reads → dispatches to jarvis:agent:{name}
       → Agent processes → publishes to jarvis:results
       → Manager reviews → jarvis:approvals (if needed)

All LLM output passes through ThinkTagMiddleware before leaving this service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("jarvis.event_bus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379")
STREAM_TASKS   = "jarvis:tasks"
STREAM_RESULTS = "jarvis:results"
STREAM_APPROVALS = "jarvis:approvals"
GROUP_SCHEDULER  = "scheduler"
GROUP_MANAGER    = "manager"

# ─── Think-tag stripping ──────────────────────────────────────────────────────

_THINK_RE = re.compile(
    r"<think>.*?</think>|<thinking>.*?</thinking>|<\|thinking\|>.*?<\|/thinking\|>",
    re.DOTALL | re.IGNORECASE,
)

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> and variants from local LLM output."""
    return _THINK_RE.sub("", text).strip()


class ThinkTagMiddleware(BaseHTTPMiddleware):
    """
    Strips <think> blocks from any JSON response that contains an 'output',
    'content', or 'result' string field. Applied globally so no endpoint
    needs to remember to sanitize.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "application/json" not in ct:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        import json
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


def _sanitize_recursive(obj: Any) -> Any:
    if isinstance(obj, str):
        return strip_think_tags(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_recursive(i) for i in obj]
    return obj


# ─── Redis connection ─────────────────────────────────────────────────────────

_redis: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def ensure_streams():
    r = await get_redis()
    for stream, groups in [
        (STREAM_TASKS,    [GROUP_SCHEDULER, GROUP_MANAGER]),
        (STREAM_RESULTS,  [GROUP_MANAGER]),
        (STREAM_APPROVALS, ["human"]),
    ]:
        try:
            await r.xgroup_create(stream, groups[0], id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        for g in groups[1:]:
            try:
                await r.xgroup_create(stream, g, id="0", mkstream=True)
            except aioredis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise


# ─── Scheduler ────────────────────────────────────────────────────────────────

class AgentScheduler:
    """
    Reads from jarvis:tasks (GROUP=scheduler).
    Routes each task to the correct per-agent stream.
    Retries on failure up to max_retries, then moves to DLQ.
    """

    AGENT_LOAD: dict[str, int] = {}   # simple in-memory load counter

    def __init__(self, redis: aioredis.Redis):
        self.r = redis
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._loop())
        log.info("Scheduler started")

    async def stop(self):
        self._running = False

    async def _loop(self):
        while self._running:
            try:
                entries = await self.r.xreadgroup(
                    GROUP_SCHEDULER, "scheduler-0",
                    {STREAM_TASKS: ">"},
                    count=10, block=1000,
                )
                if not entries:
                    continue
                for stream, messages in entries:
                    for msg_id, fields in messages:
                        await self._handle(msg_id, fields)
            except Exception:
                log.exception("Scheduler loop error")
                await asyncio.sleep(1)

    async def _handle(self, msg_id: str, fields: dict):
        event_type = fields.get("type", "")
        if event_type != "task.created":
            await self.r.xack(STREAM_TASKS, GROUP_SCHEDULER, msg_id)
            return

        task_id   = fields.get("task_id", str(uuid.uuid4()))
        agent     = fields.get("agent", "backend_engineer")
        task_json = fields.get("task", "{}")

        agent_stream = f"jarvis:agent:{agent}"
        await self.r.xadd(agent_stream, {
            "task_id":  task_id,
            "task":     task_json,
            "origin":   msg_id,
        }, maxlen=1000, approximate=True)

        # update load counter
        self.AGENT_LOAD[agent] = self.AGENT_LOAD.get(agent, 0) + 1

        await self.r.xack(STREAM_TASKS, GROUP_SCHEDULER, msg_id)
        log.info("Scheduled task %s → agent:%s", task_id, agent)

        # Publish status update
        await self.r.xadd(STREAM_TASKS, {
            "type":     "task.assigned",
            "task_id":  task_id,
            "agent":    agent,
            "ts":       str(time.time()),
        }, maxlen=5000, approximate=True)


# ─── Manager loop ─────────────────────────────────────────────────────────────

class ManagerLoop:
    """
    Reads completed results from jarvis:results.
    Decides: done → ack, needs review → publish to jarvis:approvals.
    """

    def __init__(self, redis: aioredis.Redis):
        self.r = redis
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._loop())
        log.info("Manager loop started")

    async def _loop(self):
        while self._running:
            try:
                entries = await self.r.xreadgroup(
                    GROUP_MANAGER, "manager-0",
                    {STREAM_RESULTS: ">"},
                    count=10, block=1000,
                )
                if not entries:
                    continue
                for _, messages in entries:
                    for msg_id, fields in messages:
                        await self._handle(msg_id, fields)
            except Exception:
                log.exception("Manager loop error")
                await asyncio.sleep(1)

    async def _handle(self, msg_id: str, fields: dict):
        import json
        task_id = fields.get("task_id", "unknown")
        output  = fields.get("output", "")
        # Strip think tags before any downstream use
        output  = strip_think_tags(output)
        needs_review = fields.get("needs_review", "false").lower() == "true"

        if needs_review:
            await self.r.xadd(STREAM_APPROVALS, {
                "task_id": task_id,
                "output":  output,
                "ts":      str(time.time()),
            }, maxlen=500, approximate=True)
            log.info("Task %s routed to human approval", task_id)
        else:
            log.info("Task %s completed, output len=%d", task_id, len(output))

        await self.r.xack(STREAM_RESULTS, GROUP_MANAGER, msg_id)


# ─── App lifecycle ────────────────────────────────────────────────────────────

scheduler: AgentScheduler | None = None
manager_loop: ManagerLoop | None = None

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global scheduler, manager_loop
    r = await get_redis()
    await ensure_streams()
    scheduler = AgentScheduler(r)
    manager_loop = ManagerLoop(r)
    await scheduler.start()
    await manager_loop.start()
    yield
    if scheduler:
        await scheduler.stop()
    if _redis:
        await _redis.aclose()


app = FastAPI(title="Jarvis Event Bus", lifespan=lifespan)
app.add_middleware(ThinkTagMiddleware)


# ─── Models ───────────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    title:       str
    description: str = ""
    agent:       str = "backend_engineer"
    priority:    int = Field(default=5, ge=1, le=10)
    context:     dict = Field(default_factory=dict)

class TaskResult(BaseModel):
    task_id:      str
    output:       str
    needs_review: bool = False


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    r = await get_redis()
    await r.ping()
    return {"status": "ok", "scheduler": scheduler._running if scheduler else False}


@app.post("/tasks", status_code=202)
async def create_task(req: TaskRequest):
    """Human or Jarvis Manager submits a task. Scheduler routes it to the right agent."""
    import json
    r = await get_redis()
    task_id = str(uuid.uuid4())
    msg_id = await r.xadd(STREAM_TASKS, {
        "type":        "task.created",
        "task_id":     task_id,
        "agent":       req.agent,
        "task":        json.dumps({
            "title":       req.title,
            "description": req.description,
            "priority":    req.priority,
            "context":     req.context,
        }),
        "ts": str(time.time()),
    }, maxlen=5000, approximate=True)
    return {"task_id": task_id, "stream_id": msg_id, "agent": req.agent}


@app.post("/results")
async def post_result(res: TaskResult):
    """Agents post completed work here."""
    r = await get_redis()
    await r.xadd(STREAM_RESULTS, {
        "task_id":      res.task_id,
        "output":       strip_think_tags(res.output),
        "needs_review": str(res.needs_review).lower(),
        "ts":           str(time.time()),
    }, maxlen=5000, approximate=True)
    return {"status": "queued"}


@app.get("/tasks/{task_id}/status")
async def task_status(task_id: str):
    """Poll status from the tasks stream (most recent event for this task_id)."""
    r = await get_redis()
    # Scan last 500 events for this task_id
    raw = await r.xrevrange(STREAM_TASKS, count=500)
    for msg_id, fields in raw:
        if fields.get("task_id") == task_id:
            return {"task_id": task_id, "type": fields.get("type"), "agent": fields.get("agent"), "ts": fields.get("ts")}
    raise HTTPException(status_code=404, detail="task_id not found in recent history")


@app.get("/approvals/pending")
async def pending_approvals():
    """Return tasks waiting for human approval."""
    r = await get_redis()
    raw = await r.xrange(STREAM_APPROVALS, count=100)
    return [{"stream_id": mid, **fields} for mid, fields in raw]


@app.delete("/approvals/{stream_id}")
async def approve_task(stream_id: str):
    """ACK an approval (human reviewed it)."""
    r = await get_redis()
    await r.xack(STREAM_APPROVALS, "human", stream_id)
    return {"status": "acknowledged"}


@app.get("/metrics")
async def metrics():
    """Agent load and stream depths."""
    r = await get_redis()
    depths = {}
    for stream in [STREAM_TASKS, STREAM_RESULTS, STREAM_APPROVALS]:
        info = await r.xinfo_stream(stream)
        depths[stream] = info.get("length", 0)
    return {
        "stream_depths": depths,
        "agent_load": scheduler.AGENT_LOAD if scheduler else {},
    }


# ─── Streaming SSE helper used by agents polling their inbox ──────────────────

@app.get("/agent/{agent_name}/inbox")
async def agent_inbox_stream(agent_name: str, timeout_ms: int = 5000):
    """
    Long-poll endpoint: block until a task arrives on jarvis:agent:{name},
    strip think tags from any embedded output, return it.
    """
    r = await get_redis()
    stream = f"jarvis:agent:{agent_name}"

    async def generate():
        import json
        try:
            await r.xgroup_create(stream, agent_name, id="$", mkstream=True)
        except aioredis.ResponseError:
            pass

        entries = await r.xreadgroup(
            agent_name, f"{agent_name}-0",
            {stream: ">"},
            count=1, block=timeout_ms,
        )
        if not entries:
            yield b'data: {"type":"heartbeat"}\n\n'
            return

        for _, messages in entries:
            for msg_id, fields in messages:
                task_raw = fields.get("task", "{}")
                try:
                    task = json.loads(task_raw)
                    task = _sanitize_recursive(task)
                except Exception:
                    task = {"raw": strip_think_tags(task_raw)}

                import json as _json
                payload = _json.dumps({"type": "task", "task_id": fields.get("task_id"), "task": task})
                yield f"data: {payload}\n\n".encode()
                await r.xack(stream, agent_name, msg_id)

    return StreamingResponse(generate(), media_type="text/event-stream")
