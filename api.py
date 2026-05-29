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
  GET  /production-readiness — truthful local/free/production readiness contract
  GET  /local/model-fleet — local Ollama fleet and free training-lane status
  POST /local/automation/colab-handoff — build a local training pack plus Colab notebook handoff
  POST /mode          — set mode: {"mode": "local"|"cloud"|"auto"|"open-source"}
  GET  /alerts        — pending proactive alerts (calendar + urgent email)
  POST /alerts/{id}/dismiss — dismiss a proactive alert
"""

import json
import itertools
import os
import hmac
import hashlib
import secrets
import threading
import time
import logging
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse

log = logging.getLogger("api")
from pydantic import BaseModel

from router import route_stream, record_turn as _record_turn
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
from local_runtime import model_fleet
from config import LOCAL_REASONING, HAIKU
import behavior_hooks
import capability_evals
import capability_parity
import cost_policy
import context_budget
import coder_workbench
import external_agent_patterns
import production_readiness
import security_roe
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
# Optional cloud-mode override for mobile_web requests (avoids slow local models
# on the headless server without changing the global default mode).
# mobile_web requests bypass route_stream() and go directly to Claude Haiku.
# This is thread-safe: no global state is mutated.
_PUBLIC_PATHS = {
    "/", "/status", "/webhooks/trigger", "/webhooks/github", "/bridge/pair",
    "/manifest.json", "/service-worker.js", "/assets/icon_1024.png"
}

# TV / Remote Device Pairing PIN Registry
_active_pins: dict[str, dict] = {}           # pin_string -> {"token": token, "expires_at": timestamp}
_pin_failures: dict[str, int] = {}           # client_ip -> count
_pin_lockout_until: dict[str, float] = {}    # client_ip -> lockout_timestamp


def _default_chat_lock_timeout_seconds() -> float:
    raw = os.getenv("JARVIS_CHAT_LOCK_TIMEOUT_SECONDS", "3.0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 3.0


_CHAT_LOCK_TIMEOUT_SECONDS = _default_chat_lock_timeout_seconds()


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
    
    # Allow custom permanent domains from .env
    custom = os.getenv("JARVIS_CLOUDFLARE_DOMAIN", "").strip().lower()
    if custom:
        custom_clean = custom.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
        allowed.add(custom_clean)
        
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
    
    # Fallback to query parameter (needed for streaming media/images)
    if not supplied:
        supplied = request.query_params.get("token", "").strip()
        
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


def _acquire_chat_lock() -> bool:
    timeout = max(0.0, float(_CHAT_LOCK_TIMEOUT_SECONDS))
    return _CHAT_LOCK.acquire(timeout=timeout)


def _chat_busy_payload() -> dict:
    return {
        "ok": False,
        "error": "chat_busy",
        "response": "Jarvis is still handling another request. Try again shortly.",
        "model": "System",
    }


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
    allowed = _allowed_hostnames()
    is_tunnel = host.endswith(".trycloudflare.com")
    if host and host not in allowed and not is_tunnel:
        return JSONResponse(status_code=400, content={"ok": False, "error": "host_not_allowed"})
    if request.url.path not in _PUBLIC_PATHS and not _token_authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "auth_required"})
    return await call_next(request)


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    stream: bool = False
    source: str = "api"
    meta: dict | None = None


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


class SkillProposeRequest(BaseModel):
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
    teacher_model: str = LOCAL_REASONING


class LocalTrainingModelfileRequest(BaseModel):
    base_model: str = ""
    target_name: str = ""


class LocalTrainingRunRequest(BaseModel):
    export_limit: int = 150
    distill_limit: int = 8
    expert_distill_limit: int = 3
    teacher_model: str = LOCAL_REASONING
    cloud_only_export: bool = True
    base_model: str = ""
    target_name: str = ""


class LocalTrainingHandoffRequest(BaseModel):
    pack_path: str = ""
    targets: list[str] = []


class LocalTrainingColabRequest(BaseModel):
    pack_path: str = ""
    target: str = "qwen2.5-coder:7b"


class LocalTrainingPreferenceExportRequest(BaseModel):
    limit: int = 120


class LocalTrainingPreferenceColabRequest(BaseModel):
    preference_path: str = ""
    target: str = "qwen2.5-coder:7b"


class LocalTrainingTeachRequest(BaseModel):
    prompt: str
    answer: str
    source: str = "manual_teacher"
    tags: list[str] = []
    meta: dict | None = None


class LocalModelEvalRunRequest(BaseModel):
    candidate_model: str
    baseline_model: str = ""
    limit: int = 8
    teacher_model: str = LOCAL_REASONING


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
    teacher_model: str = LOCAL_REASONING
    judge_model: str = LOCAL_REASONING
    promote_if_ready: bool = True
    cleanup_failed: bool = False
    force: bool = False


class LocalModelAutomationColabRequest(BaseModel):
    export_limit: int = 80
    distill_limit: int = 0
    expert_distill_limit: int = 0
    target: str = "qwen2.5-coder:7b"
    base_model: str = ""
    target_name: str = ""
    cloud_only_export: bool = True


class LocalBetaRunRequest(BaseModel):
    include_browser: bool = False
    limit: int = 0
    log_failures: bool = True
    build_training_pack: bool = False
    teacher_model: str = LOCAL_REASONING
    suite: str = "all"


class CoderRunVerifyPlanRequest(BaseModel):
    paths: list[str] = []
    required_only: bool = True
    stop_on_failure: bool = True
    timeout_seconds: int = 120


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


class RemoteVolumeRequest(BaseModel):
    level: int


class RemoteBrightnessRequest(BaseModel):
    level: int


class RemoteActionRequest(BaseModel):
    action: str


# ── Mobile web stream helper ───────────────────────────────────────────────────

# Cache: if Claude fails with a hard error (credits, auth), skip it for 10 min.
_claude_mobile_ok: bool = True
_claude_mobile_retry_after: float = 0.0
_CLAUDE_MOBILE_COOLDOWN = 600.0  # seconds


def _mobile_web_stream(message: str):
    """Return (stream_iterator, model_label) for a mobile_web /chat request.

    Strategy: try Claude Haiku first; on hard failure (credits/auth/429) back
    off for 10 minutes and route straight to GPT-4o-mini. No global mode touched.
    Thread-safe: flag reads/writes are atomic on CPython's GIL.
    """
    global _claude_mobile_ok, _claude_mobile_retry_after

    from config import GPT_MINI
    from brains.brain import ask_stream as _openai_stream

    # ── 1. Claude Haiku attempt (skip if recently failed) ─────────────────────
    if _claude_mobile_ok or time.time() >= _claude_mobile_retry_after:
        try:
            from brains.brain_claude import ask_claude_stream as _cs, _get_client
            if _get_client() is not None:
                gen = _cs(message, model=HAIKU, track_context=False)
                first = next(gen)  # probe — surfaces credit/auth errors immediately
                _claude_mobile_ok = True  # reset on success
                return itertools.chain([first], gen), HAIKU
        except StopIteration:
            _claude_mobile_ok = True
        except Exception as exc:
            _claude_mobile_ok = False
            _claude_mobile_retry_after = time.time() + _CLAUDE_MOBILE_COOLDOWN
            log.warning(
                "[mobile_web] Claude unavailable (%s) — using GPT-4o-mini for next %.0fs",
                exc, _CLAUDE_MOBILE_COOLDOWN,
            )

    # ── 2. OpenAI GPT-4o-mini ─────────────────────────────────────────────────
    return _openai_stream(message, model=GPT_MINI, bypass_local=True, track_context=False), GPT_MINI


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    """Send a message to Jarvis and get a response."""
    source = (req.source or "api").strip() or "api"
    client_meta = req.meta or {}
    if req.stream:
        # mobile_web bypasses _CHAT_LOCK — it goes direct to cloud, no TTS/voice
        # gating needed, and the lock must not be held across a slow HTTP stream.
        if source == "mobile_web":
            def generate_mobile():
                start_seq = usage_tracker.current_seq()
                stream, model = _mobile_web_stream(req.message)
                chunks = []
                for chunk in stream:
                    if chunk:
                        chunks.append(chunk)
                        yield f"data: {json.dumps({'chunk': chunk, 'model': model})}\n\n"
                response = "".join(chunks)
                usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
                stream_source = "mobile_web_stream"
                context_stats = ctx.record_request_stats(model, source=stream_source)
                interaction = evals.log_interaction(
                    req.message, response, model,
                    source=stream_source,
                    context={**context_stats, "client_meta": client_meta},
                )
                evals.maybe_log_automatic_failure(interaction)
                try:
                    semantic_memory.log_conversation_turn(req.message, response, model=model, source=stream_source)
                except Exception:
                    pass
                _record_turn(req.message, response)
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'usage': usage, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(generate_mobile(), media_type="text/event-stream")

        if not _acquire_chat_lock():
            return JSONResponse(status_code=409, content=_chat_busy_payload())

        def generate():
            try:
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
                stream_source = f"{source}_stream"
                context_stats = ctx.record_request_stats(model, source=stream_source)
                interaction = evals.log_interaction(
                    req.message,
                    response,
                    model,
                    source=stream_source,
                    context={**context_stats, "client_meta": client_meta},
                )
                evals.maybe_log_automatic_failure(interaction)
                try:
                    semantic_memory.log_conversation_turn(req.message, response, model=model, source=stream_source)
                except Exception:
                    pass
                # mem0 cross-session episodic memory — fire-and-forget
                _record_turn(req.message, response)
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'usage': usage, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                _CHAT_LOCK.release()
        return StreamingResponse(generate(), media_type="text/event-stream")

    if not _acquire_chat_lock():
        return JSONResponse(status_code=409, content=_chat_busy_payload())

    try:
        start_seq = usage_tracker.current_seq()
        if source == "mobile_web":
            stream, model = _mobile_web_stream(req.message)
        else:
            stream, model = route_stream(req.message)
        response = "".join(stream)
        usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
        context_stats = ctx.record_request_stats(model, source=source)
        interaction = evals.log_interaction(
            req.message,
            response,
            model,
            source=source,
            context={**context_stats, "client_meta": client_meta},
        )
        evals.maybe_log_automatic_failure(interaction)
        try:
            semantic_memory.log_conversation_turn(req.message, response, model=model, source=source)
        except Exception:
            pass
        # mem0 cross-session episodic memory — fire-and-forget
        _record_turn(req.message, response)
        return {"response": response, "model": model, "interaction_id": interaction["id"], "context": context_stats, "usage": usage}
    finally:
        _CHAT_LOCK.release()


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
    state = runtime_state.get_state()
    if state.status != "ONLINE" or not state.api_running:
        runtime_state.mark_started(
            host=get_host(),
            port=get_port(),
            thread_name=state.api_thread_name or "JarvisAPI",
            reason=state.boot_reason or "status_probe",
        )
    call_assist = runtime_state.refresh_call_assist(force_refresh=refresh)
    try:
        from brains import brain_ollama

        local_caps = brain_ollama.local_capabilities()
        local_vision = {
            "state": local_caps.get("vision_status", "unavailable"),
            "detail": local_caps.get("vision_status_detail", "Local vision status is unavailable."),
            "selected_model": local_caps.get("vision_model"),
            "preferred_model": local_caps.get("vision_preferred"),
        }
    except Exception as exc:
        local_vision = {
            "state": "unavailable",
            "detail": str(exc),
            "selected_model": None,
            "preferred_model": None,
        }
    return {
        "status": "online",
        "mode": model_router.get_mode(),
        "api_host": get_host(),
        "api_port": get_port(),
        "api_urls": get_base_urls(),
        "local_available": model_router._has_local(),
        "local_vision": local_vision,
        "context": ctx.get_stats(),
        "usage_24h": usage_tracker.summarize(hours=24, include_recent=0),
        "cost_policy": cost_policy.policy_status(),
        "provider_routing": provider_router.runtime_policy(),
        "call_assist": call_assist,
    }


@app.get("/auth/verify")
def auth_verify(request: Request):
    """Token validity probe — returns 200 if token is correct, 401 otherwise."""
    if not _token_authorized(request):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    return {"ok": True}


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


@app.post("/tasks/{task_id}/approve")
def approve_task(task_id: str):
    task = task_runtime.approve_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})
    return {"ok": True, "task": task}


@app.post("/tasks/{task_id}/deny")
def deny_task(task_id: str):
    task = task_runtime.deny_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"ok": False, "error": "task_not_found"})
    return {"ok": True, "task": task}


def create_pairing_pin() -> str:
    """
    Programmatically generate a temporary 6-digit numeric pairing PIN.
    """
    import random
    for _ in range(20):
        pin = f"{random.randint(100000, 999999)}"
        if pin not in _active_pins or _active_pins[pin]["expires_at"] < time.time():
            break
    else:
        pin = f"{random.randint(100000, 999999)}"
    
    _active_pins[pin] = {
        "token": _API_TOKEN,
        "expires_at": time.time() + 300.0  # 5 minutes
    }
    log.info(f"[Bridge] Generated temporary pairing PIN: {pin}")
    return pin


@app.post("/bridge/pin")
def generate_pairing_pin(request: Request):
    """
    Generate a temporary 6-digit numeric pairing PIN.
    Only authorized MacBook local calls can trigger this.
    """
    if not _token_authorized(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    pin = create_pairing_pin()
    return {"ok": True, "pin": pin, "expires_in": 300}


@app.get("/bridge/pair")
def authenticate_pairing_pin(request: Request, pin: str):
    """
    Authenticate a remote device (e.g. Smart TV) using a 6-digit PIN.
    Swaps the PIN for the actual secure JARVIS_API_TOKEN.
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    
    if _pin_lockout_until.get(client_ip, 0) > now:
        remaining = int(_pin_lockout_until[client_ip] - now)
        return JSONResponse(
            status_code=429,
            content={"ok": False, "error": "rate_limit_lockout", "lockout_seconds": remaining}
        )
    
    expired = [k for k, v in _active_pins.items() if v["expires_at"] < now]
    for k in expired:
        _active_pins.pop(k, None)
        
    pin_clean = "".join(c for c in (pin or "") if c.isdigit())
    if pin_clean in _active_pins:
        token = _active_pins[pin_clean]["token"]
        _active_pins.pop(pin_clean, None)
        _pin_failures[client_ip] = 0
        log.info(f"[Bridge] TV paired successfully from IP {client_ip}")
        return {"ok": True, "token": token}
    else:
        failures = _pin_failures.get(client_ip, 0) + 1
        _pin_failures[client_ip] = failures
        
        if failures >= 5:
            _pin_lockout_until[client_ip] = now + 900.0
            log.warning(f"[Bridge] IP {client_ip} triggered brute-force lockout on PIN pairing.")
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "rate_limit_lockout", "lockout_seconds": 900}
            )
        
        remaining_attempts = 5 - failures
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "invalid_pin", "remaining_attempts": remaining_attempts}
        )


@app.get("/bridge/status")
def bridge_status():
    return hw.bridge_status(api_host=get_host(), api_port=get_port())


def get_system_telemetry() -> dict:
    import subprocess
    import re
    
    battery_info = "Unknown"
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=1.5)
        pct_match = re.search(r"(\d+)%", out)
        if pct_match:
            battery_info = f"{pct_match.group(1)}%"
            if "charging" in out.lower() or "ac power" in out.lower():
                battery_info += " (Charging)"
    except Exception:
        pass

    cpu_info = "Unknown"
    try:
        out = subprocess.check_output(["sysctl", "-n", "vm.loadavg"], text=True, timeout=1.0)
        load_match = re.findall(r"\d+\.\d+", out)
        if load_match:
            cpu_info = f"{load_match[0]} (1m load)"
    except Exception:
        pass

    memory_info = "Unknown"
    try:
        total_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        total_gb = total_bytes / (1024**3)
        vm = subprocess.check_output(["vm_stat"], text=True)
        page_size = 4096
        free_pages = 0
        for line in vm.splitlines():
            if "page size of" in line:
                page_size = int(re.search(r"page size of (\d+) bytes", line).group(1))
            elif "Pages free:" in line:
                free_pages = int(re.search(r"Pages free:\s+(\d+)\.", line).group(1))
        
        used_gb = total_gb - (free_pages * page_size / (1024**3))
        memory_info = f"{used_gb:.1f} / {total_gb:.0f} GB ({int(used_gb/total_gb*100)}%)"
    except Exception:
        pass

    return {
        "battery": battery_info,
        "cpu": cpu_info,
        "memory": memory_info,
        "os": "macOS (Apple Silicon)"
    }


@app.get("/remote/screenshot")
def remote_screenshot(request: Request):
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    from desktop.screen_capture import capture_screenshot_temp
    try:
        temp_path = capture_screenshot_temp("jpg")
        if os.path.exists(temp_path):
            def iterfile():
                with open(temp_path, mode="rb") as f:
                    yield from f
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return StreamingResponse(iterfile(), media_type="image/jpeg")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Screenshot file not created")
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/remote/telemetry")
def remote_telemetry(request: Request):
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        data = get_system_telemetry()
        return {"ok": True, "telemetry": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/volume")
def remote_volume(request: Request, req: RemoteVolumeRequest):
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import tools
        msg = tools.set_volume(req.level)
        return {"ok": True, "message": msg}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/brightness")
def remote_brightness(request: Request, req: RemoteBrightnessRequest):
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import tools
        msg = tools.set_brightness(req.level)
        return {"ok": True, "message": msg}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/action")
def remote_action(request: Request, req: RemoteActionRequest):
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        action = req.action.lower().strip()
        import subprocess
        if action == "lock":
            import tools
            msg = tools.lock_screen()
        elif action == "mute":
            import tools
            msg = tools.mute()
        elif action == "unmute":
            import tools
            msg = tools.unmute()
        elif action == "play_pause":
            subprocess.run(["osascript", "-e", "tell application \"Spotify\" to playpause"], capture_output=True)
            subprocess.run(["osascript", "-e", "tell application \"Music\" to playpause"], capture_output=True)
            msg = "Media playback toggled."
        elif action == "next_track":
            subprocess.run(["osascript", "-e", "tell application \"Spotify\" to next track"], capture_output=True)
            subprocess.run(["osascript", "-e", "tell application \"Music\" to next track"], capture_output=True)
            msg = "Skipped to next track."
        elif action == "prev_track":
            subprocess.run(["osascript", "-e", "tell application \"Spotify\" to previous track"], capture_output=True)
            subprocess.run(["osascript", "-e", "tell application \"Music\" to previous track"], capture_output=True)
            msg = "Returned to previous track."
        else:
            return JSONResponse(status_code=400, content={"ok": False, "error": "unknown_action"})
        return {"ok": True, "message": msg}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/context")
def get_context_stats():
    return {
        "current": ctx.get_stats(),
        "recent_requests": ctx.recent_request_stats(10),
    }


@app.get("/usage")
def get_usage(hours: int = 24, since_seq: int = 0, recent: int = 10):
    return {"ok": True, "usage": usage_tracker.summarize(hours=hours, since_seq=since_seq, include_recent=recent)}


@app.get("/context-budget")
def get_context_budget(hours: int = 24):
    return context_budget.policy_status(hours=hours)


@app.get("/coder/status")
def get_coder_status():
    return coder_workbench.status()


@app.get("/coder/verify-plan")
def get_coder_verify_plan():
    return {"ok": True, "commands": coder_workbench.verification_plan()}


@app.post("/coder/run-verify-plan")
def run_coder_verify_plan(req: CoderRunVerifyPlanRequest):
    return coder_workbench.run_verification_plan(
        req.paths or None,
        required_only=req.required_only,
        stop_on_failure=req.stop_on_failure,
        timeout_seconds=req.timeout_seconds,
    )


@app.get("/agent-patterns")
def get_agent_patterns(category: str = ""):
    if category:
        return {"ok": True, "patterns": external_agent_patterns.list_patterns(category)}
    return external_agent_patterns.pattern_status()


@app.get("/capability-parity")
def get_capability_parity():
    return capability_parity.scorecard()


@app.get("/capability-evals")
def get_capability_evals(group: str = ""):
    return capability_evals.status(group)


@app.get("/production-readiness")
def get_production_readiness():
    return production_readiness.contract()


@app.get("/security-roe")
def get_security_roe(template: str = ""):
    return security_roe.status(template)


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


@app.post("/skills/propose")
def propose_skill(req: SkillProposeRequest):
    result = skill_factory.propose_skill_from_vault(req.query, tool=req.tool, cost_hint=req.cost_hint)
    message = (
        f"Proposed the skill {result['skill_id']} from local vault sources without writing files."
        if result.get("ok")
        else result.get("error", "Skill proposal failed.")
    )
    return {"ok": result.get("ok", False), "message": message, "result": result}


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


@app.get("/local/model-fleet")
def get_local_model_fleet():
    return model_fleet.fleet_status()


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
            "model_fleet": model_fleet.fleet_status(),
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
        "expert_distill_limit": req.expert_distill_limit,
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


@app.post("/local/training/colab")
def build_local_training_colab_handoff(req: LocalTrainingColabRequest):
    kwargs = {}
    if req.pack_path:
        kwargs["pack_path"] = req.pack_path
    if req.target:
        kwargs["target"] = req.target
    result = local_training.build_colab_handoff(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/preferences")
def export_local_training_preferences(req: LocalTrainingPreferenceExportRequest):
    result = local_training.export_preference_dataset(limit=req.limit)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/rl-colab")
def build_local_training_preference_colab_handoff(req: LocalTrainingPreferenceColabRequest):
    kwargs = {}
    if req.preference_path:
        kwargs["preference_path"] = req.preference_path
    if req.target:
        kwargs["target"] = req.target
    result = local_training.build_colab_preference_handoff(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/teach")
def record_local_training_teach(req: LocalTrainingTeachRequest):
    result = local_training.record_teacher_example(
        req.prompt,
        req.answer,
        source=req.source,
        tags=req.tags,
        meta=req.meta,
    )
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


@app.post("/local/automation/colab-handoff")
def run_local_colab_handoff_automation(req: LocalModelAutomationColabRequest):
    kwargs = {
        "export_limit": req.export_limit,
        "distill_limit": req.distill_limit,
        "expert_distill_limit": req.expert_distill_limit,
        "target": req.target,
        "cloud_only_export": req.cloud_only_export,
    }
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.target_name:
        kwargs["target_name"] = req.target_name
    result = local_model_automation.run_colab_handoff_cycle(**kwargs)
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


@app.get("/manifest.json")
def get_manifest():
    return FileResponse("manifest.json", media_type="application/json")


@app.get("/service-worker.js")
def get_service_worker():
    return FileResponse("service-worker.js", media_type="application/javascript")


@app.get("/assets/icon_1024.png")
def get_pwa_icon():
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon_1024.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    return JSONResponse(status_code=404, content={"ok": False, "error": "icon_not_found"})


@app.get("/")
async def root_web_hud(request: Request):
    """Serve a breathtaking, responsive 2026 glassmorphic mobile web HUD for iPhone & iPad sync."""
    from fastapi.responses import HTMLResponse
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="J.A.R.V.I.S">
  <link rel="apple-touch-icon" href="/assets/icon_1024.png">
  <link rel="manifest" href="/manifest.json">
  <title>J.A.R.V.I.S — Unified Brain</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    :root {
      --bg: #01080e;
      --cyan: #00d4ff;
      --cyan-dim: #0088cc;
      --cyan-glow: rgba(0, 212, 255, 0.25);
      --orange: #ff6b00;
      --orange-glow: rgba(255, 107, 0, 0.35);
      --border: rgba(13, 79, 112, 0.35);
      --glass-fill: rgba(3, 18, 28, 0.75);
      --glass-border: rgba(0, 212, 255, 0.15);
      --text: #a8e6ff;
      --text-dim: #4a8fa8;
      --white-dim: #d8f6ff;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }

    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      height: 100vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      position: relative;
    }

    /* Breathtaking background animated grid & scanlines */
    body::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: 
        radial-gradient(circle at 50% 30%, rgba(0, 212, 255, 0.12) 0%, transparent 60%),
        linear-gradient(rgba(0, 180, 220, 0.02) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0, 180, 220, 0.02) 1px, transparent 1px);
      background-size: 100% 100%, 24px 24px, 24px 24px;
      z-index: -2;
    }

    body::after {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 100%;
      background: linear-gradient(0deg, transparent 0%, rgba(0, 212, 255, 0.03) 10%, rgba(0, 212, 255, 0.08) 50%, rgba(0, 212, 255, 0.03) 90%, transparent 100%);
      background-size: 100% 400px;
      animation: scanline 12s linear infinite;
      z-index: -1;
      pointer-events: none;
    }

    @keyframes scanline {
      0% { background-position-y: -400px; }
      100% { background-position-y: 100%; }
    }

    /* Header */
    header {
      height: 70px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      background: rgba(3, 13, 20, 0.85);
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      z-index: 10;
      flex-shrink: 0;
    }

    .brand-block {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-logo {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      border: 2px solid var(--cyan);
      box-shadow: 0 0 10px var(--cyan-glow);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      font-weight: bold;
      color: var(--cyan);
      background: rgba(0, 212, 255, 0.1);
      animation: pulse-logo 4s infinite alternate;
    }

    @keyframes pulse-logo {
      0% { box-shadow: 0 0 4px var(--cyan-glow); }
      100% { box-shadow: 0 0 14px rgba(0, 212, 255, 0.5); }
    }

    .brand-title {
      display: flex;
      flex-direction: column;
    }

    .brand-name {
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 2px;
      color: var(--cyan);
      text-shadow: 0 0 8px var(--cyan-glow);
    }

    .brand-sub {
      font-size: 8px;
      color: var(--text-dim);
      letter-spacing: 1px;
      text-transform: uppercase;
    }

    .status-block {
      display: flex;
      align-items: center;
      gap: 14px;
    }

    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      background: rgba(0, 255, 136, 0.08);
      border: 1px solid rgba(0, 255, 136, 0.3);
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 10px;
      font-weight: 600;
      color: #00ff88;
      letter-spacing: 1px;
    }

    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background-color: #00ff88;
      box-shadow: 0 0 8px #00ff88;
      animation: blink-dot 1.5s infinite alternate;
    }

    @keyframes blink-dot {
      0% { opacity: 0.4; }
      100% { opacity: 1; }
    }

    .sidebar-toggle {
      background: none;
      border: 1px solid var(--border);
      color: var(--text-dim);
      padding: 6px 10px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.2s ease;
    }

    .sidebar-toggle:hover, .sidebar-toggle:active {
      border-color: var(--cyan);
      color: #fff;
      background: rgba(0, 212, 255, 0.08);
    }

    /* Layout Wrapper */
    .layout-body {
      display: flex;
      flex: 1;
      height: calc(100vh - 70px);
      position: relative;
      overflow: hidden;
    }

    /* Diagnostics Drawer */
    .diagnostics-drawer {
      width: 280px;
      background: rgba(2, 10, 16, 0.95);
      border-right: 1px solid var(--border);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      display: flex;
      flex-direction: column;
      padding: 20px;
      gap: 20px;
      transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
      position: absolute;
      top: 0;
      bottom: 0;
      left: 0;
      transform: translateX(-100%);
      z-index: 5;
    }

    .diagnostics-drawer.open {
      transform: translateX(0);
      position: relative;
    }

    .drawer-header {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 2px;
      color: var(--cyan);
      text-transform: uppercase;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }

    .stat-card {
      background: rgba(3, 18, 28, 0.5);
      border: 1px solid var(--glass-border);
      border-radius: 10px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .stat-label {
      font-size: 9px;
      color: var(--text-dim);
      letter-spacing: 1px;
      text-transform: uppercase;
    }

    .stat-value {
      font-size: 13px;
      color: var(--white-dim);
      font-weight: 600;
    }

    /* Main Chat Container */
    .chat-container {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100%;
      background: transparent;
      overflow: hidden;
      padding-bottom: 60px;
    }

    /* Message Area */
    .message-area {
      flex: 1;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      scroll-behavior: smooth;
    }

    /* Custom Scrollbar */
    .message-area::-webkit-scrollbar {
      width: 4px;
    }
    .message-area::-webkit-scrollbar-track {
      background: transparent;
    }
    .message-area::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 2px;
    }

    /* Message Bubble Capsules */
    .message-wrap {
      display: flex;
      width: 100%;
      margin: 4px 0;
    }

    .message-wrap.user {
      justify-content: flex-end;
    }

    .message-wrap.jarvis {
      justify-content: flex-start;
    }

    .bubble {
      max-width: 82%;
      padding: 12px 16px;
      font-size: 14px;
      line-height: 1.5;
      word-wrap: break-word;
      box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
    }

    .message-wrap.user .bubble {
      background: linear-gradient(135deg, rgba(255, 107, 0, 0.15), rgba(255, 107, 0, 0.05));
      border: 1px solid rgba(255, 107, 0, 0.4);
      border-radius: 16px 16px 2px 16px;
      color: #ffffff;
    }

    .message-wrap.jarvis .bubble {
      background: linear-gradient(135deg, rgba(0, 212, 255, 0.08), rgba(0, 212, 255, 0.02));
      border: 1px solid rgba(0, 212, 255, 0.25);
      border-radius: 16px 16px 16px 2px;
      color: var(--white-dim);
    }

    .bubble p {
      margin-bottom: 8px;
    }
    .bubble p:last-child {
      margin-bottom: 0;
    }

    .bubble pre {
      background: rgba(1, 8, 14, 0.85);
      border: 1px solid rgba(0, 212, 255, 0.15);
      border-radius: 8px;
      padding: 10px;
      overflow-x: auto;
      font-family: SF Mono, Consolas, Monaco, monospace;
      font-size: 11px;
      margin: 8px 0;
    }

    .bubble code {
      font-family: SF Mono, Consolas, Monaco, monospace;
      font-size: 12px;
      background: rgba(0, 212, 255, 0.1);
      padding: 2px 4px;
      border-radius: 4px;
      color: #00d4ff;
    }

    .bubble pre code {
      background: none;
      padding: 0;
      color: var(--white-dim);
    }

    /* Input Bar */
    .input-bar {
      padding: 14px 20px;
      background: rgba(3, 13, 20, 0.85);
      border-top: 1px solid var(--border);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }

    .input-wrapper {
      flex: 1;
      display: flex;
      align-items: center;
      background: rgba(1, 8, 14, 0.8);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 4px 12px;
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }

    .input-wrapper:focus-within {
      border-color: var(--cyan);
      box-shadow: 0 0 10px var(--cyan-glow);
    }

    .input-field {
      flex: 1;
      background: none;
      border: none;
      color: #ffffff;
      font-size: 14px;
      outline: none;
      padding: 8px 6px;
      resize: none;
      max-height: 80px;
      font-family: inherit;
    }

    .control-btn {
      background: none;
      border: none;
      width: 36px;
      height: 36px;
      border-radius: 50%;
      color: var(--text-dim);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      transition: all 0.2s ease;
    }

    .control-btn:hover, .control-btn:active {
      color: #fff;
    }

    .mic-btn {
      border: 1px solid var(--border);
      background: rgba(13, 79, 112, 0.1);
    }

    .mic-btn.active {
      background: var(--orange-glow);
      border-color: var(--orange);
      color: #ffffff;
      animation: pulse-mic 1s infinite alternate;
    }

    @keyframes pulse-mic {
      0% { box-shadow: 0 0 4px var(--orange-glow); }
      100% { box-shadow: 0 0 12px rgba(255, 107, 0, 0.6); }
    }

    .send-btn {
      background: var(--cyan);
      color: #01080e;
      border: none;
    }

    .send-btn:hover, .send-btn:active {
      background: #ffffff;
      box-shadow: 0 0 12px var(--cyan);
    }

    /* Auth Gate Overlay */
    .auth-gate {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(1, 8, 14, 0.85);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 100;
      padding: 20px;
    }

    .auth-card {
      width: 100%;
      max-width: 400px;
      background: linear-gradient(135deg, rgba(3, 18, 28, 0.95), rgba(1, 8, 14, 0.98));
      border: 1px solid var(--glass-border);
      box-shadow: 0 8px 32px 0 rgba(0, 212, 255, 0.2);
      border-radius: 16px;
      padding: 30px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      text-align: center;
    }

    .auth-logo {
      width: 64px;
      height: 64px;
      border-radius: 50%;
      border: 2px solid var(--cyan);
      box-shadow: 0 0 15px var(--cyan-glow);
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      font-weight: bold;
      color: var(--cyan);
      background: rgba(0, 212, 255, 0.1);
    }

    .auth-title {
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 2px;
      color: var(--cyan);
    }

    .auth-desc {
      font-size: 12px;
      color: var(--text-dim);
      line-height: 1.5;
    }

    .auth-input {
      background: rgba(1, 8, 14, 0.85);
      border: 1px solid var(--border);
      border-radius: 12px;
      color: #ffffff;
      padding: 12px 16px;
      font-size: 14px;
      outline: none;
      text-align: center;
      letter-spacing: 1px;
    }

    .auth-input:focus {
      border-color: var(--cyan);
      box-shadow: 0 0 10px var(--cyan-glow);
    }

    .auth-submit {
      background: var(--cyan);
      color: #01080e;
      border: none;
      border-radius: 12px;
      padding: 12px 16px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: 1px;
      transition: all 0.2s ease;
    }

    .auth-submit:hover, .auth-submit:active {
      background: #ffffff;
      box-shadow: 0 0 15px var(--cyan);
    }

    /* Responsive Media Queries */
    @media (min-width: 768px) {
      /* On iPads and large viewports, keep drawer open */
      .diagnostics-drawer {
        position: relative;
        transform: translateX(0);
      }
    }

    .pin-container {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin: 10px 0;
    }

    .pin-input {
      width: 48px;
      height: 54px;
      background: rgba(1, 8, 14, 0.85);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: #ffffff;
      font-size: 24px;
      font-weight: bold;
      text-align: center;
      outline: none;
      transition: all 0.2s ease;
    }

    .pin-input:focus {
      border-color: var(--cyan) !important;
      box-shadow: 0 0 12px var(--cyan-glow) !important;
    }

    .auth-toggle-btn {
      background: none;
      border: none;
      color: var(--cyan);
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      text-decoration: underline;
      letter-spacing: 0.5px;
      transition: color 0.2s ease;
      margin-top: 10px;
    }

    .auth-toggle-btn:hover {
      color: #ffffff;
      text-shadow: 0 0 8px var(--cyan);
    }

    .auth-error-msg {
      color: #ff4a4a;
      font-size: 12px;
      text-align: center;
      display: none;
      margin-top: -5px;
      text-shadow: 0 0 5px rgba(255, 74, 74, 0.3);
    }

    @keyframes shake {
      0%, 100% { transform: translateX(0); }
      20%, 60% { transform: translateX(-8px); }
      40%, 80% { transform: translateX(8px); }
    }

    /* Holographic Control Deck */
    .control-deck {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 60px; /* collapsed height */
      background: linear-gradient(0deg, rgba(3, 18, 28, 0.96), rgba(1, 8, 14, 0.98));
      border-top: 1px solid var(--glass-border);
      border-radius: 20px 20px 0 0;
      box-shadow: 0 -8px 32px rgba(0, 212, 255, 0.15);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      z-index: 90;
      transition: height 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.1);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    
    .control-deck.expanded {
      height: 480px; /* expanded height */
    }

    .deck-handle {
      height: 60px;
      min-height: 60px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      cursor: pointer;
      border-bottom: 1px solid rgba(0, 212, 255, 0.05);
    }

    .deck-title {
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 1.5px;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 8px;
      text-shadow: 0 0 8px var(--cyan-glow);
    }

    .deck-toggle-icon {
      font-size: 18px;
      color: var(--cyan);
      transition: transform 0.3s ease;
    }

    .control-deck.expanded .deck-toggle-icon {
      transform: rotate(180deg);
    }

    .deck-content {
      flex: 1;
      padding: 20px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* Grid layout of remote modules */
    .deck-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 15px;
    }

    @media (max-width: 480px) {
      .deck-grid {
        grid-template-columns: 1fr;
      }
    }

    .deck-card {
      background: rgba(3, 18, 28, 0.5);
      border: 1px solid rgba(0, 212, 255, 0.08);
      border-radius: 12px;
      padding: 15px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .card-header {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-dim);
      letter-spacing: 0.5px;
      text-transform: uppercase;
      border-bottom: 1px solid rgba(0, 212, 255, 0.05);
      padding-bottom: 6px;
    }

    /* Sensor Telemetry Rows */
    .sensor-row {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
    }
    
    .sensor-val {
      font-weight: 600;
      color: #ffffff;
      text-shadow: 0 0 4px rgba(255, 255, 255, 0.3);
    }

    /* Screenshare Feed widget */
    .screen-feed-container {
      position: relative;
      width: 100%;
      height: 120px;
      border-radius: 8px;
      overflow: hidden;
      background: #000;
      border: 1px solid rgba(0, 212, 255, 0.1);
      display: flex;
      align-items: center;
      justify-content: center;
    }

    #screenFeedImg {
      width: 100%;
      height: 100%;
      object-fit: contain;
      opacity: 0.85;
      transition: opacity 0.3s ease;
    }

    #screenFeedImg.loading {
      opacity: 0.3;
    }

    .feed-controls {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 5px;
    }

    .deck-btn {
      flex: 1;
      background: rgba(0, 212, 255, 0.08);
      border: 1px solid rgba(0, 212, 255, 0.2);
      border-radius: 8px;
      color: var(--cyan);
      padding: 8px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      transition: all 0.2s ease;
    }

    .deck-btn:hover, .deck-btn:active {
      background: var(--cyan);
      color: #01080e;
      box-shadow: 0 0 10px var(--cyan-glow);
    }

    /* Sliders styling */
    .slider-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .slider-label {
      font-size: 11px;
      color: var(--text-dim);
      display: flex;
      justify-content: space-between;
    }

    .deck-slider {
      -webkit-appearance: none;
      width: 100%;
      height: 6px;
      border-radius: 3px;
      background: rgba(13, 79, 112, 0.35);
      outline: none;
    }

    .deck-slider::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: var(--cyan);
      cursor: pointer;
      box-shadow: 0 0 8px var(--cyan);
    }

    /* Actions Matrix styling */
    .actions-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
  </style>
</head>
<body>

  <!-- Auth Gate -->
  <div id="authGate" class="auth-gate" style="display: none;">
    <div class="auth-card" id="authCard">
      <div class="auth-logo">J</div>
      <h2 class="auth-title">SECURITY GATEWAY</h2>
      
      <!-- Token Pane -->
      <div id="tokenPane" style="display: flex; flex-direction: column; gap: 20px; width: 100%;">
        <p class="auth-desc">Connect with your J.A.R.V.I.S Security Token. You can copy this link directly from the MacBook desktop shell.</p>
        <input type="password" id="authTokenInput" class="auth-input" placeholder="Paste Security Token Here">
        <button id="authSubmitBtn" class="auth-submit">AUTHENTICATE SYSTEM</button>
      </div>
      
      <!-- PIN Pane -->
      <div id="pinPane" style="display: none; flex-direction: column; gap: 20px; width: 100%;">
        <p class="auth-desc">Connect a remote device (e.g. Smart TV) using a temporary 6-digit pairing code generated on your MacBook.</p>
        <div class="pin-container">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="0">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="1">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="2">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="3">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="4">
          <input type="text" inputmode="numeric" pattern="[0-9]*" class="pin-input" maxlength="1" data-index="5">
        </div>
        <div id="pinErrorMsg" class="auth-error-msg"></div>
        <div style="font-size: 11px; color: var(--text-dim);">The pairing PIN expires after 5 minutes.</div>
      </div>

      <button id="authToggleBtn" class="auth-toggle-btn">Pair with 6-Digit PIN</button>
    </div>
  </div>

  <!-- Header -->
  <header>
    <div class="brand-block">
      <div class="brand-logo">J</div>
      <div class="brand-title">
        <span class="brand-name">J.A.R.V.I.S</span>
        <span class="brand-sub">Unified Brain Server</span>
      </div>
    </div>
    <div class="status-block">
      <div id="ttsToggle" class="control-btn" style="border: 1px solid var(--border); font-size: 14px; width: 32px; height: 32px;" title="Toggle Speech Feedback">🔊</div>
      <div class="status-badge">
        <div class="status-dot"></div>
        <span id="statusLabel">ONLINE</span>
      </div>
      <button id="sidebarToggle" class="sidebar-toggle">⬡</button>
    </div>
  </header>

  <!-- Layout Body -->
  <div class="layout-body">
    <!-- Diagnostics Sidebar -->
    <div id="sidebar" class="diagnostics-drawer">
      <div class="drawer-header">Tactical Matrix</div>
      
      <div class="stat-card">
        <div class="stat-label">Model Fleet</div>
        <div id="activeModelVal" class="stat-value">claude-sonnet</div>
      </div>
      
      <div class="stat-card">
        <div class="stat-label">Uptime</div>
        <div id="uptimeVal" class="stat-value">-- : -- : --</div>
      </div>

      <div class="stat-card">
        <div class="stat-label">Active Threads</div>
        <div id="threadsVal" class="stat-value">3 Active</div>
      </div>
      
      <div class="stat-card">
        <div class="stat-label">System Memory</div>
        <div id="memoryVal" class="stat-value">Connected</div>
      </div>
    </div>

    <!-- Main Chat Window -->
    <div class="chat-container">
      <div id="messageArea" class="message-area">
        <div class="message-wrap jarvis">
          <div class="bubble">
            <p>Unified brain online, operator. I am synchronized with your MacBook runtime. How shall we proceed?</p>
          </div>
        </div>
      </div>
      
      <!-- Input Area -->
      <div class="input-bar">
        <button id="micBtn" class="control-btn mic-btn" title="Smart Listen">🎤</button>
        <div class="input-wrapper">
          <textarea id="inputField" class="input-field" placeholder="Send a message..." rows="1"></textarea>
        </div>
        <button id="sendBtn" class="control-btn send-btn">➤</button>
      </div>
    </div>
  </div>

  <!-- Holographic Control Deck -->
  <div id="controlDeck" class="control-deck">
    <!-- Clickable Handle Bar -->
    <div class="deck-handle" id="deckHandle">
      <div class="deck-title">
        <span>⬡</span> REMOTE SYSTEMS DECK
      </div>
      <div class="deck-toggle-icon" id="deckToggleIcon">▲</div>
    </div>
    
    <!-- Expanded Deck Content -->
    <div class="deck-content">
      <!-- 2-Column Grid -->
      <div class="deck-grid">
        
        <!-- MacBook Screen Live Feed -->
        <div class="deck-card" style="grid-column: span 1;">
          <div class="card-header">MacBook Live Feed</div>
          <div class="screen-feed-container">
            <img id="screenFeedImg" src="" alt="MacBook Screen Feed">
          </div>
          <div class="feed-controls">
            <button id="refreshFeedBtn" class="deck-btn">Refresh</button>
            <button id="autoFeedBtn" class="deck-btn">Auto-Stream</button>
          </div>
        </div>

        <!-- System Sensors Telemetry -->
        <div class="deck-card">
          <div class="card-header">macOS Telemetry</div>
          <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 5px;">
            <div class="sensor-row">
              <span style="color: var(--text-dim);">Battery</span>
              <span id="telBattery" class="sensor-val">--</span>
            </div>
            <div class="sensor-row">
              <span style="color: var(--text-dim);">CPU Load</span>
              <span id="telCpu" class="sensor-val">--</span>
            </div>
            <div class="sensor-row">
              <span style="color: var(--text-dim);">Memory Usage</span>
              <span id="telMemory" class="sensor-val">--</span>
            </div>
            <div class="sensor-row">
              <span style="color: var(--text-dim);">Host OS</span>
              <span id="telOs" class="sensor-val" style="font-size: 11px;">--</span>
            </div>
          </div>
        </div>

        <!-- Audio Volume & Brightness Controls -->
        <div class="deck-card">
          <div class="card-header">hardware Actuators</div>
          <div style="display: flex; flex-direction: column; gap: 15px; margin-top: 5px;">
            <div class="slider-group">
              <div class="slider-label">
                <span>System Volume</span>
                <span id="volVal">50%</span>
              </div>
              <input type="range" id="volSlider" class="deck-slider" min="0" max="100" value="50">
            </div>
            <div class="slider-group">
              <div class="slider-label">
                <span>Screen Brightness</span>
                <span id="brightVal">50%</span>
              </div>
              <input type="range" id="brightSlider" class="deck-slider" min="0" max="100" value="50">
            </div>
          </div>
        </div>

        <!-- Quick System Actions -->
        <div class="deck-card">
          <div class="card-header">Tactical Overrides</div>
          <div class="actions-grid" style="margin-top: 5px;">
            <button id="actLockBtn" class="deck-btn" style="border-color: rgba(255, 74, 74, 0.4); color: #ff6b6b;">Lock Mac</button>
            <button id="actMuteBtn" class="deck-btn">Mute Audio</button>
            <button id="actPlayBtn" class="deck-btn">Play/Pause</button>
            <button id="actNextBtn" class="deck-btn">Next Track</button>
          </div>
        </div>

      </div>
    </div>
  </div>

  <script>
    // System Configurations
    let token = localStorage.getItem('jarvis_auth_token') || '';
    let ttsEnabled = localStorage.getItem('jarvis_tts_enabled') !== 'false';
    let recognition = null;
    let isListening = false;

    const authGate = document.getElementById('authGate');
    const authTokenInput = document.getElementById('authTokenInput');
    const authSubmitBtn = document.getElementById('authSubmitBtn');
    const sendBtn = document.getElementById('sendBtn');
    const micBtn = document.getElementById('micBtn');
    const inputField = document.getElementById('inputField');
    const messageArea = document.getElementById('messageArea');
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const ttsToggle = document.getElementById('ttsToggle');

    // TV / Remote Pairing Elements
    const tokenPane = document.getElementById('tokenPane');
    const pinPane = document.getElementById('pinPane');
    const authToggleBtn = document.getElementById('authToggleBtn');
    const authCard = document.getElementById('authCard');
    const pinErrorMsg = document.getElementById('pinErrorMsg');
    const pinInputs = document.querySelectorAll('.pin-input');

    let pairingMode = 'token'; // 'token' or 'pin'

    authToggleBtn.addEventListener('click', () => {
      if (pairingMode === 'token') {
        pairingMode = 'pin';
        tokenPane.style.display = 'none';
        pinPane.style.display = 'flex';
        authToggleBtn.textContent = 'Use Security Token';
        pinInputs[0].focus();
      } else {
        pairingMode = 'token';
        tokenPane.style.display = 'flex';
        pinPane.style.display = 'none';
        authToggleBtn.textContent = 'Pair with 6-Digit PIN';
        authTokenInput.focus();
      }
      pinErrorMsg.style.display = 'none';
    });

    // Wire up PIN input autotabbing, backspace, and paste handlers
    pinInputs.forEach((input, index) => {
      // Shift focus forward on digit input
      input.addEventListener('input', (e) => {
        const val = e.target.value.replace(/[^0-9]/g, '');
        e.target.value = val;
        
        if (val && index < 5) {
          pinInputs[index + 1].focus();
        }
        
        // If all 6 digits are filled, automatically submit
        checkAndSubmitPin();
      });

      // Handle backspace (shift focus backward)
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !e.target.value && index > 0) {
          pinInputs[index - 1].focus();
        }
      });

      // Handle pasting
      input.addEventListener('paste', (e) => {
        e.preventDefault();
        const data = (e.clipboardData || window.clipboardData).getData('text');
        const digits = data.replace(/[^0-9]/g, '').substring(0, 6).split('');
        
        digits.forEach((digit, idx) => {
          if (pinInputs[idx]) {
            pinInputs[idx].value = digit;
          }
        });
        
        if (digits.length > 0) {
          const focusIdx = Math.min(digits.length, 5);
          pinInputs[focusIdx].focus();
        }
        
        checkAndSubmitPin();
      });
    });

    async function checkAndSubmitPin() {
      const pin = Array.from(pinInputs).map(i => i.value).join('');
      if (pin.length !== 6) return;

      pinErrorMsg.style.display = 'none';
      
      try {
        const resp = await fetch(`/bridge/pair?pin=${pin}`);
        const data = await resp.json();
        
        if (data.ok && data.token) {
          token = data.token;
          localStorage.setItem('jarvis_auth_token', token);
          checkAuth();
          // Clear inputs
          pinInputs.forEach(i => i.value = '');
        } else {
          handlePinFailure(data.error, data.remaining_attempts, data.lockout_seconds);
        }
      } catch (err) {
        handlePinFailure('network_error');
      }
    }

    function handlePinFailure(error, remainingAttempts, lockoutSeconds) {
      // Shake the card
      authCard.style.animation = 'none';
      void authCard.offsetWidth; // trigger reflow
      authCard.style.animation = 'shake 0.4s ease';

      // Clear all inputs and focus on first
      pinInputs.forEach(i => i.value = '');
      pinInputs[0].focus();

      pinErrorMsg.style.display = 'block';
      if (error === 'rate_limit_lockout' || lockoutSeconds) {
        const mins = Math.ceil((lockoutSeconds || 900) / 60);
        pinErrorMsg.textContent = `Brute-force detected! Locked out for ${mins} minutes.`;
      } else if (error === 'invalid_pin') {
        pinErrorMsg.textContent = `Invalid code. ${remainingAttempts} attempts remaining.`;
      } else {
        pinErrorMsg.textContent = 'Connection error. Please try again.';
      }
    }

    // UI Updates
    ttsToggle.textContent = ttsEnabled ? '🔊' : '🔇';

    // 1. Authentication Check & URL Token Grab
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get('token');
    if (urlToken) {
      token = urlToken;
      localStorage.setItem('jarvis_auth_token', urlToken);
      // Clean query string
      const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
      window.history.replaceState({path: cleanUrl}, '', cleanUrl);
    }

    async function checkAuth() {
      if (!token) {
        authGate.style.display = 'flex';
        return;
      }
      // Validate token against server before hiding auth gate
      try {
        const resp = await fetch('/auth/verify', {
          headers: { 'Authorization': 'Bearer ' + token }
        });
        if (resp.status === 401) {
          // Stale token — clear and re-prompt
          token = '';
          localStorage.removeItem('jarvis_auth_token');
          authGate.style.display = 'flex';
          return;
        }
      } catch (e) {
        // Network error — still show app, status will show OFFLINE
      }
      authGate.style.display = 'none';
      pollSystemStatus();
      clearInterval(window._statusPoll);
      window._statusPoll = setInterval(pollSystemStatus, 15000);
    }

    authSubmitBtn.addEventListener('click', () => {
      const inputVal = authTokenInput.value.trim();
      if (inputVal) {
        token = inputVal;
        localStorage.setItem('jarvis_auth_token', inputVal);
        checkAuth();
      }
    });

    // 2. Poll System Status
    async function pollSystemStatus() {
      try {
        const resp = await fetch('/status', {
          headers: { 'Authorization': 'Bearer ' + token }
        });
        if (resp.status === 401) {
          token = '';
          localStorage.removeItem('jarvis_auth_token');
          checkAuth();
          return;
        }
        const data = await resp.json();
        if (data) {
          document.getElementById('statusLabel').textContent = 'ONLINE';
          if (data.model) {
            document.getElementById('activeModelVal').textContent = data.model;
          }
          if (data.uptime) {
            document.getElementById('uptimeVal').textContent = data.uptime;
          }
        }
      } catch (e) {
        document.getElementById('statusLabel').textContent = 'OFFLINE';
      }
    }

    // 3. Floating Sidebar Toggle
    sidebarToggle.addEventListener('click', () => {
      sidebar.classList.toggle('open');
    });

    // 4. TTS Feedback Toggle
    ttsToggle.addEventListener('click', () => {
      ttsEnabled = !ttsEnabled;
      localStorage.setItem('jarvis_tts_enabled', ttsEnabled);
      ttsToggle.textContent = ttsEnabled ? '🔊' : '🔇';
    });

    // 5. Speech Recognition Setup (Browser STT)
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRecognition) {
      recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = 'en-US';

      recognition.onstart = () => {
        isListening = true;
        micBtn.classList.add('active');
        inputField.placeholder = "Listening...";
      };

      recognition.onend = () => {
        isListening = false;
        micBtn.classList.remove('active');
        inputField.placeholder = "Send a message...";
      };

      recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        inputField.value = transcript;
        sendMessage();
      };
    } else {
      micBtn.style.display = 'none';
    }

    micBtn.addEventListener('click', () => {
      if (!recognition) return;
      if (isListening) {
        recognition.stop();
      } else {
        recognition.start();
      }
    });

    // 6. Speech Synthesis (Browser TTS)
    function speakText(text) {
      if (!ttsEnabled || !window.speechSynthesis) return;
      // Strip markdown code fences before speaking
      const plainText = text.replace(/```[\\s\\S]*?```/g, "").replace(/`[^`]+`/g, "");
      
      // Stop previous utterance
      window.speechSynthesis.cancel();
      
      const utterance = new SpeechSynthesisUtterance(plainText);
      const voices = window.speechSynthesis.getVoices();
      // Prefer standard Daniel or Google UK English voices
      const englishVoice = voices.find(v => v.name.includes('Daniel') || v.name.includes('Google UK English')) || voices.find(v => v.lang.startsWith('en'));
      if (englishVoice) {
        utterance.voice = englishVoice;
      }
      utterance.rate = 1.0;
      window.speechSynthesis.speak(utterance);
    }

    // 7. Markdown parsing
    function parseMarkdown(text) {
      // Escape HTML
      let html = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      // Handle block code
      html = html.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, (match, lang, code) => {
        return `<pre><code>${code.trim()}</code></pre>`;
      });
      // Handle inline code
      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
      // Newlines
      html = html.replace(/\\n/g, '<br>');
      return html;
    }

    // 8a. Toast notification helper
    function showToast(msg, type = 'error') {
      let toast = document.getElementById('jarvisToast');
      if (!toast) {
        toast = document.createElement('div');
        toast.id = 'jarvisToast';
        toast.style.cssText = [
          'position:fixed', 'top:80px', 'left:50%', 'transform:translateX(-50%)',
          'background:rgba(3,18,28,0.97)', 'border:1px solid rgba(0,212,255,0.4)',
          'color:#00d4ff', 'padding:12px 20px', 'border-radius:10px',
          'font-size:13px', 'font-weight:600', 'z-index:9999',
          'max-width:90vw', 'text-align:center',
          'box-shadow:0 4px 20px rgba(0,212,255,0.3)',
          'transition:opacity 0.3s ease', 'pointer-events:none'
        ].join(';');
        document.body.appendChild(toast);
      }
      if (type === 'warn') toast.style.borderColor = 'rgba(255,165,0,0.6)', toast.style.color = '#ffa500';
      else if (type === 'ok') toast.style.borderColor = 'rgba(0,255,100,0.4)', toast.style.color = '#00ff88';
      else toast.style.borderColor = 'rgba(0,212,255,0.4)', toast.style.color = '#00d4ff';
      toast.textContent = msg;
      toast.style.opacity = '1';
      clearTimeout(toast._hideTimer);
      toast._hideTimer = setTimeout(() => { toast.style.opacity = '0'; }, 3500);
    }

    // 8. Chat Operations
    let _isSending = false;
    async function sendMessage() {
      if (_isSending) { showToast('⏳ Still sending\u2026 please wait', 'warn'); return; }
      const message = inputField.value.trim();
      if (!message) return;

      _isSending = true;
      sendBtn.disabled = true;
      sendBtn.style.opacity = '0.6';
      sendBtn.innerHTML = '⏳';

      inputField.value = '';
      inputField.style.height = 'auto';

      // Append User message
      const userWrap = document.createElement('div');
      userWrap.className = 'message-wrap user';
      userWrap.innerHTML = `<div class="bubble"><p>${parseMarkdown(message)}</p></div>`;
      messageArea.appendChild(userWrap);
      messageArea.scrollTop = messageArea.scrollHeight;

      // Create empty Jarvis stream bubble
      const jarvisWrap = document.createElement('div');
      jarvisWrap.className = 'message-wrap jarvis';
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.innerHTML = `<p class="streaming-cursor">...</p>`;
      jarvisWrap.appendChild(bubble);
      messageArea.appendChild(jarvisWrap);
      messageArea.scrollTop = messageArea.scrollHeight;

      try {
        const response = await fetch('/chat', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token
          },
          body: JSON.stringify({ message: message, stream: true, source: 'mobile_web' })
        });

        if (response.status === 401) {
          token = '';
          localStorage.removeItem('jarvis_auth_token');
          jarvisWrap.remove(); userWrap.remove();
          checkAuth();
          showToast('🔒 Session expired — please re-authenticate');
          return;
        }

        if (response.status === 409) {
          // Chat lock busy — restore message and auto-retry in 2s
          jarvisWrap.remove(); userWrap.remove();
          inputField.value = message;
          showToast('⏳ Jarvis is busy — retrying in 2 seconds…', 'warn');
          setTimeout(() => {
            _isSending = false;
            sendBtn.disabled = false;
            sendBtn.style.opacity = '1';
            sendBtn.innerHTML = '➤';
            sendMessage();
          }, 2000);
          return;
        }

        if (!response.ok) {
          bubble.innerHTML = `<p style="color:#ff6644">⚠ Server error ${response.status}. Please try again.</p>`;
          messageArea.scrollTop = messageArea.scrollHeight;
          showToast(`⚠ Error ${response.status} — try again`, 'warn');
          return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let fullResponse = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            if (trimmed.startsWith('data: ')) {
              const dataStr = trimmed.slice(6);
              if (dataStr === '[DONE]') {
                break;
              } else {
                try {
                  const data = JSON.parse(dataStr);
                  if (data.chunk) {
                    fullResponse += data.chunk;
                    bubble.innerHTML = `<p>${parseMarkdown(fullResponse)}</p>`;
                    messageArea.scrollTop = messageArea.scrollHeight;
                  }
                } catch (err) {}
              }
            }
          }
        }
        
        // Final Speech Feedback
        speakText(fullResponse);

      } catch (err) {
        bubble.innerHTML = `<p style="color:#ff6644">📡 Network error — check your connection.</p>`;
        messageArea.scrollTop = messageArea.scrollHeight;
        showToast('📡 Connection lost', 'warn');
      } finally {
        _isSending = false;
        sendBtn.disabled = false;
        sendBtn.style.opacity = '1';
        sendBtn.innerHTML = '➤';
      }
    }

    sendBtn.addEventListener('click', sendMessage);
    inputField.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-expand textarea
    inputField.addEventListener('input', () => {
      inputField.style.height = 'auto';
      inputField.style.height = (inputField.scrollHeight - 4) + 'px';
    });

    // ── Progressive Web App (PWA) Service Worker Registration ─────────────────
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/service-worker.js')
          .then((reg) => console.log('[PWA] ServiceWorker registered:', reg.scope))
          .catch((err) => console.warn('[PWA] ServiceWorker registration failed:', err));
      });
    }

    // ── Holographic Control Deck JS Logic ────────────────────────────────────
    const controlDeck = document.getElementById('controlDeck');
    const deckHandle = document.getElementById('deckHandle');
    
    // Toggle Deck Expand/Collapse
    deckHandle.addEventListener('click', () => {
      controlDeck.classList.toggle('expanded');
      // If expanded and we don't have telemetry, trigger a fetch
      if (controlDeck.classList.contains('expanded')) {
        fetchTelemetry();
        if (autoStreamActive) {
          startScreenStream();
        } else {
          refreshScreenFeed();
        }
      } else {
        stopScreenStream();
      }
    });

    // 1. Telemetry Sensor Loop
    const telBattery = document.getElementById('telBattery');
    const telCpu = document.getElementById('telCpu');
    const telMemory = document.getElementById('telMemory');
    const telOs = document.getElementById('telOs');

    async function fetchTelemetry() {
      if (!token) return;
      try {
        const resp = await fetch('/remote/telemetry', {
          headers: { 'Authorization': 'Bearer ' + token }
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.telemetry) {
            const tel = data.telemetry;
            telBattery.textContent = tel.battery || '--';
            telCpu.textContent = tel.cpu || '--';
            telMemory.textContent = tel.memory || '--';
            telOs.textContent = tel.os || '--';
          }
        }
      } catch (err) {
        console.warn('Failed to fetch system telemetry:', err);
      }
    }

    // Poll telemetry every 10 seconds when active
    setInterval(() => {
      if (token && controlDeck.classList.contains('expanded')) {
        fetchTelemetry();
      }
    }, 10000);

    // 2. Volume & Brightness Sliders (with debounce)
    const volSlider = document.getElementById('volSlider');
    const volVal = document.getElementById('volVal');
    const brightSlider = document.getElementById('brightSlider');
    const brightVal = document.getElementById('brightVal');

    function setupDebouncedSlider(slider, valLabel, endpointPath, fieldName) {
      let debounceTimeout = null;
      
      slider.addEventListener('input', (e) => {
        const value = e.target.value;
        valLabel.textContent = value + '%';
        
        clearTimeout(debounceTimeout);
        debounceTimeout = setTimeout(async () => {
          if (!token) return;
          try {
            const body = {};
            body[fieldName] = parseInt(value);
            await fetch(endpointPath, {
              method: 'POST',
              headers: {
                'Authorization': 'Bearer ' + token,
                'Content-Type': 'application/json'
              },
              body: JSON.stringify(body)
            });
          } catch (err) {
            console.warn(`Failed to update slider at ${endpointPath}:`, err);
          }
        }, 200);
      });
    }

    setupDebouncedSlider(volSlider, volVal, '/remote/volume', 'level');
    setupDebouncedSlider(brightSlider, brightVal, '/remote/brightness', 'level');

    // 3. System Override Actions
    async function triggerSystemAction(actionName) {
      if (!token) return;
      try {
        const resp = await fetch('/remote/action', {
          method: 'POST',
          headers: {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ action: actionName })
        });
        const data = await resp.json();
        if (data && data.message) {
          // Speak feedback on click!
          if (ttsEnabled && window.speechSynthesis) {
            const utterance = new SpeechSynthesisUtterance(data.message);
            window.speechSynthesis.speak(utterance);
          }
        }
      } catch (err) {
        console.warn(`Action ${actionName} failed:`, err);
      }
    }

    document.getElementById('actLockBtn').addEventListener('click', () => triggerSystemAction('lock'));
    document.getElementById('actMuteBtn').addEventListener('click', () => triggerSystemAction('mute'));
    document.getElementById('actPlayBtn').addEventListener('click', () => triggerSystemAction('play_pause'));
    document.getElementById('actNextBtn').addEventListener('click', () => triggerSystemAction('next_track'));

    // 4. MacBook Screenshare Live Feed
    const screenFeedImg = document.getElementById('screenFeedImg');
    const refreshFeedBtn = document.getElementById('refreshFeedBtn');
    const autoFeedBtn = document.getElementById('autoFeedBtn');
    
    let autoStreamActive = false;
    let streamInterval = null;

    function refreshScreenFeed() {
      if (!token) return;
      screenFeedImg.classList.add('loading');
      
      // Load directly using token parameter
      const imgUrl = `/remote/screenshot?token=${token}&t=${Date.now()}`;
      
      const tempImg = new Image();
      tempImg.onload = () => {
        screenFeedImg.src = imgUrl;
        screenFeedImg.classList.remove('loading');
      };
      tempImg.onerror = () => {
        screenFeedImg.classList.remove('loading');
      };
      tempImg.src = imgUrl;
    }

    refreshFeedBtn.addEventListener('click', () => {
      refreshScreenFeed();
    });

    function startScreenStream() {
      autoStreamActive = true;
      autoFeedBtn.textContent = 'Stop Stream';
      autoFeedBtn.style.background = 'var(--orange)';
      autoFeedBtn.style.color = '#ffffff';
      autoFeedBtn.style.borderColor = 'var(--orange)';
      
      refreshScreenFeed();
      streamInterval = setInterval(() => {
        if (controlDeck.classList.contains('expanded')) {
          refreshScreenFeed();
        } else {
          stopScreenStream();
        }
      }, 2500); // 2.5 second refresh screenshare
    }

    function stopScreenStream() {
      autoStreamActive = false;
      autoFeedBtn.textContent = 'Auto-Stream';
      autoFeedBtn.style.background = '';
      autoFeedBtn.style.color = '';
      autoFeedBtn.style.borderColor = '';
      
      clearInterval(streamInterval);
      streamInterval = null;
    }

    autoFeedBtn.addEventListener('click', () => {
      if (autoStreamActive) {
        stopScreenStream();
      } else {
        startScreenStream();
      }
    });

    // Init
    checkAuth();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.get("/pending")
async def pending_approvals_page(request: Request):
    """Mobile-friendly approval page — open on your phone to approve/reject pending changes."""
    from fastapi.responses import HTMLResponse
    import router as _router
    pending = _router._pending_improvements[0]

    if pending:
        file = pending.get("file", "?")
        lines = pending.get("lines_changed", "?")
        diff = pending.get("diff", "")[:2000]
        diff_html = diff.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        body = f"""
        <div class='card'>
          <h2>Pending Code Change</h2>
          <p><b>File:</b> {file}</p>
          <p><b>Lines changed:</b> {lines}</p>
          <pre class='diff'>{diff_html}</pre>
          <div class='buttons'>
            <form method='POST' action='/pending/approve' style='display:inline'>
              <button type='submit' class='btn-approve'>Approve</button>
            </form>
            <form method='POST' action='/pending/reject' style='display:inline'>
              <button type='submit' class='btn-reject'>Reject</button>
            </form>
          </div>
        </div>"""
    else:
        body = "<div class='card'><h2>No pending approvals</h2><p>Jarvis has no changes waiting for review.</p></div>"

    host = request.headers.get("host", "127.0.0.1:8765")
    html = f"""<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='UTF-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1.0'>
  <title>Jarvis — Pending Approvals</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #0d0d0d; color: #e0e0e0; padding: 16px; max-width: 600px; margin: auto; }}
    h1 {{ color: #7ec8e3; font-size: 1.4em; }}
    .card {{ background: #1a1a1a; border: 1px solid #333; border-radius: 12px; padding: 20px; margin-top: 16px; }}
    .card h2 {{ color: #7ec8e3; margin-top: 0; }}
    pre.diff {{ background: #111; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 0.78em; color: #a8d8a8; }}
    .buttons {{ margin-top: 20px; display: flex; gap: 12px; }}
    .btn-approve {{ background: #28a745; color: white; border: none; padding: 14px 28px; border-radius: 8px; font-size: 1.1em; cursor: pointer; flex: 1; }}
    .btn-reject {{ background: #dc3545; color: white; border: none; padding: 14px 28px; border-radius: 8px; font-size: 1.1em; cursor: pointer; flex: 1; }}
    .status {{ color: #aaa; margin-top: 8px; font-size: 0.85em; }}
  </style>
</head>
<body>
  <h1>Jarvis</h1>
  <p class='status'>Connected to {host}</p>
  {body}
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/pending/approve")
async def approve_pending():
    """Approve the pending self-improvement from any device."""
    from fastapi.responses import HTMLResponse
    import router as _router
    import self_improve as _si
    pending = _router._pending_improvements[0]
    if not pending:
        return HTMLResponse("<h2>No pending improvement to approve.</h2>", status_code=404)
    result = _si.apply_pending_improvement(pending)
    _router._pending_improvements[0] = None
    if result.get("error"):
        return HTMLResponse(f"<h2>Error applying change: {result['error']}</h2>", status_code=500)
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>body{{font-family:-apple-system,sans-serif;background:#0d0d0d;color:#e0e0e0;padding:24px;text-align:center}}.ok{{color:#28a745;font-size:2em}}</style>
</head><body>
<div class='ok'>&#10003; Approved</div>
<p>Applied to <b>{result.get('file','?')}</b>. {result.get('lines_changed','?')} lines changed.</p>
<p>Tell Jarvis <b>"restart yourself"</b> to reload the new code.</p>
<a href='/pending' style='color:#7ec8e3'>Back</a>
</body></html>""")


@app.post("/pending/reject")
async def reject_pending():
    """Reject the pending self-improvement from any device."""
    from fastapi.responses import HTMLResponse
    import router as _router
    _router._pending_improvements[0] = None
    return HTMLResponse("""<!DOCTYPE html>
<html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>body{font-family:-apple-system,sans-serif;background:#0d0d0d;color:#e0e0e0;padding:24px;text-align:center}.rej{color:#dc3545;font-size:2em}</style>
</head><body>
<div class='rej'>&#10007; Rejected</div>
<p>Change discarded. No files were modified.</p>
<a href='/pending' style='color:#7ec8e3'>Back</a>
</body></html>""")


# Add /pending to public paths so no Bearer token needed from phone browser
_PUBLIC_PATHS.update({"/pending", "/pending/approve", "/pending/reject"})


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


@app.get("/memory/mem0")
def get_mem0_status():
    """Status and memory count for the mem0 cross-session episodic layer."""
    import mem0_layer as _m0
    return {"ok": True, "mem0": _m0.status()}


@app.post("/memory/mem0/search")
def search_mem0(req: dict):
    """Search mem0 episodic memory for relevant context."""
    import mem0_layer as _m0
    query = (req or {}).get("query", "")
    top_k = int((req or {}).get("top_k", 5))
    hits = _m0.search(query, top_k=top_k)
    return {"ok": True, "results": hits, "formatted": _m0.format_for_prompt(hits)}


@app.get("/watcher")
def get_watcher_status():
    """Status of the proactive background watcher."""
    import jarvis_watcher as _jw
    return {"ok": True, "watcher": _jw.status()}


@app.get("/alerts")
def get_alerts():
    """Return pending proactive alerts (calendar + urgent email)."""
    import proactive_watcher as _pw
    alerts = _pw.get_alerts()
    return {"ok": True, "alerts": alerts, "count": len(alerts)}


@app.post("/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: str):
    """Dismiss a proactive alert by ID."""
    import proactive_watcher as _pw
    ok = _pw.dismiss_alert(alert_id)
    return {"ok": ok}


@app.get("/messages/access")
def get_messages_access():
    """Check iMessage Full Disk Access status and return actionable instructions."""
    import messages as _msg
    status = _msg.messages_history_access_status()
    prompt = "" if status["ok"] else _msg.messages_history_permission_text()
    return {"ok": status["ok"], "path": str(status["path"]), "reason": status.get("reason", ""), "prompt": prompt}


@app.get("/voice/diagnostics")
def get_voice_diagnostics():
    """Return mic candidates, TTS availability, and recent voice log tail."""
    import pathlib
    diag: dict = {}
    try:
        import voice as _v
        candidates = _v._microphone_candidates()
        diag["mic_candidates"] = [label for label, _ in candidates]
        diag["mic_failure_cooldown_active"] = _v._mic_failure_cooldown_until > _v._time.monotonic()
        diag["mic_last_failure"] = _v._mic_last_failure_detail
    except Exception as e:
        diag["mic_error"] = str(e)
    try:
        from local_runtime import local_tts as _lt
        cfg = _lt.config()
        diag["tts_available"] = cfg.get("available", False)
        diag["tts_voice"] = cfg.get("voice", "")
        diag["tts_enabled"] = cfg.get("enabled", False)
    except Exception as e:
        diag["tts_error"] = str(e)
    try:
        log_path = pathlib.Path.home() / "Library" / "Application Support" / "Jarvis" / ".jarvis_voice.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            diag["voice_log_tail"] = lines[-20:]
    except Exception:
        pass
    return {"ok": True, "diagnostics": diag}


@app.post("/watcher/notify")
def watcher_notify(req: dict):
    """Send a one-shot macOS notification via the watcher."""
    import jarvis_watcher as _jw
    title = (req or {}).get("title", "Jarvis")
    body  = (req or {}).get("body", "")
    subtitle = (req or {}).get("subtitle", "")
    if not body:
        return {"ok": False, "error": "body is required"}
    _jw.notify(title, body, subtitle=subtitle)
    return {"ok": True}


@app.get("/health")
def get_health():
    """Full system health snapshot — all components."""
    import jarvis_health as _jh
    statuses = _jh.check_all(force=True)
    return {
        "ok": True,
        "components": dict(statuses),
        "degraded": _jh.degraded(),
        "summary": _jh.health_summary(force=False),
    }


@app.post("/execute")
def execute_task(req: dict):
    """Execute a multi-step goal and return the synthesised result."""
    import jarvis_executor as _je
    goal = (req or {}).get("goal", "")
    if not goal:
        return {"ok": False, "error": "goal is required"}
    steps = _je.parse_steps(goal)
    results = _je.execute_steps(steps)
    summary = _je.synthesise_results(goal, results)
    return {
        "ok": True,
        "goal": goal,
        "steps": steps,
        "results": [dict(r) for r in results],
        "summary": summary,
    }


@app.post("/extract")
def extract_facts(req: dict):
    """Run fact extraction on a conversation turn and write to vault/mem0."""
    import jarvis_extractor as _jex
    user_msg  = (req or {}).get("user", "")
    assistant = (req or {}).get("assistant", "")
    if not user_msg or not assistant:
        return {"ok": False, "error": "user and assistant fields required"}
    facts = _jex.extract(user_msg, assistant)
    return {"ok": True, "facts": facts, "count": len(facts)}


@app.post("/daily-note")
def create_daily_note(req: dict = None):
    """Create today's daily note in vault/daily/YYYY-MM-DD.md.

    Optional body: {"briefing": "...", "focus": "..."}
    If omitted, runs the briefing and focus agents to populate the note.
    """
    import jarvis_agents as _ja
    body = req or {}
    briefing_text = body.get("briefing", "")
    focus_text    = body.get("focus", "")
    # Auto-generate from agents if not provided
    if not briefing_text:
        try:
            briefing_text = _ja.run_briefing()
        except Exception:
            briefing_text = ""
    if not focus_text:
        try:
            focus_text = _ja.focus_advisor()
        except Exception:
            focus_text = ""
    result = _ja.write_daily_note(briefing_text=briefing_text, focus_text=focus_text)
    return result


@app.get("/daily-note")
def get_daily_note_status():
    """Return whether today's daily note exists and its path."""
    import datetime
    import vault
    today = datetime.date.today().isoformat()
    note_path = vault.VAULT_ROOT / "daily" / f"{today}.md"
    return {
        "date": today,
        "exists": note_path.exists(),
        "path": str(note_path) if note_path.exists() else None,
    }


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
    os.environ["JARVIS_API_TOKEN"] = _API_TOKEN
    try:
        runtime_state.write_api_endpoint(_host, _port, token=_API_TOKEN)
    except Exception:
        pass

    def _run():
        uvicorn.run(app, host=_host, port=_port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="JarvisAPI")
    t.start()
    if os.getenv("JARVIS_QUIET_BOOT", "").lower() not in {"1", "true", "yes"}:
        print(f"[API] Jarvis API running at http://{_host}:{_port}")

    # Pre-load the reasoning model in the background so first query is instant
    import model_router as _mr
    if _mr.is_open_source_mode():
        from brains.brain_ollama import warm_model_cache, warm_vision_cache
        warm_thread = threading.Thread(target=warm_model_cache, daemon=True, name="OllamaWarm")
        warm_thread.start()
        vision_warm_thread = threading.Thread(target=warm_vision_cache, daemon=True, name="OllamaVisionWarm")
        vision_warm_thread.start()

    return t
