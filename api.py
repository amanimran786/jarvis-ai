"""
Jarvis local API server — runs on http://127.0.0.1:8765

Starts automatically inside the Jarvis process (GUI or --no-ui).
Shares the same memory, model router, and conversation state.

Endpoints:
  POST /chat          — send a message, get a response (or stream it)
  POST /feedback      — log a bad answer or tool failure
  GET  /evals/summary — inspect recent eval state
  GET  /status        — current mode, online check
  GET  /memory        — facts, topics, recent conversations
  GET  /osint/status  — local OSINT tool availability
  POST /osint/username — username footprint scan via Maigret
  POST /osint/domain-typos — domain typo-squatting scan via DNSTwist
  POST /memory/add    — add a fact
  POST /memory/forget — forget by keyword
  GET  /mode          — current model routing mode
  POST /mode          — set mode: {"mode": "local"|"cloud"|"auto"|"open-source"}
"""

import json
import os
import hmac
import hashlib
import secrets
import threading
import time
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from router import route_stream
import memory as mem
import model_router
import hardware as hw
import evals
import conversation_context as ctx
import vault
import source_ingest
import skill_factory
import extension_registry
from local_runtime import local_training
from local_runtime import local_model_eval
from local_runtime import local_model_automation
from local_runtime import local_beta
import behavior_hooks
import cost_policy
import usage_tracker
import runtime_state
import provider_router
import task_runtime
import task_persistence
import semantic_memory
import graph_context as gctx
import osint_tools


def _safe_self_review(area: str | None = None) -> tuple[dict, str]:
    import self_improve as si
    review_fn = getattr(si, "self_review", None)
    format_fn = getattr(si, "review_text", None)
    if callable(review_fn) and callable(format_fn):
        result = review_fn(area=area or None)
        return result, format_fn(result)

    brief = evals.build_improvement_brief(area=area, min_failures=1)
    if brief.get("ok"):
        summary = brief.get("summary", "")
        target = brief.get("target_file", "router.py")
        evidence = " ".join(brief.get("evidence_lines", [])[:2])
        text = (
            f"My full self-review module is not available right now, so this is an eval-backed fallback. "
            f"{summary} The most likely next target is {target}. "
            f"The clearest recent signals are {evidence}"
        ).strip()
        return {"ok": True, "fallback": True, **brief}, text

    text = (
        "My full self-review module is not available right now, and there is not enough recent eval evidence to rank my shortcomings confidently."
    )
    return {"ok": False, "fallback": True, **brief}, text

app = FastAPI(title="Jarvis", version="1.0")
_CHAT_LOCK = threading.Lock()
_API_TOKEN = ""
_PUBLIC_PATHS = {"/status", "/webhooks/trigger", "/webhooks/github"}


def _host_without_port(host_header: str) -> str:
    host = (host_header or "").strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    return host.split(":", 1)[0].strip().lower()


def _allowed_hostnames() -> set[str]:
    allowed = {"127.0.0.1", "localhost", "::1", "testserver"}
    host = (get_host() or "127.0.0.1").strip().lower()
    if host in {"0.0.0.0", "::", "*"}:
        allowed.update(ip.lower() for ip in hw.local_ipv4_addresses())
    elif host:
        allowed.add(host)
    return allowed


def _token_authorized(request: Request) -> bool:
    expected = (_API_TOKEN or "").strip()
    if not expected:
        return True
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        supplied = bearer[7:].strip()
    else:
        supplied = request.headers.get("X-Jarvis-Token", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _webhook_secret() -> str:
    return (os.getenv("JARVIS_WEBHOOK_SECRET", "") or "").strip()


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if raw is None:
        return default
    lowered = str(raw).strip().lower()
    if not lowered:
        return default
    return lowered in {"1", "true", "yes", "on"}


def _signature_candidates(request: Request) -> list[str]:
    values = [
        request.headers.get("x-jarvis-signature", ""),
        request.headers.get("x-jarvis-signature-256", ""),
        request.headers.get("x-hub-signature-256", ""),
    ]
    return [value.strip() for value in values if value and value.strip()]


def _validate_webhook_signature(request: Request, body: bytes) -> tuple[bool, str]:
    secret = _webhook_secret()
    if not secret:
        if _env_truthy("JARVIS_ALLOW_UNSIGNED_WEBHOOKS", default=False):
            return True, ""
        return False, "webhook_secret_missing"
    signatures = _signature_candidates(request)
    if not signatures:
        return False, "signature_missing"
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if any(hmac.compare_digest(sig, expected) for sig in signatures):
        return True, ""
    return False, "signature_invalid"


def _webhook_secret_is_configured() -> bool:
    return bool(_webhook_secret())


def _webhook_max_age_seconds() -> int:
    raw = (os.getenv("JARVIS_WEBHOOK_MAX_AGE_SECONDS", "") or "").strip()
    if not raw:
        return 300
    try:
        parsed = int(raw)
    except ValueError:
        return 300
    return parsed if parsed > 0 else 300


def _parse_unix_seconds(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        # Accept integer strings and lossless float strings such as "1712791200.0".
        return int(float(text))
    except ValueError:
        return None


def _register_webhook_receipt(
    source: str,
    delivery_id: str,
    *,
    event_name: str = "",
    body_sha256: str = "",
) -> bool:
    register_fn = getattr(task_persistence, "register_webhook_receipt", None)
    if not callable(register_fn):
        return True
    receipt_source = str(source or "").strip() or "webhook"
    receipt_delivery = str(delivery_id or "").strip()
    if not receipt_delivery:
        return True
    try:
        result = register_fn(
            receipt_source,
            receipt_delivery,
            str(event_name or ""),
            str(body_sha256 or ""),
        )
    except TypeError:
        result = register_fn(receipt_source, receipt_delivery)
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        if "duplicate" in result:
            return not bool(result.get("duplicate"))
        if "ok" in result:
            return bool(result.get("ok"))
        if "accepted" in result:
            return bool(result.get("accepted"))
    return bool(result)


def _coerce_json_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {"payload": data}


def _compact_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    except Exception:
        return repr(value)


def _payload_meta(body: bytes, payload: dict) -> dict:
    # Keep webhook task metadata lightweight by default to reduce leakage risk.
    # Full raw payload storage is opt-in for debugging.
    digest = hashlib.sha256(body or b"").hexdigest()
    if _env_truthy("JARVIS_WEBHOOK_STORE_FULL_PAYLOAD", default=False):
        return {"sha256": digest, "payload": payload}
    keys = sorted(str(key) for key in payload.keys()) if isinstance(payload, dict) else []
    return {"sha256": digest, "payload_keys": keys, "payload_bytes": len(body or b"")}


def _generic_webhook_prompt(payload: dict, event_name: str) -> str:
    explicit = str(payload.get("prompt") or "").strip()
    if explicit:
        return explicit
    action = str(payload.get("action") or "").strip()
    subject = (
        payload.get("title")
        or payload.get("name")
        or payload.get("summary")
        or payload.get("description")
        or payload.get("message")
        or payload.get("text")
        or ""
    )
    subject_text = str(subject).strip()
    summary_bits = [f"event={event_name or 'webhook'}"]
    if action:
        summary_bits.append(f"action={action}")
    if subject_text:
        summary_bits.append(f"subject={subject_text[:160]}")
    else:
        summary_bits.append(f"payload={_compact_json(payload)[:220]}")
    return "Handle incoming webhook trigger. " + " | ".join(summary_bits)


def _github_webhook_prompt(event_name: str, payload: dict) -> str:
    action = str(payload.get("action") or "").strip()
    repo = ((payload.get("repository") or {}).get("full_name") or "").strip()
    issue = payload.get("issue") or {}
    pull_request = payload.get("pull_request") or {}
    comment = payload.get("comment") or {}
    review = payload.get("review") or {}
    release = payload.get("release") or {}
    sender = ((payload.get("sender") or {}).get("login") or "").strip()

    title = (
        issue.get("title")
        or pull_request.get("title")
        or release.get("name")
        or release.get("tag_name")
        or comment.get("body")
        or review.get("body")
        or ""
    )
    number = issue.get("number") or pull_request.get("number") or ""

    parts = [f"Handle GitHub webhook event '{event_name or 'unknown'}'."]
    if repo:
        parts.append(f"Repository: {repo}.")
    if action:
        parts.append(f"Action: {action}.")
    if number:
        parts.append(f"Number: {number}.")
    if title:
        parts.append(f"Title/body: {str(title).strip()[:220]}.")
    if sender:
        parts.append(f"Sender: {sender}.")
    if not any([repo, action, number, title, sender]):
        parts.append(f"Payload summary: {_compact_json(payload)[:260]}.")
    return " ".join(parts)


def _submit_webhook_task(
    *,
    prompt: str,
    kind: str,
    source: str,
    terse_mode: str = "",
    isolated_workspace: bool | None = None,
    meta: dict | None = None,
):
    return task_runtime.submit_task(
        prompt,
        kind=kind or "task",
        source=source,
        terse_mode=terse_mode or "",
        isolated_workspace=isolated_workspace,
        meta=meta or {},
    )


@app.middleware("http")
async def _guard_requests(request: Request, call_next):
    host = _host_without_port(request.headers.get("host", ""))
    if host and host not in _allowed_hostnames():
        return JSONResponse(status_code=400, content={"ok": False, "error": "host_not_allowed"})
    if request.url.path not in _PUBLIC_PATHS and not _token_authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "auth_required"})
    return await call_next(request)


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    stream: bool = False


class TaskRequest(BaseModel):
    prompt: str
    kind: str = "task"
    source: str = "api"
    assigned_agent_id: str = ""
    terse_mode: str = ""
    isolated_workspace: bool | None = None
    meta: dict | None = None


class FactRequest(BaseModel):
    fact: str


class ForgetRequest(BaseModel):
    keyword: str


class ModeRequest(BaseModel):
    mode: str


class FeedbackRequest(BaseModel):
    issue: str
    interaction_id: str | None = None
    expected: str = ""
    user_input: str = ""
    response: str = ""
    model: str = ""
    source: str = "user_feedback"


class VaultIngestRequest(BaseModel):
    source: str
    source_type: str = "auto"
    auto_build: bool = True


class SkillCreateRequest(BaseModel):
    query: str
    tool: str = "chat"
    cost_hint: str = "local"


class SkillPromoteRequest(BaseModel):
    min_failures: int = 2


class SelfReviewRequest(BaseModel):
    area: str = ""


class LocalTrainingExportRequest(BaseModel):
    limit: int = 150
    cloud_only: bool = True


class LocalTrainingDistillRequest(BaseModel):
    limit: int = 12
    teacher_model: str = "claude-sonnet-4-6"


class LocalTrainingModelfileRequest(BaseModel):
    base_model: str = ""
    target_name: str = ""


class LocalTrainingRunRequest(BaseModel):
    export_limit: int = 150
    distill_limit: int = 8
    teacher_model: str = "claude-sonnet-4-6"
    cloud_only_export: bool = True
    base_model: str = ""
    target_name: str = ""


class LocalTrainingHandoffRequest(BaseModel):
    pack_path: str = ""
    targets: list[str] = []


class LocalModelEvalRunRequest(BaseModel):
    candidate_model: str
    baseline_model: str = ""
    limit: int = 8
    teacher_model: str = "claude-haiku-4-5-20251001"


class LocalModelPromoteRequest(BaseModel):
    candidate_model: str = ""
    eval_path: str = ""
    min_pass_rate: float = 0.6
    min_score_delta: float = 0.35


class LocalModelAutomationRunRequest(BaseModel):
    export_limit: int = 40
    distill_limit: int = 3
    eval_limit: int = 2
    base_model: str = ""
    baseline_model: str = ""
    candidate_name: str = ""
    teacher_model: str = "claude-sonnet-4-6"
    judge_model: str = "claude-haiku-4-5-20251001"
    promote_if_ready: bool = True
    cleanup_failed: bool = False
    force: bool = False


class LocalBetaRunRequest(BaseModel):
    include_browser: bool = False
    limit: int = 0
    log_failures: bool = True
    build_training_pack: bool = False
    teacher_model: str = "claude-sonnet-4-6"
    suite: str = "all"


class OsintUsernameRequest(BaseModel):
    username: str
    timeout_seconds: int = 45
    top_sites: int = 200
    max_results: int = 25


class OsintDomainTyposRequest(BaseModel):
    domain: str
    timeout_seconds: int = 60
    max_results: int = 25
    registered_only: bool = True


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    """Send a message to Jarvis and get a response."""
    if req.stream:
        def generate():
            with _CHAT_LOCK:
                start_seq = usage_tracker.current_seq()
                stream, model = route_stream(req.message)
                chunks = []
                for chunk in stream:
                    if chunk:
                        chunks.append(chunk)
                        yield f"data: {json.dumps({'chunk': chunk, 'model': model})}\n\n"
                    else:
                        # Empty keepalive from DeepSeek R1 think phase —
                        # send SSE comment to hold the connection open
                        yield ": keepalive\n\n"
                response = "".join(chunks)
                usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
                context_stats = ctx.record_request_stats(model, source="api_stream")
                interaction = evals.log_interaction(req.message, response, model, source="api_stream", context=context_stats)
                evals.maybe_log_automatic_failure(interaction)
                try:
                    semantic_memory.log_conversation_turn(req.message, response, model=model, source="api_stream")
                except Exception:
                    pass
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'usage': usage, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    with _CHAT_LOCK:
        start_seq = usage_tracker.current_seq()
        stream, model = route_stream(req.message)
        response = "".join(stream)
        usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
        context_stats = ctx.record_request_stats(model, source="api")
        interaction = evals.log_interaction(req.message, response, model, context=context_stats)
        evals.maybe_log_automatic_failure(interaction)
        try:
            semantic_memory.log_conversation_turn(req.message, response, model=model, source="api")
        except Exception:
            pass
        return {"response": response, "model": model, "interaction_id": interaction["id"], "context": context_stats, "usage": usage}


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    entry = evals.log_failure(
        issue=req.issue,
        interaction_id=req.interaction_id,
        expected=req.expected,
        user_input=req.user_input,
        response=req.response,
        model=req.model,
        source=req.source,
    )
    return {"ok": True, "failure": entry}


@app.get("/evals/summary")
def eval_summary(hours: int = 24 * 7):
    return evals.summary(hours=hours)


@app.get("/status")
def status(refresh: bool = False):
    call_assist = runtime_state.refresh_call_assist(force_refresh=refresh)
    return {
        "status": "online",
        "mode": model_router.get_mode(),
        "api_host": get_host(),
        "api_port": get_port(),
        "api_urls": get_base_urls(),
        "local_available": model_router._has_local(),
        "context": ctx.get_stats(),
        "usage_24h": usage_tracker.summarize(hours=24, include_recent=0),
        "cost_policy": cost_policy.policy_status(),
        "provider_routing": provider_router.runtime_policy(),
        "call_assist": call_assist,
    }


@app.get("/runtime/state")
def get_runtime_state(refresh: bool = False):
    try:
        runtime_state.refresh_call_assist(force_refresh=refresh)
        return {"ok": True, "state": runtime_state.snapshot()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "state": {}}


@app.get("/agents")
def list_agents():
    return {"ok": True, "agents": task_runtime.list_agents()}


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    agent = task_runtime.get_agent(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"ok": False, "error": "agent_not_found"})
    return {"ok": True, "agent": agent}


@app.get("/extensions")
def list_extensions():
    return {"ok": True, "extensions": extension_registry.discovery_snapshot()}


@app.get("/skills")
def list_skills():
    return {"ok": True, "skills": extension_registry.list_skills()}


@app.get("/skills/{skill_id}")
def get_skill(skill_id: str):
    skill = extension_registry.get_skill_detail(skill_id)
    if not skill:
        return JSONResponse(status_code=404, content={"ok": False, "error": "skill_not_found"})
    return {"ok": True, "skill": skill}


@app.get("/connectors")
def list_connectors():
    return {"ok": True, "connectors": extension_registry.list_connectors()}


@app.get("/connectors/{connector_id}")
def get_connector(connector_id: str):
    connector = extension_registry.connector_detail(connector_id)
    if not connector:
        return JSONResponse(status_code=404, content={"ok": False, "error": "connector_not_found"})
    return {"ok": True, "connector": connector}


@app.get("/plugins")
def list_plugins():
    return {"ok": True, "plugins": extension_registry.list_plugins()}


@app.get("/plugins/{plugin_id}")
def get_plugin(plugin_id: str):
    plugin = extension_registry.plugin_detail(plugin_id)
    if not plugin:
        return JSONResponse(status_code=404, content={"ok": False, "error": "plugin_not_found"})
    return {"ok": True, "plugin": plugin}


@app.get("/graph/query")
def graph_query(q: str, topn: int = 8):
    result = gctx.query_graph(q, topn=topn)
    return {"ok": bool(result.get("ready", False)), "result": result}


@app.get("/graph/path")
def graph_path(source: str, target: str, max_depth: int = 6):
    result = gctx.shortest_path(source, target, max_depth=max_depth)
    status = 200 if result.get("ok") else 404
    return JSONResponse(status_code=status, content={"ok": bool(result.get("ok")), "result": result})


@app.get("/tasks")
def list_tasks(limit: int = 25, status: str = ""):
    return {"ok": True, "tasks": task_runtime.list_tasks(limit=limit, status=status)}


@app.post("/tasks")
def create_task(req: TaskRequest):
    task = task_runtime.submit_task(
        req.prompt,
        kind=req.kind,
        source=req.source,
        assigned_agent_id=req.assigned_agent_id,
        terse_mode=req.terse_mode,
        isolated_workspace=req.isolated_workspace,
        meta=req.meta,
    )
    return {"ok": True, "task": task}


@app.post("/webhooks/trigger")
async def webhook_trigger(request: Request):
    body = await request.body()
    authorized, error = _validate_webhook_signature(request, body)
    if not authorized:
        return JSONResponse(status_code=401, content={"ok": False, "error": error})

    payload = _coerce_json_body(body)
    delivery_id = str(
        request.headers.get("x-jarvis-delivery", "")
        or payload.get("delivery_id")
        or ""
    ).strip()
    timestamp_header = str(request.headers.get("x-jarvis-timestamp", "") or "").strip()
    timestamp_unix: int | None = _parse_unix_seconds(timestamp_header)
    if _webhook_secret_is_configured():
        if not delivery_id:
            return JSONResponse(status_code=400, content={"ok": False, "error": "missing_delivery_id"})
        if not timestamp_header:
            return JSONResponse(status_code=400, content={"ok": False, "error": "missing_timestamp"})
        if timestamp_unix is None:
            return JSONResponse(status_code=400, content={"ok": False, "error": "stale_timestamp"})
        now_unix = int(time.time())
        if abs(now_unix - timestamp_unix) > _webhook_max_age_seconds():
            return JSONResponse(status_code=400, content={"ok": False, "error": "stale_timestamp"})
        event_name = str(
            payload.get("event")
            or payload.get("event_type")
            or payload.get("type")
            or request.headers.get("x-jarvis-event", "")
            or "webhook.trigger"
        ).strip()
        if not _register_webhook_receipt(
            "trigger",
            delivery_id,
            event_name=event_name,
            body_sha256=hashlib.sha256(body).hexdigest(),
        ):
            return JSONResponse(status_code=409, content={"ok": False, "error": "replay_detected"})
    else:
        event_name = str(
            payload.get("event")
            or payload.get("event_type")
            or payload.get("type")
            or request.headers.get("x-jarvis-event", "")
            or "webhook.trigger"
        ).strip()
    kind = str(payload.get("kind") or "task").strip() or "task"
    terse_mode = str(payload.get("terse_mode") or "").strip()
    isolated_workspace = payload.get("isolated_workspace")
    meta = {
        "event_name": event_name,
        "delivery_id": delivery_id,
        "timestamp": timestamp_unix,
        "headers": {
            "x-jarvis-delivery": request.headers.get("x-jarvis-delivery", ""),
            "x-jarvis-timestamp": request.headers.get("x-jarvis-timestamp", ""),
            "x-jarvis-signature": request.headers.get("x-jarvis-signature", ""),
            "x-jarvis-signature-256": request.headers.get("x-jarvis-signature-256", ""),
            "content-type": request.headers.get("content-type", ""),
            "user-agent": request.headers.get("user-agent", ""),
        },
        "payload_meta": _payload_meta(body, payload),
    }
    user_meta = payload.get("meta")
    if isinstance(user_meta, dict):
        meta.update(user_meta)
    prompt = _generic_webhook_prompt(payload, event_name)
    task = _submit_webhook_task(
        prompt=prompt,
        kind=kind,
        source="webhook",
        terse_mode=terse_mode,
        isolated_workspace=isolated_workspace if isinstance(isolated_workspace, bool) else None,
        meta=meta,
    )
    return {"ok": True, "task_id": task.get("id"), "status": task.get("status"), "task": task}


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    authorized, error = _validate_webhook_signature(request, body)
    if not authorized:
        return JSONResponse(status_code=401, content={"ok": False, "error": error})

    payload = _coerce_json_body(body)
    event_name = str(request.headers.get("x-github-event", "") or payload.get("event") or "github").strip()
    delivery = str(request.headers.get("x-github-delivery", "")).strip()
    if _webhook_secret_is_configured() and not delivery:
        return JSONResponse(status_code=400, content={"ok": False, "error": "missing_delivery_id"})
    if delivery and not _register_webhook_receipt(
        "github",
        delivery,
        event_name=event_name,
        body_sha256=hashlib.sha256(body).hexdigest(),
    ):
        return JSONResponse(status_code=409, content={"ok": False, "error": "replay_detected"})
    action = str(payload.get("action") or "").strip()
    repo = (payload.get("repository") or {}).get("full_name") if isinstance(payload.get("repository"), dict) else ""
    kind = str(payload.get("kind") or "task").strip() or "task"
    terse_mode = str(payload.get("terse_mode") or "").strip()
    isolated_workspace = payload.get("isolated_workspace")
    meta = {
        "event_name": event_name,
        "delivery": delivery,
        "action": action,
        "repository": repo,
        "headers": {
            "x-github-event": request.headers.get("x-github-event", ""),
            "x-github-delivery": delivery,
            "x-hub-signature-256": request.headers.get("x-hub-signature-256", ""),
            "x-jarvis-signature": request.headers.get("x-jarvis-signature", ""),
            "x-jarvis-signature-256": request.headers.get("x-jarvis-signature-256", ""),
            "content-type": request.headers.get("content-type", ""),
            "user-agent": request.headers.get("user-agent", ""),
        },
        "payload_meta": _payload_meta(body, payload),
    }
    user_meta = payload.get("meta")
    if isinstance(user_meta, dict):
        meta.update(user_meta)
    prompt = _github_webhook_prompt(event_name, payload)
    task = _submit_webhook_task(
        prompt=prompt,
        kind=kind,
        source="github_webhook",
        terse_mode=terse_mode,
        isolated_workspace=isolated_workspace if isinstance(isolated_workspace, bool) else None,
        meta=meta,
    )
    return {"ok": True, "task_id": task.get("id"), "status": task.get("status"), "task": task}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    task = task_runtime.get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})
    return {"ok": True, "task": task}


@app.get("/tasks/{task_id}/events")
def get_task_events(task_id: str):
    task = task_runtime.get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})
    return {"ok": True, "events": task_runtime.get_task_events(task_id)}


@app.get("/tasks/{task_id}/stream")
def stream_task(task_id: str):
    task = task_runtime.get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})

    def generate():
        for event in task_runtime.stream_task_events(task_id):
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "done":
                break
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    task = task_runtime.cancel_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})
    return {"ok": True, "task": task}


@app.get("/bridge/status")
def bridge_status():
    return hw.bridge_status(api_host=get_host(), api_port=get_port())


@app.get("/context")
def get_context_stats():
    return {
        "current": ctx.get_stats(),
        "recent_requests": ctx.recent_request_stats(10),
    }


@app.get("/usage")
def get_usage(hours: int = 24, since_seq: int = 0, recent: int = 10):
    return {"ok": True, "usage": usage_tracker.summarize(hours=hours, since_seq=since_seq, include_recent=recent)}


@app.get("/cost-policy")
def get_cost_policy():
    return {"ok": True, "policy": cost_policy.policy_status()}


@app.get("/hooks/status")
def get_hook_status(hours: int = 24):
    return {"ok": True, "hooks": behavior_hooks.summary(hours=hours)}


@app.get("/vault")
def get_vault_status():
    return vault.status()


@app.post("/vault/build")
def build_vault():
    message = vault.build_wiki_text()
    return {"ok": True, "message": message, "vault": vault.status()}


@app.post("/vault/ingest")
def ingest_vault(req: VaultIngestRequest):
    result = source_ingest.ingest_source(req.source, source_type=req.source_type, auto_build=req.auto_build)
    return {"ok": result.get("ok", False), "message": source_ingest.result_text(result), "result": result, "vault": vault.status()}


@app.post("/skills/create")
def create_skill(req: SkillCreateRequest):
    result = skill_factory.create_skill_from_vault(req.query, tool=req.tool, cost_hint=req.cost_hint)
    return {"ok": result.get("ok", False), "message": skill_factory.result_text(result), "result": result}


@app.post("/skills/promote")
def promote_skills(req: SkillPromoteRequest):
    result = skill_factory.promote_failures(min_failures=req.min_failures)
    return {"ok": result.get("ok", False), "message": skill_factory.result_text(result), "result": result}


@app.get("/local/training/status")
def get_local_training_status():
    return {"ok": True, "status": local_training.status()}


@app.get("/local/evals/status")
def get_local_eval_status():
    return {"ok": True, "status": local_model_eval.status()}


@app.get("/local/automation/status")
def get_local_automation_status():
    return {"ok": True, "status": local_model_automation.status()}


@app.get("/local/beta/status")
def get_local_beta_status():
    return {"ok": True, "status": local_beta.status()}


@app.get("/local/capabilities")
def get_local_capabilities():
    from brains import brain_ollama
    from local_runtime import local_stt, local_tts
    import semantic_memory

    example_query = "Why does TCP have a three-way handshake and not a two-way handshake?"
    return {
        "ok": True,
        "mode": model_router.get_mode(),
        "capabilities": {
            **brain_ollama.local_capabilities(),
            "reasoning_route": model_router.describe_runtime_for(example_query),
            "stt": local_stt.status(),
            "tts": local_tts.status(),
            "semantic_memory": semantic_memory.status(),
        },
    }


@app.post("/local/training/export")
def export_local_training(req: LocalTrainingExportRequest):
    result = local_training.export_sft_dataset(limit=req.limit, cloud_only=req.cloud_only)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/distill")
def distill_local_training(req: LocalTrainingDistillRequest):
    result = local_training.distill_failures(limit=req.limit, teacher_model=req.teacher_model)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/modelfile")
def build_local_training_modelfile(req: LocalTrainingModelfileRequest):
    kwargs = {}
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.target_name:
        kwargs["target_name"] = req.target_name
    result = local_training.build_modelfile(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/run")
def run_local_training(req: LocalTrainingRunRequest):
    kwargs = {
        "export_limit": req.export_limit,
        "distill_limit": req.distill_limit,
        "teacher_model": req.teacher_model,
        "cloud_only_export": req.cloud_only_export,
    }
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.target_name:
        kwargs["target_name"] = req.target_name
    result = local_training.build_training_pack(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/handoff")
def build_local_training_handoff(req: LocalTrainingHandoffRequest):
    kwargs = {}
    if req.pack_path:
        kwargs["pack_path"] = req.pack_path
    if req.targets:
        kwargs["targets"] = req.targets
    result = local_training.build_finetune_handoff(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/evals/run")
def run_local_eval(req: LocalModelEvalRunRequest):
    kwargs = {
        "candidate_model": req.candidate_model,
        "limit": req.limit,
        "teacher_model": req.teacher_model,
    }
    if req.baseline_model:
        kwargs["baseline_model"] = req.baseline_model
    result = local_model_eval.run_eval(**kwargs)
    return {"ok": result.get("ok", False), "message": local_model_eval.result_text(result), "result": result}


@app.post("/local/evals/promote")
def promote_local_eval(req: LocalModelPromoteRequest):
    kwargs = {
        "min_pass_rate": req.min_pass_rate,
        "min_score_delta": req.min_score_delta,
    }
    if req.candidate_model:
        kwargs["candidate_model"] = req.candidate_model
    if req.eval_path:
        kwargs["eval_path"] = req.eval_path
    result = local_model_eval.promote_candidate(**kwargs)
    return {"ok": result.get("ok", False), "message": local_model_eval.result_text(result), "result": result}


@app.post("/local/automation/run")
def run_local_automation(req: LocalModelAutomationRunRequest):
    kwargs = {
        "export_limit": req.export_limit,
        "distill_limit": req.distill_limit,
        "eval_limit": req.eval_limit,
        "teacher_model": req.teacher_model,
        "judge_model": req.judge_model,
        "promote_if_ready": req.promote_if_ready,
        "cleanup_failed": req.cleanup_failed,
        "force": req.force,
    }
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.baseline_model:
        kwargs["baseline_model"] = req.baseline_model
    if req.candidate_name:
        kwargs["candidate_name"] = req.candidate_name
    result = local_model_automation.run_cycle(**kwargs)
    return {"ok": result.get("ok", False), "message": local_model_automation.result_text(result), "result": result}


@app.post("/local/beta/run")
def run_local_beta(req: LocalBetaRunRequest):
    result = local_beta.run_beta_suite(
        include_browser=req.include_browser,
        limit=req.limit,
        log_failures=req.log_failures,
        build_training_pack=req.build_training_pack,
        teacher_model=req.teacher_model,
        suite=req.suite,
    )
    return {"ok": result.get("ok", False), "message": local_beta.result_text(result), "result": result}


@app.get("/self/review")
def get_self_review(area: str = ""):
    result, message = _safe_self_review(area=area or None)
    return {"ok": result.get("ok", False), "message": message, "result": result}


@app.post("/self/review")
def post_self_review(req: SelfReviewRequest):
    result, message = _safe_self_review(area=req.area or None)
    return {"ok": result.get("ok", False), "message": message, "result": result}


@app.get("/osint/status")
def osint_status():
    return {"ok": True, "status": osint_tools.status()}


@app.post("/osint/username")
def osint_username(req: OsintUsernameRequest):
    result = osint_tools.username_lookup(
        req.username,
        timeout_seconds=req.timeout_seconds,
        top_sites=req.top_sites,
        max_results=req.max_results,
    )
    return result


@app.post("/osint/domain-typos")
def osint_domain_typos(req: OsintDomainTyposRequest):
    result = osint_tools.domain_typo_scan(
        req.domain,
        timeout_seconds=req.timeout_seconds,
        max_results=req.max_results,
        registered_only=req.registered_only,
    )
    return result


@app.get("/memory")
def get_memory():
    return {
        "facts": mem.list_facts(),
        "preferences": mem.get_all_preferences(),
        "top_topics": mem.get_top_topics(5),
        "recent_conversations": mem.get_recent_conversations(3),
        "working_memory": mem.memory_status().get("working_memory", {}),
        "long_term_profile": mem.memory_status().get("long_term_profile", {}),
    }


@app.get("/memory/status")
def get_memory_status():
    return {"ok": True, "status": mem.memory_status()}


@app.post("/memory/consolidate")
def consolidate_memory():
    result = mem.consolidate_memory()
    return {"ok": result.get("ok", False), "result": result}


@app.post("/memory/add")
def add_memory(req: FactRequest):
    mem.add_fact(req.fact)
    return {"ok": True, "fact": req.fact}


@app.post("/memory/forget")
def forget_memory(req: ForgetRequest):
    removed = mem.forget(req.keyword)
    return {"ok": removed, "keyword": req.keyword}


@app.get("/mode")
def get_mode():
    return {"mode": model_router.get_mode()}


@app.post("/mode")
def set_mode(req: ModeRequest):
    result = model_router.set_mode(req.mode)
    return {"ok": True, "message": result, "mode": model_router.get_mode()}


# ── Server startup ─────────────────────────────────────────────────────────────

_port: int = 8765  # actual port after binding
_host: str = "127.0.0.1"
_API_STARTED = False  # Guard against multiple start() calls


def _find_free_port(start: int = 8765, attempts: int = 10, host: str = "127.0.0.1") -> int:
    import socket
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                bind_host = "" if host in {"0.0.0.0", "::", "*"} else host
                s.bind((bind_host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range 8765-8774")


def get_port() -> int:
    return _port


def get_host() -> str:
    return _host


def get_base_urls() -> list[str]:
    return hw.bridge_status(api_host=_host, api_port=_port).get("urls", [])


def get_base_url() -> str:
    urls = get_base_urls()
    return urls[0] if urls else f"http://{_host}:{_port}"


def get_api_token() -> str:
    return _API_TOKEN


hw.register_api_routes(app)


def start(host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    """Start the API server in a background daemon thread."""
    global _host, _port, _API_TOKEN, _API_STARTED
    
    if _API_STARTED:
        return threading.current_thread()
    
    _API_STARTED = True
    _host = host or "127.0.0.1"
    _port = _find_free_port(port, host=_host)
    _API_TOKEN = os.getenv("JARVIS_API_TOKEN", "").strip() or secrets.token_urlsafe(24)

    def _run():
        uvicorn.run(app, host=_host, port=_port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="JarvisAPI")
    t.start()
    print(f"[API] Jarvis API running at http://{_host}:{_port}")

    # Pre-load the reasoning model in the background so first query is instant
    import model_router as _mr
    if _mr.is_open_source_mode():
        from brains.brain_ollama import warm_model_cache
        warm_thread = threading.Thread(target=warm_model_cache, daemon=True, name="OllamaWarm")
        warm_thread.start()

    return t
