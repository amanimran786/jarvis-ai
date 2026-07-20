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
  POST /osint/subdomains — passive subdomain enumeration via subfinder
  POST /osint/whois — WHOIS domain registration lookup
  POST /osint/worldview — aggregated OSINT scan (domain or username)
  GET  /osint/cache — list cached scan results
  DELETE /osint/cache — clear scan cache
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
import re
import sqlite3
from pathlib import Path
import hashlib
import secrets
import threading
import time
import logging
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse

log = logging.getLogger("api")
from pydantic import BaseModel, Field

from router import route_stream as _route_stream_raw, record_turn as _record_turn
from operative_approval import RouteContext, redact_approval_data, redact_approval_ids
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
try:
    from harness.audit import audit_log as _audit_log
except Exception:
    def _audit_log(*a, **kw): pass
import task_runtime
import task_persistence
import semantic_memory
import graph_context as gctx
import osint_tools
import skill_monitor as _skill_monitor


def route_stream(
    user_input: str,
    *,
    context: RouteContext | None = None,
) -> tuple:
    """Thin wrapper that records every route_stream call to skill_monitor.

    Adaptive routing: if a text-only route is demoted (consistently low quality
    with ≥5 samples), the response stream is replaced with a direct LLM fallback.
    Side-effect routes (Calendar, Gmail, Messages, Browser) are never bypassed.
    """
    _t0 = time.monotonic()
    try:
        result = _route_stream_raw(user_input, context=context)
        skill = result[1] if isinstance(result, tuple) and len(result) > 1 else "chat"

        # Adaptive routing: replace demoted safe-to-bypass routes with LLM fallback
        try:
            from harness import adaptive_router as _ar
            _ar.notify_route_used(user_input, skill)
            if _ar.should_fallback(skill):
                logging.info(
                    "[AdaptiveRouter] Route '%s' is demoted — using LLM fallback", skill
                )
                fallback_stream = _ar.build_fallback_stream(user_input, skill)
                result = (fallback_stream, f"Fallback[{skill}]")
                skill = result[1]
        except Exception as _ar_exc:
            logging.debug("[AdaptiveRouter] intercept failed: %s", _ar_exc)

        _skill_monitor.record_call(skill, success=True, latency_ms=(time.monotonic() - _t0) * 1000)
        return result
    except Exception as exc:
        _skill_monitor.record_call("route_error", success=False,
                                   latency_ms=(time.monotonic() - _t0) * 1000,
                                   error=str(exc)[:200])
        raise


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


def _api_source(source: str) -> str:
    del source
    return "api"


def _api_route_context(source: str, session_id: str = "") -> RouteContext:
    normalized_source = (
        source if source in {"api", "mobile_web", "openai_compat"} else "api"
    )
    raw_session = str(session_id or "").strip() or f"{normalized_source}:default"
    normalized_session = hashlib.sha256(
        f"{normalized_source}\0{raw_session}".encode("utf-8")
    ).hexdigest()[:32]
    credential = _API_TOKEN or "local-api-token"
    principal = "api:" + hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
    return RouteContext(
        principal=principal,
        session_id=normalized_session,
        source=normalized_source,
        authenticated=bool(_API_TOKEN),
    )
# Optional cloud-mode override for mobile_web requests (avoids slow local models
# on the headless server without changing the global default mode).
# mobile_web requests bypass route_stream() and go directly to Claude Haiku.
# This is thread-safe: no global state is mutated.
_PUBLIC_PATHS = {
    "/", "/status", "/webhooks/trigger", "/webhooks/github", "/bridge/pair",
    "/manifest.json", "/service-worker.js", "/assets/icon_1024.png",
}

# TV / Remote Device Pairing PIN Registry
_active_pins: dict[str, dict] = {}           # pin_string -> {"token": token, "expires_at": timestamp}
_pin_failures: dict[str, int] = {}           # client_ip -> count
_pin_lockout_until: dict[str, float] = {}    # client_ip -> lockout_timestamp
_pin_lock = threading.Lock()                 # guards all three dicts (M1: race-free brute-force counter)


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

    # Allow custom permanent domain from .env
    custom = os.getenv("JARVIS_CLOUDFLARE_DOMAIN", "").strip().lower()
    if custom:
        custom_clean = custom.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
        allowed.add(custom_clean)

    # Allow only OUR active tunnel URL — not the *.trycloudflare.com wildcard.
    # The wildcard would accept any other Cloudflare quick-tunnel as a valid origin.
    try:
        from tunnel_manager import get_tunnel_url
        tu = get_tunnel_url()
        if tu:
            tunnel_host = tu.replace("https://", "").replace("http://", "").split("/")[0].lower()
            allowed.add(tunnel_host)
    except Exception:
        logging.debug("[API] tunnel CORS origin fetch failed", exc_info=True)

    return allowed


def _no_auth_explicitly_allowed() -> bool:
    """True only if the operator explicitly opted into unauthenticated access.

    Guards the empty-token path so serving ``api:app`` without calling
    ``start()`` (which always sets a token) fails closed instead of exposing
    every command-capable endpoint to whoever can reach the socket.
    """
    return os.getenv("JARVIS_API_ALLOW_NO_AUTH", "").strip().lower() in {"1", "true", "yes"}


def _token_authorized(request: Request) -> bool:
    expected = (_API_TOKEN or "").strip()
    if not expected:
        # No token configured. Fail CLOSED unless no-auth was explicitly
        # enabled — a missing token must never mean "allow everyone".
        return _no_auth_explicitly_allowed()
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        supplied = bearer[7:].strip()
    else:
        supplied = request.headers.get("X-Jarvis-Token", "").strip()
    
    # Query-token auth for browser GETs where headers cannot be attached.
    # Mutating endpoints must use Bearer or X-Jarvis-Token.
    if not supplied:
        if request.method == "GET" and request.url.path in {
            "/remote/screenshot", "/remote/screen/frame", "/dashboard", "/pending",
        }:
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
    # M2: removed wildcard *.trycloudflare.com — only our registered tunnel host is in allowed
    if host and host not in allowed:
        return JSONResponse(status_code=400, content={"ok": False, "error": "host_not_allowed"})
    if request.url.path not in _PUBLIC_PATHS and not _token_authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "auth_required"})
    return await call_next(request)


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    stream: bool = False
    source: str = "api"
    session_id: str = "mobile_default"
    meta: dict | None = None


class TaskRequest(BaseModel):
    prompt: str
    kind: str = "task"
    source: str = "api"
    assigned_agent_id: str = ""
    # Alias — callers keep sending agent_id; silently ignoring it made tasks
    # run unverified as jarvis-manager.
    agent_id: str = ""
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


class VaultSearchRequest(BaseModel):
    query: str
    topn: int = 3


class VaultReadRequest(BaseModel):
    path: str
    max_chars: int = 4000


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
    timeout_seconds: int = Field(45, ge=5, le=120)
    top_sites: int = Field(200, ge=20, le=500)
    max_results: int = Field(25, ge=1, le=100)


class OsintDomainTyposRequest(BaseModel):
    domain: str
    timeout_seconds: int = Field(60, ge=5, le=120)
    max_results: int = Field(25, ge=1, le=100)
    registered_only: bool = True


class OsintSubdomainRequest(BaseModel):
    domain: str
    timeout_seconds: int = Field(60, ge=5, le=300)
    max_results: int = Field(100, ge=1, le=1000)
    passive_only: bool = True


class OsintWhoisRequest(BaseModel):
    domain: str
    timeout_seconds: int = Field(15, ge=5, le=60)


class OsintWorldviewRequest(BaseModel):
    target: str
    timeout_seconds: int = Field(90, ge=10, le=300)
    max_results_per_tool: int = Field(25, ge=1, le=100)
    include_typos: bool = False


class RemoteVolumeRequest(BaseModel):
    level: int


class RemoteBrightnessRequest(BaseModel):
    level: int


class RemoteActionRequest(BaseModel):
    action: str


class RemoteClickRequest(BaseModel):
    x: float   # 0.0–1.0 relative to screen width
    y: float   # 0.0–1.0 relative to screen height
    double: bool = False


class RemoteScrollRequest(BaseModel):
    dx: float = 0.0   # horizontal scroll delta (-1.0 to 1.0)
    dy: float = 0.0   # vertical scroll delta (-1.0 to 1.0)


class RemoteTypeRequest(BaseModel):
    text: str
    submit: bool = False  # press Return after typing


class OAIMessage(BaseModel):
    role: str
    content: str = ""


class OAICompletionRequest(BaseModel):
    model: str = "jarvis"
    messages: list[OAIMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    session_id: str = "openai_default"


# ── Mobile web stream helper ───────────────────────────────────────────────────

# Cache: if Claude fails with a hard error (credits, auth), skip it for 10 min.
_claude_mobile_ok: bool = True
_claude_mobile_retry_after: float = 0.0
_CLAUDE_MOBILE_COOLDOWN = 600.0  # seconds

_MOBILE_SYSTEM_EXTRA = (
    "You are Jarvis running on the user's MacBook, accessed via mobile web. "
    "You have access to: Calendar (read/add events), Gmail (read inbox, send email), "
    "iMessage (read/send messages), Web search, Reminders, Notes, Weather, "
    "System controls (volume, brightness, screenshots), and Memory (remember facts). "
    "When asked what you can do, list these capabilities confidently. "
    "When asked for your security token or API token, respond: "
    "The Jarvis API token is in Settings > API Token on the desktop app, "
    "or check the .env file for JARVIS_API_TOKEN. "
    "Be concise — this is a mobile interface."
)

# Per-session conversation history for mobile web: session_id -> list of (role, text) tuples
# Capped at 8 turns per session. Not persisted across process restarts.
_mobile_sessions: dict[str, list[tuple[str, str]]] = {}
_MOBILE_MAX_TURNS = 8


def _mobile_history_prefix(session_id: str) -> str:
    """Return a plain-text conversation history prefix for the given session, or ''."""
    history = _mobile_sessions.get(session_id)
    if not history:
        return ""
    lines = []
    for role, text in history:
        label = "User" if role == "user" else "Jarvis"
        lines.append(f"{label}: {text}")
    return "Prior conversation:\n" + "\n".join(lines) + "\n\nCurrent message:"


def _mobile_history_append(session_id: str, user_text: str, assistant_text: str) -> None:
    """Append a turn to the session history, evicting oldest turns beyond the cap."""
    history = _mobile_sessions.setdefault(session_id, [])
    history.append(("user", user_text))
    history.append(("assistant", assistant_text))
    # Keep last _MOBILE_MAX_TURNS * 2 entries (each turn = 2 entries)
    if len(history) > _MOBILE_MAX_TURNS * 2:
        _mobile_sessions[session_id] = history[-(  _MOBILE_MAX_TURNS * 2):]


def _mobile_web_stream(
    message: str,
    system_extra: str = "",
    *,
    context: RouteContext | None = None,
):
    """Return (stream_iterator, model_label) for a mobile_web /chat request.

    Routes through the full Jarvis router (calendar, email, messages, web search,
    reminders, etc.) using a thread-local override that forces GPT-4o-mini for any
    LLM calls inside the router — bypasses slow local Ollama without touching globals.

    Falls back to direct GPT-4o-mini if route_stream itself raises.
    Thread-safe: the mobile override is thread-local and is kept active while
    the deferred stream generator is consumed.
    """
    from config import GPT_MINI
    from brains.brain import ask_stream as _openai_stream

    # ── 1. Full Jarvis routing (calendar, email, messages, search, etc.) ───────
    try:
        with model_router.mobile_web_override(system_extra=system_extra):
            stream, model = route_stream(message, context=context)

            def _wrap(gen):
                with model_router.mobile_web_override(system_extra=system_extra):
                    yield from gen

        return _wrap(stream), model
    except Exception as exc:
        log.warning("[mobile_web] route_stream failed (%s), falling back to GPT-4o-mini", exc)

    # ── 2. Direct GPT-4o-mini fallback ────────────────────────────────────────
    return _openai_stream(message, model=GPT_MINI, bypass_local=True, track_context=False, system_extra=system_extra), GPT_MINI


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    """Send a message to Jarvis and get a response."""
    source = _api_source(req.source)
    client_meta = redact_approval_data(req.meta or {})
    cancel_request = req.message.strip().lower() in {
        "/cancel",
        "/cancel task",
        "cancel task",
        "stop task",
        "/stop task",
    }
    if req.stream:
        # mobile_web bypasses _CHAT_LOCK — it goes direct to cloud, no TTS/voice
        # gating needed, and the lock must not be held across a slow HTTP stream.
        if source == "mobile_web":
            def generate_mobile():
                session_id = req.session_id or "mobile_default"
                # Route on the raw message — history as system_extra only.
                # Prepending history to message would contaminate fast-path routing
                # (e.g. "calendar" in prior turn triggers calendar dispatch for all future turns).
                history_prefix = _mobile_history_prefix(session_id)
                combined_system = (
                    _MOBILE_SYSTEM_EXTRA
                    + ("\n\n" + history_prefix if history_prefix else "")
                )
                start_seq = usage_tracker.current_seq()
                stream, model = _mobile_web_stream(
                    req.message,
                    system_extra=combined_system,
                    context=_api_route_context(source, session_id),
                )
                chunks = []
                for chunk in stream:
                    if chunk:
                        chunks.append(chunk)
                        yield f"data: {json.dumps({'chunk': chunk, 'model': model})}\n\n"
                response = "".join(chunks)
                safe_message = redact_approval_ids(req.message)
                safe_response = redact_approval_ids(response)
                _mobile_history_append(session_id, safe_message, safe_response)
                usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
                stream_source = "mobile_web_stream"
                context_stats = ctx.record_request_stats(model, source=stream_source)
                interaction = evals.log_interaction(
                    safe_message, safe_response, model,
                    source=stream_source,
                    context={**context_stats, "routing_tag": model, "client_meta": client_meta},
                )
                evals.maybe_log_automatic_failure(interaction)
                try:
                    semantic_memory.log_conversation_turn(safe_message, safe_response, model=model, source=stream_source)
                except Exception:
                    logging.debug("[API] semantic_memory turn log failed", exc_info=True)
                _record_turn(safe_message, safe_response)
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'usage': usage, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(generate_mobile(), media_type="text/event-stream")

        lock_acquired = False
        if not cancel_request and not _acquire_chat_lock():
            return JSONResponse(status_code=409, content=_chat_busy_payload())
        if not cancel_request:
            lock_acquired = True

        def generate():
            try:
                start_seq = usage_tracker.current_seq()
                stream, model = route_stream(
                    req.message,
                    context=_api_route_context(source, req.session_id or ""),
                )
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
                safe_message = redact_approval_ids(req.message)
                safe_response = redact_approval_ids(response)
                _audit_log("response_sent", model_used=model, query=safe_message[:200], chars=len(response))
                usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
                stream_source = f"{source}_stream"
                context_stats = ctx.record_request_stats(model, source=stream_source)
                interaction = evals.log_interaction(
                    safe_message,
                    safe_response,
                    model,
                    source=stream_source,
                    context={**context_stats, "routing_tag": model, "client_meta": client_meta},
                )
                evals.maybe_log_automatic_failure(interaction)
                try:
                    semantic_memory.log_conversation_turn(safe_message, safe_response, model=model, source=stream_source)
                except Exception:
                    logging.debug("[API] semantic_memory turn log failed", exc_info=True)
                # mem0 cross-session episodic memory — fire-and-forget
                _record_turn(safe_message, safe_response)
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'usage': usage, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if lock_acquired:
                    _CHAT_LOCK.release()
        return StreamingResponse(generate(), media_type="text/event-stream")

    lock_acquired = False
    if not cancel_request and not _acquire_chat_lock():
        return JSONResponse(status_code=409, content=_chat_busy_payload())
    if not cancel_request:
        lock_acquired = True

    try:
        start_seq = usage_tracker.current_seq()
        if source == "mobile_web":
            session_id = req.session_id or "mobile_default"
            history_prefix = _mobile_history_prefix(session_id)
            combined_system = (
                _MOBILE_SYSTEM_EXTRA
                + ("\n\n" + history_prefix if history_prefix else "")
            )
            stream, model = _mobile_web_stream(
                req.message,
                system_extra=combined_system,
                context=_api_route_context(source, session_id),
            )
        else:
            stream, model = route_stream(
                req.message,
                context=_api_route_context(source, req.session_id or ""),
            )
        response = "".join(stream)
        safe_message = redact_approval_ids(req.message)
        safe_response = redact_approval_ids(response)
        _audit_log("response_sent", model_used=model, query=safe_message[:200], chars=len(response))
        if source == "mobile_web":
            _mobile_history_append(session_id, safe_message, safe_response)
        usage = usage_tracker.summarize(since_seq=start_seq, include_recent=10)
        context_stats = ctx.record_request_stats(model, source=source)
        interaction = evals.log_interaction(
            safe_message,
            safe_response,
            model,
            source=source,
            context={**context_stats, "routing_tag": model, "client_meta": client_meta},
        )
        evals.maybe_log_automatic_failure(interaction)
        try:
            semantic_memory.log_conversation_turn(safe_message, safe_response, model=model, source=source)
        except Exception:
            logging.debug("[API] semantic_memory turn log failed", exc_info=True)
        # mem0 cross-session episodic memory — fire-and-forget
        _record_turn(safe_message, safe_response)
        return {"response": response, "model": model, "interaction_id": interaction["id"], "context": context_stats, "usage": usage}
    finally:
        if lock_acquired:
            _CHAT_LOCK.release()


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    entry = evals.log_failure(
        issue=redact_approval_ids(req.issue),
        interaction_id=req.interaction_id,
        expected=redact_approval_ids(req.expected),
        user_input=redact_approval_ids(req.user_input),
        response=redact_approval_ids(req.response),
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


@app.post("/agents/standup")
async def agents_standup(req: Request):
    """Generate LLM standup updates for all agents based on current task data."""
    if not _token_authorized(req):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    # Gather recent tasks per agent
    all_tasks = task_runtime.list_tasks(limit=200)
    agent_data: dict[str, list] = {}
    for t in sorted(all_tasks, key=lambda x: x.get("updated_at") or "", reverse=True):
        aid = (t.get("assigned_agent_id") or "").replace("-", "_").strip()
        if aid and aid not in ("", "manager"):
            if aid not in agent_data:
                agent_data[aid] = []
            if len(agent_data[aid]) < 3:
                agent_data[aid].append(t)

    lines = []
    for agent, tasks in sorted(agent_data.items()):
        for t in tasks:
            desc = (t.get("description") or t.get("prompt") or "")[:80]
            lines.append(f"{agent}: [{t.get('status', '?')}] {desc}")

    context = "\n".join(lines) if lines else "No tasks on record."

    prompt = (
        "Generate concise standup updates for an AI agent team. "
        "For each agent listed, write 1-2 sentences as if the agent is speaking in a daily standup. "
        "Mention what they worked on and any blockers. "
        "Respond with a JSON array ONLY — no markdown fencing. "
        'Each item: {"agent":"name","update":"...","blockers":null_or_brief_string}\n\n'
        f"Task data:\n{context}"
    )

    try:
        raw = _cloud_agent_run("pipeline_monitor", prompt, "standup_report")
        import re as _re
        m = _re.search(r"\[[\s\S]*?\]", raw)
        if m:
            updates = json.loads(m.group())
            return JSONResponse({"ok": True, "updates": updates})
    except Exception as _exc:
        log.warning("standup generation error: %s", _exc)

    # Fallback: template-based updates
    fallback = []
    for agent, tasks in agent_data.items():
        latest = tasks[0] if tasks else None
        if latest:
            st = latest.get("status", "?")
            desc = (latest.get("description") or latest.get("prompt") or "")[:60]
            upd = f"I'm currently [{st}] on: {desc}." if desc else f"Status: {st}."
            fallback.append({"agent": agent, "update": upd, "blockers": None})
    return JSONResponse({"ok": True, "updates": fallback})


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    agent = task_runtime.get_agent(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"ok": False, "error": "agent_not_found"})
    return {"ok": True, "agent": agent}


@app.get("/agents/{agent_id}/lessons/pending")
async def get_pending_lesson(agent_id: str, request: Request):
    import re as _re
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not _re.match(r'^[a-z0-9-]+$', agent_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid agent_id"})
    from task_runtime import _LESSONS_DIR
    path = _LESSONS_DIR / f"{agent_id}.pending.md"
    if not path.exists():
        return {"ok": True, "pending": None}
    return {"ok": True, "pending": path.read_text(encoding="utf-8")}


@app.post("/agents/{agent_id}/lessons/approve")
async def approve_lesson(agent_id: str, request: Request):
    import re as _re
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not _re.match(r'^[a-z0-9-]+$', agent_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid agent_id"})
    from task_runtime import _LESSONS_DIR
    pending = _LESSONS_DIR / f"{agent_id}.pending.md"
    active = _LESSONS_DIR / f"{agent_id}.md"
    if not pending.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": "no pending lesson"})
    content = pending.read_text(encoding="utf-8")
    lines = content.splitlines()
    body_lines = [
        l for l in lines
        if not l.startswith("# PENDING_APPROVAL")
        and not l.startswith("# Agent:")
        and not l.startswith("# Proposed:")
        and not l.startswith("# Failure")
    ]
    new_lesson = "\n".join(body_lines).strip()
    existing = active.read_text(encoding="utf-8").strip() if active.exists() else ""
    if new_lesson and new_lesson in existing:
        pending.unlink()
        return {"ok": True, "activated": str(active), "duplicate": True}
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"- [{stamp}] {new_lesson}" if "\n" not in new_lesson else f"## {stamp}\n{new_lesson}"
    # Approved lessons accumulate; earlier lessons are never clobbered.
    active_content = (existing + "\n\n" + entry + "\n") if existing else (entry + "\n")
    active.write_text(active_content, encoding="utf-8")
    pending.unlink()
    return {"ok": True, "activated": str(active)}


@app.post("/agents/{agent_id}/lessons/dismiss")
async def dismiss_lesson(agent_id: str, request: Request):
    import re as _re
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not _re.match(r'^[a-z0-9-]+$', agent_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid agent_id"})
    from task_runtime import _LESSONS_DIR
    pending = _LESSONS_DIR / f"{agent_id}.pending.md"
    if pending.exists():
        pending.unlink()
    return {"ok": True}


# ── Routines endpoints ──────────────────────────────────────────────────────────

class RoutineCreateRequest(BaseModel):
    name: str
    prompt: str
    agent_id: str = ""
    schedule: str
    enabled: bool = True


class RoutinePatchRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    agent_id: str | None = None
    schedule: str | None = None
    enabled: bool | None = None


@app.get("/routines")
async def list_routines(request: Request):
    from infra.rbac import registry
    registry.caller_from_request(request)
    return {"ok": True, "routines": _routines_db_list()}


@app.post("/routines")
async def create_routine(req: RoutineCreateRequest, request: Request):
    import re as _re
    import uuid as _uuid
    from datetime import datetime, timezone
    from infra.rbac import registry
    registry.caller_from_request(request)
    name = req.name.strip()[:120]
    prompt = req.prompt.strip()[:4096]
    agent_id = req.agent_id.strip()
    schedule = req.schedule.strip()
    if not name:
        return JSONResponse(status_code=400, content={"ok": False, "error": "name required"})
    if not prompt:
        return JSONResponse(status_code=400, content={"ok": False, "error": "prompt required"})
    if agent_id and not _re.match(r'^[a-z0-9-]+$', agent_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid agent_id"})
    if not _validate_schedule(schedule):
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "invalid schedule — use 'interval:<seconds>' (min 60), a cron expression, or @daily/@hourly/@weekly",
        })
    r = {
        "id": f"rtn_{_uuid.uuid4().hex[:12]}",
        "name": name,
        "prompt": prompt,
        "agent_id": agent_id,
        "schedule": schedule,
        "enabled": int(req.enabled),
        "last_fired_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _routines_db_upsert(r)
    return {"ok": True, "routine": r}


@app.patch("/routines/{routine_id}")
async def patch_routine(routine_id: str, req: RoutinePatchRequest, request: Request):
    import re as _re
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not _re.match(r'^rtn_[a-f0-9]+$', routine_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid routine_id"})
    rows = _routines_db_list()
    existing = next((r for r in rows if r["id"] == routine_id), None)
    if existing is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
    if req.name is not None:
        existing["name"] = req.name.strip()[:120]
    if req.prompt is not None:
        existing["prompt"] = req.prompt.strip()[:4096]
    if req.agent_id is not None:
        aid = req.agent_id.strip()
        if aid and not _re.match(r'^[a-z0-9-]+$', aid):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid agent_id"})
        existing["agent_id"] = aid
    if req.schedule is not None:
        if not _validate_schedule(req.schedule.strip()):
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid schedule"})
        existing["schedule"] = req.schedule.strip()
    if req.enabled is not None:
        existing["enabled"] = int(req.enabled)
    _routines_db_upsert(existing)
    return {"ok": True, "routine": existing}


@app.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str, request: Request):
    import re as _re
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not _re.match(r'^rtn_[a-f0-9]+$', routine_id):
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid routine_id"})
    _routines_db_delete(routine_id)
    return {"ok": True}


class AgentRunRequest(BaseModel):
    task: str
    context: str = ""


def _dispatch_agent_name(agent_name: str) -> str:
    return (agent_name or "").strip().replace("-", "_")


_AGENT_GARBAGE = (
    "open tasks:", "your inbox is clear", "no pending tasks",
    "tasks completed", "• ship mic fix", "jarvis decision log",
    "run_tests", "test_result", "running tests", "all tests passed",
    "successfully created", "tests are complete", "pass verification",
    "encountered pre-existing", "module resolution", "implementation and unit tests",
    "endpoint implementation", "task completed successfully",
)

# Code-writing agents: if no code block present, output is a summary not code
_CODE_AGENTS = {"backend_engineer", "frontend_designer", "qa_tester", "automation_engineer"}

# Agents that hang or produce garbage with local Ollama — go straight to cloud.
# Only _CODE_AGENTS benefit from local dispatch (GLM produces good Python/JS).
# All others: skip local, save 15s of wasted timeout.
_CLOUD_FIRST_AGENTS = {
    "security_reviewer", "security-reviewer",
    "devops_release", "devops-release",
    "researcher",
    "ux_researcher",
    "career_agent",
    "memory_librarian",
    "ai_safety_agent",
    "ai_evaluator",
    "pipeline_monitor", "pipeline-monitor",
    "output_quality_checker", "output-quality-checker",
}

def _agent_output_is_garbage(s: str, agent_name: str = "") -> bool:
    low = s.strip().lower()
    if any(g in low for g in _AGENT_GARBAGE) or len(low) < 20:
        return True
    # For code agents, a response with no code block is just a task summary
    if agent_name in _CODE_AGENTS and "```" not in s:
        return True
    return False


def _cloud_agent_run(agent_name: str, task: str, context: str) -> str:
    """OpenAI → Gemini fallback for single-agent direct assignment."""
    from config import AGENT_ROSTER
    spec = AGENT_ROSTER.get(agent_name, {})
    system = spec.get("system_prompt") or (
        f"You are the {agent_name} agent. Role: {spec.get('role', '')}. "
        "Produce concrete, actionable output. Write actual code/plans/specs — do not use tools."
    )
    user_msg = f"{task}\n\nContext:\n{context}" if context else task

    for prov, model in [("openai", "gpt-4o-mini"), ("gemini", "gemini-2.5-flash-lite")]:
        try:
            if prov == "openai":
                import openai as _oai
                key = os.getenv("OPENAI_API_KEY", "")
                if not key:
                    continue
                client = _oai.OpenAI(api_key=key, timeout=45)
                r = client.chat.completions.create(
                    model=model, max_tokens=2048,
                    messages=[{"role": "system", "content": system},
                               {"role": "user",   "content": user_msg}],
                )
                out = (r.choices[0].message.content or "").strip()
            else:
                from google import genai as _gai
                from google.genai import types as _gtypes
                key = os.getenv("GEMINI_API_KEY", "")
                if not key:
                    continue
                c = _gai.Client(api_key=key)
                r = c.models.generate_content(
                    model=model, contents=user_msg,
                    config=_gtypes.GenerateContentConfig(
                        system_instruction=system, max_output_tokens=2048, timeout=45,
                    ),
                )
                out = (r.text or "").strip()

            if out and not _agent_output_is_garbage(out):
                log.info("agent %s used cloud fallback (%s/%s)", agent_name, prov, model)
                return out
        except Exception as exc:
            log.debug("cloud agent fallback %s/%s for %s failed: %s", prov, model, agent_name, exc)

    return ""


@app.post("/agents/{agent_name}/run")
def run_agent(agent_name: str, req: AgentRunRequest, request: Request):
    from infra.rbac import registry
    import agent_dispatch as _dispatch
    dispatch_name = _dispatch_agent_name(agent_name)
    registry.enforce(request, dispatch_name)

    def generate():
        output = ""

        # Skip local dispatch entirely for agents that hang or produce garbage locally
        if dispatch_name not in _CLOUD_FIRST_AGENTS:
            try:
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                    fut = _ex.submit(lambda: "".join(_dispatch.dispatch(dispatch_name, req.task, req.context)))
                    try:
                        local_out = fut.result(timeout=15)
                        if _agent_output_is_garbage(local_out, dispatch_name):
                            log.info("agent %s local output is garbage -- trying cloud", dispatch_name)
                        else:
                            output = local_out
                    except _cf.TimeoutError:
                        log.warning("agent %s local dispatch timed out (15s) -- trying cloud", dispatch_name)
            except RuntimeError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                yield "data: [DONE]\n\n"
                return
            except Exception as exc:
                log.warning("agent %s local dispatch error: %s", dispatch_name, exc)

        # Cloud fallback if local produced nothing useful (or was skipped)
        if not output.strip():
            output = _cloud_agent_run(dispatch_name, req.task, req.context)

        if output:
            yield f"data: {json.dumps({'chunk': output})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class ManagerRunRequest(BaseModel):
    goal:       str
    session_id: str = ""
    project_id: str = ""


class ManagerRunStreamRequest(BaseModel):
    goal:       str
    cloud_plan: bool = True
    project_id: str = ""


class CloudWorkOrderRequest(BaseModel):
    brief: str
    source: str = "cloud"


@app.post("/manager/run")
def manager_run(req: ManagerRunRequest, request: Request):
    """
    Submit a broad goal to the Jarvis Manager.
    Decomposes into agent tasks, applies security gates, and schedules via event bus.
    Returns the full ExecutionPlan synchronously (decomposition + scheduling, not task results).
    """
    from infra.rbac import registry
    from core.manager import manager as _manager
    caller = registry.caller_from_request(request)
    registry.check_rate_limit(caller)
    try:
        plan = _manager.run(req.goal, session_id=req.session_id, project_id=req.project_id)
        return {"ok": True, "plan": plan.to_dict()}
    except Exception as exc:
        log.exception("manager_run error")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


# Eval log — persistent JSONL, survives restarts
from pathlib import Path as _EvalPath
_EVAL_LOG_PATH = _EvalPath("~/.jarvis_eval_log.jsonl").expanduser()
_EVAL_RUBRICS: dict[str, dict] = {}


def _append_eval_log(entry: dict) -> None:
    import time as _t
    entry.setdefault("ts", _t.time())
    try:
        with _EVAL_LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as _exc:
        log.warning("eval log write failed: %s", _exc)


_PIPELINE_ALERTS: list[dict] = []   # in-memory ring buffer, max 50
_PIPELINE_ALERTS_LOCK = threading.Lock()


def _pipeline_health_check() -> dict:
    """
    Pipeline Monitor agent — runs a synchronous health sweep.
    Returns a structured health report with any alerts.
    """
    import time
    now = time.time()
    alerts = []
    task_runtime.bootstrap()
    with task_runtime._LOCK:
        tasks = list(task_runtime._TASKS.values())

    # Detect stalled running tasks (running > 90s)
    for t in tasks:
        if t.get("status") == "running":
            started = t.get("started_at") or t.get("created_at", "")
            if started:
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
                    age = now - ts.timestamp()
                    if age > 90:
                        alerts.append({
                            "level": "warning",
                            "type": "stalled_task",
                            "task_id": t.get("id", "?"),
                            "agent": t.get("assigned_agent_id", "?"),
                            "age_seconds": int(age),
                            "message": f"Task {t.get('id','?')[-8:]} running for {int(age)}s",
                        })
                except Exception:
                    logging.debug("[API] stalled-task alert build failed", exc_info=True)

    # Detect garbage outputs in recently completed tasks
    garbage_patterns = ("task completed", "successfully created", "open tasks:",
                        "your inbox is clear", "no pending tasks")
    for t in tasks:
        if t.get("status") in {"succeeded", "completed"}:
            result = str(t.get("result") or t.get("response") or "")
            if result and len(result) < 100:
                alerts.append({
                    "level": "warning",
                    "type": "garbage_output",
                    "task_id": t.get("id", "?"),
                    "agent": t.get("assigned_agent_id", "?"),
                    "message": f"Short output ({len(result)} chars) may be garbage",
                })
            elif result:
                low = result.lower()
                if any(p in low for p in garbage_patterns):
                    alerts.append({
                        "level": "warning",
                        "type": "garbage_output",
                        "task_id": t.get("id", "?"),
                        "agent": t.get("assigned_agent_id", "?"),
                        "message": "Output matches known garbage pattern",
                    })

    by_status = {}
    for t in tasks:
        s = t.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

    report = {
        "ok": True,
        "checked_at": time.strftime("%H:%M:%S"),
        "total_tasks": len(tasks),
        "by_status": by_status,
        "alerts": alerts,
        "health": "degraded" if alerts else "healthy",
    }

    with _PIPELINE_ALERTS_LOCK:
        _PIPELINE_ALERTS.extend(alerts)
        del _PIPELINE_ALERTS[:-50]  # keep last 50

    return report


@app.get("/pipeline/health")
def pipeline_health(request: Request):
    """Pipeline Monitor — synchronous health check of all tasks and agents."""
    _token_authorized(request)
    return _pipeline_health_check()


@app.get("/pipeline/alerts")
def pipeline_alerts(request: Request):
    """Return recent pipeline alerts from the background monitor."""
    _token_authorized(request)
    with _PIPELINE_ALERTS_LOCK:
        return {"ok": True, "alerts": list(_PIPELINE_ALERTS[-20:])}


def _run_eval(agent_name: str, task_description: str, output: str, rubric_criteria: list[str] | None = None) -> dict:
    """
    AI Evaluation Lab — scores agent output quality against the task.
    Returns {"verdict": "pass"|"fail"|"needs_review", "score": 0-100,
             "feedback": str, "issues": list[str]}.
    Uses cloud (GPT-4o-mini → Gemini) for reliable structured verdicts.
    """
    from config import AGENT_ROSTER
    spec = AGENT_ROSTER.get(agent_name, {})
    role = spec.get("role", agent_name)

    eval_system = (
        "You are the Jarvis AI Evaluation Lab. You review agent outputs for quality. "
        "Given an agent role, the task it was assigned, and its output, return ONLY a JSON object:\n"
        '{"verdict":"pass"|"fail"|"needs_review","score":0-100,'
        '"feedback":"1-2 sentence summary","issues":["issue1",...]}\n\n'
        "Scoring guide: 90-100=excellent concrete output, 70-89=good with minor gaps, "
        "50-69=partial/scaffold only, <50=missing/wrong/empty.\n"
        "verdict=pass if score>=70, needs_review if 50-69, fail if <50.\n"
        "issues: list specific gaps (e.g. 'No error handling', 'Missing test for edge case X').\n"
        "Output raw JSON only — no markdown fences, no prose."
    )
    if rubric_criteria:
        eval_system += "\n\nAdditional rubric criteria:\n" + "\n".join(f"- {c}" for c in rubric_criteria)
    user_msg = (
        f"Agent role: {role}\n"
        f"Task: {task_description}\n\n"
        f"Output:\n{output[:3000]}"
    )

    for prov, model in [("openai", "gpt-4o-mini"), ("gemini", "gemini-2.5-flash-lite")]:
        try:
            if prov == "openai":
                import openai as _oai
                key = os.getenv("OPENAI_API_KEY", "")
                if not key:
                    continue
                client = _oai.OpenAI(api_key=key, timeout=20)
                r = client.chat.completions.create(
                    model=model, max_tokens=512,
                    messages=[{"role": "system", "content": eval_system},
                               {"role": "user",   "content": user_msg}],
                    response_format={"type": "json_object"},
                )
                raw = (r.choices[0].message.content or "").strip()
            else:
                from google import genai as _gai
                from google.genai import types as _gtypes
                key = os.getenv("GEMINI_API_KEY", "")
                if not key:
                    continue
                c = _gai.Client(api_key=key)
                r = c.models.generate_content(
                    model=model, contents=user_msg,
                    config=_gtypes.GenerateContentConfig(
                        system_instruction=eval_system,
                        response_mime_type="application/json",
                        max_output_tokens=512, timeout=20,
                    ),
                )
                raw = (r.text or "").strip()

            if raw:
                import json as _j
                data = _j.loads(raw)
                return {
                    "verdict":  data.get("verdict", "needs_review"),
                    "score":    int(data.get("score", 50)),
                    "feedback": data.get("feedback", ""),
                    "issues":   data.get("issues", []),
                }
        except Exception as exc:
            log.debug("eval lab %s/%s failed: %s", prov, model, exc)

    return {"verdict": "unknown", "score": 0, "feedback": "Eval providers unavailable", "issues": []}


class EvalRunRequest(BaseModel):
    agent:     str
    task:      str
    output:    str
    rubric_id: str = ""


@app.post("/eval/run")
def eval_run(req: EvalRunRequest, request: Request):
    """Standalone Eval Lab endpoint. Returns structured quality verdict."""
    from infra.rbac import registry
    registry.enforce(request, "eval")
    rubric = _EVAL_RUBRICS.get(req.rubric_id, {}) if req.rubric_id else {}
    result = _run_eval(req.agent, req.task, req.output, rubric_criteria=rubric.get("criteria"))
    _append_eval_log({"agent": req.agent, "task": req.task[:100], **result})
    return {"ok": True, **result}


@app.get("/eval/log")
def get_eval_log(request: Request, limit: int = 50, agent: str = "", verdict: str = ""):
    """Return recent eval log entries, newest first."""
    from infra.rbac import registry
    registry.enforce(request, "eval")
    entries: list[dict] = []
    if _EVAL_LOG_PATH.exists():
        lines = _EVAL_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        for line in lines:
            try:
                e = json.loads(line)
                if agent and e.get("agent") != agent:
                    continue
                if verdict and e.get("verdict") != verdict:
                    continue
                entries.append(e)
            except Exception:
                logging.debug("[API] verdict jsonl parse error — skipping line", exc_info=True)
    return {"ok": True, "entries": entries[-limit:][::-1]}


@app.get("/eval/rubrics")
def list_rubrics(request: Request):
    from infra.rbac import registry
    registry.enforce(request, "eval")
    return {"ok": True, "rubrics": list(_EVAL_RUBRICS.values())}


class RubricCreateRequest(BaseModel):
    id:       str
    name:     str
    criteria: list[str]


@app.post("/eval/rubrics")
def create_rubric(req: RubricCreateRequest, request: Request):
    from infra.rbac import registry
    registry.enforce(request, "eval")
    _EVAL_RUBRICS[req.id] = {"id": req.id, "name": req.name, "criteria": req.criteria}
    return {"ok": True, "rubric": _EVAL_RUBRICS[req.id]}


@app.delete("/eval/rubrics/{rubric_id}")
def delete_rubric(rubric_id: str, request: Request):
    from infra.rbac import registry
    registry.enforce(request, "eval")
    removed = _EVAL_RUBRICS.pop(rubric_id, None)
    return {"ok": removed is not None, "removed": removed is not None}


@app.post("/manager/run-stream")
async def manager_run_stream(req: ManagerRunStreamRequest, request: Request):
    """
    Decompose a project goal into agent tasks, register each in task_runtime,
    run agents sequentially, and stream SSE events for the dashboard.

    Events:
      {"type":"plan",      "tasks":[{agent,title,priority,needs_security_review}]}
      {"type":"start",     "task_id":"...", "agent":"...", "title":"..."}
      {"type":"chunk",     "task_id":"...", "text":"..."}
      {"type":"blocked",   "task_id":"...", "agent":"...", "reason":"..."}
      {"type":"eval_start","task_id":"...", "agent":"..."}
      {"type":"eval",      "task_id":"...", "verdict":"pass|fail|needs_review", "score":int, "feedback":"...", "issues":[...]}
      {"type":"done",      "task_id":"...", "agent":"...", "eval_verdict":"..."}
      {"type":"complete"}
    """
    import asyncio
    import re as _re

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def _cloud_decompose(goal: str):
        from core.manager import AgentTask, _agent_roster_summary
        from config import AGENT_ROSTER
        roster = _agent_roster_summary()
        system = (
            "You are the Jarvis Manager. Decompose the goal into 2-5 independent agent tasks.\n\n"
            f"Available agents:\n{roster}\n\n"
            "Rules:\n"
            "- DISTRIBUTE work across different specialist agents — never assign more than 2 tasks to the same agent.\n"
            "- Match the task to the right specialist: backend_engineer=API/data, frontend_designer=UI/components, "
            "qa_tester=tests/validation, devops_release=CI/deploy/cron, researcher=research/survey, "
            "automation_engineer=scripts/file-ops, security_reviewer=security-audit.\n"
            "- set needs_security_review=true ONLY for shell-execution, git-push, file-delete, or database-mutation tasks.\n"
            "- description must be self-contained and specific enough for the agent to work without other context.\n\n"
            "Output ONLY this JSON, no prose:\n"
            '{"goal_summary":"...","tasks":[{"title":"...","description":"...","agent":"...",'
            '"priority":5,"needs_security_review":false,"context":{}}]}'
        )
        valid = set(AGENT_ROSTER.keys())

        # Try GPT-4o-mini → Gemini → local
        providers = [("openai", "gpt-4o-mini"), ("gemini", "gemini-2.5-flash-lite")]
        raw = ""
        for prov, model in providers:
            try:
                if prov == "openai":
                    import openai
                    key = os.getenv("OPENAI_API_KEY", "")
                    if not key:
                        continue
                    client = openai.OpenAI(api_key=key)
                    r = client.chat.completions.create(
                        model=model, max_tokens=1024,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": f"Decompose:\n\n{goal}"}],
                        response_format={"type": "json_object"},
                    )
                    raw = (r.choices[0].message.content or "").strip()
                else:
                    from google import genai as gai
                    from google.genai import types as gtypes
                    key = os.getenv("GEMINI_API_KEY", "")
                    if not key:
                        continue
                    c = gai.Client(api_key=key)
                    r = c.models.generate_content(
                        model=model,
                        contents=f"Decompose:\n\n{goal}",
                        config=gtypes.GenerateContentConfig(
                            system_instruction=system,
                            response_mime_type="application/json",
                            max_output_tokens=1024,
                        ),
                    )
                    raw = (r.text or "").strip()
                if raw:
                    break
            except Exception as exc:
                log.debug("cloud decompose %s/%s failed: %s", prov, model, exc)

        if not raw:
            from core.manager import manager as _mgr
            return _mgr.decompose(goal)

        raw = _re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        try:
            data = json.loads(raw)
        except Exception:
            from core.manager import manager as _mgr
            return _mgr.decompose(goal)

        tasks = []
        for item in data.get("tasks", []):
            agent = item.get("agent", "researcher")
            if agent not in valid:
                agent = "researcher"
            tasks.append(AgentTask(
                title=str(item.get("title", "Task"))[:120],
                description=str(item.get("description", goal)),
                agent=agent,
                priority=int(item.get("priority", 5)),
                needs_security_review=bool(item.get("needs_security_review", False)),
                context=item.get("context", {}),
            ))
        return tasks or _cloud_decompose.__wrapped__(goal)  # fallback if empty

    async def _generate():
        import agent_dispatch as _dispatch
        from core.manager import AgentTask, _task_requires_manager_gate, _run_security_gate
        import uuid

        # ── Step 1: Decompose ────────────────────────────────────────────────
        try:
            tasks = await asyncio.to_thread(
                _cloud_decompose if req.cloud_plan else
                __import__("core.manager", fromlist=["manager"]).manager.decompose,
                req.goal,
            )
        except Exception as exc:
            yield _sse({"type": "error", "message": f"Decompose failed: {exc}"})
            return

        plan_data = [
            {"agent": t.agent, "title": t.title,
             "priority": t.priority, "needs_security_review": t.needs_security_review}
            for t in tasks
        ]
        yield _sse({"type": "plan", "tasks": plan_data})

        # ── Step 2: Register + run each task ─────────────────────────────────
        for t in sorted(tasks, key=lambda x: -x.priority):
            # Register in task_runtime so dashboard tabs see it
            registered = task_runtime.submit_task(
                t.description,
                kind="code",
                source="manager",
                assigned_agent_id=t.agent,
            )
            task_id = registered.get("id", str(uuid.uuid4()))
            if registered.get("approval_required") or registered.get("status") == "waiting_approval":
                yield _sse({
                    "type": "blocked",
                    "task_id": task_id,
                    "agent": t.agent,
                    "title": t.title,
                    "reason": "approval_required",
                    "approval_reason": registered.get("approval_reason", ""),
                    "confidence": registered.get("confidence", {}),
                })
                continue

            # Mark running immediately so Active tab shows it
            task_runtime._set_task_status(task_id, "running")
            yield _sse({"type": "start", "task_id": task_id,
                        "agent": t.agent, "title": t.title})

            # Security gate — fast deterministic pre-screen only in streaming pipeline.
            # LLM-based deep review is too slow (30s+ hang); pre-screen catches real attacks.
            # Full _run_security_gate() is reserved for the synchronous /manager/run path.
            if _task_requires_manager_gate(t):
                t.task_id = task_id
                try:
                    from infra.threat_screen import screen_payload as _screen_payload
                    _screen = _screen_payload(json.dumps({
                        "title": t.title, "description": t.description,
                        "agent": t.agent, "context": t.context,
                    }))
                    approved = not _screen.blocked
                    if not approved:
                        t.security_verdict = "prescreen_blocked"
                        log.warning("Pre-screen blocked task %s agent=%s findings=%d",
                                    task_id, t.agent, len(_screen.findings))
                except Exception as _exc:
                    log.warning("Pre-screen error for task %s: %s — approving cautiously", task_id, _exc)
                    approved = True
                if not approved:
                    task_runtime._set_task_status(task_id, "failed", error="prescreen_blocked")
                    yield _sse({"type": "blocked", "task_id": task_id,
                                "agent": t.agent, "reason": "prescreen_blocked"})
                    continue

            # Run agent
            context_str = json.dumps(t.context) if t.context else ""
            output = ""

            def _is_garbage(s: str) -> bool:
                return _agent_output_is_garbage(s, t.agent)

            # cloud_plan=True → use cloud for execution too (local GLM hijacks tools)
            # Also skip local for agents known to hang or produce garbage locally.
            if not req.cloud_plan and t.agent not in _CLOUD_FIRST_AGENTS:
                import concurrent.futures as _cf
                try:
                    with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                        fut = _ex.submit(
                            lambda: list(_dispatch.dispatch(t.agent, t.description, context_str))
                        )
                        try:
                            chunks = fut.result(timeout=15)
                            output = "".join(chunks)
                            if _is_garbage(output):
                                log.info("agent %s local garbage in pipeline -- using cloud", t.agent)
                                output = ""
                        except _cf.TimeoutError:
                            log.warning("agent %s pipeline local dispatch timed out (15s)", t.agent)
                except Exception as exc:
                    log.warning("agent %s local error: %s", t.agent, exc)

            # Cloud execution (primary when cloud_plan=True, fallback otherwise)
            # best_effort holds the longest non-empty response even if it fails code-block check.
            # This prevents silent empty cards — we emit best_effort if no clean output found.
            if not output.strip():
                from config import AGENT_ROSTER
                spec = AGENT_ROSTER.get(t.agent, {})
                system = spec.get("system_prompt") or (
                    f"You are the {t.agent} agent. Role: {spec.get('role','')}. "
                    "Produce concrete, actionable output. Write actual code/plans/specs — do not use tools."
                )
                best_effort = ""
                for prov, model in [("openai","gpt-4o-mini"),("gemini","gemini-2.5-flash-lite")]:
                    try:
                        if prov == "openai":
                            import openai
                            key = os.getenv("OPENAI_API_KEY","")
                            if not key: continue
                            client = openai.OpenAI(api_key=key, timeout=45)
                            r = client.chat.completions.create(
                                model=model, max_tokens=2048,
                                messages=[{"role":"system","content":system},
                                          {"role":"user","content":t.description}],
                            )
                            candidate = (r.choices[0].message.content or "").strip()
                        else:
                            from google import genai as gai
                            from google.genai import types as gtypes
                            key = os.getenv("GEMINI_API_KEY","")
                            if not key: continue
                            c = gai.Client(api_key=key)
                            r = c.models.generate_content(
                                model=model, contents=t.description,
                                config=gtypes.GenerateContentConfig(
                                    system_instruction=system, max_output_tokens=2048,
                                    timeout=45,
                                ),
                            )
                            candidate = (r.text or "").strip()
                        if candidate and not _is_garbage(candidate):
                            output = candidate
                            best_effort = candidate
                            break
                        # Keep longest non-garbage-keyword response as best_effort
                        if candidate and len(candidate) > len(best_effort):
                            best_effort = candidate
                    except Exception as exc:
                        log.debug("cloud agent %s/%s failed: %s", prov, model, exc)
                # If quality bar not met but we have something, use it — avoid silent empty card
                if not output.strip() and best_effort.strip():
                    output = best_effort
                    log.warning("agent %s: no code-block output — using best_effort (%d chars)", t.agent, len(output))

            # Emit full output as one chunk (already complete, no true streaming from dispatch)
            if output:
                yield _sse({"type": "chunk", "task_id": task_id, "text": output})

            # Eval Lab — auto-evaluate agent output quality before marking complete
            eval_result = None
            if output.strip():
                yield _sse({"type": "eval_start", "task_id": task_id, "agent": t.agent})
                try:
                    eval_result = await asyncio.wait_for(
                        asyncio.to_thread(_run_eval, t.agent, t.description, output),
                        timeout=25.0,
                    )
                    _append_eval_log({
                        "agent": t.agent, "task": t.description[:100],
                        "verdict": eval_result.get("verdict", "unknown"),
                        "score": eval_result.get("score", 0),
                        "feedback": eval_result.get("feedback", ""),
                        "issues": eval_result.get("issues", []),
                    })
                    yield _sse({"type": "eval", "task_id": task_id,
                                "verdict": eval_result.get("verdict", "unknown"),
                                "score": eval_result.get("score", 0),
                                "feedback": eval_result.get("feedback", ""),
                                "issues": eval_result.get("issues", [])})
                except asyncio.TimeoutError:
                    log.warning("Eval Lab timed out for task %s", task_id)
                    yield _sse({"type": "eval", "task_id": task_id,
                                "verdict": "timeout", "score": 0,
                                "feedback": "Eval timed out", "issues": []})
                except Exception as _exc:
                    log.warning("Eval Lab error for task %s: %s", task_id, _exc)

            # Update task_runtime with result
            task_runtime._complete_task(
                task_id,
                response=output or "(no output)",
                model="dispatch",
                usage={},
                interaction_id="",
                source="manager",
            )
            yield _sse({"type": "done", "task_id": task_id, "agent": t.agent,
                        "eval_verdict": eval_result.get("verdict") if eval_result else None})

        yield _sse({"type": "complete"})

    return StreamingResponse(_generate(), media_type="text/event-stream")


@app.post("/manager/work-order")
def manager_work_order(req: CloudWorkOrderRequest, request: Request):
    """
    Convert a pasted cloud-model brief into a local Jarvis work-order preview.
    This endpoint never schedules tasks; execution must go through Manager review.
    """
    from infra.rbac import registry
    from core.work_order import preview_work_order
    caller = registry.caller_from_request(request)
    registry.check_rate_limit(caller)
    try:
        return {"ok": True, "work_order": preview_work_order(req.brief, source=req.source)}
    except Exception as exc:
        log.exception("manager_work_order error")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/manager/status/{session_id}")
def manager_status(session_id: str, request: Request):
    """Poll task statuses for a session. Queries event bus if reachable."""
    from infra.rbac import registry
    registry.caller_from_request(request)   # auth only — no role restriction
    # Thin status endpoint: without storing the plan server-side we return event bus data
    try:
        import httpx
        event_bus_url = os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")
        resp = httpx.get(f"{event_bus_url}/metrics", timeout=2.0)
        if resp.status_code == 200:
            return {"ok": True, "metrics": resp.json()}
    except Exception:
        logging.debug("[API] event_bus metrics fetch failed", exc_info=True)
    return {"ok": True, "metrics": {}, "note": "event_bus_unreachable"}


# --- Project Plan Mode ----------------------------------------------------------

_PLAN_AGENTS = [
    "backend-engineer", "researcher", "security-reviewer", "automation-engineer",
    "devops-release", "frontend-designer", "ux-researcher", "qa-tester",
    "ai-evaluator", "ai-safety-agent", "pipeline-monitor", "output-quality-checker",
    "career-agent", "memory-librarian",
]

# ── Project orchestration state ───────────────────────────────────────────────
_PROJECTS: dict[str, dict] = {}
_PROJECTS_LOCK = threading.Lock()
_PROJECTS_DB = Path.home() / ".jarvis" / "projects.db"


def _projects_db_init() -> None:
    _PROJECTS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_PROJECTS_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            goal TEXT,
            status TEXT,
            created_at TEXT,
            synthesis_result TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS project_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            type TEXT NOT NULL,
            ts TEXT NOT NULL,
            payload TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            agent_id TEXT,
            schedule TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_fired_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

try:
    _projects_db_init()
except Exception:
    log.exception("projects DB init failed — running without persistence")


def _projects_db_load() -> None:
    if not _PROJECTS_DB.exists():
        return
    con = sqlite3.connect(str(_PROJECTS_DB))
    con.row_factory = sqlite3.Row
    try:
        for row in con.execute("SELECT * FROM projects ORDER BY rowid"):
            pid = row["id"]
            events = []
            for ev in con.execute(
                "SELECT type, ts, payload FROM project_events WHERE project_id=? ORDER BY id",
                (pid,)
            ):
                entry = {"type": ev["type"], "ts": ev["ts"]}
                entry.update(json.loads(ev["payload"]))
                events.append(entry)
            _PROJECTS[pid] = {
                "id": pid,
                "goal": row["goal"],
                "status": row["status"],
                "created_at": row["created_at"],
                "synthesis_result": row["synthesis_result"] or "",
                "events": events,
                "waves": {},
                "current_wave": 0,
            }
    finally:
        con.close()

try:
    _projects_db_load()
except Exception:
    log.exception("projects DB load failed — starting with empty project history")


def _projects_db_upsert(proj: dict) -> None:
    try:
        con = sqlite3.connect(str(_PROJECTS_DB))
        con.execute("""
            INSERT OR REPLACE INTO projects (id, goal, status, created_at, synthesis_result)
            VALUES (?, ?, ?, ?, ?)
        """, (proj["id"], proj["goal"], proj["status"], proj["created_at"],
              proj.get("synthesis_result", "")))
        con.commit()
        con.close()
    except Exception:
        log.exception("projects db upsert failed")


def _projects_db_append_event(project_id: str, event_type: str, ts: str, payload: dict) -> None:
    try:
        con = sqlite3.connect(str(_PROJECTS_DB))
        con.execute(
            "INSERT INTO project_events (project_id, type, ts, payload) VALUES (?,?,?,?)",
            (project_id, event_type, ts, json.dumps(payload))
        )
        con.commit()
        con.close()
    except Exception:
        log.exception("projects db append event failed")


# ── Routines (cron / interval-triggered tasks) ────────────────────────────────

def _routines_db_list() -> list[dict]:
    try:
        con = sqlite3.connect(str(_PROJECTS_DB))
        con.row_factory = sqlite3.Row
        rows = list(con.execute("SELECT * FROM routines ORDER BY created_at"))
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("routines db list failed")
        return []


def _routines_db_upsert(r: dict) -> None:
    try:
        con = sqlite3.connect(str(_PROJECTS_DB))
        con.execute("""
            INSERT OR REPLACE INTO routines
              (id, name, prompt, agent_id, schedule, enabled, last_fired_at, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (r["id"], r["name"], r["prompt"], r.get("agent_id") or "",
              r["schedule"], int(r.get("enabled", 1)),
              r.get("last_fired_at") or None, r["created_at"]))
        con.commit()
        con.close()
    except Exception:
        log.exception("routines db upsert failed")


def _routines_db_delete(routine_id: str) -> None:
    try:
        con = sqlite3.connect(str(_PROJECTS_DB))
        con.execute("DELETE FROM routines WHERE id=?", (routine_id,))
        con.commit()
        con.close()
    except Exception:
        log.exception("routines db delete failed")


def _cron_field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            rng, step_s = part.split("/", 1)
            step = int(step_s)
            if rng == "*":
                start, end = lo, hi
            elif "-" in rng:
                start, end = map(int, rng.split("-"))
            else:
                start, end = int(rng), hi
            if value in range(start, end + 1, step):
                return True
        elif "-" in part:
            a, b = map(int, part.split("-"))
            if a <= value <= b:
                return True
        else:
            if int(part) == value:
                return True
    return False


_CRON_MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _cron_matches(expr: str, dt) -> bool:
    expr = _CRON_MACROS.get(expr.strip().lower(), expr)
    parts = expr.split()
    if len(parts) != 5:
        return False
    min_e, hour_e, dom_e, month_e, dow_e = parts
    # cron dow: 0=Sunday ... 6=Saturday; isoweekday: 1=Mon...7=Sun
    cron_dow = dt.isoweekday() % 7
    return (
        _cron_field_matches(min_e, dt.minute, 0, 59)
        and _cron_field_matches(hour_e, dt.hour, 0, 23)
        and _cron_field_matches(dom_e, dt.day, 1, 31)
        and _cron_field_matches(month_e, dt.month, 1, 12)
        and _cron_field_matches(dow_e, cron_dow, 0, 6)
    )


def _validate_schedule(schedule: str) -> bool:
    s = schedule.strip().lower()
    if s.startswith("interval:"):
        try:
            secs = int(s[9:])
            return secs >= 60
        except ValueError:
            return False
    macro = _CRON_MACROS.get(s)
    if macro:
        return True
    parts = s.split()
    if len(parts) != 5:
        return False
    allowed = set("0123456789*,-/")
    return all(all(c in allowed for c in p) for p in parts)


def _routines_scheduler() -> None:
    """Background thread: fire scheduled routines when due."""
    from datetime import datetime, timezone
    import uuid as _uuid
    while True:
        try:
            time.sleep(30)
            now = datetime.now(timezone.utc)
            now_minute = now.strftime("%Y-%m-%dT%H:%M")
            for r in _routines_db_list():
                if not r.get("enabled"):
                    continue
                schedule = (r.get("schedule") or "").strip().lower()
                last = r.get("last_fired_at") or ""
                should_fire = False
                if schedule.startswith("interval:"):
                    try:
                        secs = int(schedule[9:])
                    except ValueError:
                        continue
                    if not last:
                        should_fire = True
                    else:
                        try:
                            from datetime import datetime as _dt
                            elapsed = (now - _dt.fromisoformat(last)).total_seconds()
                            should_fire = elapsed >= secs
                        except Exception:
                            should_fire = True
                else:
                    if _cron_matches(schedule, now):
                        # Fire once per minute — skip if already fired this minute
                        last_minute = last[:16] if last else ""
                        should_fire = last_minute != now_minute
                if not should_fire:
                    continue
                prompt = (r.get("prompt") or "")[:4096]
                agent_id = (r.get("agent_id") or "").strip()
                import re as _re
                if agent_id and not _re.match(r'^[a-z0-9-]+$', agent_id):
                    log.warning("routine %s has invalid agent_id %r — skipping", r["id"], agent_id)
                    continue
                try:
                    task_runtime.submit_task(
                        prompt,
                        kind="chat",
                        source="routine",
                        assigned_agent_id=agent_id,
                        meta={"routine_id": r["id"], "routine_name": r.get("name", "")},
                    )
                    r["last_fired_at"] = now.isoformat()
                    _routines_db_upsert(r)
                    log.info("routine %r fired: %s", r.get("name"), r["id"])
                except Exception:
                    log.exception("routine %s fire failed", r["id"])
        except Exception:
            log.exception("routines scheduler tick failed")


def _append_project_event(project_id: str, event_type: str, **kwargs) -> None:
    with _PROJECTS_LOCK:
        proj = _PROJECTS.get(project_id)
        if proj is None:
            return
        ts = _utcnow()
        proj["events"].append({"type": event_type, "ts": ts, **kwargs})
    _projects_db_append_event(project_id, event_type, ts, kwargs)


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _run_project_waves(project_id: str, tasks_by_group: dict, goal: str) -> None:
    """Background thread: dispatch wave by wave, inject prior results, synthesize."""
    import time as _time
    proj = _PROJECTS[project_id]
    proj["status"] = "running"
    _projects_db_upsert(proj)

    all_results: list[tuple[int, str, str, str]] = []  # (group, agent_id, task_id, result)

    for group_num in sorted(tasks_by_group.keys()):
        group_tasks = tasks_by_group[group_num]
        proj["current_wave"] = group_num
        _append_project_event(project_id, "wave_start", group=group_num,
                               count=len(group_tasks))

        # Context minimalism: inject only a compact index of prior results.
        # Agents pull any full result on demand via <task_result>TASK_ID</task_result>.
        prior_ctx = ""
        if all_results:
            lines = []
            for _, agent, tid, res in all_results:
                summary = " ".join(res.split())[:120]
                lines.append(f"- {tid} [{agent}]: {summary}")
            prior_ctx = (
                "Index of earlier-wave results (summaries only). "
                "To read a full result, output <task_result>TASK_ID</task_result> "
                "on its own line and the runtime will return it:\n"
                + "\n".join(lines)
            )

        wave_tasks: list[tuple[str, str]] = []  # (agent_id, task_id)
        for item in group_tasks:
            prompt = item["prompt"]
            if prior_ctx:
                prompt = f"{prompt}\n\n---\n{prior_ctx}"
            try:
                task = task_runtime.submit_task(
                    prompt,
                    kind="chat",
                    source="project",
                    assigned_agent_id=item.get("agent_id", ""),
                    meta={"confidence_score": 0.9},
                    _trusted_runtime_meta=True,
                )
                wave_tasks.append((item.get("agent_id", ""), task["id"]))
                _append_project_event(project_id, "task_dispatched",
                                       group=group_num,
                                       agent_id=item.get("agent_id", ""),
                                       task_id=task["id"])
            except Exception as exc:
                log.warning("project wave dispatch error: %s", exc)

        with _PROJECTS_LOCK:
            proj["waves"][group_num] = {
                "status": "running",
                "tasks": [{"agent_id": a, "task_id": t} for a, t in wave_tasks],
            }

        # Poll until all tasks in this wave reach terminal state
        deadline = _time.time() + 300
        terminal = {"succeeded", "failed", "cancelled", "waiting_approval"}
        while _time.time() < deadline:
            done = all(
                (task_runtime.get_task(tid) or {}).get("status") in terminal
                for _, tid in wave_tasks
            )
            if done:
                break
            _time.sleep(3)

        # Collect results
        for agent_id, task_id in wave_tasks:
            t = task_runtime.get_task(task_id) or {}
            result = (t.get("result") or "").strip()
            all_results.append((group_num, agent_id, task_id, result))

        with _PROJECTS_LOCK:
            proj["waves"][group_num]["status"] = "complete"
        _append_project_event(project_id, "wave_complete", group=group_num)

    # Synthesis: feed all results to a single agent
    proj["status"] = "synthesizing"
    _projects_db_upsert(proj)
    synthesis_sections = "\n".join(
        f"- {tid} [{agent_id}, wave {g}]: {' '.join(res.split())[:200]}"
        for g, agent_id, tid, res in all_results
        if res
    )
    synthesis_prompt = (
        f"Project goal: {goal}\n\n"
        f"Index of completed agent task results (summaries only):\n\n{synthesis_sections}\n\n"
        "To read any full result, output <task_result>TASK_ID</task_result> on its own line "
        "and the runtime will return it. Pull the results you need before synthesizing.\n\n"
        "Produce a synthesis with:\n"
        "1. Top 3 critical findings (specific, with evidence from the agents above)\n"
        "2. Prioritized action list (P0 / P1 / P2) with owner agent suggestion\n"
        "3. One concrete next step to take right now"
    )
    try:
        synth_task = task_runtime.submit_task(
            synthesis_prompt,
            kind="chat",
            source="project",
            assigned_agent_id="researcher",
            meta={"confidence_score": 0.95, "approval_reason": "internal orchestrator synthesis"},
            _trusted_runtime_meta=True,
        )
        proj["synthesis_task_id"] = synth_task["id"]
        _append_project_event(project_id, "synthesis_start",
                               task_id=synth_task["id"])

        deadline = _time.time() + 180
        while _time.time() < deadline:
            t = task_runtime.get_task(synth_task["id"]) or {}
            if t.get("status") in {"succeeded", "failed", "cancelled"}:
                break
            _time.sleep(3)

        synth_result = (task_runtime.get_task(synth_task["id"]) or {}).get("result", "")
        proj["synthesis_result"] = synth_result
    except Exception as exc:
        log.warning("synthesis error: %s", exc)
        proj["synthesis_result"] = f"Synthesis failed: {exc}"

    proj["status"] = "complete"
    _projects_db_upsert(proj)
    _append_project_event(project_id, "project_complete")


@app.post("/projects/plan")
async def projects_plan(request: Request):
    """Generate an orchestration plan for a project goal."""
    from infra.rbac import registry
    from model_router import smart_stream
    registry.caller_from_request(request)
    body = await request.json()
    goal = (body.get("goal") or "").strip()
    if not goal:
        return JSONResponse(status_code=400, content={"ok": False, "error": "goal required"})

    system = (
        "You are the Jarvis project orchestrator. Given a project goal, decompose it into "
        "a list of concrete tasks to assign to specialist agents. "
        f"Available agents: {', '.join(_PLAN_AGENTS)}. "
        "Return ONLY valid JSON — no markdown, no prose — in this exact shape:\n"
        '{"tasks": [{"agent_id": "...", "prompt": "...", "group": 1}]}\n'
        "Use group=1 for tasks that can run in parallel. "
        "Use group=2 for a review/verification agent that should run after group-1 completes "
        "and can reference their outputs. "
        "A synthesis step is added automatically — do not add one yourself. "
        "Be specific and actionable in each prompt. Limit to 6 tasks max across all groups."
    )
    try:
        stream, _ = smart_stream(goal, tool="chat", extra_system=system)
        raw = "".join(stream)
        import re as _re
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        plan = json.loads(m.group(0)) if m else {"tasks": []}
        _known_plan = set(_PLAN_AGENTS)

        def _fix_agent_id(aid: str) -> str:
            norm = aid.replace("_", "-").lower().strip()
            if norm in _known_plan:
                return norm
            if any(k in norm for k in ("quality", "checker")):
                return "output-quality-checker"
            if any(k in norm for k in ("test", "coverage", "qa")):
                return "qa-tester"
            if any(k in norm for k in ("security", "safety", "threat")):
                return "security-reviewer"
            if any(k in norm for k in ("synthesis", "compile", "summarize", "aggregate")):
                return "researcher"
            if any(k in norm for k in ("code", "backend", "api", "engineer")):
                return "backend-engineer"
            if any(k in norm for k in ("front", "ui", "design", "visual")):
                return "frontend-designer"
            if any(k in norm for k in ("monitor", "pipeline", "health")):
                return "pipeline-monitor"
            return "researcher"

        for task in plan.get("tasks", []):
            task["agent_id"] = _fix_agent_id(task.get("agent_id", ""))
        return {"ok": True, "plan": plan}
    except Exception as exc:
        log.exception("projects_plan error")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/projects/execute")
async def projects_execute(request: Request):
    """Dispatch a plan wave-by-wave; inject prior results into each subsequent wave."""
    from infra.rbac import registry
    import uuid as _uuid
    registry.caller_from_request(request)
    body = await request.json()
    tasks_in = body.get("tasks", [])
    goal = (body.get("goal") or "").strip() or "Unnamed project"
    if not tasks_in:
        return JSONResponse(status_code=400, content={"ok": False, "error": "tasks required"})

    tasks_by_group: dict[int, list] = {}
    for item in tasks_in:
        g = int(item.get("group") or 1)
        tasks_by_group.setdefault(g, []).append(item)

    project_id = f"proj_{_uuid.uuid4().hex[:12]}"
    with _PROJECTS_LOCK:
        _PROJECTS[project_id] = {
            "id": project_id,
            "goal": goal,
            "status": "starting",
            "created_at": _utcnow(),
            "current_wave": 0,
            "waves": {},
            "synthesis_task_id": "",
            "synthesis_result": "",
            "events": [],
        }
    _projects_db_upsert(_PROJECTS[project_id])

    t = threading.Thread(
        target=_run_project_waves,
        args=(project_id, tasks_by_group, goal),
        daemon=True,
    )
    t.start()
    return {"ok": True, "project_id": project_id}


@app.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    """Poll project state."""
    from infra.rbac import registry
    registry.caller_from_request(request)
    proj = _PROJECTS.get(project_id)
    if not proj:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
    with _PROJECTS_LOCK:
        return {"ok": True, "project": dict(proj)}


@app.get("/projects/{project_id}/stream")
async def project_stream(project_id: str, request: Request):
    """SSE stream of project orchestration events."""
    from infra.rbac import registry
    registry.caller_from_request(request)
    proj = _PROJECTS.get(project_id)
    if not proj:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})

    async def _gen():
        import asyncio as _asyncio
        seen = 0
        while True:
            if await request.is_disconnected():
                break
            with _PROJECTS_LOCK:
                events = list(proj.get("events", []))
                status = proj.get("status", "")
            for ev in events[seen:]:
                yield f"data: {json.dumps(ev)}\n\n"
                seen += 1
            if status == "complete":
                yield "data: [DONE]\n\n"
                break
            await _asyncio.sleep(1)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.get("/v1/models")
def oai_list_models():
    """OpenAI-compatible model list — lets OpenClaw discover Jarvis as a provider."""
    import time
    return {
        "object": "list",
        "data": [{"id": "jarvis", "object": "model", "created": int(time.time()), "owned_by": "jarvis"}],
    }


@app.post("/v1/chat/completions")
def oai_chat_completions(req: OAICompletionRequest):
    """OpenAI-compatible chat endpoint — bridges OpenClaw → Jarvis → Ollama.

    Extracts the last user message and routes through Jarvis's full intelligence
    stack (memory, vault, tools, agent dispatch) before returning.
    """
    import time, uuid

    # Only the final user turn is routed. Flattening history into router input can
    # replay earlier side-effect commands; model-only history needs a separate API.
    last_user_index = next(
        (
            index
            for index in range(len(req.messages) - 1, -1, -1)
            if (req.messages[index].role or "").lower() == "user"
            and (req.messages[index].content or "").strip()
        ),
        None,
    )
    if last_user_index is None:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "No user message found", "type": "invalid_request_error"}},
        )

    user_msg = (req.messages[last_user_index].content or "").strip()
    # Commands remain verbatim so conversation history cannot alter strict
    # router parsing or replay a prior side effect.
    routed_input = user_msg

    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if req.stream:
        def generate():
            # Opening role delta
            yield "data: " + json.dumps({
                "id": cmpl_id, "object": "chat.completion.chunk",
                "created": created, "model": req.model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }) + "\n\n"

            stream, _model = route_stream(
                routed_input,
                context=_api_route_context("openai_compat", req.session_id),
            )
            for chunk in stream:
                if chunk:
                    yield "data: " + json.dumps({
                        "id": cmpl_id, "object": "chat.completion.chunk",
                        "created": created, "model": req.model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                    }) + "\n\n"

            yield "data: " + json.dumps({
                "id": cmpl_id, "object": "chat.completion.chunk",
                "created": created, "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    # Non-streaming
    stream, _model = route_stream(
        routed_input,
        context=_api_route_context("openai_compat", req.session_id),
    )
    response_text = "".join(chunk for chunk in stream if chunk)
    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


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


def _get_agent_stats_data() -> dict:
    """Aggregate task outcomes by assigned agent for the Agent Ops stats view."""
    tasks = task_runtime.list_tasks(limit=10000)
    buckets: dict[str, dict] = {}
    success_statuses = {"succeeded", "completed"}
    for task in tasks:
        agent_id = str(task.get("assigned_agent_id") or "unassigned")
        bucket = buckets.setdefault(
            agent_id,
            {"agent_id": agent_id, "task_count": 0, "success_count": 0, "last_active": None},
        )
        bucket["task_count"] += 1
        if task.get("status") in success_statuses:
            bucket["success_count"] += 1
        timestamp = task.get("updated_at") or task.get("finished_at") or task.get("created_at") or None
        if timestamp and (bucket["last_active"] is None or timestamp > bucket["last_active"]):
            bucket["last_active"] = timestamp

    agents = []
    for bucket in buckets.values():
        count = int(bucket["task_count"])
        agents.append({
            "agent_id": bucket["agent_id"],
            "task_count": count,
            "success_rate": float(bucket["success_count"] / count) if count else 0.0,
            "last_active": bucket["last_active"],
        })
    agents.sort(key=lambda item: (-item["task_count"], item["agent_id"]))
    return {"agents": agents, "total": len(agents)}


@app.get("/stats/agents")
def stats_agents():
    return _get_agent_stats_data()


@app.get("/security/events")
def security_events(limit: int = 200, action: str = "", severity: str = "", task_id: str = ""):
    from infra.security_audit import read_events
    return {
        "ok": True,
        "events": read_events(
            limit=limit,
            action=action,
            severity=severity,
            task_id=task_id,
        ),
    }


@app.get("/security/summary")
def security_summary(limit: int = 1000):
    from infra.security_audit import summarize
    return {"ok": True, "summary": summarize(limit=limit)}


def _safe_pipeline_audit_summary() -> dict:
    try:
        from infra import pipeline_audit
        report = pipeline_audit.run_once(append=False)
        return {
            "ok": True,
            "verdict": report.get("verdict", "UNKNOWN"),
            "severity_counts": report.get("severity_counts", {}),
            "acknowledged_count": report.get("acknowledged_count", 0),
            "verdicts_count": report.get("verdicts_count", 0),
            "records_parsed": report.get("records_parsed", 0),
            "findings": report.get("findings", [])[:10],
        }
    except Exception as exc:
        return {"ok": False, "verdict": "UNKNOWN", "error": str(exc)}


def _safe_upgrade_loop_state() -> dict:
    try:
        from core import upgrade_loop
        summary = upgrade_loop.summarize()
        tickets = upgrade_loop.read_tickets()
        recent = list(reversed(tickets[-50:]))
        return {
            "ok": True,
            "summary": summary,
            "tickets": recent,
            "watchlist": upgrade_loop.DEFAULT_WATCHLIST,
        }
    except Exception as exc:
        return {"ok": False, "summary": {}, "tickets": [], "watchlist": [], "error": str(exc)}


@app.get("/dashboard/state")
def dashboard_state(request: Request):
    """Aggregated local Manager Console state for the dashboard."""
    _token_authorized(request)
    tasks = task_runtime.list_tasks(limit=200)
    active_statuses = {"queued", "assigned", "waiting_approval", "running", "streaming"}
    completed_statuses = {"succeeded", "completed"}
    failed_statuses = {"failed", "cancelled", "denied"}

    def _is_restart_casualty(t: dict) -> bool:
        # The daemon mass-fails in-flight tasks on restart (bootstrap) with
        # error 'daemon_restart'. Those are infrastructure interruptions, not
        # agent quality failures — report them separately from the failure rate.
        return t.get("status") == "failed" and (t.get("error") or "") == "daemon_restart"
    try:
        training_status = local_training.status()
    except Exception as exc:
        training_status = {"ok": False, "error": str(exc)}
    try:
        eval_status = local_model_eval.status()
    except Exception as exc:
        eval_status = {"ok": False, "error": str(exc)}
    try:
        from infra.security_audit import summarize as _security_summarize
        security = _security_summarize(limit=1000)
    except Exception as exc:
        security = {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "tasks": {
            "total": len(tasks),
            "active": sum(1 for t in tasks if t.get("status") in active_statuses),
            "awaiting_approval": sum(1 for t in tasks if t.get("status") == "waiting_approval"),
            "completed": sum(1 for t in tasks if t.get("status") in completed_statuses),
            "failed": sum(1 for t in tasks if t.get("status") in failed_statuses and not _is_restart_casualty(t)),
            "interrupted": sum(1 for t in tasks if _is_restart_casualty(t)),
        },
        "agents": _get_agent_stats_data(),
        "pipeline_audit": _safe_pipeline_audit_summary(),
        "upgrade_loop": _safe_upgrade_loop_state(),
        "training": training_status,
        "evals": eval_status,
        "security": security,
        "projects": _safe_projects_status(),
    }


@app.post("/tasks")
def create_task(req: TaskRequest):
    task = task_runtime.submit_task(
        req.prompt,
        kind=req.kind,
        source=req.source,
        assigned_agent_id=req.assigned_agent_id or req.agent_id,
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


@app.delete("/tasks/history")
def clear_task_history(request: Request):
    """Remove all terminal (succeeded/failed/cancelled) tasks from memory and persistence."""
    if not _token_authorized(request):
        return JSONResponse(status_code=401, content={"ok": False, "error": "auth_required"})
    removed = task_runtime.purge_terminal_tasks()
    return {"ok": True, "removed": removed}


def create_pairing_pin() -> str:
    """Generate a cryptographically random 6-digit numeric pairing PIN."""
    token = (_API_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Jarvis API token is not initialized; start the API before generating a pairing PIN.")
    with _pin_lock:
        # secrets.randbelow is CSPRNG — safe for auth tokens
        for _ in range(20):
            pin = f"{100000 + secrets.randbelow(900000)}"
            if pin not in _active_pins or _active_pins[pin]["expires_at"] < time.time():
                break
        else:
            pin = f"{100000 + secrets.randbelow(900000)}"

        _active_pins[pin] = {
            "token": token,
            "expires_at": time.time() + 300.0  # 5 minutes
        }
    # Do NOT log the PIN value — it is a short-lived credential
    log.info("[Bridge] Temporary pairing PIN generated (not logged for security)")
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
    pin_clean = "".join(c for c in (pin or "") if c.isdigit())

    with _pin_lock:  # M1: atomic read-modify-write — no race on brute-force counter
        if _pin_lockout_until.get(client_ip, 0) > now:
            remaining = int(_pin_lockout_until[client_ip] - now)
            return JSONResponse(
                status_code=429,
                content={"ok": False, "error": "rate_limit_lockout", "lockout_seconds": remaining}
            )

        # Evict expired PINs
        expired = [k for k, v in _active_pins.items() if v["expires_at"] < now]
        for k in expired:
            _active_pins.pop(k, None)

        if pin_clean in _active_pins:
            token = _active_pins[pin_clean]["token"]
            _active_pins.pop(pin_clean, None)   # one-time use
            _pin_failures[client_ip] = 0
            log.info(f"[Bridge] TV paired successfully from IP {client_ip}")
            return {"ok": True, "token": token}

        failures = _pin_failures.get(client_ip, 0) + 1
        _pin_failures[client_ip] = failures

        if failures >= 5:
            _pin_lockout_until[client_ip] = now + 900.0
            log.warning("[Bridge] IP %s triggered brute-force lockout on PIN pairing.", client_ip)
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

    battery_info = "Unknown"
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=1.5)
        pct_match = re.search(r"(\d+)%", out)
        if pct_match:
            battery_info = f"{pct_match.group(1)}%"
            if "charging" in out.lower() or "ac power" in out.lower():
                battery_info += " (Charging)"
    except Exception:
        logging.debug("[API] battery stat unavailable", exc_info=True)

    cpu_info = "Unknown"
    try:
        out = subprocess.check_output(["sysctl", "-n", "vm.loadavg"], text=True, timeout=1.0)
        load_match = re.findall(r"\d+\.\d+", out)
        if load_match:
            cpu_info = f"{load_match[0]} (1m load)"
    except Exception:
        logging.debug("[API] CPU stat unavailable", exc_info=True)

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
        logging.debug("[API] memory stat unavailable", exc_info=True)

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


@app.get("/remote/screen/frame")
def remote_screen_frame(request: Request):
    """Return a compressed JPEG screenshot suitable for live screen streaming.

    Scaled to 50%, JPEG quality 60 — ~140KB per frame, ~280ms capture.
    Query param ?token= accepted for <img src> embedding (no Authorization header).
    """
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import io
        from PIL import Image
        from desktop.screen_capture import capture_screenshot_temp
        temp_path = capture_screenshot_temp("png")
        img = Image.open(temp_path).convert("RGB")
        os.unlink(temp_path)
        w, h = img.size
        img_small = img.resize((w // 2, h // 2), Image.LANCZOS)
        buf = io.BytesIO()
        img_small.save(buf, format="JPEG", quality=60, optimize=True)
        buf.seek(0)
        # Expose actual frame dimensions in headers for JS click mapping
        headers = {
            "X-Frame-Width": str(w // 2),
            "X-Frame-Height": str(h // 2),
            "X-Screen-Width": str(w),
            "X-Screen-Height": str(h),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(buf, media_type="image/jpeg", headers=headers)
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/click")
def remote_click(request: Request, req: RemoteClickRequest):
    """Send a mouse click at a relative position (x: 0.0–1.0, y: 0.0–1.0)."""
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import Quartz
        from Quartz import (
            CGEventCreateMouseEvent, CGEventPost, CGMainDisplayID, CGDisplayBounds,
            kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft,
            kCGHIDEventTap, kCGEventMouseMoved,
        )
        bounds = CGDisplayBounds(CGMainDisplayID())
        sw, sh = bounds.size.width, bounds.size.height
        px = max(0.0, min(1.0, req.x)) * sw
        py = max(0.0, min(1.0, req.y)) * sh
        point = Quartz.CGPoint(px, py)

        # Move cursor to location first
        move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, move)

        # Click down + up (repeat for double-click)
        clicks = 2 if req.double else 1
        for _ in range(clicks):
            down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, down)
            up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, up)

        return {"ok": True, "x": px, "y": py}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/scroll")
def remote_scroll(request: Request, req: RemoteScrollRequest):
    """Send a scroll wheel event. dy > 0 = scroll up, dy < 0 = scroll down."""
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import Quartz
        from Quartz import CGEventCreateScrollWheelEvent, CGEventPost, kCGHIDEventTap
        SCALE = 12
        scroll_y = int(req.dy * SCALE)
        scroll_x = int(req.dx * SCALE)
        # kCGScrollEventUnitLine = 1
        event = CGEventCreateScrollWheelEvent(None, 1, 2, scroll_y, scroll_x)
        CGEventPost(kCGHIDEventTap, event)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/remote/type")
def remote_type(request: Request, req: RemoteTypeRequest):
    """Type text on the Mac as keyboard input. Optionally press Return after."""
    if not _token_authorized(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import subprocess
        safe_text = req.text.replace('"', '\\"').replace('\\', '\\\\')[:500]
        script = f'tell application "System Events" to keystroke "{safe_text}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        if req.submit:
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 36'],
                capture_output=True, timeout=3,
            )
        return {"ok": True, "typed": req.text[:40]}
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


@app.post("/vault/search")
def search_vault(req: VaultSearchRequest):
    bounded_topn = max(1, min(int(req.topn or 3), 10))
    results = vault.search(req.query, topn=bounded_topn)
    return {"ok": True, "query": req.query, "results": results, "count": len(results)}


@app.post("/vault/read")
def read_vault(req: VaultReadRequest):
    bounded_max_chars = max(200, min(int(req.max_chars or 4000), 12000))
    result = vault.read(req.path, max_chars=bounded_max_chars)
    return {"ok": bool(result.get("ok")), "result": result}


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
async def root_redirect(request: Request):
    """Redirect root to the agent ops dashboard."""
    from fastapi.responses import RedirectResponse
    token = request.query_params.get("token", "")
    dest = f"/dashboard?token={token}" if token else "/dashboard"
    return RedirectResponse(url=dest)



# ── Meeting Room + Agent Panel JS (raw string) ───────────────────────────────

_MEETING_JS = r"""
// ============================================================
// MEETING ROOM + AGENT DETAIL PANEL
// ============================================================

let _meetingActive = false;
let _meetingPhase = 'idle'; // idle | gathering | standup | done
let _meetingSpeakerIdx = -1;
let _meetingNames = [];
let _meetingUpdates = {}; // name -> {update, blockers}
let _meetingTimer = null;
let _meetingSpots = [];
let _lastTaskData = [];  // updated by refresh(), used by standup board

// Meeting table center (lobby zone)
const _MTABLE = [6.5, 12.0];

function _calcSpots(n){
  return Array.from({length:n},(_,i)=>{
    const a = (i/n)*Math.PI*2 - Math.PI/2;
    return [_MTABLE[0]+Math.cos(a)*2.2, _MTABLE[1]+Math.sin(a)*2.1];
  });
}

function _wMeetingDraw(){
  // no-op: Three.js renders the meeting table in the 3D scene
}

function _wAgentClick(name){
  if(_meetingActive) return; // don't open panel during meeting
  _showAgentPanel(name);
}

function _showAgentPanel(name){
  const panel=document.getElementById('agent-panel');
  if(!panel) return;
  document.getElementById('ap-icon').textContent=AGENT_ICONS[name]||'🤖';
  document.getElementById('ap-name').textContent=name.replace(/_/g,' ').replace(/\b./g,c=>c.toUpperCase());
  const zone=AGENT_HOME_ZONES[name]||'?';
  const zoneLabels={'eng':'Engineering Bay','design':'Design Studio','research':'Research Lab','qa':'QA Floor','safety':'Safety Room','ops':'Ops Center','lobby':'Lobby'};
  document.getElementById('ap-dept').textContent=zoneLabels[zone]||zone;
  // Status & task
  const tasks=(_lastTaskData||[]).filter(t=>normalizeId(t.assigned_agent_id)===name)
    .sort((a,b)=>new Date(b.updated_at||b.created_at||0)-new Date(a.updated_at||a.created_at||0));
  const latest=tasks[0];
  const st=latest?latest.status:'idle';
  const stColors={'running':'#7ec8e3','queued':'#7ec8e3','working':'#7ec8e3','succeeded':'#28c76f','completed':'#28c76f','failed':'#ea5455','waiting_approval':'#ffd700','idle':'#555'};
  document.getElementById('ap-status').innerHTML=`<span style="font-size:.75em;background:rgba(0,0,0,.4);border-radius:4px;padding:2px 8px;color:${stColors[st]||'#888'}">${st}</span>`;
  document.getElementById('ap-task').textContent=latest?(latest.description||latest.prompt||'(no description)').slice(0,120):'No active task.';
  // Recent history
  const hist=tasks.slice(0,5);
  document.getElementById('ap-history').innerHTML=hist.length?
    `<div style="font-size:.7em;letter-spacing:.08em;color:#555;margin-bottom:4px">RECENT (${hist.length})</div>`+
    hist.map(t=>{
      const ic=t.status==='succeeded'||t.status==='completed'?'✅':t.status==='failed'?'❌':t.status==='running'||t.status==='queued'?'⏳':'○';
      return `<div style="font-size:.8em;color:#666;padding:3px 0;border-bottom:1px solid #1a1a1a">${ic} ${(t.description||t.prompt||'').slice(0,50)}</div>`;
    }).join('')
    :'<div style="font-size:.8em;color:#444">No task history.</div>';
  document.getElementById('ap-assign-btn').onclick=()=>{panel.style.display='none';quickAssign(name);};
  panel.style.display='block';
}

// ── Meeting / Standup ─────────────────────────────────────────────────────

function startMeeting(){
  if(_meetingActive){ endMeeting(); return; }
  const overlay=document.getElementById('standup-overlay');
  if(!overlay) return;
  _meetingActive=true;
  _meetingPhase='gathering';
  _meetingNames=Object.keys(_ISO_DESKS);
  _meetingSpeakerIdx=-1;
  _meetingSpots=_calcSpots(_meetingNames.length);
  // Dispatch agents to meeting spots
  _meetingNames.forEach((name,i)=>{
    const a=_wAgents[name];
    if(!a) return;
    a.state='meeting_walk';
    a.tx=_meetingSpots[i][0]; a.ty=_meetingSpots[i][1];
    a.task='';
  });
  // Build standup cards
  overlay.style.display='flex';
  _buildStandupCards();
  document.getElementById('standup-status-line').textContent='Gathering in meeting room…';
  document.getElementById('standup-progress').textContent=`0/${_meetingNames.length}`;
  // Fetch LLM updates in background
  _fetchStandupUpdates();
  // Start standup after agents gather
  _meetingTimer=setTimeout(_advanceSpeaker,3500);
}

function endMeeting(){
  _meetingActive=false;
  _meetingPhase='idle';
  if(_meetingTimer){clearTimeout(_meetingTimer);_meetingTimer=null;}
  document.getElementById('standup-overlay').style.display='none';
  // Return all agents to idle
  Object.keys(_wAgents).forEach(name=>{
    const a=_wAgents[name];
    if(!a) return;
    a.state='idle'; a.task='';
    const [dc,dr]=_ISO_DESKS[name]||[0,0];
    a.tx=dc+0.5; a.ty=dr+0.5;
  });
}

function _advanceSpeaker(){
  if(!_meetingActive){return;}
  _meetingSpeakerIdx++;
  if(_meetingSpeakerIdx>=_meetingNames.length){
    _meetingPhase='done';
    document.getElementById('standup-status-line').textContent='Standup complete.';
    document.getElementById('standup-speaker-name').textContent='—';
    _meetingTimer=setTimeout(endMeeting,3000);
    return;
  }
  const name=_meetingNames[_meetingSpeakerIdx];
  _meetingPhase='standup';
  // Update UI
  document.getElementById('standup-status-line').textContent='Team updates in progress';
  document.getElementById('standup-speaker-name').textContent=name.replace(/_/g,' ');
  document.getElementById('standup-progress').textContent=`${_meetingSpeakerIdx+1}/${_meetingNames.length}`;
  // Highlight card
  document.querySelectorAll('.standup-card').forEach(c=>c.style.borderColor='#1e2a3a');
  const card=document.getElementById('sc-'+name);
  if(card) card.style.borderColor='#4a9eff';
  document.querySelectorAll('.spk-badge').forEach(b=>b.style.display='none');
  const badge=document.getElementById('spk-'+name);
  if(badge) badge.style.display='inline';
  // Canvas: set agent to speaking state with update text
  Object.keys(_wAgents).forEach(n=>{
    if(_wAgents[n]&&(_wAgents[n].state==='seated'||_wAgents[n].state==='speaking'))
      _wAgents[n].state=n===name?'speaking':'seated';
  });
  if(_wAgents[name]){
    const upd=_meetingUpdates[name];
    _wAgents[name].task=upd?upd.update.slice(0,40):'...';
  }
  // Scroll card into view
  if(card) card.scrollIntoView({behavior:'smooth',block:'nearest'});
  // Advance after speak duration (5s per agent, faster with more agents)
  const dur=Math.max(3500,7000-_meetingNames.length*200);
  _meetingTimer=setTimeout(_advanceSpeaker,dur);
}

function _buildStandupCards(){
  const container=document.getElementById('standup-cards');
  if(!container) return;
  container.innerHTML='';
  Object.keys(_ISO_DESKS).forEach(name=>{
    const tasks=(_lastTaskData||[]).filter(t=>normalizeId(t.assigned_agent_id)===name)
      .sort((a,b)=>new Date(b.updated_at||b.created_at||0)-new Date(a.updated_at||a.created_at||0));
    const latest=tasks[0];
    const blockers=tasks.filter(t=>t.status==='failed').slice(0,2);
    const card=document.createElement('div');
    card.id='sc-'+name;
    card.className='standup-card';
    card.style.cssText='background:#0d1520;border:1px solid #1e2a3a;border-radius:8px;padding:14px;transition:border-color .3s,background .3s;min-width:220px';
    card.innerHTML=`
      <div style="font-size:.6em;letter-spacing:.12em;color:#444;margin-bottom:3px">PARTICIPANT</div>
      <div style="font-size:1em;font-weight:bold;color:#eee;margin-bottom:8px;display:flex;align-items:center;gap:6px">
        ${AGENT_ICONS[name]||'🤖'} ${name.replace(/_/g,' ')}
        <span id="spk-${name}" class="spk-badge" style="display:none;background:#7ec8e3;color:#0a1520;padding:1px 6px;border-radius:3px;font-size:.6em;letter-spacing:.1em">SPEAKING</span>
      </div>
      <div style="font-size:.65em;letter-spacing:.08em;color:#444;margin-bottom:2px">CURRENT TASK</div>
      <div id="sct-${name}" style="font-size:.8em;color:#bbb;margin-bottom:8px;min-height:16px">${latest?(latest.description||latest.prompt||'').slice(0,60):'—'}</div>
      <div style="font-size:.65em;letter-spacing:.08em;color:#444;margin-bottom:2px">STATUS UPDATE</div>
      <div id="scu-${name}" style="font-size:.8em;color:#7ec8e3;margin-bottom:8px;min-height:32px;font-style:italic;color:#668">Generating update…</div>
      <div style="font-size:.65em;letter-spacing:.08em;color:#444;margin-bottom:2px">BLOCKERS</div>
      <div id="scb-${name}" style="font-size:.8em;color:${blockers.length?'#ea5455':'#333'}">${blockers.length?blockers.map(t=>(t.description||t.prompt||'').slice(0,50)).join('; '):'No blockers reported.'}</div>`;
    container.appendChild(card);
  });
}

async function _fetchStandupUpdates(){
  try{
    const resp=await apiFetch('/agents/standup',{method:'POST'});
    const updates=(resp.updates||[]);
    updates.forEach(u=>{
      _meetingUpdates[u.agent]=u;
      const el=document.getElementById('scu-'+u.agent);
      if(el) el.textContent=u.update||'No update available.';
      if(u.blockers){
        const bel=document.getElementById('scb-'+u.agent);
        if(bel){bel.textContent=u.blockers; bel.style.color='#ea5455';}
      }
    });
  } catch(e){ console.warn('standup fetch failed',e); }
}

// Expose lastTaskData updater — called from refresh()
function _updateMeetingTaskData(tasks){ _lastTaskData=tasks||[]; }
"""

# ── 3D Office World JS — Three.js Roblox-style engine (raw string) ───────────

_WORLD_JS = r"""
// ── Three.js 3D Roblox-style Office World ──────────────────────────────────

const _TILE = 1.0;
const _WORLD_COLS = 22, _WORLD_ROWS = 15;

const _ISO_ZONES = {
  eng:[0,0,6,4], design:[7,0,13,4], research:[14,0,21,4],
  qa:[0,5,6,9], safety:[7,5,13,9], ops:[14,5,21,9],
  lobby:[0,10,13,14], gym:[14,10,21,14],
};
const _ISO_FLOOR_HEX = {
  eng:0x0d1e2e, design:0x190d22, research:0x0d1e1e,
  qa:0x1e1a08, safety:0x1e0d0d, ops:0x100d1e,
  lobby:0x121212, gym:0x1a0d1a,
};
const _ISO_LABELS = {
  eng:'⚙ Engineering', design:'🎨 Design', research:'🔬 Research',
  qa:'🧪 QA', safety:'🛡 Safety', ops:'📡 Ops',
  lobby:'🛋 Lobby', gym:'🏋 Eval Gym',
};
const _ISO_DESKS = {
  backend_engineer:[2,1], automation_engineer:[4,2], devops_release:[1,3],
  frontend_designer:[8,1], ux_researcher:[11,2],
  researcher:[15,1], security_reviewer:[18,3],
  qa_tester:[2,6], ai_evaluator:[4,7],
  ai_safety_agent:[9,6],
  pipeline_monitor:[15,6], output_quality_checker:[18,7],
  career_agent:[3,11], memory_librarian:[8,12],
};
const _ISO_COLORS = {
  backend_engineer:0x4a9eff, automation_engineer:0x2d7dd2, devops_release:0x1e5fa8,
  frontend_designer:0xff6b9d, ux_researcher:0xe05888,
  researcher:0x5bc8c8, security_reviewer:0x3a9090,
  qa_tester:0xf5a623, ai_evaluator:0xd4870a,
  ai_safety_agent:0xef4444,
  pipeline_monitor:0x8b5cf6, output_quality_checker:0x6d3aed,
  career_agent:0xa0a0a0, memory_librarian:0x7a7a8a,
};
const _GYM_SPOT = [17, 12];

const AGENT_HOME_ZONES = {
  backend_engineer:'eng', automation_engineer:'eng', devops_release:'eng',
  frontend_designer:'design', ux_researcher:'design',
  researcher:'research', security_reviewer:'research',
  qa_tester:'qa', ai_evaluator:'qa',
  ai_safety_agent:'safety',
  pipeline_monitor:'ops', output_quality_checker:'ops',
  career_agent:'lobby', memory_librarian:'lobby',
};
const AGENT_ICONS = {
  backend_engineer:'⚙️', automation_engineer:'🤖', devops_release:'🚀',
  frontend_designer:'🎨', ux_researcher:'🔍', researcher:'📚',
  security_reviewer:'🛡️', qa_tester:'🧪', ai_evaluator:'🏆',
  ai_safety_agent:'⚠️', pipeline_monitor:'📡', output_quality_checker:'✅',
  career_agent:'💼', memory_librarian:'🧠',
};

// ── Shared agent state (also used by _MEETING_JS) ──
let _wAgents = {};

// ── Three.js state ──
let _3wScene=null, _3wRenderer=null, _3wCamera=null;
let _3wInited=false, _3wCanvas=null, _3wRaf=null, _3wLastTs=0;
let _3wMeshes={}, _3wClickables=[];
let _3wRaycaster=null, _3wMouse=null;

// ── Init ──────────────────────────────────────────────────────────────────────

function initWorldIfNeeded(){
  const canvas=document.getElementById('office-canvas');
  if(!canvas||_3wInited) return;
  if(typeof THREE==='undefined') return;
  _3wCanvas=canvas;
  _3wInited=true;

  const W=canvas.parentElement.clientWidth||900;
  const H=Math.round(Math.max(340, Math.min(620, W*9/16)));
  canvas.style.height=H+'px';

  _3wRenderer=new THREE.WebGLRenderer({canvas,antialias:true});
  _3wRenderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
  _3wRenderer.shadowMap.enabled=true;
  _3wRenderer.shadowMap.type=THREE.PCFSoftShadowMap;
  _3wRenderer.setSize(W,H,false);

  _3wScene=new THREE.Scene();
  _3wScene.background=new THREE.Color(0x0d1117);
  _3wScene.fog=new THREE.Fog(0x0d1117,30,65);

  _3wSetupCamera(W,H);
  _3wBuildLights();
  _3wBuildFloor();
  _3wBuildFurniture();
  _3wBuildMeetingTable();

  _3wRaycaster=new THREE.Raycaster();
  _3wMouse=new THREE.Vector2();
  canvas.addEventListener('click',_3wHandleClick);
  window.addEventListener('resize',_3wOnResize);

  Object.keys(_ISO_DESKS).forEach(name=>{
    const [dc,dr]=_ISO_DESKS[name];
    _wAgents[name]={
      state:'idle', tx:dc+0.5, ty:dr+0.5,
      x:dc+0.5, y:dr+0.5,
      frame:Math.random()*1000, wanderT:Math.random()*3,
      celebT:0, task:'', score:null,
    };
    const m=_3wMakeChar(name);
    _3wScene.add(m.group);
    _3wMeshes[name]=m;
    m.group.position.set(dc+0.5,0,dr+0.5);
  });

  _3wRaf=requestAnimationFrame(_3wTick);
}

function _3wSetupCamera(W,H){
  const aspect=(W||900)/(H||614);
  const fH=15;
  _3wCamera=new THREE.OrthographicCamera(-fH*aspect,fH*aspect,fH,-fH,-200,200);
  const cx=_WORLD_COLS/2, cz=_WORLD_ROWS/2, d=24;
  _3wCamera.position.set(cx+d,d*0.6,cz+d);
  _3wCamera.lookAt(cx,0,cz);
}

function _3wOnResize(){
  if(!_3wCanvas||!_3wRenderer||!_3wCamera) return;
  const W=_3wCanvas.parentElement.clientWidth||900;
  const H=Math.round(Math.max(340, Math.min(620, W*9/16)));
  _3wCanvas.style.height=H+'px';
  _3wRenderer.setSize(W,H,false);
  const fH=15, aspect=W/H;
  _3wCamera.left=-fH*aspect; _3wCamera.right=fH*aspect;
  _3wCamera.top=fH; _3wCamera.bottom=-fH;
  _3wCamera.updateProjectionMatrix();
}

// ── Lights ────────────────────────────────────────────────────────────────────

function _3wBuildLights(){
  _3wScene.add(new THREE.AmbientLight(0x8899cc,0.9));
  const sun=new THREE.DirectionalLight(0xfff0e0,2.0);
  sun.position.set(14,22,8);
  sun.castShadow=true;
  sun.shadow.mapSize.set(2048,2048);
  sun.shadow.camera.left=-20; sun.shadow.camera.right=20;
  sun.shadow.camera.top=20; sun.shadow.camera.bottom=-20;
  sun.shadow.camera.near=0.5; sun.shadow.camera.far=80;
  sun.shadow.bias=-0.001;
  _3wScene.add(sun);
  const fill=new THREE.DirectionalLight(0x4466cc,0.45);
  fill.position.set(-8,12,-6);
  _3wScene.add(fill);
}

// ── Floor & Zones ─────────────────────────────────────────────────────────────

function _3wBuildFloor(){
  const gnd=new THREE.Mesh(
    new THREE.PlaneGeometry(_WORLD_COLS+2,_WORLD_ROWS+2),
    new THREE.MeshStandardMaterial({color:0x080810,roughness:1})
  );
  gnd.rotation.x=-Math.PI/2;
  gnd.position.set(_WORLD_COLS/2,-0.02,_WORLD_ROWS/2);
  gnd.receiveShadow=true;
  _3wScene.add(gnd);

  const borderCol={
    eng:0x1a4a7a, design:0x4a1a7a, research:0x1a5a4a,
    qa:0x5a3a1a, safety:0x5a1a1a, ops:0x1a3a5a,
    lobby:0x3a3a1a, gym:0x5a0aaa,
  };

  Object.entries(_ISO_ZONES).forEach(([zone,[x1,z1,x2,z2]])=>{
    const w=(x2-x1)*_TILE, d=(z2-z1)*_TILE;
    const floor=new THREE.Mesh(
      new THREE.BoxGeometry(w,0.05,d),
      new THREE.MeshStandardMaterial({color:_ISO_FLOOR_HEX[zone]||0x111111,roughness:0.9,metalness:0.04})
    );
    floor.position.set(x1*_TILE+w/2,0,z1*_TILE+d/2);
    floor.receiveShadow=true;
    _3wScene.add(floor);
    const pts=[
      new THREE.Vector3(x1,0.04,z1),new THREE.Vector3(x2,0.04,z1),
      new THREE.Vector3(x2,0.04,z2),new THREE.Vector3(x1,0.04,z2),
      new THREE.Vector3(x1,0.04,z1),
    ];
    _3wScene.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({color:borderCol[zone]||0x2a2a4a,transparent:true,opacity:0.65})
    ));
    const lbl=_3wLabelSprite(_ISO_LABELS[zone]||zone);
    lbl.position.set(x1*_TILE+w/2,0.35,z1*_TILE+1.1);
    lbl.scale.set(Math.min(w*0.88,6.5),0.55,1);
    _3wScene.add(lbl);
  });
}

// ── Furniture ─────────────────────────────────────────────────────────────────

function _3wBuildFurniture(){
  Object.entries(_ISO_DESKS).forEach(([name,[dc,dr]])=>{
    _3wPlaceDesk(dc,dr,_ISO_COLORS[name]||0x334466);
  });
  _3wPlaceGym();
}

function _3wPlaceDesk(col,row,agentColor){
  const desk=new THREE.Mesh(
    new THREE.BoxGeometry(0.82,0.06,0.52),
    new THREE.MeshStandardMaterial({color:0x1a1a2e,roughness:0.7})
  );
  desk.position.set(col+0.5,0.40,row+0.5);
  desk.castShadow=true; desk.receiveShadow=true;
  _3wScene.add(desk);
  const mon=new THREE.Mesh(
    new THREE.BoxGeometry(0.42,0.30,0.04),
    new THREE.MeshStandardMaterial({color:agentColor,roughness:0.3,metalness:0.4,emissive:agentColor,emissiveIntensity:0.2})
  );
  mon.position.set(col+0.5,0.63,row+0.26);
  _3wScene.add(mon);
  [[-.36,.21],[.36,.21],[-.36,-.16],[.36,-.16]].forEach(([lx,lz])=>{
    const leg=new THREE.Mesh(
      new THREE.BoxGeometry(0.04,0.38,0.04),
      new THREE.MeshStandardMaterial({color:0x22223a})
    );
    leg.position.set(col+0.5+lx,0.19,row+0.5+lz);
    _3wScene.add(leg);
  });
}

function _3wPlaceGym(){
  const [gc,gr]=_GYM_SPOT;
  const bar=new THREE.Mesh(
    new THREE.CylinderGeometry(0.04,0.04,2.4,8),
    new THREE.MeshStandardMaterial({color:0x888888,metalness:0.8,roughness:0.2})
  );
  bar.rotation.z=Math.PI/2;
  bar.position.set(gc+0.5,0.56,gr+0.5);
  _3wScene.add(bar);
  [-0.9,0.9].forEach(ox=>{
    const wt=new THREE.Mesh(
      new THREE.CylinderGeometry(0.34,0.34,0.14,14),
      new THREE.MeshStandardMaterial({color:0x222244,metalness:0.6,roughness:0.3})
    );
    wt.rotation.z=Math.PI/2;
    wt.position.set(gc+0.5+ox,0.56,gr+0.5);
    _3wScene.add(wt);
  });
  const mat=new THREE.Mesh(
    new THREE.BoxGeometry(3,0.03,2),
    new THREE.MeshStandardMaterial({color:0x2a0a4a,roughness:0.95})
  );
  mat.position.set(gc+0.5,0.04,gr+0.5);
  _3wScene.add(mat);
}

function _3wBuildMeetingTable(){
  const tx=6.5, tz=12.0;
  const tblMat=new THREE.MeshStandardMaterial({color:0x5a2e10,roughness:0.5,metalness:0.1});
  const tbl=new THREE.Mesh(new THREE.CylinderGeometry(1.5,1.4,0.12,24),tblMat);
  tbl.position.set(tx,0.48,tz);
  tbl.castShadow=true; tbl.receiveShadow=true;
  _3wScene.add(tbl);
  const ped=new THREE.Mesh(new THREE.CylinderGeometry(0.14,0.20,0.46,10),tblMat);
  ped.position.set(tx,0.23,tz);
  _3wScene.add(ped);
}

// ── Roblox R6 Character ───────────────────────────────────────────────────────

function _3wMakeChar(name){
  const col=_ISO_COLORS[name]||0x4a4a8a;
  const bodyMat=new THREE.MeshStandardMaterial({color:col,roughness:0.3,metalness:0.05});
  const skinMat=new THREE.MeshStandardMaterial({color:0xf5c08a,roughness:0.5});
  const pantMat=new THREE.MeshStandardMaterial({color:0x111122,roughness:0.8});
  const group=new THREE.Group();

  // Torso
  const torso=new THREE.Mesh(new THREE.BoxGeometry(0.72,0.88,0.42),bodyMat);
  torso.position.y=0.44; torso.castShadow=true;
  group.add(torso);

  // Head pivot at neck
  const headPiv=new THREE.Group();
  headPiv.position.y=0.93;
  group.add(headPiv);
  const head=new THREE.Mesh(new THREE.BoxGeometry(0.65,0.65,0.65),skinMat);
  head.position.y=0.325; head.castShadow=true;
  headPiv.add(head);
  [-0.12,0.12].forEach(ex=>{
    const eye=new THREE.Mesh(
      new THREE.BoxGeometry(0.09,0.07,0.02),
      new THREE.MeshStandardMaterial({color:0x111111})
    );
    eye.position.set(ex,0.35,0.325);
    headPiv.add(eye);
  });
  const crown=_3wMakeCrown();
  crown.position.y=0.67;
  headPiv.add(crown);

  // Arms
  const lArmPiv=new THREE.Group();
  lArmPiv.position.set(-0.53,0.82,0);
  group.add(lArmPiv);
  const lArm=new THREE.Mesh(new THREE.BoxGeometry(0.34,0.74,0.34),bodyMat);
  lArm.position.y=-0.37; lArm.castShadow=true;
  lArmPiv.add(lArm);

  const rArmPiv=new THREE.Group();
  rArmPiv.position.set(0.53,0.82,0);
  group.add(rArmPiv);
  const rArm=new THREE.Mesh(new THREE.BoxGeometry(0.34,0.74,0.34),bodyMat);
  rArm.position.y=-0.37; rArm.castShadow=true;
  rArmPiv.add(rArm);

  // Legs
  const lLegPiv=new THREE.Group();
  lLegPiv.position.set(-0.19,0.02,0);
  group.add(lLegPiv);
  const lLeg=new THREE.Mesh(new THREE.BoxGeometry(0.34,0.74,0.34),pantMat);
  lLeg.position.y=-0.37; lLeg.castShadow=true;
  lLegPiv.add(lLeg);

  const rLegPiv=new THREE.Group();
  rLegPiv.position.set(0.19,0.02,0);
  group.add(rLegPiv);
  const rLeg=new THREE.Mesh(new THREE.BoxGeometry(0.34,0.74,0.34),pantMat);
  rLeg.position.y=-0.37; rLeg.castShadow=true;
  rLegPiv.add(rLeg);

  // Name label sprite
  const label=_3wNameSprite(name.replace(/_/g,' '),col);
  label.position.y=2.15;
  label.scale.set(3.2,0.72,1);
  group.add(label);

  // Status dot
  const dotMat=new THREE.MeshStandardMaterial({color:0x555577,emissive:0x555577,emissiveIntensity:0.6});
  const dot=new THREE.Mesh(new THREE.SphereGeometry(0.09,8,6),dotMat);
  dot.position.set(0.4,2.05,0);
  group.add(dot);

  // Invisible hit box for raycaster
  const hit=new THREE.Mesh(
    new THREE.BoxGeometry(0.9,2.1,0.65),
    new THREE.MeshBasicMaterial({visible:false})
  );
  hit.position.y=0.9;
  hit.userData={agentName:name};
  group.add(hit);
  _3wClickables.push(hit);

  return {group, parts:{torso,headPiv,lArmPiv,rArmPiv,lLegPiv,rLegPiv,dot}};
}

function _3wMakeCrown(){
  const g=new THREE.Group();
  const mat=new THREE.MeshStandardMaterial({color:0xffd700,roughness:0.25,metalness:0.7});
  g.add(new THREE.Mesh(new THREE.CylinderGeometry(0.29,0.31,0.13,6,1,true),mat));
  for(let i=0;i<5;i++){
    const a=(i/5)*Math.PI*2;
    const s=new THREE.Mesh(new THREE.ConeGeometry(0.046,0.20,5),mat);
    s.position.set(Math.cos(a)*0.27,0.17,Math.sin(a)*0.27);
    g.add(s);
  }
  return g;
}

function _3wNameSprite(text,color){
  const cv=document.createElement('canvas');
  cv.width=256; cv.height=52;
  const c=cv.getContext('2d');
  c.clearRect(0,0,256,52);
  c.fillStyle='rgba(0,0,0,0.62)';
  if(c.roundRect) c.roundRect(2,2,252,48,8); else c.rect(2,2,252,48);
  c.fill();
  c.fillStyle='#'+(color).toString(16).padStart(6,'0');
  c.font='bold 18px -apple-system,Arial,sans-serif';
  c.textAlign='center'; c.textBaseline='middle';
  c.fillText(text,128,26);
  const tex=new THREE.CanvasTexture(cv);
  return new THREE.Sprite(new THREE.SpriteMaterial({map:tex,transparent:true,depthTest:false}));
}

function _3wLabelSprite(text){
  const cv=document.createElement('canvas');
  cv.width=320; cv.height=44;
  const c=cv.getContext('2d');
  c.clearRect(0,0,320,44);
  c.fillStyle='rgba(255,255,255,0.12)';
  c.font='bold 15px -apple-system,Arial,sans-serif';
  c.textAlign='center'; c.textBaseline='middle';
  c.fillText(text,160,22);
  const tex=new THREE.CanvasTexture(cv);
  return new THREE.Sprite(new THREE.SpriteMaterial({map:tex,transparent:true,depthTest:false}));
}

// ── Animation Loop ────────────────────────────────────────────────────────────

function _3wTick(ts){
  if(!_3wInited) return;
  _3wRaf=requestAnimationFrame(_3wTick);
  const dt=Math.min((ts-_3wLastTs)/1000,0.05);
  _3wLastTs=ts;
  const t=ts/1000;
  Object.entries(_wAgents).forEach(([name,a])=>_3wAnimAgent(name,a,t,dt));
  _3wRenderer.render(_3wScene,_3wCamera);
}

function _3wAnimAgent(name,a,t,dt){
  const m=_3wMeshes[name];
  if(!m) return;
  const {group,parts}=m;

  // Movement — lerp in tile space
  a.frame=(a.frame||0)+dt*60;
  const spd=(a.state==='working'||a.state==='gym'||a.state==='meeting_walk')?3.5:1.8;
  const dx=a.tx-a.x, dz=a.ty-a.y;
  const dist=Math.sqrt(dx*dx+dz*dz);
  const moving=dist>0.04;
  if(moving){
    const step=Math.min(spd*dt,dist);
    a.x+=dx/dist*step; a.y+=dz/dist*step;
    if(dist<0.06){a.x=a.tx; a.y=a.ty;}
    if(dist>0.1) group.rotation.y=Math.atan2(dx,dz);
  }
  group.position.x=a.x; group.position.z=a.y;

  // State transitions
  a.wanderT=(a.wanderT||0)-dt;
  if(a.celebT>0){a.celebT-=dt*60; if(a.celebT<=0){a.celebT=0;a.state='idle';}}
  if(a.state==='idle'&&!moving&&a.wanderT<=0){
    a.wanderT=2.5+Math.random()*4;
    const zone=AGENT_HOME_ZONES[name]||'lobby';
    const [x1,z1,x2,z2]=_ISO_ZONES[zone];
    a.tx=x1+0.5+Math.random()*Math.max(0,x2-x1-1);
    a.ty=z1+0.5+Math.random()*Math.max(0,z2-z1-1);
  }else if(a.state==='meeting_walk'&&!moving){
    a.state='seated';
  }

  // Reset limb rotations each frame
  parts.lArmPiv.rotation.x=0; parts.rArmPiv.rotation.x=0;
  parts.lLegPiv.rotation.x=0; parts.rLegPiv.rotation.x=0;
  parts.headPiv.rotation.y=0; parts.headPiv.rotation.x=0;
  parts.torso.position.y=0.44;
  group.position.y=0;

  // Walk cycle — applied when moving regardless of state
  if(moving){
    const w=t*6;
    parts.lArmPiv.rotation.x=Math.sin(w)*0.62;
    parts.rArmPiv.rotation.x=-Math.sin(w)*0.62;
    parts.lLegPiv.rotation.x=-Math.sin(w)*0.62;
    parts.rLegPiv.rotation.x=Math.sin(w)*0.62;
  }else{
    // State-specific static animations
    if(a.state==='idle'||a.state==='seated'){
      parts.torso.position.y=0.44+Math.sin(t*1.4)*0.013;
      parts.headPiv.rotation.y=Math.sin(t*0.65)*0.14;
    }else if(a.state==='working'){
      parts.lArmPiv.rotation.x=-0.45+Math.sin(t*9)*0.13;
      parts.rArmPiv.rotation.x=-0.45+Math.cos(t*9+1)*0.13;
      parts.torso.position.y=0.44+Math.sin(t*1.2)*0.008;
    }else if(a.state==='gym'){
      const lift=Math.max(0,Math.sin(t*2.2));
      parts.lArmPiv.rotation.x=-1.3+lift*0.9;
      parts.rArmPiv.rotation.x=-1.3+lift*0.9;
      parts.torso.position.y=0.44-lift*0.04;
    }else if(a.state==='done'){
      group.position.y=Math.abs(Math.sin(t*6))*0.38;
      parts.lArmPiv.rotation.x=-1.7+Math.sin(t*6)*0.5;
      parts.rArmPiv.rotation.x=-1.7+Math.cos(t*6)*0.5;
    }else if(a.state==='failed'){
      parts.lArmPiv.rotation.x=0.35; parts.rArmPiv.rotation.x=0.35;
      parts.headPiv.rotation.x=0.38;
    }else if(a.state==='speaking'){
      parts.rArmPiv.rotation.x=-0.5+Math.sin(t*3.5)*0.45;
      parts.lArmPiv.rotation.x=-0.15+Math.sin(t*2.8+1)*0.18;
      parts.torso.position.y=0.44+Math.sin(t*2)*0.018;
      parts.headPiv.rotation.y=Math.sin(t*1.8)*0.22;
    }else if(a.state==='waiting'){
      parts.rArmPiv.rotation.x=0.18; parts.lArmPiv.rotation.x=0.18;
      parts.headPiv.rotation.y=Math.sin(t*2.2)*0.28;
    }
  }

  // Status dot color
  const dotCols={idle:0x555577,working:0x7ec8e3,gym:0xa78bfa,done:0x28c76f,
    failed:0xea5455,waiting:0xffd700,meeting_walk:0xffd700,seated:0xffd700,speaking:0xff9f43};
  const dc=dotCols[a.state]||0x555577;
  parts.dot.material.color.setHex(dc);
  parts.dot.material.emissive.setHex(dc);
}

// ── Click ─────────────────────────────────────────────────────────────────────

function _3wHandleClick(e){
  if(!_3wCamera||!_3wRaycaster) return;
  const rect=_3wCanvas.getBoundingClientRect();
  _3wMouse.x=((e.clientX-rect.left)/rect.width)*2-1;
  _3wMouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
  _3wRaycaster.setFromCamera(_3wMouse,_3wCamera);
  const hits=_3wRaycaster.intersectObjects(_3wClickables);
  if(hits.length){
    const n=hits[0].object.userData.agentName;
    if(n)(typeof _wAgentClick==='function'?_wAgentClick:quickAssign)(n);
  }
}

// ── Public API ────────────────────────────────────────────────────────────────

function renderOfficeFloor(agentsData,tasks){
  initWorldIfNeeded();
  if(!_3wInited) return;
  if(typeof _meetingActive!=='undefined'&&_meetingActive) return;

  (agentsData||[]).forEach(agent=>{
    const name=(agent.id||agent.name||'').replace(/-/g,'_');
    const a=_wAgents[name];
    if(!a) return;
    const prev=a.state;
    const latest=(tasks||[])
      .filter(t=>(t.assigned_agent_id||'').replace(/-/g,'_')===name)
      .sort((p,q)=>(q.updated_at||'').localeCompare(p.updated_at||''))[0];
    const st=latest?.status||null;
    const inGym=typeof _gymAgents!=='undefined'&&_gymAgents.has&&_gymAgents.has(name);
    if(inGym&&st!=='running'){
      a.state='gym'; a.task=latest?.description||'';
      a.tx=_GYM_SPOT[0]+(Math.random()-0.5)*1.5;
      a.ty=_GYM_SPOT[1]+(Math.random()-0.5)*1.5;
    }else if(st==='running'||st==='queued'||st==='assigned'||st==='streaming'){
      if(prev!=='working'){
        a.state='working'; a.task=latest?.description||latest?.prompt||'';
        const [dc,dr]=_ISO_DESKS[name]||[1,1];
        a.tx=dc+0.5; a.ty=dr+0.5;
      }
    }else if(st==='succeeded'||st==='completed'){
      if(prev==='working'||prev==='gym'){a.state='done';a.celebT=90;}
      else if(prev!=='done') a.state='idle';
      a.task='';
    }else if(st==='failed'){
      a.state='failed'; a.task='';
    }else if(st==='waiting_approval'){
      a.state='waiting'; a.task='';
    }else{
      if(prev!=='done'&&prev!=='failed') a.state='idle';
      a.task='';
    }
    if(typeof _evalScores!=='undefined'&&_evalScores[name]) a.score=_evalScores[name].score;
  });
}
"""

# ── Agent Operations Dashboard ──────────────────────────────────────────────

@app.get("/dashboard")
async def agent_dashboard(request: Request):
    """Agent Operations Dashboard — assign work, approve tasks, review decisions."""
    from fastapi.responses import HTMLResponse
    token_js = f"'{_API_TOKEN}'" if _API_TOKEN else "localStorage.getItem('jarvis_auth_token') || ''"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jarvis — Agent Ops</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#d0d0e0;min-height:100vh}}
  .topbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 24px;background:#111120;border-bottom:1px solid #222244;position:sticky;top:0;z-index:100;flex-wrap:wrap}}
  .topbar h1{{font-size:1.1em;font-weight:600;color:#7ec8e3;letter-spacing:.04em;line-height:1.25}}
  .badge{{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:20px;font-size:.75em;font-weight:600}}
  .badge-ok{{background:#0d2a1a;color:#28c76f;border:1px solid #1a4a2a}}
  .badge-warn{{background:#2a1a0a;color:#ff9f43;border:1px solid #4a2a0a}}
  .badge-err{{background:#2a0a0a;color:#ea5455;border:1px solid #4a0a0a}}
  #auth-gate{{position:fixed;inset:0;background:#0a0a0f;display:flex;align-items:center;justify-content:center;z-index:999}}
  .auth-box{{background:#13131f;border:1px solid #333;border-radius:12px;padding:32px;max-width:360px;width:100%;text-align:center}}
  .auth-box h2{{color:#7ec8e3;margin-bottom:16px}}
  .auth-box input{{width:100%;padding:10px;background:#0a0a0f;border:1px solid #333;border-radius:8px;color:#d0d0e0;font-size:1em;margin-bottom:12px}}
  .btn{{padding:8px 18px;border:none;border-radius:8px;font-size:.9em;font-weight:600;cursor:pointer;transition:.15s}}
  .btn-primary{{background:#7ec8e3;color:#0a0a0f}}
  .btn-primary:hover{{background:#a8dff0}}
  .btn-approve{{background:#0d2a1a;color:#28c76f;border:1px solid #1a4a2a}}
  .btn-approve:hover{{background:#1a4a2a}}
  .btn-deny{{background:#2a0a0a;color:#ea5455;border:1px solid #4a0a0a}}
  .btn-deny:hover{{background:#4a0a0a}}
  .btn-run{{background:#1a1a3a;color:#7ec8e3;border:1px solid #2a2a5a}}
  .btn-run:hover{{background:#2a2a5a}}
  .btn-sm{{padding:5px 12px;font-size:.8em}}
  .tabs{{display:flex;gap:4px;padding:12px 24px;border-bottom:1px solid #1a1a3a;overflow-x:auto;scrollbar-width:thin}}
  .tab{{padding:8px 18px;border-radius:8px;font-size:.88em;cursor:pointer;color:#a0a0b8;border:1px solid transparent;white-space:nowrap}}
  .tab.active{{color:#7ec8e3;background:#111128;border-color:#2a2a5a}}
  .tab:hover:not(.active){{color:#c8c8da}}
  .pane{{display:none;padding:20px 24px}}
  .pane.active{{display:block}}
  .stat-row{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
  .stat{{flex:1;min-width:140px;background:#111120;border:1px solid #1a1a3a;border-radius:10px;padding:16px;text-align:center}}
  .stat-num{{font-size:2em;font-weight:700;color:#7ec8e3}}
  .stat-lbl{{font-size:.78em;color:#9a9ab0;margin-top:4px}}
  .card{{background:#111120;border:1px solid #1a1a3a;border-radius:10px;margin-bottom:12px;overflow:hidden}}
  .card-head{{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #1a1a3a;gap:8px;flex-wrap:wrap}}
  .card-body{{padding:12px 16px;font-size:.85em;color:#aaa;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto}}
  .card-foot{{padding:10px 16px;display:flex;gap:8px;border-top:1px solid #1a1a3a;flex-wrap:wrap}}
  .tag{{padding:3px 8px;border-radius:4px;font-size:.72em;font-weight:600}}
  .tag-wait{{background:#2a2a0a;color:#ffd700}}
  .tag-run{{background:#0a1a2a;color:#7ec8e3}}
  .tag-done{{background:#0d1a0d;color:#28c76f}}
  .tag-fail{{background:#1a0a0a;color:#ea5455}}
  .tag-agent{{background:#1a1a3a;color:#bb99ff}}
  .agent-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
  .agent-card{{background:#111120;border:1px solid #1a1a3a;border-radius:10px;padding:14px}}
  .agent-card h3{{font-size:.9em;color:#7ec8e3;margin-bottom:4px}}
  .agent-card p{{font-size:.78em;color:#888;margin-bottom:10px;min-height:32px}}
  .form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
  .form-group{{display:flex;flex-direction:column;gap:6px}}
  .form-group.full{{grid-column:1/-1}}
  label{{font-size:.82em;color:#a8a8c0}}
  select,textarea,input[type=text]{{background:#0a0a0f;border:1px solid #2a2a4a;border-radius:8px;color:#d0d0e0;padding:8px 10px;font-size:.88em;resize:vertical}}
  select{{cursor:pointer}}
  textarea{{min-height:100px;font-family:inherit}}
  .result-area{{background:#0a0a0f;border:1px solid #1a1a3a;border-radius:8px;padding:12px;font-size:.82em;color:#aaa;min-height:60px;max-height:300px;overflow-y:auto;white-space:pre-wrap;margin-top:12px}}
  #agent-stream{{white-space:pre-wrap;word-break:break-word}}
  .empty{{text-align:center;color:#8888a4;padding:32px;font-size:.9em}}
  .dot{{width:8px;height:8px;border-radius:50%;display:inline-block;animation:pulse 1.5s ease-in-out infinite}}
  .dot-green{{background:#28c76f}}
  .dot-yellow{{background:#ffd700}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .scroll-table{{overflow-x:auto}}
  table{{width:100%;border-collapse:collapse;font-size:.82em;table-layout:fixed}}
  #history-table td:nth-child(3){{width:auto}}
  #history-table td:nth-child(1),#history-table th:nth-child(1){{width:72px}}
  #history-table td:nth-child(2),#history-table th:nth-child(2){{width:150px}}
  #history-table td:nth-child(4),#history-table th:nth-child(4){{width:100px}}
  #history-table td:nth-child(5),#history-table th:nth-child(5){{width:90px}}
  th{{text-align:left;padding:8px 10px;color:#666;border-bottom:1px solid #1a1a3a;font-weight:500}}
  td{{padding:8px 10px;border-bottom:1px solid #111128;vertical-align:top}}
  .truncate{{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  @media(max-width:600px){{.form-grid{{grid-template-columns:1fr}}.stat-row{{flex-direction:column}}.topbar{{align-items:flex-start}}.topbar h1{{font-size:1em;max-width:220px}}}}
  /* ---- Virtual Office Floor ---- */
  .office-stage{{position:relative;border-radius:8px;overflow:hidden;background:#0d1117;border:1px solid #1e2430;min-height:340px}}
  .floor-plan{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:0}}
  .floor-zone{{background:#111120;border:1px solid #1a1a3a;border-radius:10px;padding:12px}}
  .floor-zone.zone-eng{{border-color:#1a3a5a;background:linear-gradient(135deg,#0a111a,#111120)}}
  .floor-zone.zone-design{{border-color:#3a1a5a;background:linear-gradient(135deg,#110a1a,#111120)}}
  .floor-zone.zone-research{{border-color:#1a3a2a;background:linear-gradient(135deg,#0a1a0f,#111120)}}
  .floor-zone.zone-qa{{border-color:#3a2a1a;background:linear-gradient(135deg,#1a120a,#111120)}}
  .floor-zone.zone-safety{{border-color:#3a1a1a;background:linear-gradient(135deg,#1a0a0a,#111120)}}
  .floor-zone.zone-ops{{border-color:#1a2a3a;background:linear-gradient(135deg,#0a1219,#111120)}}
  .floor-zone.zone-lobby{{grid-column:1/-1;border-color:#2a2a1a;background:linear-gradient(135deg,#131308,#111120)}}
  .floor-zone.zone-gym{{grid-column:1/-1;background:linear-gradient(135deg,#1a0a3a,#0d0d20);border-color:#5a0aaa;display:none}}
  .zone-hdr{{font-size:.72em;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:10px;font-weight:600;display:flex;align-items:center;gap:6px}}
  .zone-seats{{display:flex;flex-wrap:wrap;gap:8px;min-height:44px;align-items:flex-start}}
  .agent-seat{{background:#0d0d1a;border:1px solid #1a1a2a;border-radius:8px;padding:10px 8px;min-width:82px;max-width:96px;cursor:pointer;text-align:center;position:relative;transition:all .2s;flex-shrink:0}}
  .agent-seat:hover{{border-color:#3a3a6a;background:#131328;transform:translateY(-1px)}}
  .seat-active{{border-color:#2a5a7a!important;background:#0a1a2a!important;animation:seatpulse 1.5s ease-in-out infinite}}
  .seat-done{{border-color:#1a4a1a!important;background:#0a1a0a!important}}
  .seat-fail{{border-color:#4a1a1a!important;background:#1a0a0a!important}}
  .seat-wait{{border-color:#4a4a1a!important;background:#1a1a0a!important}}
  .seat-idle{{opacity:.55}}
  .seat-icon{{font-size:1.5em;margin-bottom:3px;line-height:1}}
  .seat-name{{font-size:.58em;color:#999;line-height:1.3;word-break:break-all}}
  .seat-score{{font-size:.6em;margin-top:3px;font-weight:600;line-height:1}}
  .seat-dot{{position:absolute;top:5px;right:5px;width:6px;height:6px;border-radius:50%}}
  .sdot-active{{background:#7ec8e3;animation:pulse 1.5s ease-in-out infinite}}
  .sdot-done{{background:#28c76f}}
  .sdot-fail{{background:#ea5455}}
  .sdot-wait{{background:#ffd700;animation:pulse 1.5s ease-in-out infinite}}
  .gym-pill{{display:inline-flex;align-items:center;gap:5px;padding:5px 12px;background:#2a1a4a;border-radius:20px;color:#a78bfa;font-size:.78em;margin:2px;border:1px solid #4a2a8a}}
  @keyframes seatpulse{{0%,100%{{box-shadow:0 0 0 0 rgba(126,200,227,.25)}}50%{{box-shadow:0 0 0 10px rgba(126,200,227,0)}}}}
  @media(max-width:720px){{.floor-plan{{grid-template-columns:1fr 1fr}}}}
  @media(max-width:460px){{.floor-plan{{grid-template-columns:1fr}}}}
  /* ---- Accessibility base ---- */
  :focus-visible{{outline:2px solid #7ec8e3;outline-offset:2px;border-radius:4px}}
  .sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
  @media(prefers-reduced-motion:reduce){{*,*::before,*::after{{animation:none!important;transition:none!important}}}}
  button.tab{{background:none;font-family:inherit}}
  /* ---- Live activity ticker ---- */
  .ticker{{font-size:.75em;color:#9a9ab8;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  /* ---- Pipeline board ---- */
  .pipe-board{{display:grid;grid-template-columns:repeat(5,minmax(170px,1fr));gap:10px;align-items:start}}
  @media(max-width:1100px){{.pipe-board{{grid-template-columns:repeat(3,1fr)}}}}
  @media(max-width:640px){{.pipe-board{{grid-template-columns:1fr}}}}
  .pipe-col{{background:#0d0d18;border:1px solid #1a1a3a;border-radius:10px;padding:10px;min-height:130px}}
  .pipe-col-hdr{{font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin-bottom:8px;font-weight:600;display:flex;justify-content:space-between;align-items:center}}
  .pipe-count{{background:#1a1a3a;border-radius:10px;padding:1px 8px;color:#bcbcd8;font-size:.95em}}
  .pipe-card{{display:block;width:100%;text-align:left;background:#111122;border:1px solid #22224a;border-radius:8px;padding:9px 10px;margin-bottom:8px;cursor:pointer;color:#c8c8dc;font-size:.8em;font-family:inherit;transition:border-color .15s}}
  .pipe-card:hover{{border-color:#3a3a7a;background:#15152c}}
  .pipe-card .pc-agent{{font-size:.82em;color:#bb99ff;font-weight:600}}
  .pipe-card .pc-prompt{{display:block;margin-top:4px;color:#a8a8c0;line-height:1.35;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}}
  .pipe-card .pc-time{{display:block;margin-top:5px;font-size:.85em;color:#77779a}}
  .col-queued{{border-top:3px solid #5a5a9a}}
  .col-wait{{border-top:3px solid #ffd700}}
  .col-run{{border-top:3px solid #7ec8e3}}
  .col-done{{border-top:3px solid #28c76f}}
  .col-fail{{border-top:3px solid #ea5455}}
  .feed{{margin-top:18px;background:#0d0d18;border:1px solid #1a1a3a;border-radius:10px;padding:12px 14px}}
  .feed-item{{font-size:.78em;color:#a0a0bc;padding:4px 0;border-bottom:1px solid #14142a;display:flex;gap:8px;align-items:baseline}}
  .feed-item:last-child{{border-bottom:none}}
  .feed-ts{{color:#70708e;font-size:.9em;white-space:nowrap}}
  /* ---- Task drawer ---- */
  .drawer{{position:fixed;top:0;right:0;height:100vh;width:min(580px,95vw);background:#0f0f1d;border-left:1px solid #2a2a4a;z-index:350;display:none;flex-direction:column;box-shadow:-12px 0 40px rgba(0,0,0,.6)}}
  .drawer.open{{display:flex}}
  .drawer-head{{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid #1a1a3a;flex-wrap:wrap}}
  .drawer-body{{flex:1;overflow-y:auto;padding:14px 18px}}
  .drawer-foot{{padding:12px 18px;border-top:1px solid #1a1a3a;display:flex;gap:8px;flex-wrap:wrap}}
  .timeline{{list-style:none;margin:10px 0 0;padding:0;position:relative}}
  .timeline::before{{content:'';position:absolute;left:5px;top:6px;bottom:6px;width:2px;background:#1f1f40}}
  .tl-item{{position:relative;padding:0 0 14px 22px;font-size:.8em;color:#a8a8c0}}
  .tl-item:last-child{{padding-bottom:2px}}
  .tl-dot{{position:absolute;left:0;top:4px;width:12px;height:12px;border-radius:50%;border:2px solid #0f0f1d}}
  .tl-status{{background:#7ec8e3}} .tl-error{{background:#ea5455}} .tl-chunk{{background:#5a5a9a}} .tl-ws{{background:#28c76f}}
  .tl-ts{{color:#70708e;font-size:.88em;margin-left:6px}}
  /* ---- Command palette ---- */
  .palette{{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:500;padding-top:12vh}}
  .palette.open{{display:flex}}
  .palette-box{{background:#13131f;border:1px solid #3a3a6a;border-radius:12px;width:min(540px,92vw);overflow:hidden;box-shadow:0 16px 60px rgba(0,0,0,.7)}}
  .palette-box input{{width:100%;padding:14px 16px;background:#0d0d18;border:none;border-bottom:1px solid #1f1f40;color:#e0e0f0;font-size:1em}}
  .palette-list{{max-height:320px;overflow-y:auto;margin:0;padding:6px;list-style:none}}
  .palette-item{{padding:9px 12px;border-radius:7px;font-size:.88em;color:#b8b8d0;cursor:pointer;display:flex;justify-content:space-between;gap:10px}}
  .palette-item[aria-selected="true"],.palette-item:hover{{background:#1c1c3a;color:#e8e8f8}}
  .palette-hint{{color:#70708e;font-size:.85em}}
  /* ---- Help modal ---- */
  .help-modal{{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:300}}
  .help-modal.open{{display:flex}}
  .help-box{{background:#13131f;border:1px solid #333;border-radius:14px;padding:28px 24px;max-width:560px;width:92%;max-height:82vh;overflow-y:auto}}
  .help-box h2{{color:#7ec8e3;margin-bottom:16px;font-size:1em;letter-spacing:.03em}}
  .help-sec{{margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid #1a1a3a}}
  .help-sec:last-child{{border-bottom:none;margin-bottom:0}}
  .help-sec h3{{color:#aaa;font-size:.82em;margin-bottom:4px}}
  .help-sec p{{font-size:.76em;color:#666;line-height:1.55}}
  /* ---- Eval Log tab ---- */
  #eval-filter-agent,#eval-filter-verdict{{background:#0a0a0f;border:1px solid #2a2a4a;border-radius:6px;color:#d0d0e0;padding:5px 8px;font-size:.8em}}
  /* ---- Manager Console + Upgrade Loop ---- */
  .console-grid{{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);gap:14px;align-items:start}}
  .console-rail{{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px;margin-bottom:14px}}
  .console-tile{{background:#0d0d18;border:1px solid #1a1a3a;border-radius:8px;padding:12px;min-height:92px}}
  .console-tile .kpi{{font-size:1.8em;font-weight:750;color:#7ec8e3;font-variant-numeric:tabular-nums;line-height:1.1}}
  .console-tile .kpi.ok{{color:#28c76f}} .console-tile .kpi.warn{{color:#ffd700}} .console-tile .kpi.bad{{color:#ea5455}}
  .console-tile .label{{font-size:.75em;color:#9a9ab0;line-height:1.35;margin-top:7px}}
  .manager-layout{{display:grid;grid-template-columns:220px minmax(0,1fr);gap:10px}}
  .manager-nav{{background:#0d0d18;border:1px solid #1a1a3a;border-radius:8px;padding:8px}}
  .manager-nav-row{{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:8px 9px;border-radius:6px;color:#b8b8d0;font-size:.82em;margin-bottom:3px;background:transparent;border:0;width:100%;text-align:left;font-family:inherit}}
  .manager-nav-row.active,.manager-nav-row:hover{{background:#17172c;color:#e8e8f8}}
  .decision-list{{display:flex;flex-direction:column;gap:8px}}
  .decision-item{{background:#0d0d18;border:1px solid #1a1a3a;border-radius:8px;padding:10px 12px}}
  .decision-item strong{{display:block;color:#d8d8e8;font-size:.85em;margin-bottom:5px}}
  .decision-item p{{color:#8f8fac;font-size:.78em;line-height:1.45;margin:0 0 8px}}
  .upgrade-board{{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px;align-items:start}}
  .upgrade-col{{background:#0d0d18;border:1px solid #1a1a3a;border-radius:8px;padding:10px;min-height:180px}}
  .upgrade-col h3{{font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}}
  .upgrade-ticket{{background:#111122;border:1px solid #22224a;border-radius:8px;padding:10px;margin-bottom:8px}}
  .upgrade-ticket strong{{display:block;font-size:.83em;color:#e0e0ec;line-height:1.35;margin-bottom:6px}}
  .upgrade-ticket p{{font-size:.76em;color:#9999b4;line-height:1.42;margin:0 0 8px}}
  .source-row{{display:grid;grid-template-columns:minmax(140px,1fr) auto;gap:8px;align-items:center;border-bottom:1px solid #17172a;padding:8px 0;font-size:.8em}}
  .source-row:last-child{{border-bottom:0}}
  @media(max-width:1400px){{.console-rail{{grid-template-columns:repeat(3,1fr)}}}}
  @media(max-width:1200px){{.console-grid{{grid-template-columns:1fr}}.console-rail{{grid-template-columns:repeat(3,1fr)}}.upgrade-board{{grid-template-columns:repeat(2,1fr)}}}}
  @media(max-width:700px){{.manager-layout{{grid-template-columns:1fr}}.console-rail,.upgrade-board{{grid-template-columns:1fr}}}}
</style>
<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
</head>
<body>

<div id="auth-gate">
  <div class="auth-box">
    <h2>Jarvis Agent Ops</h2>
    <p style="color:#777;font-size:.85em;margin-bottom:16px">Enter your API token to continue</p>
    <input type="password" id="auth-input" placeholder="API token" aria-label="API token">
    <button class="btn btn-primary" style="width:100%" onclick="doAuth()">Connect</button>
  </div>
</div>

<div class="topbar">
  <h1>⚡ Jarvis Agent Operations</h1>
  <div style="display:flex;align-items:center;gap:10px">
    <span id="live-ticker" class="ticker" role="status" aria-live="polite"></span>
    <button class="btn btn-run btn-sm" onclick="openPalette()" title="Command palette (Cmd+K)" aria-label="Open command palette" style="padding:4px 10px;font-size:.75em">⌘K</button>
    <span id="refresh-lbl" style="font-size:.75em;color:#8888a4">auto-refresh 5s</span>
    <span id="pipeline-health-badge" class="badge" style="background:#0d1a2a;color:#7ec8e3;border:1px solid #1a3a5a;cursor:pointer;font-size:.72em"
          onclick="checkPipelineHealth()" title="Click to run pipeline health check">
      🔍 Pipeline Monitor
    </span>
    <button class="btn btn-run btn-sm" onclick="openHelp()" title="How to use this dashboard" style="padding:4px 12px;font-size:.78em">? Help</button>
    <span id="status-badge" class="badge badge-ok"><span class="dot dot-green"></span>Online</span>
  </div>
</div>

<div class="tabs" role="tablist" aria-label="Dashboard sections">
  <button class="tab active" role="tab" id="tab-console" aria-controls="pane-console" aria-selected="true" data-tab="console" onclick="switchTab('console')">⌁ Manager Console</button>
  <button class="tab" role="tab" id="tab-upgrades" aria-controls="pane-upgrades" aria-selected="false" data-tab="upgrades" tabindex="-1" onclick="switchTab('upgrades')">↟ Upgrade Loop <span id="upgrade-count" style="color:#7ec8e3"></span></button>
  <button class="tab" role="tab" id="tab-pipeline" aria-controls="pane-pipeline" aria-selected="false" data-tab="pipeline" tabindex="-1" onclick="switchTab('pipeline')">🛰 Pipeline <span id="pipeline-count" style="color:#7ec8e3"></span></button>
  <button class="tab" role="tab" id="tab-queue" aria-controls="pane-queue" aria-selected="false" data-tab="queue" tabindex="-1" onclick="switchTab('queue')">⏳ Approval Queue <span id="queue-count" style="color:#ffd700"></span></button>
  <button class="tab" role="tab" id="tab-active" aria-controls="pane-active" aria-selected="false" data-tab="active" tabindex="-1" onclick="switchTab('active')">▶ Active <span id="active-count" style="color:#7ec8e3"></span></button>
  <button class="tab" role="tab" id="tab-history" aria-controls="pane-history" aria-selected="false" data-tab="history" tabindex="-1" onclick="switchTab('history')">📋 History</button>
  <button class="tab" role="tab" id="tab-assign" aria-controls="pane-assign" aria-selected="false" data-tab="assign" tabindex="-1" onclick="switchTab('assign')">＋ Assign Work</button>
  <button class="tab" role="tab" id="tab-agents" aria-controls="pane-agents" aria-selected="false" data-tab="agents" tabindex="-1" onclick="switchTab('agents')">🤖 Agents</button>
  <button class="tab" role="tab" id="tab-project" aria-controls="pane-project" aria-selected="false" data-tab="project" tabindex="-1" onclick="switchTab('project')">🚀 Project</button>
  <button class="tab" role="tab" id="tab-evallog" aria-controls="pane-evallog" aria-selected="false" data-tab="evallog" tabindex="-1" onclick="switchTab('evallog')">📊 Eval Log</button>
  <button class="tab" role="tab" id="tab-lessons" aria-controls="pane-lessons" aria-selected="false" data-tab="lessons" tabindex="-1" onclick="switchTab('lessons')">🧠 Lessons <span id="lessons-count" style="color:#c084fc"></span></button>
  <button class="tab" role="tab" id="tab-routines" aria-controls="pane-routines" aria-selected="false" data-tab="routines" tabindex="-1" onclick="switchTab('routines')">⏰ Routines <span id="routines-count" style="color:#34d399"></span></button>
  <button class="tab" role="tab" id="tab-security" aria-controls="pane-security" aria-selected="false" data-tab="security" tabindex="-1" onclick="switchTab('security')">🛡 Security <span id="security-count" style="color:#ea5455"></span></button>
  <button class="tab" role="tab" id="tab-tokens" aria-controls="pane-tokens" aria-selected="false" data-tab="tokens" tabindex="-1" onclick="switchTab('tokens')">💸 Tokens <span id="tokens-count" style="color:#ffd700"></span></button>
</div>

<!-- Manager Console -->
<div class="pane active" id="pane-console" role="tabpanel" aria-labelledby="tab-console">
  <div class="console-rail">
    <div class="console-tile"><div class="kpi" id="mc-audit">—</div><div class="label">Pipeline truthfulness verdict</div></div>
    <div class="console-tile"><div class="kpi" id="mc-active">—</div><div class="label">Active + approval-gated tasks</div></div>
    <div class="console-tile"><div class="kpi" id="mc-taskrate">—</div><div class="label">Task success rate (completed/total)</div></div>
    <div class="console-tile"><div class="kpi" id="mc-upgrades">—</div><div class="label">Upgrade tickets in local backlog</div></div>
    <div class="console-tile"><div class="kpi" id="mc-security">—</div><div class="label">Security denials / critical events</div></div>
  </div>
  <div class="console-grid">
    <section class="card">
      <div class="card-head"><strong>Manager Operating Picture</strong><button class="btn btn-run btn-sm" onclick="loadManagerConsole()">Refresh</button></div>
      <div class="card-body" style="max-height:none">
        <div class="manager-layout">
          <div class="manager-nav" aria-label="Manager console sections">
            <button class="manager-nav-row active" onclick="switchTab('upgrades')"><span>Upgrade Loop</span><span id="mc-nav-upgrades">—</span></button>
            <button class="manager-nav-row" onclick="switchTab('pipeline')"><span>Pipeline Board</span><span id="mc-nav-pipeline">—</span></button>
            <button class="manager-nav-row" onclick="switchTab('security')"><span>Security Gates</span><span id="mc-nav-security">—</span></button>
            <button class="manager-nav-row" onclick="switchTab('agents')"><span>Agent Team</span><span id="mc-nav-agents">—</span></button>
            <button class="manager-nav-row" onclick="switchTab('evallog')"><span>Training / Evals</span><span id="mc-nav-evals">—</span></button>
          </div>
          <div>
            <div id="mc-narrative" style="font-size:.86em;color:#b8b8d0;line-height:1.65;margin-bottom:12px">Loading manager state…</div>
            <div class="decision-list" id="mc-decisions"></div>
          </div>
        </div>
      </div>
    </section>
    <aside class="card">
      <div class="card-head"><strong>Human Review Queue</strong><span class="tag tag-wait" id="mc-review-count">—</span></div>
      <div class="card-body" id="mc-review-list" style="max-height:520px">Loading…</div>
    </aside>
  </div>
</div>

<!-- Upgrade Loop -->
<div class="pane" id="pane-upgrades" role="tabpanel" aria-labelledby="tab-upgrades">
  <div class="stat-row">
    <div class="stat"><div class="stat-num" id="up-cycles">—</div><div class="stat-lbl">Research Cycles</div></div>
    <div class="stat"><div class="stat-num" id="up-candidates">—</div><div class="stat-lbl">Candidates Ranked</div></div>
    <div class="stat"><div class="stat-num" id="up-tickets">—</div><div class="stat-lbl">Tickets Created</div></div>
    <div class="stat"><div class="stat-num" id="up-decisions">—</div><div class="stat-lbl">Human Decisions</div></div>
  </div>
  <div class="upgrade-board" id="upgrade-board">
    <div class="upgrade-col"><h3>Signals <span id="up-signal-count">0</span></h3><div id="up-col-signals"></div></div>
    <div class="upgrade-col"><h3>Proposed <span id="up-proposed-count">0</span></h3><div id="up-col-proposed"></div></div>
    <div class="upgrade-col"><h3>Backlog <span id="up-backlog-count">0</span></h3><div id="up-col-backlog"></div></div>
    <div class="upgrade-col"><h3>Needs Review <span id="up-review-count">0</span></h3><div id="up-col-review"></div></div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-head"><strong>Watched Upgrade Sources</strong><button class="btn btn-run btn-sm" onclick="loadUpgradeLoop()">Refresh</button></div>
    <div class="card-body" id="up-watchlist" style="max-height:none">Loading…</div>
  </div>
</div>

<!-- Pipeline board -->
<div class="pane" id="pane-pipeline" role="tabpanel" aria-labelledby="tab-pipeline">
  <div class="stat-row">
    <div class="stat"><div class="stat-num" id="pp-queued">—</div><div class="stat-lbl">Queued</div></div>
    <div class="stat"><div class="stat-num" id="pp-wait">—</div><div class="stat-lbl">Awaiting Approval</div></div>
    <div class="stat"><div class="stat-num" id="pp-run">—</div><div class="stat-lbl">Running</div></div>
    <div class="stat"><div class="stat-num" id="pp-done">—</div><div class="stat-lbl">Succeeded</div></div>
    <div class="stat"><div class="stat-num" id="pp-fail">—</div><div class="stat-lbl">Failed / Stopped</div></div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
    <label for="pipe-filter-text" class="sr-only">Filter tasks by text</label>
    <input type="text" id="pipe-filter-text" placeholder="🔍 Filter tasks — agent or prompt text…" oninput="renderPipelineFromCache()"
           style="flex:1;min-width:180px;font-size:.82em;padding:7px 10px" autocomplete="off">
    <label for="pipe-filter-agent" class="sr-only">Filter tasks by agent</label>
    <select id="pipe-filter-agent" onchange="renderPipelineFromCache()" style="font-size:.8em;padding:6px 8px">
      <option value="">All agents</option>
    </select>
    <span id="pipe-filter-count" style="font-size:.75em;color:#9a9ab0;white-space:nowrap"></span>
    <button class="btn btn-run btn-sm" onclick="clearPipeFilter()" style="font-size:.78em">Clear</button>
  </div>
  <div class="pipe-board" id="pipe-board" aria-label="Task pipeline board">
    <div class="pipe-col col-queued"><div class="pipe-col-hdr">Queued <span class="pipe-count" id="pc-queued">0</span></div><div id="col-queued"></div></div>
    <div class="pipe-col col-wait"><div class="pipe-col-hdr">Awaiting Approval <span class="pipe-count" id="pc-wait">0</span></div><div id="col-wait"></div></div>
    <div class="pipe-col col-run"><div class="pipe-col-hdr">Running <span class="pipe-count" id="pc-run">0</span></div><div id="col-run"></div></div>
    <div class="pipe-col col-done"><div class="pipe-col-hdr">Succeeded <span class="pipe-count" id="pc-done">0</span></div><div id="col-done"></div></div>
    <div class="pipe-col col-fail"><div class="pipe-col-hdr">Failed / Stopped <span class="pipe-count" id="pc-fail">0</span></div><div id="col-fail"></div></div>
  </div>
  <div class="feed">
    <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin-bottom:8px;font-weight:600">Live Activity</div>
    <div id="activity-feed" aria-live="off"><div class="empty" style="padding:12px">No activity yet — task status changes will appear here.</div></div>
  </div>
</div>

<!-- Queue -->
<div class="pane" id="pane-queue" role="tabpanel" aria-labelledby="tab-queue">
  <div class="stat-row">
    <div class="stat"><div class="stat-num" id="st-pending">—</div><div class="stat-lbl">Awaiting Approval</div></div>
    <div class="stat"><div class="stat-num" id="st-running">—</div><div class="stat-lbl">Active Tasks</div></div>
    <div class="stat"><div class="stat-num" id="st-done">—</div><div class="stat-lbl">Completed (all time)</div></div>
  </div>
  <div id="queue-list"><div class="empty">Loading…</div></div>
</div>

<!-- Active -->
<div class="pane" id="pane-active" role="tabpanel" aria-labelledby="tab-active">
  <div id="active-list"><div class="empty">Loading…</div></div>
</div>

<!-- History -->
<div class="pane" id="pane-history" role="tabpanel" aria-labelledby="tab-history">
  <div style="display:flex;justify-content:flex-end;margin-bottom:10px">
    <button class="btn btn-deny btn-sm" onclick="clearHistory()" style="font-size:.8em">🗑 Clear History</button>
  </div>
  <div class="scroll-table">
    <table id="history-table">
      <thead><tr><th>ID</th><th>Agent</th><th>Prompt + Result Preview</th><th>Status</th><th>Finished</th></tr></thead>
      <tbody id="history-body"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
</div>

<!-- Assign -->
<div class="pane" id="pane-assign" role="tabpanel" aria-labelledby="tab-assign">
  <div class="card">
    <div class="card-head"><strong>Assign Work to Agent</strong></div>
    <div style="padding:16px">
      <div class="form-grid">
        <div class="form-group">
          <label>Agent</label>
          <select id="assign-agent">
            <option value="">Auto-select (Manager)</option>
            <option value="backend_engineer">Backend Engineer</option>
            <option value="frontend_designer">Frontend Designer</option>
            <option value="ux_researcher">UX Researcher</option>
            <option value="qa_tester">QA Tester</option>
            <option value="devops_release">DevOps Release</option>
            <option value="researcher">Researcher</option>
            <option value="automation_engineer">Automation Engineer</option>
            <option value="career_agent">Career Agent</option>
            <option value="memory_librarian">Memory Librarian</option>
            <option value="security_reviewer">Security Reviewer</option>
            <option value="ai_safety_agent">AI Safety Agent</option>
            <option value="ai_evaluator">AI Evaluator (Eval Lab)</option>
            <option value="pipeline_monitor">Pipeline Monitor</option>
            <option value="output_quality_checker">Output Quality Checker</option>
          </select>
        </div>
        <div class="form-group">
          <label>Kind</label>
          <select id="assign-kind">
            <option value="chat">Chat / General</option>
            <option value="code">Code</option>
            <option value="research">Research</option>
            <option value="review">Review</option>
            <option value="plan">Plan</option>
          </select>
        </div>
        <div class="form-group full">
          <label>Task Description</label>
          <textarea id="assign-prompt" placeholder="Describe the task in detail…"></textarea>
        </div>
        <div class="form-group full">
          <label>Context (optional)</label>
          <textarea id="assign-context" placeholder="Any additional context for the agent…" style="min-height:60px"></textarea>
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-primary" onclick="submitTask()">Assign Task</button>
        <span id="assign-status" style="font-size:.82em;color:#888"></span>
      </div>
      <div id="assign-result" class="result-area" style="display:none"></div>
    </div>
  </div>
</div>

<!-- Agents — Virtual Office Floor -->
<div class="pane" id="pane-agents" role="tabpanel" aria-labelledby="tab-agents">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
    <span style="font-size:.75em;color:#555">Click a character to view agent details.</span>
    <button onclick="startMeeting()" style="background:#1a2a1a;border:1px solid #2a4a2a;color:#5a9a5a;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:.8em;font-weight:600;letter-spacing:.05em">🪑 Meeting Room</button>
  </div>
  <div class="office-stage">
    <canvas id="office-canvas" style="display:block;width:100%;cursor:pointer"></canvas>
    <div style="position:absolute;bottom:8px;left:12px;font-size:.68em;color:#444;pointer-events:none;line-height:1.6">
      Click a character to assign work &nbsp;•&nbsp;
      <span style="color:#7ec8e3">●</span> Working &nbsp;
      <span style="color:#28c76f">●</span> Done &nbsp;
      <span style="color:#a78bfa">●</span> Eval Gym &nbsp;
      <span style="color:#333">●</span> Idle
    </div>
  </div>
</div>

<!-- Project -->
<div class="pane" id="pane-project" role="tabpanel" aria-labelledby="tab-project">
  <!-- Step 1: Goal input -->
  <div class="card" id="proj-card-goal">
    <div class="card-head"><strong>🚀 Project Orchestration</strong><span style="font-size:.75em;color:#555;margin-left:8px">Plan → Review → Execute</span></div>
    <div style="padding:16px">
      <div class="form-group full" style="margin-bottom:12px">
        <label>Project Goal</label>
        <textarea id="proj-goal" placeholder="e.g. Audit the entire Jarvis pipeline, identify gaps, and produce a hardening report with code improvements." style="min-height:90px"></textarea>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-primary" id="proj-plan-btn" onclick="generatePlan()">⚡ Generate Plan</button>
        <button class="btn btn-run" id="proj-run-btn" onclick="runProject()" style="display:none">▶ Run Without Plan</button>
        <span id="proj-status" style="font-size:.82em;color:#888"></span>
      </div>
    </div>
  </div>

  <!-- Step 2: Plan review (shown after generatePlan) -->
  <div id="proj-plan-review" style="display:none;margin-top:16px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <div>
        <span style="font-size:.8em;text-transform:uppercase;letter-spacing:.08em;color:#7ec8e3">Orchestration Plan</span>
        <span style="font-size:.75em;color:#555;margin-left:8px">Edit any task before executing</span>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-deny btn-sm" onclick="clearPlan()">✕ Clear</button>
        <button class="btn btn-primary" id="proj-execute-btn" onclick="executePlan()">▶ Execute Plan</button>
      </div>
    </div>
    <div id="proj-plan-cards" style="display:flex;flex-direction:column;gap:10px"></div>
  </div>

  <!-- Step 3: Live execution (shown after executePlan) -->
  <div id="proj-execution" style="display:none;margin-top:16px">
    <div style="font-size:.78em;text-transform:uppercase;letter-spacing:.08em;color:#555;margin-bottom:10px">Live Execution</div>
    <div id="proj-agents-output" style="display:flex;flex-direction:column;gap:10px"></div>
  </div>
</div>

<!-- Eval Log -->
<div class="pane" id="pane-evallog" role="tabpanel" aria-labelledby="tab-evallog">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <strong style="font-size:.9em">AI Evaluation History</strong>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="eval-filter-agent" onchange="loadEvalLog()">
        <option value="">All agents</option>
        <option value="backend_engineer">backend_engineer</option>
        <option value="frontend_designer">frontend_designer</option>
        <option value="qa_tester">qa_tester</option>
        <option value="devops_release">devops_release</option>
        <option value="security_reviewer">security_reviewer</option>
        <option value="researcher">researcher</option>
        <option value="ux_researcher">ux_researcher</option>
        <option value="automation_engineer">automation_engineer</option>
      </select>
      <select id="eval-filter-verdict" onchange="loadEvalLog()">
        <option value="">All verdicts</option>
        <option value="pass">pass</option>
        <option value="needs_review">needs_review</option>
        <option value="fail">fail</option>
      </select>
      <button class="btn btn-run btn-sm" onclick="loadEvalLog()">Refresh</button>
    </div>
  </div>
  <div class="scroll-table">
    <table id="eval-log-table">
      <thead><tr><th>Time</th><th>Agent</th><th>Task</th><th>Score</th><th>Verdict</th><th>Feedback</th></tr></thead>
      <tbody id="eval-log-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
  <!-- Rubrics section -->
  <div style="margin-top:24px;border-top:1px solid #1a1a3a;padding-top:16px">
    <strong style="font-size:.85em">Custom Rubrics</strong>
    <div id="rubric-list" style="margin-top:8px;font-size:.8em;color:#888">No rubrics defined yet.</div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start">
      <div style="display:flex;flex-direction:column;gap:6px;flex:1;min-width:200px">
        <input type="text" id="rubric-id" placeholder="Rubric ID (e.g. security_qa)" style="font-size:.82em">
        <input type="text" id="rubric-name" placeholder="Display name" style="font-size:.82em">
        <textarea id="rubric-criteria" placeholder="One criterion per line&#10;e.g. Must include Executive Summary&#10;Must have at least 3 findings" style="min-height:80px;font-size:.82em"></textarea>
        <button class="btn btn-run btn-sm" onclick="createRubric()">Save Rubric</button>
      </div>
      <div id="rubric-status" style="font-size:.78em;color:#888;padding-top:4px"></div>
    </div>
  </div>
</div>

<!-- Lessons -->
<div class="pane" id="pane-lessons" role="tabpanel" aria-labelledby="tab-lessons">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <strong style="font-size:.9em">Agent Lessons (Pending Approval)</strong>
    <button class="btn btn-run btn-sm" onclick="loadPendingLessons()">Refresh</button>
  </div>
  <div id="lessons-list"><div class="empty">Loading…</div></div>
</div>

<!-- Routines -->
<div class="pane" id="pane-routines" role="tabpanel" aria-labelledby="tab-routines">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
    <strong style="font-size:.9em">Scheduled Routines</strong>
    <button class="btn btn-approve btn-sm" onclick="showRoutineForm()">+ New Routine</button>
  </div>
  <div id="routine-form" style="display:none;background:#111128;border:1px solid #2a2a5a;border-radius:10px;padding:14px;margin-bottom:16px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
      <div><label style="font-size:.8em;color:#888;display:block;margin-bottom:4px">Name</label>
        <input id="rtn-name" type="text" placeholder="e.g. Daily security scan" style="width:100%;background:#0d0d1f;border:1px solid #2a2a5a;border-radius:6px;padding:6px 10px;color:#e0e0ff;font-size:.85em"></div>
      <div><label style="font-size:.8em;color:#888;display:block;margin-bottom:4px">Agent (leave blank for Manager)</label>
        <input id="rtn-agent" type="text" placeholder="e.g. security-reviewer" style="width:100%;background:#0d0d1f;border:1px solid #2a2a5a;border-radius:6px;padding:6px 10px;color:#e0e0ff;font-size:.85em"></div>
    </div>
    <div style="margin-bottom:10px"><label style="font-size:.8em;color:#888;display:block;margin-bottom:4px">Schedule</label>
      <input id="rtn-schedule" type="text" placeholder="@daily  or  0 9 * * *  or  interval:3600" style="width:100%;background:#0d0d1f;border:1px solid #2a2a5a;border-radius:6px;padding:6px 10px;color:#e0e0ff;font-size:.85em">
      <div style="font-size:.75em;color:#555;margin-top:3px">Cron (5-field), @daily/@hourly/@weekly, or interval:&lt;seconds&gt; (min 60)</div>
    </div>
    <div style="margin-bottom:12px"><label style="font-size:.8em;color:#888;display:block;margin-bottom:4px">Prompt</label>
      <textarea id="rtn-prompt" rows="3" placeholder="What should the agent do?" style="width:100%;background:#0d0d1f;border:1px solid #2a2a5a;border-radius:6px;padding:6px 10px;color:#e0e0ff;font-size:.85em;resize:vertical"></textarea>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-approve btn-sm" onclick="saveRoutine()">Save</button>
      <button class="btn btn-deny btn-sm" onclick="hideRoutineForm()">Cancel</button>
    </div>
  </div>
  <div id="routines-list"><div class="empty">Loading…</div></div>
</div>

<!-- Security -->
<div class="pane" id="pane-security" role="tabpanel" aria-labelledby="tab-security">
  <div class="stat-row">
    <div class="stat"><div class="stat-num" id="sec-total">—</div><div class="stat-lbl">Audited Actions</div></div>
    <div class="stat"><div class="stat-num" id="sec-denials" style="color:#ea5455">—</div><div class="stat-lbl">Denials</div></div>
    <div class="stat"><div class="stat-num" id="sec-blocks" style="color:#ff9f43">—</div><div class="stat-lbl">Threat Blocks</div></div>
    <div class="stat"><div class="stat-num" id="sec-cloud" style="color:#ffd700">—</div><div class="stat-lbl">Cloud Calls</div></div>
    <div class="stat"><div class="stat-num" id="sec-rollback" style="color:#28c76f">—</div><div class="stat-lbl">Rollback-able</div></div>
  </div>
  <div id="sec-critical" style="margin-bottom:14px"></div>
  <div class="card">
    <div class="card-head"><strong style="font-size:.9em">What happened (plain language)</strong>
      <button class="btn btn-run btn-sm" onclick="loadSecurity()">Refresh</button></div>
    <div id="sec-plain" style="padding:12px 16px;font-size:.84em;color:#b8b8d0;line-height:1.7"><div class="empty">Loading…</div></div>
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin:18px 0 10px;flex-wrap:wrap;gap:8px">
    <strong style="font-size:.85em">Audit Events (technical)</strong>
    <div style="display:flex;gap:8px;align-items:center">
      <label for="sec-filter-action" class="sr-only">Filter by action / source</label>
      <select id="sec-filter-action" onchange="loadSecurity()" style="font-size:.8em;padding:5px 8px">
        <option value="">All actions</option>
      </select>
      <label for="sec-filter-severity" class="sr-only">Filter by severity</label>
      <select id="sec-filter-severity" onchange="loadSecurity()" style="font-size:.8em;padding:5px 8px">
        <option value="">All severities</option>
        <option value="info">info</option>
        <option value="notice">notice</option>
        <option value="warning">warning</option>
        <option value="critical">critical</option>
      </select>
    </div>
  </div>
  <div class="scroll-table">
    <table id="sec-events-table" style="table-layout:auto">
      <thead><tr><th>Time</th><th>Severity</th><th>Action</th><th>Actor</th><th>Target</th><th>Decision</th><th>Task</th></tr></thead>
      <tbody id="sec-events-body"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
</div>

<!-- Tokens / rate-limit -->
<div class="pane" id="pane-tokens" role="tabpanel" aria-labelledby="tab-tokens">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
    <strong style="font-size:.9em">Token &amp; Cost Usage</strong>
    <div style="display:flex;gap:8px;align-items:center">
      <label for="tok-window" class="sr-only">Time window</label>
      <select id="tok-window" onchange="loadTokens()" style="font-size:.8em;padding:5px 8px">
        <option value="1">Last 1 hour</option>
        <option value="24" selected>Last 24 hours</option>
        <option value="168">Last 7 days</option>
      </select>
      <button class="btn btn-run btn-sm" onclick="loadTokens()">Refresh</button>
    </div>
  </div>
  <div class="stat-row">
    <div class="stat"><div class="stat-num" id="tok-total">—</div><div class="stat-lbl">Total Tokens</div></div>
    <div class="stat"><div class="stat-num" id="tok-cloud" style="color:#ffd700">—</div><div class="stat-lbl">Cloud Tokens</div></div>
    <div class="stat"><div class="stat-num" id="tok-local" style="color:#28c76f">—</div><div class="stat-lbl">Local Tokens</div></div>
    <div class="stat"><div class="stat-num" id="tok-cost" style="color:#7ec8e3">—</div><div class="stat-lbl">Est. Cost (USD)</div></div>
    <div class="stat"><div class="stat-num" id="tok-calls">—</div><div class="stat-lbl">Calls (cloud/local)</div></div>
  </div>

  <!-- Cloud rate-limit budget guard -->
  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><strong style="font-size:.88em">⚡ Cloud Rate Guard — last 60 min</strong>
      <span style="font-size:.72em;color:#9a9ab0">meters <code style="color:#ffd700">JARVIS_CLOUD_TOKENS_PER_HOUR</code></span></div>
    <div style="padding:14px 16px">
      <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px;flex-wrap:wrap">
        <span style="font-size:1.8em;font-weight:700;color:#ffd700" id="tok-hourly">—</span>
        <span style="font-size:.8em;color:#9a9ab0">cloud tokens used in the last hour</span>
      </div>
      <div style="position:relative;height:14px;background:#0a0a14;border:1px solid #1a1a3a;border-radius:7px;overflow:hidden" role="img" aria-label="Cloud token usage vs observed range">
        <div id="tok-gauge-fill" style="height:100%;width:0%;background:linear-gradient(90deg,#28c76f,#ffd700);transition:width .4s"></div>
        <div style="position:absolute;top:0;bottom:0;left:13.3%;width:1px;background:#5a5a9a" title="observed median 40K"></div>
        <div style="position:absolute;top:0;bottom:0;left:73%;width:1px;background:#ff9f43" title="observed p90 219K"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:.68em;color:#70708e;margin-top:4px">
        <span>0</span><span>median 40K</span><span>p90 219K</span><span>300K suggested cap</span>
      </div>
      <p style="font-size:.78em;color:#a8a8c0;line-height:1.6;margin-top:10px">
        This is the exact figure F's guard meters: when the last hour's cloud token total reaches
        <code style="color:#ffd700">JARVIS_CLOUD_TOKENS_PER_HOUR</code>, auto-mode cloud routing degrades to local
        (explicit cloud and forced models are unaffected; the guard is dormant unless the env var is set to a positive int).
        Observed 7-day range: median 40K/hr, p90 219K/hr, max 486K/hr — F's data-driven suggestion is
        <strong style="color:#ffd700">300000</strong> (clears healthy operation, catches runaway bursts).
      </p>
    </div>
  </div>

  <!-- Context budget governor -->
  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><strong style="font-size:.88em">🧭 Context Governor</strong>
      <span style="font-size:.72em;color:#9a9ab0">local prompt budget compiler</span></div>
    <div style="padding:14px 16px">
      <div class="stat-row" style="margin-bottom:10px">
        <div class="stat"><div class="stat-num" id="ctx-calls">—</div><div class="stat-lbl">Budgeted Calls</div></div>
        <div class="stat"><div class="stat-num" id="ctx-selected" style="color:#28c76f">—</div><div class="stat-lbl">Context Blocks Used</div></div>
        <div class="stat"><div class="stat-num" id="ctx-dropped" style="color:#ff9f43">—</div><div class="stat-lbl">Blocks Dropped</div></div>
        <div class="stat"><div class="stat-num" id="ctx-turns" style="color:#ffd700">—</div><div class="stat-lbl">Old Turns Dropped</div></div>
        <div class="stat"><div class="stat-num" id="ctx-avg" style="color:#7ec8e3">—</div><div class="stat-lbl">Avg Context Tokens</div></div>
      </div>
      <div id="ctx-recent" style="font-size:.78em;color:#a8a8c0;line-height:1.55">No context-governed calls recorded in this window yet.</div>
      <div id="ctx-conv-recent" style="font-size:.78em;color:#a8a8c0;line-height:1.55;margin-top:8px">No conversation-history trims recorded in this window yet.</div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><strong style="font-size:.85em">By Model</strong></div>
    <div class="scroll-table">
      <table id="tok-model-table" style="table-layout:auto">
        <thead><tr><th>Model</th><th>Where</th><th>Calls</th><th>Prompt</th><th>Completion</th><th>Total</th><th>Cost</th></tr></thead>
        <tbody id="tok-model-body"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Help Modal -->
<div class="help-modal" id="help-modal" role="dialog" aria-label="Dashboard help" onclick="if(event.target===this)closeHelp()">
  <div class="help-box">
    <h2>⚡ How to Use Jarvis Agent Ops</h2>
    <div class="help-sec"><h3>🛰 Pipeline</h3><p>Every task flows left to right: Queued → Awaiting Approval → Running → Succeeded / Failed. Click any card to open the task drawer with its full event timeline, live output, and actions (approve, deny, stop, re-run, copy). The Live Activity feed below the board shows every status transition as it happens.</p></div>
    <div class="help-sec"><h3>⌁ Manager Console</h3><p>The default operating view for Aman-as-PM: audit verdict, active tasks, upgrade backlog, human review gates, agent health, training/eval state, and security counters in one place.</p></div>
    <div class="help-sec"><h3>↟ Upgrade Loop</h3><p>Shows the continuous local-first improvement loop: watched AI/MLX/Ollama/Apple sources, ranked tickets, sandbox-ready backlog items, and items that require human approval before execution.</p></div>
    <div class="help-sec"><h3>🛡 Security</h3><p>Dual-audience audit view over the append-only security log. The plain-language panel says what happened in human terms; the events table below gives the technical record (action, actor, target, decision, severity) with links into the task drawer. Stat tiles count denials, threat blocks, cloud calls, and how many actions carry a rollback reference.</p></div>
    <div class="help-sec"><h3>💸 Tokens</h3><p>Token and cost usage from the usage tracker, switchable across 1h / 24h / 7d. The <strong>Cloud Rate Guard</strong> card shows the exact figure Jarvis's rate guard meters — cloud tokens used in the last 60 minutes — against the observed range (median 40K, p90 219K) so you can pick a value for <code>JARVIS_CLOUD_TOKENS_PER_HOUR</code>. When set, auto-mode cloud routing degrades to local once the hourly cloud total hits that cap. The By Model table breaks down calls, prompt/completion tokens, and cost per model.</p></div>
    <div class="help-sec"><h3>⌘K Command Palette</h3><p>Press <code>Cmd+K</code> (or <code>Ctrl+K</code>) anywhere to open the palette. Type to filter, arrows to navigate, Enter to run: jump to any tab, assign work to a specific agent, run a pipeline health check, or start a standup. <code>Esc</code> closes any open drawer, palette, or dialog. Press <code>?</code> for this help. Tabs support ←/→ arrow keys.</p></div>
    <div class="help-sec"><h3>⏳ Approval Queue</h3><p>Tasks flagged by the security gate land here before running. Approve to execute or Deny to cancel. Risk tasks from risky prompts (vault writes, exec, external calls) route here automatically.</p></div>
    <div class="help-sec"><h3>▶ Active</h3><p>Tasks currently running or queued. Shows which agent is working and a live output preview. Use Stop to cancel a running task.</p></div>
    <div class="help-sec"><h3>📋 History</h3><p>All completed tasks. Click a row to expand the full output. Use Clear History to reset. Results persist until cleared or server restart.</p></div>
    <div class="help-sec"><h3>＋ Assign Work</h3><p>Send a task directly to one agent. Pick the agent, describe what you want, hit Assign. The output streams live into the result box. Leave agent blank to let the Manager route it to whoever's best suited.</p></div>
    <div class="help-sec"><h3>🤖 Agents (Virtual Office Floor)</h3><p>See all 15 agents in their department zones. Blue pulse = active, green = done, gold = waiting approval. The 🏋️ Eval Gym row lights up purple whenever an agent output is being quality-reviewed. Click any agent seat to jump directly to Assign Work for that agent.</p></div>
    <div class="help-sec"><h3>🚀 Project</h3><p>Enter a goal and click <strong>Generate Plan</strong>. The LLM decomposes it into tasks per agent, grouped into parallel waves. Edit any task inline before hitting <strong>Execute Plan</strong>. Each dispatched task streams live output in the Active tab and in the execution panel. Use <strong>Run Without Plan</strong> to dispatch immediately via the Manager stream.</p></div>
    <div class="help-sec"><h3>⌨️ Slash Commands</h3><p>In the Assign Work prompt box, type a command on its own line and press Enter: <code>/plan &lt;goal&gt;</code> — generate a project plan, <code>/project &lt;goal&gt;</code> — open project tab with goal pre-filled, <code>/status</code> — jump to active tasks, <code>/help</code> — show this dialog.</p></div>
    <div class="help-sec"><h3>📊 Eval Log</h3><p>Persistent history of every AI Evaluation Lab result across all project runs and direct agent evals. Filter by agent or verdict. Create custom rubrics to add extra criteria to any eval.</p></div>
    <div class="help-sec"><h3>⏰ Routines</h3><p>Schedule recurring agent tasks. Supports cron expressions (<code>0 9 * * 1</code>), macros (<code>@daily</code>, <code>@hourly</code>, <code>@weekly</code>), and intervals (<code>interval:3600</code>). The scheduler fires every 30 seconds and dispatches due tasks via <code>submit_task</code>. Toggle enabled/disabled without deleting. Click the trash icon to remove a routine.</p></div>
    <div class="help-sec"><h3>🔍 Pipeline Monitor (top bar)</h3><p>Click to sweep all active tasks for stalls, short outputs, or garbage. Shows alert count or green if healthy. Alerts persist for 8s then reset.</p></div>
    <button class="btn btn-primary" style="margin-top:16px;width:100%" onclick="closeHelp()">Got it</button>
  </div>
</div>

<!-- Task Detail Drawer -->
<aside id="task-drawer" class="drawer" role="dialog" aria-labelledby="drawer-title" aria-describedby="drawer-prompt">
  <div class="drawer-head">
    <strong id="drawer-title" style="font-size:.95em;color:#7ec8e3">Task Details</strong>
    <span class="tag tag-agent" id="drawer-agent">—</span>
    <span id="drawer-status"></span>
    <code id="drawer-id" style="font-size:.7em;color:#70708e"></code>
    <button id="drawer-close" class="btn btn-run btn-sm" style="margin-left:auto" onclick="closeTaskDrawer()" aria-label="Close task details">✕ Close</button>
  </div>
  <div class="drawer-body">
    <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin-bottom:6px;font-weight:600">Prompt</div>
    <div id="drawer-prompt" style="font-size:.84em;color:#c8c8dc;background:#0a0a14;border:1px solid #1a1a3a;border-radius:8px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;max-height:140px;overflow-y:auto"></div>
    <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin:16px 0 4px;font-weight:600">Event Timeline</div>
    <div id="drawer-timeline"><div class="empty" style="padding:12px">Loading…</div></div>
    <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#9a9ab0;margin:16px 0 6px;font-weight:600">Output</div>
    <pre id="drawer-output" style="white-space:pre-wrap;word-break:break-word;font-size:.8em;color:#a8d8c0;background:#060610;border:1px solid #1a1a3a;border-radius:8px;padding:10px 12px;margin:0;max-height:320px;overflow-y:auto;min-height:48px"></pre>
  </div>
  <div class="drawer-foot" id="drawer-actions"></div>
</aside>

<!-- Command Palette -->
<div id="cmd-palette" class="palette" role="dialog" aria-label="Command palette" onclick="if(event.target===this)closePalette()">
  <div class="palette-box">
    <input id="palette-input" type="text" placeholder="Type a command — tabs, agents, actions…"
           role="combobox" aria-expanded="true" aria-controls="palette-list" aria-label="Search commands" autocomplete="off">
    <ul id="palette-list" class="palette-list" role="listbox" aria-label="Commands"></ul>
  </div>
</div>

<script>
  let token = {token_js};
  let currentTab = 'console';
  let refreshTimer = null;

  const AGENT_DESCRIPTIONS = {{
    backend_engineer:  'API design, FastAPI endpoints, database schemas, async Python',
    frontend_designer: 'PyQt6 components, UI layout, dark-theme design specs',
    ux_researcher:     'User flow critique, interaction patterns, accessibility',
    qa_tester:         'Test plans, pytest suites, edge case analysis',
    devops_release:    'Release checklists, CI/CD, packaging, deployment',
    researcher:        'Web research, synthesis, best-practice surveys',
    automation_engineer: 'Shell scripts, file pipelines, batch ops',
    career_agent:      'Resume tailoring, job scoring, interview prep',
    memory_librarian:  'Store/recall project knowledge, semantic search',
    security_reviewer: 'Script review, threat analysis, CVE scanning',
    ai_safety_agent:   'Risk triage, threat scoring, pre-execution review',
    ai_evaluator:          'Output quality scoring, pass/fail/needs-review verdicts, defect feedback',
    pipeline_monitor:      'Stall detection, task health sweep, garbage output alerts',
    output_quality_checker:'Validates completed outputs: code presence, completeness, policy compliance',
    agent_worker:          'Task dispatch, routing, orchestration',
  }};

  function doAuth() {{
    const v = document.getElementById('auth-input').value.trim();
    if (!v) return;
    token = v;
    localStorage.setItem('jarvis_auth_token', v);
    document.getElementById('auth-gate').style.display = 'none';
    start();
  }}

  function hdrs() {{ return token ? {{'Authorization': 'Bearer ' + token}} : {{}}; }}

  async function apiFetch(path, opts={{}}) {{
    const r = await fetch(path, {{ headers: hdrs(), ...opts }});
    if (r.status === 401) {{
      document.getElementById('auth-gate').style.display = 'flex';
      throw new Error('unauthorized');
    }}
    return r.json();
  }}

  function switchTab(name) {{
    currentTab = name;
    document.querySelectorAll('[role="tab"]').forEach(t => {{
      const sel = t.dataset.tab === name;
      t.classList.toggle('active', sel);
      t.setAttribute('aria-selected', sel ? 'true' : 'false');
      t.setAttribute('tabindex', sel ? '0' : '-1');
    }});
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
    document.getElementById('pane-' + name).classList.add('active');
    if (name === 'agents') initWorldIfNeeded();
    if (name === 'lessons') loadPendingLessons();
    if (name === 'routines') loadRoutines();
    if (name === 'security') loadSecurity();
    if (name === 'tokens') loadTokens();
    if (name === 'upgrades') loadUpgradeLoop();
    if (name === 'console') loadManagerConsole();
    if (!['project','lessons','routines','security','upgrades','console'].includes(name)) refresh();
  }}

  // Arrow-key navigation within the tablist (WAI-ARIA tabs pattern)
  function _initTablistKeys() {{
    const list = document.querySelector('[role="tablist"]');
    if (!list) return;
    list.addEventListener('keydown', (e) => {{
      const tabs = [...list.querySelectorAll('[role="tab"]')];
      const idx = tabs.indexOf(document.activeElement);
      if (idx < 0) return;
      let next = -1;
      if (e.key === 'ArrowRight') next = (idx + 1) % tabs.length;
      else if (e.key === 'ArrowLeft') next = (idx - 1 + tabs.length) % tabs.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = tabs.length - 1;
      if (next < 0) return;
      e.preventDefault();
      tabs[next].focus();
      switchTab(tabs[next].dataset.tab);
    }});
  }}

  function fmtTime(ts) {{
    if (!ts) return '—';
    const d = new Date(ts);
    return d.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}}) + ' ' + d.toLocaleDateString([], {{month:'short',day:'numeric'}});
  }}

  function statusTag(s) {{
    const map = {{
      waiting_approval:'tag-wait', queued:'tag-run', assigned:'tag-run',
      running:'tag-run', streaming:'tag-run',
      completed:'tag-done', succeeded:'tag-done',
      failed:'tag-fail', cancelled:'tag-fail',
    }};
    return `<span class="tag ${{map[s]||'tag-wait'}}">${{s||'?'}}</span>`;
  }}

  async function refresh() {{
    try {{
      const [allTasks, agentsData] = await Promise.all([
        apiFetch('/tasks?limit=100'),
        apiFetch('/agents'),
      ]);
      const tasks = allTasks.tasks || [];
      const agents = agentsData.agents || [];
      if(typeof _updateMeetingTaskData==='function') _updateMeetingTaskData(tasks);

      const pending = tasks.filter(t => t.status === 'waiting_approval');
      const running = tasks.filter(t => t.status === 'running' || t.status === 'queued');
      const done = tasks.filter(t => ['completed','succeeded','failed'].includes(t.status));

      document.getElementById('st-pending').textContent = pending.length;
      document.getElementById('st-running').textContent = running.length;
      document.getElementById('st-done').textContent = done.length;
      document.getElementById('queue-count').textContent = pending.length ? `(${{pending.length}})` : '';
      document.getElementById('active-count').textContent = running.length ? `(${{running.length}})` : '';
      const inflight = tasks.filter(t => ['queued','assigned','running','streaming','waiting_approval'].includes(t.status)).length;
      document.getElementById('pipeline-count').textContent = inflight ? `(${{inflight}})` : '';

      _recordActivity(tasks);
      if (currentTab === 'pipeline') renderPipeline(tasks);
      if (currentTab === 'queue') renderQueue(pending);
      if (currentTab === 'active') renderActive(running);
      if (currentTab === 'history') renderHistory(done);
      if (currentTab === 'agents') renderOfficeFloor(agents, tasks);
      if (currentTab === 'evallog') loadEvalLog();
      if (currentTab === 'console') loadManagerConsole();
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        document.getElementById('status-badge').className = 'badge badge-err';
    }}
  }}

  function _setKpi(id, value, cls='') {{
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.className = 'kpi ' + cls;
  }}

  function _ticketStatusGroup(t) {{
    const status = (t.status || 'proposed').toLowerCase();
    if (t.requires_review || t.risk_level === 'high') return 'review';
    if (status.includes('backlog') || status.includes('promoted')) return 'backlog';
    return 'proposed';
  }}

  function _upgradeTicketHtml(t) {{
    const risk = t.risk_level || 'low';
    const riskCls = risk === 'high' ? 'tag-fail' : risk === 'medium' ? 'tag-wait' : 'tag-done';
    const title = escHtml(t.title || 'Upgrade ticket');
    const summary = escHtml((t.work_order?.tasks?.[0]?.description || t.source_url || '').slice(0, 180));
    return `<div class="upgrade-ticket">
      <strong>${{title}}</strong>
      <p>${{summary || 'Local preview ticket. Human approval required before execution.'}}</p>
      <div style="display:flex;gap:5px;flex-wrap:wrap">
        <span class="tag tag-agent">${{escHtml(t.category || 'general')}}</span>
        <span class="tag ${{riskCls}}">${{escHtml(risk)}}</span>
        <span class="tag tag-run">score ${{t.score ?? '—'}}</span>
        ${{t.status ? `<span class="tag">${{escHtml(t.status)}}</span>` : ''}}
      </div>
    </div>`;
  }}

  function _renderUpgradeData(upgrade) {{
    const summary = upgrade.summary || {{}};
    const tickets = upgrade.tickets || [];
    document.getElementById('up-cycles').textContent = summary.cycle_count ?? 0;
    document.getElementById('up-candidates').textContent = summary.candidate_count ?? 0;
    document.getElementById('up-tickets').textContent = summary.ticket_count ?? tickets.length;
    document.getElementById('up-decisions').textContent = summary.decision_count ?? 0;
    document.getElementById('upgrade-count').textContent = tickets.length ? `(${{tickets.length}})` : '';

    const groups = {{proposed: [], backlog: [], review: []}};
    tickets.forEach(t => groups[_ticketStatusGroup(t)].push(t));
    const fill = (id, list) => {{
      const el = document.getElementById(id);
      if (el) el.innerHTML = list.length ? list.slice(0, 12).map(_upgradeTicketHtml).join('')
        : '<div style="font-size:.75em;color:#70708e;padding:6px 2px">—</div>';
    }};
    fill('up-col-proposed', groups.proposed);
    fill('up-col-backlog', groups.backlog);
    fill('up-col-review', groups.review);
    document.getElementById('up-proposed-count').textContent = groups.proposed.length;
    document.getElementById('up-backlog-count').textContent = groups.backlog.length;
    document.getElementById('up-review-count').textContent = groups.review.length;

    const watch = upgrade.watchlist || [];
    document.getElementById('up-signal-count').textContent = watch.length;
    document.getElementById('up-col-signals').innerHTML = watch.length ? watch.map(s => `
      <div class="upgrade-ticket">
        <strong>${{escHtml(s.title || 'Watch source')}}</strong>
        <p>${{escHtml(s.why || s.source_url || '')}}</p>
        <div style="display:flex;gap:5px;flex-wrap:wrap">
          <span class="tag tag-agent">${{escHtml(s.category || 'source')}}</span>
          <span class="tag tag-run">${{String(s.source_url||'').startsWith('local:') ? 'local' : 'public'}}</span>
        </div>
      </div>`).join('') : '<div style="font-size:.75em;color:#70708e;padding:6px 2px">No watchlist sources configured.</div>';
    document.getElementById('up-watchlist').innerHTML = watch.length ? watch.map(s => `
      <div class="source-row">
        <div>
          <strong style="font-size:.86em;color:#d8d8e8">${{escHtml(s.title || 'Source')}}</strong>
          <div style="font-size:.75em;color:#8a8aa4;margin-top:3px">${{escHtml(s.why || '')}}</div>
        </div>
        <span class="tag tag-agent">${{escHtml(s.category || 'source')}}</span>
      </div>`).join('') : '<div class="empty" style="padding:8px">No watchlist sources configured.</div>';
  }}

  async function loadUpgradeLoop() {{
    try {{
      const state = await apiFetch('/dashboard/state');
      _renderUpgradeData(state.upgrade_loop || {{}});
    }} catch(e) {{
      if (e.message !== 'unauthorized') {{
        document.getElementById('up-col-proposed').innerHTML = `<div class="empty">Failed to load upgrade loop: ${{escHtml(e.message)}}</div>`;
      }}
    }}
  }}

  function _reviewItemsFromState(state) {{
    const tickets = (state.upgrade_loop?.tickets || []).filter(t => t.requires_review || t.risk_level === 'high');
    const out = tickets.slice(0, 6).map(t => ({{
      title: t.title || 'Upgrade requires review',
      copy: `${{t.category || 'general'}} · ${{t.risk_level || 'risk unknown'}} · score ${{t.score ?? '—'}}`,
      tag: 'upgrade',
    }}));
    const security = state.security || {{}};
    const critical = (security.recent_critical || []).slice(0, 3);
    critical.forEach(e => out.push({{
      title: e.action || 'Security event requires review',
      copy: e.reason || e.target || 'Critical security event in audit log.',
      tag: 'security',
    }}));
    if (state.tasks?.awaiting_approval) out.unshift({{
      title: `${{state.tasks.awaiting_approval}} task(s) waiting for approval`,
      copy: 'Review the Approval Queue before any risky action executes.',
      tag: 'task gate',
    }});
    return out;
  }}

  async function loadManagerConsole() {{
    try {{
      const state = await apiFetch('/dashboard/state');
      const audit = state.pipeline_audit || {{}};
      const verdict = audit.verdict || 'UNKNOWN';
      const vCls = verdict === 'CLEAN' ? 'ok' : verdict === 'WARN' ? 'warn' : 'bad';
      const active = state.tasks?.active ?? 0;
      const awaitingApproval = state.tasks?.awaiting_approval ?? 0;
      const totalInFlight = active + awaitingApproval;
      const totalTasks = state.tasks?.total ?? 0;
      const completedTasks = state.tasks?.completed ?? 0;
      const failedTasks = state.tasks?.failed ?? 0;
      const interruptedTasks = state.tasks?.interrupted ?? 0;
      const decided = completedTasks + failedTasks;  // terminal quality outcomes (excl. restart casualties + in-flight)
      const successRate = decided > 0 ? Math.round((completedTasks / decided) * 100) : null;
      const rateCls = successRate === null ? '' : successRate >= 70 ? 'ok' : successRate >= 50 ? 'warn' : 'bad';
      const rateStr = successRate !== null ? ` (${{successRate}}% quality pass rate)` : '';
      const interruptedStr = interruptedTasks ? `; ${{interruptedTasks}} interrupted by restarts (not counted as failures)` : '';
      const upgrades = state.upgrade_loop?.summary?.ticket_count ?? (state.upgrade_loop?.tickets || []).length;
      const denials = state.security?.denials ?? 0;
      const critical = state.security?.by_severity?.critical ?? 0;
      _setKpi('mc-audit', verdict, vCls);
      _setKpi('mc-active', totalInFlight, totalInFlight ? 'warn' : 'ok');
      _setKpi('mc-taskrate', successRate !== null ? `${{successRate}}%` : '—', rateCls);
      _setKpi('mc-upgrades', upgrades, upgrades ? 'warn' : '');
      _setKpi('mc-security', `${{denials}}/${{critical}}`, critical ? 'bad' : denials ? 'warn' : 'ok');
      document.getElementById('mc-nav-upgrades').textContent = upgrades;
      document.getElementById('mc-nav-pipeline').textContent = totalInFlight;
      document.getElementById('mc-nav-security').textContent = critical ? 'critical' : 'ok';
      document.getElementById('mc-nav-agents').textContent = state.agents?.total ?? 0;
      document.getElementById('mc-nav-evals').textContent = state.evals?.ok === false ? 'check' : 'ok';
      document.getElementById('upgrade-count').textContent = upgrades ? `(${{upgrades}})` : '';

      const acknowledged = audit.acknowledged_count ?? 0;
      const warnCount = audit.severity_counts?.warning ?? 0;
      const agentList = state.agents?.agents || [];
      const agentSummary = agentList.length
        ? agentList.map(a => `${{escHtml(a.agent_id)}} at ${{Math.round((a.success_rate ?? 0)*100)}}%`).join(', ')
        : 'none tracked';
      document.getElementById('mc-narrative').innerHTML =
        `<strong style="color:#e0e0ef">Current read:</strong> pipeline audit is <span style="color:${{verdict==='CLEAN'?'#28c76f':verdict==='WARN'?'#ffd700':'#ea5455'}}">${{escHtml(verdict)}}</span>, ` +
        `${{acknowledged}} historical warning(s) triaged, ${{warnCount}} active. ` +
        `Task outcome: <strong style="color:${{rateCls==='ok'?'#28c76f':rateCls==='warn'?'#ffd700':'#ea5455'}}">${{completedTasks}} succeeded, ${{failedTasks}} failed</strong> of ${{decided}} decided${{rateStr}}${{interruptedStr}}. ` +
        `Agent success rates — ${{agentSummary}}. ` +
        `Execution, package installs, cloud escalation, merge, push, and deploy still require human approval.`;

      const decisions = [
        ['Upgrade scout', upgrades ? `${{upgrades}} ticket(s) need review or promotion.` : 'No upgrade tickets waiting.'],
        ['Training signal', state.training?.ok === false ? 'Training status endpoint reported a problem.' : 'Training status endpoint is reachable.'],
        ['Pipeline trust', verdict === 'CLEAN' ? 'No active truthfulness warnings.' : 'Review active audit findings before treating outputs as complete.'],
      ];
      document.getElementById('mc-decisions').innerHTML = decisions.map(([title, copy]) => `
        <div class="decision-item"><strong>${{escHtml(title)}}</strong><p>${{escHtml(copy)}}</p></div>`).join('');

      const reviews = _reviewItemsFromState(state);
      document.getElementById('mc-review-count').textContent = reviews.length;
      document.getElementById('mc-review-list').innerHTML = reviews.length ? reviews.map(item => `
        <div class="review-item">
          <div class="review-title">${{escHtml(item.title)}}</div>
          <div class="review-copy">${{escHtml(item.copy)}}</div>
          <div class="meta"><span class="tag tag-wait">${{escHtml(item.tag)}}</span></div>
        </div>`).join('') : '<div class="empty" style="padding:8px">No human review items right now.</div>';
      _renderUpgradeData(state.upgrade_loop || {{}});
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        document.getElementById('mc-narrative').textContent = 'Failed to load manager console: ' + e.message;
    }}
  }}

  function renderQueue(tasks) {{
    const el = document.getElementById('queue-list');
    if (!tasks.length) {{ el.innerHTML = '<div class="empty">No tasks awaiting approval</div>'; return; }}
    el.innerHTML = tasks.map(t => `
      <div class="card" data-task-id="${{t.id}}">
        <div class="card-head">
          <div>
            <span class="tag tag-agent">${{t.assigned_agent_id || 'unassigned'}}</span>
            <span class="tag tag-wait" style="margin-left:4px">awaiting approval</span>
          </div>
          <span style="font-size:.75em;color:#555">${{fmtTime(t.created_at)}}</span>
        </div>
        <div class="card-body">${{escHtml(t.prompt || '')}}</div>
        ${{t.approval_reason ? `<div style="padding:8px 16px;font-size:.78em;color:#888;background:#0d0d1a;border-top:1px solid #1a1a3a">⚠ ${{escHtml(t.approval_reason)}}</div>` : ''}}
        <div class="card-foot">
          <button class="btn btn-approve btn-sm" onclick="approveTask('${{t.id}}')">✓ Approve</button>
          <button class="btn btn-deny btn-sm" onclick="denyTask('${{t.id}}')">✗ Deny</button>
          <button class="btn btn-run btn-sm" onclick="openTaskDrawer('${{t.id}}')">☰ Details</button>
          <span style="font-size:.75em;color:#555;margin-left:auto">${{t.id}}</span>
        </div>
      </div>`).join('');
  }}

  // ── Pipeline board ────────────────────────────────────────────────────────
  const _PIPE_BUCKETS = {{
    queued: ['queued','assigned'],
    wait:   ['waiting_approval'],
    run:    ['running','streaming'],
    done:   ['succeeded','completed'],
    fail:   ['failed','cancelled','denied'],
  }};

  function _pipeCard(t) {{
    const when = fmtTime(t.updated_at || t.finished_at || t.started_at || t.created_at);
    return `<button class="pipe-card" onclick="openTaskDrawer('${{t.id}}')" aria-label="Open task ${{t.id}} details">
      <span class="pc-agent">${{escHtml(t.assigned_agent_id || 'unassigned')}}</span>
      <span class="pc-prompt">${{escHtml((t.prompt||'').slice(0,160))}}</span>
      <span class="pc-time">${{statusTag(t.status)}} ${{when}}</span>
    </button>`;
  }}

  let _pipeTasksCache = [];

  function renderPipeline(tasks) {{
    _pipeTasksCache = tasks;
    // Agent filter options auto-discovered from current tasks (preserve selection)
    const agents = [...new Set(tasks.map(t => t.assigned_agent_id).filter(Boolean))].sort();
    _syncPipeAgentFilter(agents);
    const sorted = tasks.slice().sort((a,b) =>
      new Date(b.updated_at||b.created_at||0) - new Date(a.updated_at||a.created_at||0));
    const text = (document.getElementById('pipe-filter-text')?.value || '').trim().toLowerCase();
    const agent = document.getElementById('pipe-filter-agent')?.value || '';
    const match = t => (!agent || t.assigned_agent_id === agent)
      && (!text || ((t.prompt||'') + ' ' + (t.assigned_agent_id||'')).toLowerCase().includes(text));
    const stats = {{queued:'pp-queued',wait:'pp-wait',run:'pp-run',done:'pp-done',fail:'pp-fail'}};
    let shown = 0, total = 0;
    for (const [key, statuses] of Object.entries(_PIPE_BUCKETS)) {{
      const all = sorted.filter(t => statuses.includes(t.status));
      const filtered = all.filter(match);
      total += all.length; shown += filtered.length;
      const bucket = filtered.slice(0, 25);
      const col = document.getElementById('col-' + key);
      if (col) col.innerHTML = bucket.length ? bucket.map(_pipeCard).join('')
        : '<div style="font-size:.75em;color:#70708e;padding:6px 2px">—</div>';
      const cnt = document.getElementById('pc-' + key);
      if (cnt) cnt.textContent = filtered.length;
      // Stat tiles always reflect total pipeline health (unaffected by filter)
      const stat = document.getElementById(stats[key]);
      if (stat) stat.textContent = all.length;
    }}
    const filterActive = text || agent;
    const cntEl = document.getElementById('pipe-filter-count');
    if (cntEl) cntEl.textContent = filterActive ? `showing ${{shown}} of ${{total}}` : '';
  }}

  // Re-render from the last fetched tasks without an API round-trip (filter typing).
  function renderPipelineFromCache() {{ renderPipeline(_pipeTasksCache); }}

  function clearPipeFilter() {{
    const t = document.getElementById('pipe-filter-text'); if (t) t.value = '';
    const a = document.getElementById('pipe-filter-agent'); if (a) a.value = '';
    renderPipelineFromCache();
  }}

  function _syncPipeAgentFilter(agents) {{
    const sel = document.getElementById('pipe-filter-agent');
    if (!sel) return;
    const want = ['', ...agents].join('|');
    if (sel.dataset.signature === want) return;  // unchanged — don't clobber selection
    const current = sel.value;
    sel.dataset.signature = want;
    sel.innerHTML = '<option value="">All agents</option>' +
      agents.map(a => `<option value="${{escHtml(a)}}"${{a===current?' selected':''}}>${{escHtml(a)}}</option>`).join('');
    sel.value = current;
  }}

  // ── Live activity feed + ticker ──────────────────────────────────────────
  let _prevStatuses = null;  // null until first refresh (seed silently)
  const _activityLog = [];

  function _recordActivity(tasks) {{
    const now = {{}};
    tasks.forEach(t => {{ now[t.id] = t.status; }});
    if (_prevStatuses !== null) {{
      tasks.forEach(t => {{
        const prev = _prevStatuses[t.id];
        if (prev === t.status) return;
        const agent = t.assigned_agent_id || 'unassigned';
        const text = prev === undefined
          ? `${{agent}}: new task ${{t.status}}`
          : `${{agent}}: ${{prev}} → ${{t.status}}`;
        _activityLog.unshift({{ts: new Date(), text, prompt: (t.prompt||'').slice(0,60), id: t.id}});
      }});
      if (_activityLog.length > 30) _activityLog.length = 30;
      if (_activityLog.length) {{
        const latest = _activityLog[0];
        document.getElementById('live-ticker').textContent = `${{latest.text}} — ${{latest.prompt}}`;
      }}
    }}
    _prevStatuses = now;
    const feed = document.getElementById('activity-feed');
    if (feed && currentTab === 'pipeline' && _activityLog.length) {{
      feed.innerHTML = _activityLog.map(a => `
        <div class="feed-item">
          <span class="feed-ts">${{a.ts.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit',second:'2-digit'}})}}</span>
          <span>${{escHtml(a.text)}}</span>
          <button class="btn btn-run btn-sm" style="font-size:.85em;padding:1px 8px;margin-left:auto" onclick="openTaskDrawer('${{a.id}}')" aria-label="Open task details">view</button>
        </div>`).join('');
    }}
  }}

  // ── Task detail drawer ────────────────────────────────────────────────────
  let _drawerTaskId = null;
  let _drawerTimer = null;
  let _drawerStream = null;
  let _drawerReturnFocus = null;
  let _drawerLastTask = null;

  async function openTaskDrawer(id) {{
    if (_drawerStream) {{ _drawerStream.close(); _drawerStream = null; }}
    _drawerTaskId = id;
    _drawerLastTask = null;
    _drawerReturnFocus = document.activeElement;
    const drawer = document.getElementById('task-drawer');
    drawer.classList.add('open');
    document.getElementById('drawer-close').focus();
    await _refreshDrawer();
    if (_drawerTimer) clearInterval(_drawerTimer);
    _drawerTimer = setInterval(() => {{
      const t = _drawerLastTask;
      if (t && ['queued','assigned','waiting_approval'].includes(t.status)) _refreshDrawer();
    }}, 3000);
  }}

  function closeTaskDrawer() {{
    document.getElementById('task-drawer').classList.remove('open');
    _drawerTaskId = null;
    if (_drawerTimer) {{ clearInterval(_drawerTimer); _drawerTimer = null; }}
    if (_drawerStream) {{ _drawerStream.close(); _drawerStream = null; }}
    if (_drawerReturnFocus && _drawerReturnFocus.focus) _drawerReturnFocus.focus();
  }}

  function _timelineHtml(events) {{
    if (!events.length) return '<div class="empty" style="padding:12px">No events recorded.</div>';
    // Collapse consecutive chunk events into one summary item
    const items = [];
    let chunkRun = 0;
    const flushChunks = () => {{
      if (chunkRun > 0) {{
        items.push({{cls:'tl-chunk', label:`output streamed (${{chunkRun}} chunk${{chunkRun>1?'s':''}})`, ts:null}});
        chunkRun = 0;
      }}
    }};
    events.forEach(ev => {{
      if (ev.type === 'chunk') {{ chunkRun++; return; }}
      flushChunks();
      if (ev.type === 'status') items.push({{cls:'tl-status', label:`status: ${{ev.status||'?'}}${{ev.reason?' — '+ev.reason:''}}`, ts:ev.ts}});
      else if (ev.type === 'error') items.push({{cls:'tl-error', label:`error: ${{ev.error||'unknown'}}`, ts:ev.ts}});
      else if (ev.type === 'workspace' || ev.type === 'workspace_diff') items.push({{cls:'tl-ws', label:ev.type.replace('_',' '), ts:ev.ts}});
      else items.push({{cls:'tl-status', label:ev.type, ts:ev.ts}});
    }});
    flushChunks();
    return '<ol class="timeline">' + items.map(i => `
      <li class="tl-item"><span class="tl-dot ${{i.cls}}" aria-hidden="true"></span>${{escHtml(i.label)}}${{i.ts?`<span class="tl-ts">${{fmtTime(i.ts)}}</span>`:''}}</li>`).join('') + '</ol>';
  }}

  async function _refreshDrawer() {{
    if (!_drawerTaskId) return;
    const id = _drawerTaskId;
    try {{
      const [td, ed] = await Promise.all([
        apiFetch(`/tasks/${{id}}`),
        apiFetch(`/tasks/${{id}}/events`),
      ]);
      const t = td.task || td;
      _drawerLastTask = t;
      document.getElementById('drawer-agent').textContent = t.assigned_agent_id || 'unassigned';
      document.getElementById('drawer-status').innerHTML = statusTag(t.status);
      document.getElementById('drawer-id').textContent = t.id;
      document.getElementById('drawer-prompt').textContent = t.prompt || '(no prompt)';
      document.getElementById('drawer-timeline').innerHTML = _timelineHtml(ed.events || []);
      const out = document.getElementById('drawer-output');
      out.textContent = t.result || '';
      if (t.error) out.textContent += (out.textContent ? '\\n\\n' : '') + 'Error: ' + t.error;
      // Live-follow output while running
      if (['running','streaming'].includes(t.status) && !_drawerStream) {{
        const src = new EventSource(`/tasks/${{id}}/stream`);
        _drawerStream = src;
        src.onmessage = (e) => {{
          if (e.data === '[DONE]') {{ src.close(); _drawerStream = null; _refreshDrawer(); return; }}
          try {{
            const d = JSON.parse(e.data);
            if (d.chunk) {{ out.textContent += d.chunk; out.scrollTop = out.scrollHeight; }}
            if (d.type === 'done') {{ src.close(); _drawerStream = null; _refreshDrawer(); }}
          }} catch {{}}
        }};
        src.onerror = () => {{ src.close(); _drawerStream = null; }};
      }}
      // Action buttons depend on status
      const foot = document.getElementById('drawer-actions');
      const btns = [];
      if (t.status === 'waiting_approval') {{
        btns.push(`<button class="btn btn-approve" onclick="_drawerAct('approve')">✓ Approve</button>`);
        btns.push(`<button class="btn btn-deny" onclick="_drawerAct('deny')">✗ Deny</button>`);
      }}
      if (['queued','assigned','running','streaming'].includes(t.status))
        btns.push(`<button class="btn btn-deny" onclick="_drawerAct('cancel')">■ Stop</button>`);
      if (['succeeded','completed','failed','cancelled','denied'].includes(t.status))
        btns.push(`<button class="btn btn-run" onclick="rerunDrawerTask()">↻ Re-run</button>`);
      if (t.result) btns.push(`<button class="btn btn-run" onclick="copyDrawerOutput(this)">⧉ Copy output</button>`);
      foot.innerHTML = btns.join('') + `<span style="margin-left:auto;font-size:.72em;color:#70708e;align-self:center">${{fmtTime(t.created_at)}} → ${{fmtTime(t.finished_at)}}</span>`;
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        document.getElementById('drawer-timeline').innerHTML = `<div class="empty">Failed to load task: ${{escHtml(e.message)}}</div>`;
    }}
  }}

  async function _drawerAct(action) {{
    if (!_drawerTaskId) return;
    await apiFetch(`/tasks/${{_drawerTaskId}}/${{action}}`, {{method:'POST'}});
    await _refreshDrawer();
    refresh();
  }}

  async function rerunDrawerTask() {{
    const t = _drawerLastTask;
    if (!t || !t.prompt) return;
    const kinds = ['chat','code','research','review','plan'];
    const data = await apiFetch('/tasks', {{
      method: 'POST',
      headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ prompt: t.prompt, kind: kinds.includes(t.kind) ? t.kind : 'chat', source: 'dashboard' }}),
    }});
    if (data.ok && data.task) {{
      await openTaskDrawer(data.task.id);
      refresh();
    }}
  }}

  function copyDrawerOutput(btn) {{
    const out = document.getElementById('drawer-output');
    navigator.clipboard.writeText(out.textContent || '').then(() => {{
      const old = btn.textContent;
      btn.textContent = '✓ Copied';
      setTimeout(() => {{ btn.textContent = old; }}, 1500);
    }});
  }}

  // ── Token / rate-limit panel (renders /usage; cloud-rate guard = F's metric) ─
  function _fmtTokens(n) {{
    n = Number(n) || 0;
    if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
    return String(n);
  }}

  async function loadTokens() {{
    try {{
      const hours = parseInt(document.getElementById('tok-window')?.value || '24', 10);
      // 24h-style window for the totals, plus a fixed 1h window for the rate guard.
      const [winData, hourData] = await Promise.all([
        apiFetch('/usage?hours=' + hours + '&recent=0'),
        apiFetch('/usage?hours=1&recent=0'),
      ]);
      const u = winData.usage || {{}};
      document.getElementById('tok-total').textContent = _fmtTokens(u.total_tokens);
      const cloudTokens = (u.total_tokens || 0) - _localTokens(u);
      document.getElementById('tok-cloud').textContent = _fmtTokens(cloudTokens);
      document.getElementById('tok-local').textContent = _fmtTokens(_localTokens(u));
      document.getElementById('tok-cost').textContent = '$' + (Number(u.estimated_cost_usd) || 0).toFixed(4);
      document.getElementById('tok-calls').textContent = `${{u.cloud_call_count || 0}} / ${{u.local_call_count || 0}}`;

      const ctx = u.context_budget || {{}};
      document.getElementById('ctx-calls').textContent = ctx.call_count || 0;
      document.getElementById('ctx-selected').textContent = ctx.selected_block_count || 0;
      document.getElementById('ctx-dropped').textContent = ctx.dropped_block_count || 0;
      document.getElementById('ctx-avg').textContent = _fmtTokens(ctx.average_used_tokens || 0);
      const conv = u.conversation_budget || {{}};
      document.getElementById('ctx-turns').textContent = conv.dropped_message_count || 0;
      const recentCtx = (ctx.recent || []).slice().reverse().slice(0, 4);
      document.getElementById('ctx-recent').innerHTML = recentCtx.length ? recentCtx.map(item => `
        <div style="display:flex;justify-content:space-between;gap:10px;border-top:1px solid #17172a;padding:7px 0">
          <span>${{escHtml(item.model || 'local')}}</span>
          <span style="color:#8f8fac">${{_fmtTokens(item.context_used_tokens || 0)}} / ${{_fmtTokens(item.context_budget_tokens || 0)}} ctx tokens</span>
          <span style="color:${{(item.dropped || []).length ? '#ff9f43' : '#28c76f'}}">${{(item.dropped || []).length}} dropped</span>
        </div>`).join('') : 'No context-governed calls recorded in this window yet.';
      const recentConv = (conv.recent || []).slice().reverse().slice(0, 3);
      document.getElementById('ctx-conv-recent').innerHTML = recentConv.length ? recentConv.map(item => `
        <div style="display:flex;justify-content:space-between;gap:10px;border-top:1px solid #17172a;padding:7px 0">
          <span>${{escHtml(item.model || 'local')}} conversation</span>
          <span style="color:#8f8fac">${{_fmtTokens(item.original_prompt_tokens || 0)}} → ${{_fmtTokens(item.final_prompt_tokens || 0)}} prompt tokens</span>
          <span style="color:${{item.dropped_message_count ? '#ffd700' : '#28c76f'}}">${{item.dropped_message_count || 0}} old turns</span>
        </div>`).join('') : 'No conversation-history trims recorded in this window yet.';

      // Rate guard: last-hour cloud tokens — the exact quantity F's guard sums.
      const hu = hourData.usage || {{}};
      const hourlyCloud = (hu.total_tokens || 0) - _localTokens(hu);
      document.getElementById('tok-hourly').textContent = _fmtTokens(hourlyCloud);
      // Gauge scaled so 300K suggested cap ≈ full bar.
      const pct = Math.min(100, (hourlyCloud / 300000) * 100);
      const fill = document.getElementById('tok-gauge-fill');
      fill.style.width = pct + '%';
      fill.style.background = hourlyCloud >= 219000 ? 'linear-gradient(90deg,#ff9f43,#ea5455)'
        : hourlyCloud >= 40000 ? 'linear-gradient(90deg,#28c76f,#ffd700)' : '#28c76f';
      document.getElementById('tokens-count').textContent = hourlyCloud >= 219000 ? '(high)' : '';

      // By-model table (sorted by total tokens desc)
      const models = Object.entries(u.by_model || {{}})
        .map(([name, m]) => ({{name, ...m}}))
        .sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0));
      const tbody = document.getElementById('tok-model-body');
      tbody.innerHTML = models.length ? models.map(m => `
        <tr>
          <td style="font-size:.8em;color:#c8c8dc;white-space:nowrap">${{escHtml(m.name)}}</td>
          <td><span class="tag ${{m.local ? 'tag-done' : 'tag-wait'}}" style="font-size:.7em">${{m.local ? 'local' : 'cloud'}}</span></td>
          <td style="font-size:.8em;color:#9a9ab8">${{m.call_count || 0}}</td>
          <td style="font-size:.8em;color:#9a9ab8">${{_fmtTokens(m.prompt_tokens)}}</td>
          <td style="font-size:.8em;color:#9a9ab8">${{_fmtTokens(m.completion_tokens)}}</td>
          <td style="font-size:.8em;color:#c8c8dc;font-weight:600">${{_fmtTokens(m.total_tokens)}}</td>
          <td style="font-size:.8em;color:#7ec8e3">$${{(Number(m.estimated_cost_usd)||0).toFixed(4)}}</td>
        </tr>`).join('')
        : '<tr><td colspan="7" class="empty">No usage recorded in this window.</td></tr>';
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        document.getElementById('tok-model-body').innerHTML = `<tr><td colspan="7" class="empty">Failed to load usage: ${{escHtml(e.message)}}</td></tr>`;
    }}
  }}

  // Local token total = sum of by_model buckets flagged local (summary has no
  // direct local-token field; cloud = total − local).
  function _localTokens(u) {{
    return Object.values(u.by_model || {{}})
      .filter(m => m && m.local)
      .reduce((s, m) => s + (Number(m.total_tokens) || 0), 0);
  }}

  // ── Security panel (renders C's /security/summary + /security/events) ─────
  const _SEV_COLORS = {{info:'#7ec8e3', notice:'#a78bfa', warning:'#ff9f43', critical:'#ea5455'}};

  // Keep the action/source <select> in sync with the actions actually present,
  // preserving the current selection. plan_precheck (PreFlect, D's lane) shows
  // up automatically once those events start flowing through /security/events.
  function _syncActionFilter(actions, current) {{
    const sel = document.getElementById('sec-filter-action');
    if (!sel) return;
    const want = ['', ...actions].join('|');
    if (sel.dataset.signature === want) return;  // unchanged — don't clobber focus
    sel.dataset.signature = want;
    sel.innerHTML = '<option value="">All actions</option>' +
      actions.map(a => `<option value="${{escHtml(a)}}"${{a===current?' selected':''}}>${{escHtml(a)}}</option>`).join('');
    sel.value = current;
  }}

  async function loadSecurity() {{
    try {{
      const severity = document.getElementById('sec-filter-severity')?.value || '';
      const action = document.getElementById('sec-filter-action')?.value || '';
      const params = new URLSearchParams({{limit: 100}});
      if (severity) params.set('severity', severity);
      if (action) params.set('action', action);
      const [sumData, evData] = await Promise.all([
        apiFetch('/security/summary'),
        apiFetch('/security/events?' + params),
      ]);
      const s = sumData.summary || {{}};
      // Populate the action/source filter from observed actions (auto-includes
      // plan_precheck once PreFlect lands — D's lane emits via audit_event).
      _syncActionFilter(Object.keys(s.by_action || {{}}).sort(), action);
      document.getElementById('sec-total').textContent = s.total ?? 0;
      document.getElementById('sec-denials').textContent = s.denials ?? 0;
      document.getElementById('sec-blocks').textContent = s.threat_blocks ?? 0;
      document.getElementById('sec-cloud').textContent = s.cloud_calls ?? 0;
      document.getElementById('sec-rollback').textContent = s.rollbackable ?? 0;
      const critCount = (s.by_severity||{{}}).critical || 0;
      document.getElementById('security-count').textContent = critCount ? `(${{critCount}})` : '';
      // Critical banner cards
      const critEl = document.getElementById('sec-critical');
      const crits = s.recent_critical || [];
      critEl.innerHTML = crits.length ? crits.map(e => `
        <div class="card" style="border-color:#4a0a0a;margin-bottom:8px">
          <div style="padding:10px 14px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
            <span class="tag tag-fail">critical</span>
            <strong style="font-size:.84em;color:#ea5455">${{escHtml(e.action||'?')}}</strong>
            <span style="font-size:.8em;color:#c8c8dc">${{escHtml(e.reason || e.target || '')}}</span>
            <span style="font-size:.72em;color:#70708e;margin-left:auto">${{fmtTime(e.ts)}}</span>
          </div>
        </div>`).join('') : '';
      // Plain-language panel
      const plain = s.plain_language || [];
      document.getElementById('sec-plain').innerHTML = plain.length
        ? plain.map(l => `<div>• ${{escHtml(l)}}</div>`).join('')
        : '<div class="empty" style="padding:8px">No security activity recorded yet.</div>';
      // Technical events table
      const events = evData.events || [];
      const tbody = document.getElementById('sec-events-body');
      tbody.innerHTML = events.length ? events.map(e => `
        <tr>
          <td style="white-space:nowrap;color:#70708e;font-size:.75em">${{fmtTime(e.ts)}}</td>
          <td><span style="color:${{_SEV_COLORS[e.severity]||'#888'}};font-size:.78em;font-weight:600">${{escHtml(e.severity||'?')}}</span></td>
          <td style="font-size:.78em;color:#b8b8d0;white-space:nowrap">${{escHtml(e.action||'?')}}</td>
          <td style="font-size:.78em;color:#bb99ff">${{escHtml(e.actor||'—')}}</td>
          <td style="font-size:.76em;color:#9a9ab8;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{escHtml(e.target||'')}}">${{escHtml((e.target||'—').slice(0,80))}}</td>
          <td style="font-size:.78em;color:${{e.decision==='fail'||e.decision==='deny'?'#ea5455':'#a8d8c0'}}">${{escHtml(e.decision||'—')}}</td>
          <td>${{e.task_id ? `<button class="btn btn-run btn-sm" style="font-size:.85em;padding:1px 8px" onclick="openTaskDrawer('${{escHtml(e.task_id)}}')">view</button>` : '—'}}</td>
        </tr>`).join('')
        : '<tr><td colspan="7" class="empty">No audit events match.</td></tr>';
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        document.getElementById('sec-plain').innerHTML = `<div class="empty">Failed to load security data: ${{escHtml(e.message)}}</div>`;
    }}
  }}

  // ── Command palette (Cmd+K) ───────────────────────────────────────────────
  let _paletteIdx = 0;
  let _paletteReturnFocus = null;
  let _paletteFiltered = [];

  function _paletteActions() {{
    const acts = [
      {{label:'⌁ Go to Manager Console', hint:'tab', run:() => switchTab('console')}},
      {{label:'↟ Go to Upgrade Loop', hint:'tab', run:() => switchTab('upgrades')}},
      {{label:'🛰 Go to Pipeline', hint:'tab', run:() => switchTab('pipeline')}},
      {{label:'⏳ Go to Approval Queue', hint:'tab', run:() => switchTab('queue')}},
      {{label:'▶ Go to Active', hint:'tab', run:() => switchTab('active')}},
      {{label:'📋 Go to History', hint:'tab', run:() => switchTab('history')}},
      {{label:'＋ Go to Assign Work', hint:'tab', run:() => switchTab('assign')}},
      {{label:'🤖 Go to Agents (Office Floor)', hint:'tab', run:() => switchTab('agents')}},
      {{label:'🚀 Go to Project', hint:'tab', run:() => switchTab('project')}},
      {{label:'📊 Go to Eval Log', hint:'tab', run:() => switchTab('evallog')}},
      {{label:'🧠 Go to Lessons', hint:'tab', run:() => switchTab('lessons')}},
      {{label:'⏰ Go to Routines', hint:'tab', run:() => switchTab('routines')}},
      {{label:'🛡 Go to Security', hint:'tab', run:() => switchTab('security')}},
      {{label:'💸 Go to Tokens / cost usage', hint:'tab', run:() => switchTab('tokens')}},
      {{label:'🔍 Run pipeline health check', hint:'action', run:() => checkPipelineHealth()}},
      {{label:'🪑 Start standup meeting', hint:'action', run:() => {{ switchTab('agents'); setTimeout(startMeeting, 300); }}}},
      {{label:'? Open help', hint:'action', run:() => openHelp()}},
    ];
    Object.keys(AGENT_DESCRIPTIONS).filter(a => a !== 'agent_worker').forEach(a => {{
      acts.push({{label:`✎ Assign work to ${{a}}`, hint:'agent', run:() => quickAssign(a)}});
    }});
    return acts;
  }}

  function openPalette() {{
    _paletteReturnFocus = document.activeElement;
    const pal = document.getElementById('cmd-palette');
    pal.classList.add('open');
    const input = document.getElementById('palette-input');
    input.value = '';
    _paletteIdx = 0;
    _renderPaletteList('');
    input.focus();
  }}

  function closePalette() {{
    document.getElementById('cmd-palette').classList.remove('open');
    if (_paletteReturnFocus && _paletteReturnFocus.focus) _paletteReturnFocus.focus();
  }}

  function _renderPaletteList(q) {{
    const list = document.getElementById('palette-list');
    const acts = _paletteActions().filter(a => a.label.toLowerCase().includes(q.toLowerCase()));
    _paletteFiltered = acts;
    if (_paletteIdx >= acts.length) _paletteIdx = Math.max(0, acts.length - 1);
    list.innerHTML = acts.length ? acts.map((a, i) => `
      <li class="palette-item" role="option" id="pal-opt-${{i}}" aria-selected="${{i===_paletteIdx}}" onclick="_runPaletteIdx(${{i}})">
        <span>${{escHtml(a.label)}}</span><span class="palette-hint">${{a.hint}}</span>
      </li>`).join('') : '<li class="palette-item" aria-disabled="true">No matching commands</li>';
    document.getElementById('palette-input').setAttribute('aria-activedescendant', acts.length ? `pal-opt-${{_paletteIdx}}` : '');
  }}

  function _runPaletteIdx(i) {{
    const act = _paletteFiltered[i];
    closePalette();
    if (act) act.run();
  }}

  function _initPalette() {{
    const input = document.getElementById('palette-input');
    input.addEventListener('input', () => {{ _paletteIdx = 0; _renderPaletteList(input.value); }});
    input.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowDown') {{ e.preventDefault(); _paletteIdx = Math.min(_paletteIdx + 1, _paletteFiltered.length - 1); _renderPaletteList(input.value); }}
      else if (e.key === 'ArrowUp') {{ e.preventDefault(); _paletteIdx = Math.max(_paletteIdx - 1, 0); _renderPaletteList(input.value); }}
      else if (e.key === 'Enter') {{ e.preventDefault(); _runPaletteIdx(_paletteIdx); }}
    }});
  }}

  // ── Help modal focus management ───────────────────────────────────────────
  let _helpReturnFocus = null;
  function openHelp() {{
    _helpReturnFocus = document.activeElement;
    const m = document.getElementById('help-modal');
    m.classList.add('open');
    const btn = m.querySelector('.btn-primary');
    if (btn) btn.focus();
  }}
  function closeHelp() {{
    document.getElementById('help-modal').classList.remove('open');
    if (_helpReturnFocus && _helpReturnFocus.focus) _helpReturnFocus.focus();
  }}

  // ── Global keyboard shortcuts ─────────────────────────────────────────────
  // The currently-open modal dialog (palette > drawer > help), or null. Used to
  // trap Tab focus inside it — an open role="dialog" must not leak focus to the
  // page behind it (WCAG 2.4.3 focus order; ARIA dialog pattern).
  function _openDialog() {{
    for (const id of ['cmd-palette', 'task-drawer', 'help-modal']) {{
      const el = document.getElementById(id);
      if (el && el.classList.contains('open')) return el;
    }}
    return null;
  }}

  function _focusables(container) {{
    return [...container.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter(el => el.offsetParent !== null);  // visible only
  }}

  function _trapTab(e, container) {{
    const items = _focusables(container);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {{
      if (active === first || !container.contains(active)) {{ e.preventDefault(); last.focus(); }}
    }} else {{
      if (active === last || !container.contains(active)) {{ e.preventDefault(); first.focus(); }}
    }}
  }}

  function _initGlobalKeys() {{
    document.addEventListener('keydown', (e) => {{
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {{
        e.preventDefault();
        const pal = document.getElementById('cmd-palette');
        pal.classList.contains('open') ? closePalette() : openPalette();
        return;
      }}
      if (e.key === 'Escape') {{
        if (document.getElementById('cmd-palette').classList.contains('open')) {{ closePalette(); return; }}
        if (document.getElementById('task-drawer').classList.contains('open')) {{ closeTaskDrawer(); return; }}
        if (document.getElementById('help-modal').classList.contains('open')) {{ closeHelp(); return; }}
      }}
      if (e.key === 'Tab') {{
        const dialog = _openDialog();
        if (dialog) {{ _trapTab(e, dialog); return; }}
      }}
      const typing = ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);
      if (e.key === '?' && !typing) {{ e.preventDefault(); openHelp(); }}
    }});
  }}

  const _liveStreams = {{}};  // task_id → EventSource

  function _attachLiveStream(taskId) {{
    if (_liveStreams[taskId]) return;
    const el = document.getElementById('live-out-' + taskId);
    if (!el) return;
    const src = new EventSource(`/tasks/${{taskId}}/stream`);
    _liveStreams[taskId] = src;
    src.onmessage = (e) => {{
      if (e.data === '[DONE]') {{ src.close(); delete _liveStreams[taskId]; return; }}
      try {{
        const d = JSON.parse(e.data);
        if (d.chunk) el.textContent += d.chunk;
        if (d.type === 'done') {{ src.close(); delete _liveStreams[taskId]; }}
      }} catch {{}}
    }};
    src.onerror = () => {{ src.close(); delete _liveStreams[taskId]; }};
  }}

  function renderActive(tasks) {{
    const el = document.getElementById('active-list');
    if (!tasks.length) {{ el.innerHTML = '<div class="empty">No active tasks</div>'; return; }}
    el.innerHTML = tasks.map(t => {{
      const isStreaming = t.status === 'running' || t.status === 'streaming';
      return `
      <div class="card">
        <div class="card-head">
          <div style="display:flex;align-items:center;gap:8px">
            <span class="tag tag-agent">${{t.assigned_agent_id || 'unassigned'}}</span>
            ${{statusTag(t.status)}}
            ${{isStreaming ? '<span style="font-size:.7em;color:#7ec8e3;animation:pulse 1s infinite">● live</span>' : ''}}
          </div>
          <span style="font-size:.75em;color:#555">${{fmtTime(t.started_at || t.created_at)}}</span>
        </div>
        <div class="card-body" style="color:#aaa;font-size:.82em">${{escHtml(t.prompt || '')}}</div>
        <div style="background:#060610;border-top:1px solid #1a1a3a;padding:10px 14px;min-height:48px">
          <div style="font-size:.65em;text-transform:uppercase;letter-spacing:.08em;color:#444;margin-bottom:4px">Live Output</div>
          <pre id="live-out-${{t.id}}" style="white-space:pre-wrap;word-break:break-word;font-size:.78em;color:#7ec8e3;margin:0;max-height:220px;overflow-y:auto">${{escHtml(t.result || '')}}</pre>
        </div>
        <div class="card-foot">
          <button class="btn btn-deny btn-sm" onclick="cancelTask('${{t.id}}')">Stop</button>
          <button class="btn btn-run btn-sm" onclick="openTaskDrawer('${{t.id}}')">☰ Details</button>
          <span style="font-size:.72em;color:#444;margin-left:auto">${{t.id}}</span>
        </div>
      </div>`;
    }}).join('');
    // Attach SSE to any running/streaming tasks
    tasks.filter(t => t.status === 'running' || t.status === 'streaming')
         .forEach(t => setTimeout(() => _attachLiveStream(t.id), 50));
  }}

  function renderHistory(tasks) {{
    const tbody = document.getElementById('history-body');
    const shown = tasks
      .slice()
      .sort((a,b) => new Date(b.finished_at||b.updated_at||0) - new Date(a.finished_at||a.updated_at||0))
      .slice(0, 50);
    if (!shown.length) {{ tbody.innerHTML = '<tr><td colspan="5" class="empty">No completed tasks</td></tr>'; return; }}
    tbody.innerHTML = shown.map(t => {{
      const hasResult = t.result && t.result.trim().length > 0;
      const resultPreview = hasResult ? escHtml(t.result.slice(0, 300)) + (t.result.length > 300 ? '…' : '') : '';
      return `
        <tr style="cursor:${{hasResult ? 'pointer' : 'default'}}" onclick="toggleResult('${{t.id}}')" title="${{hasResult ? 'Click to expand result' : ''}}">
          <td style="font-size:.75em;color:#555;white-space:nowrap">${{t.id.slice(-8)}}</td>
          <td style="white-space:nowrap"><span class="tag tag-agent" style="font-size:.75em">${{t.assigned_agent_id || '—'}}</span></td>
          <td style="word-break:break-word;max-width:0;width:100%">
            <div style="white-space:normal;line-height:1.4">${{escHtml((t.prompt||'').slice(0,200))}}</div>
            ${{resultPreview ? `<div style="font-size:.72em;color:#5a8a5a;margin-top:4px;white-space:normal;line-height:1.4">${{resultPreview}}</div>` : ''}}
          </td>
          <td>${{statusTag(t.status)}}</td>
          <td style="font-size:.75em;color:#777">${{fmtTime(t.finished_at)}}<br>
            <button class="btn btn-run btn-sm" style="font-size:.85em;padding:2px 8px;margin-top:4px" onclick="event.stopPropagation();openTaskDrawer('${{t.id}}')">☰ Details</button></td>
        </tr>
        <tr id="result-${{t.id}}" style="display:none">
          <td colspan="5" style="padding:0">
            <div style="background:#0a0a14;border-top:1px solid #1a1a3a;padding:14px 16px">
              <div style="font-size:.72em;color:#555;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Full Result — ${{t.assigned_agent_id}} · ${{t.id}}</div>
              <pre style="white-space:pre-wrap;word-break:break-word;font-size:.8em;color:#c0d8c0;margin:0;max-height:400px;overflow-y:auto">${{escHtml(t.result || '(no output)')}}</pre>
              ${{t.error ? `<div style="margin-top:8px;font-size:.78em;color:#ea5455">Error: ${{escHtml(t.error)}}</div>` : ''}}
            </div>
          </td>
        </tr>`;
    }}).join('');
  }}

  function toggleResult(id) {{
    const row = document.getElementById('result-' + id);
    if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
  }}

  function titleCase(s) {{
    return s.replace(/_/g,' ').replace(/\b[a-z]/g, c => c.toUpperCase());
  }}

  // Normalize agent id: hyphens → underscores (API uses hyphens, JS uses underscores)
  function normalizeId(id) {{ return (id||'').replace(/-/g,'_'); }}

  // Eval scores stored per agent during live project runs
  const _evalScores = {{}};
  // Agents currently in the eval gym (populated by runProject SSE events)
  const _gymAgents = new Set();

  // AGENT_HOME_ZONES, AGENT_ICONS, renderOfficeFloor — defined in _WORLD_JS (injected below)

  async function loadEvalLog() {{
    const agent = document.getElementById('eval-filter-agent')?.value || '';
    const verdict = document.getElementById('eval-filter-verdict')?.value || '';
    const tbody = document.getElementById('eval-log-body');
    if (!tbody) return;
    try {{
      const params = new URLSearchParams({{limit:50}});
      if (agent) params.set('agent', agent);
      if (verdict) params.set('verdict', verdict);
      const data = await apiFetch('/eval/log?' + params);
      const entries = data.entries || [];
      if (!entries.length) {{
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No eval history yet. Run a project or use Assign Work to generate evals.</td></tr>';
        return;
      }}
      const vcColor = {{pass:'#5a8a5a',fail:'#ea5455',needs_review:'#e8a838',timeout:'#555',unknown:'#555'}};
      tbody.innerHTML = entries.map(e => {{
        const t = e.ts ? new Date(e.ts*1000).toLocaleTimeString([],{{hour:'2-digit',minute:'2-digit'}}) + ' ' + new Date(e.ts*1000).toLocaleDateString([],{{month:'short',day:'numeric'}}) : '—';
        const vc = vcColor[e.verdict]||'#888';
        const issues = (e.issues||[]).slice(0,2).join('; ');
        return `<tr>
          <td style="white-space:nowrap;color:#666;font-size:.75em">${{t}}</td>
          <td><span class="tag tag-agent" style="font-size:.7em">${{escHtml(e.agent||'?')}}</span></td>
          <td class="truncate" style="max-width:200px;font-size:.78em;color:#888" title="${{escHtml(e.task||'')}}">${{escHtml((e.task||'').slice(0,60))}}</td>
          <td style="font-weight:600;color:${{vc}};font-size:.82em">${{e.score??'—'}}/100</td>
          <td><span style="color:${{vc}};font-size:.78em;font-weight:600">${{e.verdict||'?'}}</span></td>
          <td style="font-size:.75em;color:#777" title="${{escHtml(e.feedback||'')}}">${{escHtml((e.feedback||'').slice(0,80))}}${{issues?`<div style="color:#666;margin-top:2px">${{escHtml(issues)}}</div>`:''}}</td>
        </tr>`;
      }}).join('');
    }} catch(e) {{
      if (e.message !== 'unauthorized')
        tbody.innerHTML = `<tr><td colspan="6" class="empty">Error loading eval log: ${{e.message}}</td></tr>`;
    }}
  }}

  async function loadRubrics() {{
    try {{
      const data = await apiFetch('/eval/rubrics');
      const el = document.getElementById('rubric-list');
      if (!el) return;
      const list = data.rubrics || [];
      if (!list.length) {{ el.textContent = 'No rubrics defined yet.'; return; }}
      el.innerHTML = list.map(r => `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="color:#aaa;font-size:.8em"><strong>${{escHtml(r.name)}}</strong> <span style="color:#555">(id: ${{escHtml(r.id)}})</span> — ${{r.criteria?.length||0}} criteria</span>
        <button class="btn btn-deny btn-sm" style="font-size:.72em;padding:2px 8px" onclick="deleteRubric('${{escHtml(r.id)}}')">Remove</button>
      </div>`).join('');
    }} catch(e) {{ }}
  }}

  async function createRubric() {{
    const id = document.getElementById('rubric-id')?.value.trim();
    const name = document.getElementById('rubric-name')?.value.trim();
    const criteriaRaw = document.getElementById('rubric-criteria')?.value.trim();
    const statusEl = document.getElementById('rubric-status');
    if (!id || !name || !criteriaRaw) {{ if(statusEl) statusEl.textContent='Fill in all fields.'; return; }}
    const criteria = criteriaRaw.split('\\n').map(s=>s.trim()).filter(Boolean);
    try {{
      await apiFetch('/eval/rubrics', {{
        method:'POST',
        headers:{{...hdrs(),'Content-Type':'application/json'}},
        body:JSON.stringify({{id,name,criteria}})
      }});
      if(statusEl) statusEl.textContent='Saved!';
      document.getElementById('rubric-id').value='';
      document.getElementById('rubric-name').value='';
      document.getElementById('rubric-criteria').value='';
      loadRubrics();
    }} catch(e) {{ if(statusEl) statusEl.textContent='Error: '+e.message; }}
  }}

  async function deleteRubric(id) {{
    if (!confirm('Delete rubric "'+id+'"?')) return;
    await apiFetch('/eval/rubrics/'+id, {{method:'DELETE'}});
    loadRubrics();
  }}

  async function checkPipelineHealth() {{
    const badge = document.getElementById('pipeline-health-badge');
    badge.textContent = '🔍 Checking...';
    try {{
      const data = await apiFetch('/pipeline/health');
      const alerts = data.alerts || [];
      if (alerts.length === 0) {{
        badge.style.background = '#0d2a1a';
        badge.style.color = '#28c76f';
        badge.style.borderColor = '#1a4a2a';
        badge.textContent = '✅ Pipeline healthy (' + (data.total_tasks||0) + ' tasks)';
      }} else {{
        badge.style.background = '#2a1a0a';
        badge.style.color = '#f0a500';
        badge.style.borderColor = '#4a2a0a';
        badge.textContent = '⚠ ' + alerts.length + ' alert' + (alerts.length>1?'s':'');
        badge.title = alerts.map(a => a.message).join('\\n');
      }}
      setTimeout(() => {{
        badge.style.background='#0d1a2a'; badge.style.color='#7ec8e3';
        badge.style.borderColor='#1a3a5a';
        badge.textContent='🔍 Pipeline Monitor';
      }}, 8000);
    }} catch(e) {{
      badge.textContent = '❌ Monitor error';
    }}
  }}

  async function approveTask(id) {{
    const card = document.querySelector(`[data-task-id="${{id}}"]`);
    await apiFetch(`/tasks/${{id}}/approve`, {{method:'POST'}});
    if (card) {{
      card.querySelector('.card-foot').innerHTML =
        `<span id="appr-status-${{id}}" class="tag tag-run">starting…</span>`;
      const out = document.createElement('pre');
      out.id = `appr-out-${{id}}`;
      out.style.cssText = 'margin:0;padding:12px 16px;white-space:pre-wrap;word-break:break-word;font-size:.78em;color:#7ec8e3;max-height:220px;overflow-y:auto;background:#060610;border-top:1px solid #1a1a3a';
      card.appendChild(out);
      _watchExecutedTask(id, `appr-out-${{id}}`, `appr-status-${{id}}`);
    }} else {{
      refresh();
    }}
  }}

  async function denyTask(id) {{
    await apiFetch(`/tasks/${{id}}/deny`, {{method:'POST'}});
    refresh();
  }}

  async function loadPendingLessons() {{
    const el = document.getElementById('lessons-list');
    const countEl = document.getElementById('lessons-count');
    if (!el) return;
    el.innerHTML = '<div class="empty">Loading…</div>';
    try {{
      // Fetch pending lessons for all verifiable agents
      const verifiableAgents = ['security-reviewer','qa-tester','backend-engineer','researcher','output-quality-checker'];
      const results = await Promise.all(
        verifiableAgents.map(id => apiFetch(`/agents/${{id}}/lessons/pending`).catch(() => ({{ok:true, pending:null}})))
      );
      const pending = results
        .map((r, i) => r.pending ? {{agent_id: verifiableAgents[i], content: r.pending}} : null)
        .filter(Boolean);
      countEl.textContent = pending.length ? `(${{pending.length}})` : '';
      if (!pending.length) {{
        el.innerHTML = '<div class="empty">No pending lessons</div>';
        return;
      }}
      el.innerHTML = pending.map(p => `
        <div class="card" data-agent="${{p.agent_id}}">
          <div class="card-head">
            <span class="tag tag-agent">${{p.agent_id}}</span>
            <span class="tag tag-wait" style="margin-left:4px">pending approval</span>
          </div>
          <div class="card-body"><pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-size:.82em;color:#c084fc">${{escHtml(p.content)}}</pre></div>
          <div class="card-foot">
            <button class="btn btn-approve btn-sm" onclick="approveLesson('${{p.agent_id}}')">✓ Approve</button>
            <button class="btn btn-deny btn-sm" onclick="dismissLesson('${{p.agent_id}}')">✗ Dismiss</button>
          </div>
        </div>`).join('');
    }} catch(e) {{
      el.innerHTML = `<div class="empty">Error loading lessons: ${{e.message}}</div>`;
    }}
  }}

  async function approveLesson(agentId) {{
    await apiFetch(`/agents/${{agentId}}/lessons/approve`, {{method:'POST'}});
    loadPendingLessons();
  }}

  async function dismissLesson(agentId) {{
    await apiFetch(`/agents/${{agentId}}/lessons/dismiss`, {{method:'POST'}});
    loadPendingLessons();
  }}

  // ── Routines ──────────────────────────────────────────────────────────────
  let _editingRoutineId = null;
  let _routinesCache = [];

  function showRoutineForm(routineId) {{
    const r = routineId ? _routinesCache.find(x => x.id === routineId) : null;
    _editingRoutineId = r ? r.id : null;
    document.getElementById('rtn-name').value = r ? r.name : '';
    document.getElementById('rtn-agent').value = r ? (r.agent_id||'') : '';
    document.getElementById('rtn-schedule').value = r ? r.schedule : '';
    document.getElementById('rtn-prompt').value = r ? r.prompt : '';
    document.getElementById('routine-form').style.display = 'block';
  }}

  function hideRoutineForm() {{
    document.getElementById('routine-form').style.display = 'none';
    _editingRoutineId = null;
  }}

  async function saveRoutine() {{
    const name = document.getElementById('rtn-name').value.trim();
    const agent_id = document.getElementById('rtn-agent').value.trim();
    const schedule = document.getElementById('rtn-schedule').value.trim();
    const prompt = document.getElementById('rtn-prompt').value.trim();
    if (!name || !schedule || !prompt) {{ alert('Name, schedule, and prompt are required.'); return; }}
    if (_editingRoutineId) {{
      await apiFetch(`/routines/${{_editingRoutineId}}`, {{
        method: 'PATCH',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{name, agent_id, schedule, prompt}}),
      }});
    }} else {{
      const r = await apiFetch('/routines', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{name, agent_id, schedule, prompt, enabled: true}}),
      }});
      if (r && !r.ok && r.error) {{ alert('Error: ' + r.error); return; }}
    }}
    hideRoutineForm();
    loadRoutines();
  }}

  async function toggleRoutine(id, enabled) {{
    await apiFetch(`/routines/${{id}}`, {{
      method: 'PATCH',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{enabled}}),
    }});
    loadRoutines();
  }}

  async function deleteRoutine(id) {{
    if (!confirm('Delete this routine?')) return;
    await apiFetch(`/routines/${{id}}`, {{method:'DELETE'}});
    loadRoutines();
  }}

  async function loadRoutines() {{
    const el = document.getElementById('routines-list');
    const cntEl = document.getElementById('routines-count');
    const d = await apiFetch('/routines');
    if (!d || !d.routines) {{ el.innerHTML='<div class="empty">Failed to load</div>'; return; }}
    const list = d.routines;
    _routinesCache = list;
    cntEl.textContent = list.length ? `(${{list.length}})` : '';
    if (!list.length) {{ el.innerHTML='<div class="empty">No routines yet — click + New Routine to add one</div>'; return; }}
    el.innerHTML = list.map(r => {{
      const enabledLabel = r.enabled ? '<span class="tag tag-run">enabled</span>' : '<span class="tag" style="background:#333;color:#888">paused</span>';
      const lastFired = r.last_fired_at ? 'Last: ' + r.last_fired_at.slice(0,16).replace('T',' ') : 'never fired';
      return `<div class="card" style="margin-bottom:10px">
        <div class="card-head" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          ${{enabledLabel}}
          <strong style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{escHtml(r.name)}}</strong>
          <span class="tag tag-agent">${{escHtml(r.agent_id || 'manager')}}</span>
          <code style="font-size:.78em;color:#7ec8e3;background:#0d0d1f;padding:2px 8px;border-radius:4px">${{escHtml(r.schedule)}}</code>
        </div>
        <div style="padding:6px 16px 4px;font-size:.8em;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{escHtml(r.prompt.slice(0,120))}}${{r.prompt.length>120?'…':''}}</div>
        <div class="card-foot" style="display:flex;gap:8px;align-items:center">
          <span style="font-size:.75em;color:#555">${{lastFired}}</span>
          <span style="flex:1"></span>
          <button class="btn btn-sm" style="background:#1a2a1a;color:#34d399;border:1px solid #34d399" onclick="showRoutineForm('${{r.id}}')">Edit</button>
          <button class="btn btn-sm ${{r.enabled?'btn-deny':'btn-approve'}}" onclick="toggleRoutine('${{r.id}}', ${{!r.enabled}})">${{r.enabled?'Pause':'Resume'}}</button>
          <button class="btn btn-deny btn-sm" onclick="deleteRoutine('${{r.id}}')">Delete</button>
        </div>
      </div>`;
    }}).join('');
  }}

  async function cancelTask(id) {{
    await apiFetch(`/tasks/${{id}}/cancel`, {{method:'POST'}});
    refresh();
  }}

  async function clearHistory() {{
    if (!confirm('Remove all completed tasks from history?')) return;
    const r = await fetch('/tasks/history', {{method:'DELETE', headers: hdrs()}});
    const d = await r.json();
    document.getElementById('history-body').innerHTML =
      '<tr><td colspan="5" class="empty">Cleared ' + (d.removed||0) + ' tasks</td></tr>';
    setTimeout(refresh, 800);
  }}

  function quickAssign(agentName) {{
    document.getElementById('assign-agent').value = agentName;
    switchTab('assign');
  }}

  async function submitTask() {{
    const agent = document.getElementById('assign-agent').value;
    const prompt = document.getElementById('assign-prompt').value.trim();
    const context = document.getElementById('assign-context').value.trim();
    const kind = document.getElementById('assign-kind').value;
    const statusEl = document.getElementById('assign-status');
    const resultEl = document.getElementById('assign-result');

    if (!prompt) {{ statusEl.textContent = 'Task description is required.'; return; }}

    statusEl.textContent = 'Submitting…';
    resultEl.style.display = 'none';
    resultEl.innerHTML = '';

    if (agent) {{
      // Direct agent run via SSE stream
      const resp = await fetch(`/agents/${{agent}}/run`, {{
        method: 'POST',
        headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ task: prompt, context: context }})
      }});
      if (!resp.ok) {{
        statusEl.textContent = `Error: ${{resp.status}}`;
        return;
      }}
      statusEl.textContent = `Running ${{agent}}…`;
      resultEl.style.display = 'block';
      resultEl.innerHTML = '<span id="agent-stream"></span>';
      const streamEl = document.getElementById('agent-stream');
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {{
        const {{ done, value }} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {{stream: true}});
        const lines = buf.split('\\n');
        buf = lines.pop();
        for (const line of lines) {{
          if (line.startsWith('data: ')) {{
            const data = line.slice(6);
            if (data === '[DONE]') {{ statusEl.textContent = 'Done.'; break; }}
            try {{ streamEl.textContent += (JSON.parse(data).chunk || ''); }} catch {{}}
          }}
        }}
      }}
    }} else {{
      // Submit via task queue
      const data = await apiFetch('/tasks', {{
        method: 'POST',
        headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ prompt, kind, source: 'dashboard' }})
      }});
      statusEl.textContent = data.ok ? `Queued: ${{data.task?.id||'?'}}` : `Error: ${{data.error||'unknown'}}`;
      if (data.ok) {{
        document.getElementById('assign-prompt').value = '';
        document.getElementById('assign-context').value = '';
        setTimeout(() => switchTab('queue'), 800);
      }}
    }}
    refresh();
  }}

  // ---- Plan Mode -------------------------------------------------------

  let _currentPlan = [];

  async function generatePlan() {{
    const goal = document.getElementById('proj-goal').value.trim();
    const statusEl = document.getElementById('proj-status');
    const btn = document.getElementById('proj-plan-btn');
    if (!goal) {{ statusEl.textContent = 'Enter a project goal first.'; return; }}
    btn.disabled = true;
    statusEl.textContent = '⚡ Generating plan…';
    document.getElementById('proj-plan-review').style.display = 'none';
    document.getElementById('proj-execution').style.display = 'none';
    try {{
      const data = await apiFetch('/projects/plan', {{
        method: 'POST',
        headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ goal }}),
      }});
      if (!data.ok) {{ statusEl.textContent = `Error: ${{data.error}}`; return; }}
      _currentPlan = data.plan?.tasks || [];
      renderPlanReview(_currentPlan);
      statusEl.textContent = `Plan ready — ${{_currentPlan.length}} task(s). Review and edit before executing.`;
    }} catch(e) {{
      statusEl.textContent = `Plan error: ${{e.message}}`;
    }} finally {{
      btn.disabled = false;
    }}
  }}

  function renderPlanReview(tasks) {{
    const groupColors = ['#1a3a5a','#1a3a2a','#3a2a1a','#3a1a3a','#1a2a3a'];
    const reviewEl = document.getElementById('proj-plan-review');
    const cards = document.getElementById('proj-plan-cards');
    const groups = [...new Set(tasks.map(t => t.group || 1))].sort();
    cards.innerHTML = groups.map(g => {{
      const groupTasks = tasks.filter(t => (t.group||1) === g);
      return `
        <div style="margin-bottom:8px">
          <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#555;margin-bottom:6px">
            Wave ${{g}} ${{g===1?'— runs immediately':'— runs after wave '+(g-1)}} · ${{groupTasks.length}} agent(s) in parallel
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">
            ${{groupTasks.map((t, i) => {{
              const idx = tasks.indexOf(t);
              return `
              <div style="background:#111120;border:1px solid ${{groupColors[(g-1)%groupColors.length]}};border-radius:8px;padding:10px 14px;display:flex;gap:10px;align-items:flex-start">
                <span class="tag tag-agent" style="white-space:nowrap;flex-shrink:0">${{escHtml(t.agent_id||'?')}}</span>
                <textarea data-plan-idx="${{idx}}" style="flex:1;background:transparent;border:none;color:#ccc;font-size:.8em;resize:vertical;min-height:44px;outline:none;font-family:inherit;line-height:1.5" oninput="_currentPlan[${{idx}}].prompt=this.value">${{escHtml(t.prompt||'')}}</textarea>
              </div>`;
            }}).join('')}}
          </div>
        </div>`;
    }}).join('');
    reviewEl.style.display = 'block';
  }}

  function clearPlan() {{
    _currentPlan = [];
    document.getElementById('proj-plan-review').style.display = 'none';
    document.getElementById('proj-execution').style.display = 'none';
    document.getElementById('proj-status').textContent = '';
  }}

  let _activeProjectId = null;

  async function executePlan() {{
    if (!_currentPlan.length) return;
    const statusEl = document.getElementById('proj-status');
    const execBtn = document.getElementById('proj-execute-btn');
    const goal = document.getElementById('proj-goal').value.trim();
    execBtn.disabled = true;
    statusEl.textContent = 'Dispatching…';
    try {{
      const data = await apiFetch('/projects/execute', {{
        method: 'POST',
        headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ tasks: _currentPlan, goal }}),
      }});
      if (!data.ok) {{ statusEl.textContent = `Error: ${{data.error}}`; execBtn.disabled=false; return; }}
      _activeProjectId = data.project_id;
      statusEl.textContent = `Project ${{_activeProjectId}} running…`;
      const execEl = document.getElementById('proj-execution');
      const agentsOut = document.getElementById('proj-agents-output');
      execEl.style.display = 'block';
      agentsOut.innerHTML = `<div id="proj-waves" style="display:flex;flex-direction:column;gap:16px"></div>
        <div id="proj-synthesis" style="display:none;margin-top:16px">
          <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#f0a;margin-bottom:8px">
            ⚡ Synthesis
          </div>
          <div class="card">
            <div class="card-head">
              <span class="tag tag-agent">researcher</span>
              <span id="synth-status" class="tag tag-run" style="margin-left:8px">waiting</span>
            </div>
            <pre id="synth-out" style="margin:0;padding:14px 16px;white-space:pre-wrap;word-break:break-word;font-size:.82em;color:#e8d8a0;background:#0a0808;max-height:500px;overflow-y:auto;border-top:1px solid #333"></pre>
          </div>
        </div>`;
      _listenProjectStream(_activeProjectId);
    }} catch(e) {{
      statusEl.textContent = `Execute error: ${{e.message}}`;
      execBtn.disabled = false;
    }}
  }}

  function _listenProjectStream(projectId) {{
    const src = new EventSource(`/projects/${{projectId}}/stream`);
    src.onmessage = (e) => {{
      if (e.data === '[DONE]') {{ src.close(); return; }}
      try {{
        const ev = JSON.parse(e.data);
        _handleProjectEvent(ev);
      }} catch {{}}
    }};
    src.onerror = () => src.close();
  }}

  function _handleProjectEvent(ev) {{
    const wavesEl = document.getElementById('proj-waves');
    const statusEl = document.getElementById('proj-status');
    if (!wavesEl) return;

    if (ev.type === 'wave_start') {{
      const g = ev.group;
      let waveDiv = document.getElementById(`proj-wave-${{g}}`);
      if (!waveDiv) {{
        waveDiv = document.createElement('div');
        waveDiv.id = `proj-wave-${{g}}`;
        waveDiv.innerHTML = `
          <div style="font-size:.7em;text-transform:uppercase;letter-spacing:.1em;color:#555;margin-bottom:8px">
            Wave ${{g}} <span id="wave-${{g}}-badge" class="tag tag-run" style="margin-left:6px">running</span>
            · ${{ev.count}} agent(s)
          </div>
          <div id="wave-${{g}}-tasks" style="display:flex;flex-direction:column;gap:6px"></div>`;
        wavesEl.appendChild(waveDiv);
      }}
      statusEl.textContent = `Wave ${{g}} running (${{ev.count}} agents)…`;
    }}

    if (ev.type === 'task_dispatched') {{
      const tasksEl = document.getElementById(`wave-${{ev.group}}-tasks`);
      if (tasksEl && !document.getElementById(`exec-card-${{ev.task_id}}`)) {{
        const card = document.createElement('div');
        card.className = 'card';
        card.id = `exec-card-${{ev.task_id}}`;
        card.innerHTML = `
          <div class="card-head">
            <span class="tag tag-agent">${{escHtml(ev.agent_id||'?')}}</span>
            <span id="exec-status-${{ev.task_id}}" class="tag tag-run" style="margin-left:8px">queued</span>
          </div>
          <pre id="exec-out-${{ev.task_id}}" style="margin:0;padding:10px 14px;white-space:pre-wrap;word-break:break-word;font-size:.78em;color:#7ec8e3;background:#060610;max-height:260px;overflow-y:auto"></pre>`;
        tasksEl.appendChild(card);
        _watchExecutedTask(ev.task_id);
      }}
    }}

    if (ev.type === 'wave_complete') {{
      const badge = document.getElementById(`wave-${{ev.group}}-badge`);
      if (badge) {{ badge.className = 'tag tag-done'; badge.textContent = 'complete'; }}
      statusEl.textContent = `Wave ${{ev.group}} complete.`;
    }}

    if (ev.type === 'synthesis_start') {{
      const synthEl = document.getElementById('proj-synthesis');
      if (synthEl) synthEl.style.display = 'block';
      statusEl.textContent = 'Synthesizing results…';
      _watchExecutedTask(ev.task_id, 'synth-out', 'synth-status');
    }}

    if (ev.type === 'project_complete') {{
      statusEl.textContent = '✓ Project complete — synthesis ready';
      const execBtn = document.getElementById('proj-execute-btn');
      if (execBtn) execBtn.disabled = false;
    }}
  }}

  function _watchExecutedTask(taskId, outId, stId) {{
    const outElId = outId || `exec-out-${{taskId}}`;
    const stElId  = stId  || `exec-status-${{taskId}}`;
    let polls = 0;
    const iv = setInterval(async () => {{
      polls++;
      if (polls > 120) {{ clearInterval(iv); return; }}
      try {{
        const d = await apiFetch(`/tasks/${{taskId}}`);
        const t = d.task || d;
        const stEl = document.getElementById(stElId);
        if (stEl) stEl.textContent = t.status || '?';
        if (t.status === 'running' || t.status === 'streaming') {{
          clearInterval(iv);
          const outEl = document.getElementById(outElId);
          if (!outEl) return;
          const src = new EventSource(`/tasks/${{taskId}}/stream`);
          src.onmessage = (e) => {{
            if (e.data === '[DONE]') {{ src.close(); return; }}
            try {{
              const ev = JSON.parse(e.data);
              if (ev.chunk) {{ outEl.textContent += ev.chunk; outEl.scrollTop = outEl.scrollHeight; }}
              if (ev.type === 'done') {{
                src.close();
                if (stEl) {{ stEl.className = 'tag tag-done'; stEl.textContent = 'done'; }}
              }}
            }} catch {{}}
          }};
          src.onerror = () => src.close();
        }} else if (['succeeded','failed','cancelled'].includes(t.status)) {{
          clearInterval(iv);
          if (stEl) {{ stEl.className = t.status==='succeeded'?'tag tag-done':'tag tag-fail'; stEl.textContent = t.status; }}
          const outEl = document.getElementById(outElId);
          if (outEl && t.result) outEl.textContent = t.result.slice(0, 2000);
        }}
      }} catch {{}}
    }}, 1500);
  }}

  // ---- Slash commands in Assign Work textarea --------------------------

  function _initSlashCommands() {{
    const ta = document.getElementById('assign-prompt');
    if (!ta) return;
    ta.addEventListener('keydown', (e) => {{
      if (e.key !== 'Enter') return;
      const val = ta.value;
      if (!val.startsWith('/')) return;
      e.preventDefault();
      const [cmd, ...rest] = val.slice(1).split(' ');
      const arg = rest.join(' ').trim();
      if (cmd === 'plan' && arg) {{
        document.getElementById('proj-goal').value = arg;
        switchTab('project');
        setTimeout(generatePlan, 100);
        ta.value = '';
      }} else if (cmd === 'project' && arg) {{
        document.getElementById('proj-goal').value = arg;
        switchTab('project');
        ta.value = '';
      }} else if (cmd === 'status') {{
        switchTab('active');
        ta.value = '';
      }} else if (cmd === 'help') {{
        openHelp();
        ta.value = '';
      }}
    }});
  }}

  // ---- Legacy runProject (SSE stream path — kept for Run Without Plan) --------

  async function runProject() {{
    const goal = document.getElementById('proj-goal').value.trim();
    const statusEl = document.getElementById('proj-status');
    const agentsOut = document.getElementById('proj-agents-output');

    if (!goal) {{ statusEl.textContent = 'Enter a project goal first.'; return; }}

    const runBtn = document.getElementById('proj-run-btn');
    if (runBtn) runBtn.disabled = true;
    statusEl.textContent = 'Decomposing…';
    agentsOut.innerHTML = '';
    document.getElementById('proj-execution').style.display = 'block';

    const agentOutputs = {{}};  // task_id → DOM element

    try {{
      const resp = await fetch('/manager/run-stream', {{
        method: 'POST',
        headers: {{ ...hdrs(), 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ goal, cloud_plan: false }}),
      }});
      if (!resp.ok) {{
        statusEl.textContent = `Error ${{resp.status}}`;
        if(runBtn) runBtn.disabled = false;
        return;
      }}

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {{
        const {{ done, value }} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {{stream: true}});
        const lines = buf.split('\\n');
        buf = lines.pop();
        for (const line of lines) {{
          if (!line.startsWith('data: ')) continue;
          let ev;
          try {{ ev = JSON.parse(line.slice(6)); }} catch {{ continue; }}

          if (ev.type === 'plan') {{
            statusEl.textContent = `Plan ready: ${{ev.tasks.length}} task(s)`;

          }} else if (ev.type === 'start') {{
            statusEl.textContent = `Running: ${{ev.agent}} → ${{ev.title||ev.task_id}}`;
            const card = document.createElement('div');
            card.className = 'card';
            card.id = `agent-card-${{ev.task_id}}`;
            card.innerHTML = `
              <div class="card-head">
                <span class="tag tag-agent">${{escHtml(ev.agent)}}</span>
                <span style="font-size:.82em;color:#aaa">${{escHtml(ev.title||'')}}</span>
                <span class="tag tag-run" id="status-${{ev.task_id}}" style="margin-left:auto">running</span>
              </div>
              <pre id="out-${{ev.task_id}}" style="margin:0;padding:12px 16px;white-space:pre-wrap;word-break:break-word;font-size:.8em;color:#c0c0c0;background:#0a0a14;max-height:320px;overflow-y:auto"></pre>`;
            agentsOut.appendChild(card);
            agentOutputs[ev.task_id] = card;

          }} else if (ev.type === 'chunk') {{
            const pre = document.getElementById(`out-${{ev.task_id}}`);
            if (pre) {{
              pre.textContent += ev.text;
              pre.scrollTop = pre.scrollHeight;
            }}

          }} else if (ev.type === 'blocked') {{
            const st = document.getElementById(`status-${{ev.task_id}}`);
            if (st) {{ st.className = 'tag tag-fail'; st.textContent = 'blocked'; }}
            const pre = document.getElementById(`out-${{ev.task_id}}`);
            if (pre) pre.textContent += `\n[Security blocked: ${{ev.reason}}]`;

          }} else if (ev.type === 'eval_start') {{
            const st = document.getElementById(`status-${{ev.task_id}}`);
            if (st) {{ st.className = 'tag'; st.style.background='#2a1a4a'; st.style.color='#a78bfa'; st.textContent = '🏋️ in gym'; }}
            statusEl.textContent = `Evaluating ${{ev.agent}} output...`;
            // Move agent to gym on the office floor
            _gymAgents.add(normalizeId(ev.agent));

          }} else if (ev.type === 'eval') {{
            const verdictColor = {{pass:'#5a8a5a',fail:'#ea5455',needs_review:'#e8a838',timeout:'#555',unknown:'#555'}};
            const vc = verdictColor[ev.verdict] || '#555';
            const card = document.getElementById(`agent-card-${{ev.task_id}}`);
            if (card) {{
              const evalDiv = document.createElement('div');
              evalDiv.style = `padding:8px 16px;background:#0d0d1e;border-top:1px solid #1a1a3a;font-size:.78em`;
              evalDiv.innerHTML = `<span style="color:${{vc}};font-weight:600">Eval: ${{ev.verdict}} ${{ev.score}}/100</span>`
                + (ev.feedback ? ` — <span style="color:#aaa">${{escHtml(ev.feedback)}}</span>` : '')
                + (ev.issues?.length ? `<div style="margin-top:4px;color:#888">${{ev.issues.map(i=>`• ${{escHtml(i)}}`).join('<br>')}}</div>` : '');
              card.appendChild(evalDiv);
            }}
            // Store score for agent office floor
            const agentKey = normalizeId(ev.agent);
            _evalScores[agentKey] = {{ verdict: ev.verdict, score: ev.score }};
            _gymAgents.delete(agentKey);

          }} else if (ev.type === 'done') {{
            const st = document.getElementById(`status-${{ev.task_id}}`);
            const verdict = ev.eval_verdict;
            const doneColor = verdict==='pass'?'tag-done':verdict==='fail'?'tag-fail':'tag-done';
            if (st) {{ st.className = `tag ${{doneColor}}`; st.style=''; st.textContent = verdict ? `done · ${{verdict}}` : 'done'; }}
            _gymAgents.delete(normalizeId(ev.agent));

          }} else if (ev.type === 'error') {{
            statusEl.textContent = `Error: ${{ev.message}}`;

          }} else if (ev.type === 'complete') {{
            statusEl.textContent = 'All agents finished.';
            _gymAgents.clear();
            if(runBtn) runBtn.disabled = false;
            setTimeout(() => refresh(), 1000);
          }}
        }}
      }}
      // Stream closed — re-enable button even if complete event was never sent (server error)
      _gymAgents.clear();
      if(runBtn) runBtn.disabled = false;
    }} catch(e) {{
      statusEl.textContent = `Stream error: ${{e.message}}`;
      _gymAgents.clear();
      if(runBtn) runBtn.disabled = false;
    }}
  }}

  function escHtml(s) {{
    return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }}

  let _initialTab = '';

  function start() {{
    if (_initialTab && document.getElementById('pane-' + _initialTab)) switchTab(_initialTab);
    refresh();
    loadRubrics();
    _initSlashCommands();
    _initTablistKeys();
    _initPalette();
    _initGlobalKeys();
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, 5000);
  }}

  // Accept token passed via URL (#token=… from bridge links, ?token=… from the
  // root redirect), persist it, then scrub it from the address bar.
  (function _tokenFromUrl() {{
    const hashMatch = window.location.hash.match(/token=([^&]+)/);
    const queryMatch = window.location.search.match(/[?&]token=([^&]+)/);
    const urlToken = hashMatch ? hashMatch[1] : (queryMatch ? queryMatch[1] : '');
    const tabMatch = window.location.hash.match(/tab=([a-z]+)/);
    if (tabMatch) _initialTab = tabMatch[1];
    if (urlToken) {{
      token = decodeURIComponent(urlToken);
      localStorage.setItem('jarvis_auth_token', token);
      history.replaceState(null, '', window.location.pathname);
    }}
  }})();

  // Init
  if (token) {{
    document.getElementById('auth-gate').style.display = 'none';
    start();
  }}

  document.getElementById('auth-input').addEventListener('keydown', e => {{ if (e.key==='Enter') doAuth(); }});
  document.addEventListener('click', e => {{
    const panel = document.getElementById('agent-panel');
    if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target.id !== 'office-canvas') {{
      panel.style.display = 'none';
    }}
  }});

{_WORLD_JS}
{_MEETING_JS}
</script>

<!-- Standup Board Overlay -->
<div id="standup-overlay" style="display:none;position:fixed;inset:0;z-index:400;background:rgba(5,8,14,.92);flex-direction:column;overflow:auto">
  <div style="max-width:1200px;margin:0 auto;padding:16px 20px;width:100%">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:10px;border-bottom:1px solid #1a2a3a;padding-bottom:12px">
      <div>
        <div style="font-size:.65em;letter-spacing:.18em;color:#444;margin-bottom:2px">STANDUP BOARD</div>
        <div style="font-size:.8em;color:#555" id="standup-status-line">Gathering in meeting room…</div>
      </div>
      <div style="display:flex;align-items:center;gap:18px;font-size:.75em">
        <span style="color:#555">Speaking: <span id="standup-speaker-name" style="color:#7ec8e3">—</span></span>
        <span style="color:#555">Progress: <span id="standup-progress" style="color:#888">0/14</span></span>
        <button onclick="endMeeting()" style="background:#1a1a1a;border:1px solid #333;color:#666;padding:4px 12px;border-radius:5px;cursor:pointer;font-size:.85em;letter-spacing:.05em">✕ CLOSE</button>
      </div>
    </div>
    <div id="standup-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px"></div>
  </div>
</div>

<!-- Agent Detail Panel -->
<div id="agent-panel" style="display:none;position:fixed;right:20px;top:70px;width:290px;z-index:300;background:#0f1520;border:1px solid #1e2a3a;border-radius:10px;padding:16px;box-shadow:0 8px 40px rgba(0,0,0,.7)">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
    <span id="ap-icon" style="font-size:2em;line-height:1"></span>
    <div style="flex:1;min-width:0">
      <div id="ap-name" style="font-size:1em;font-weight:700;color:#eee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
      <div id="ap-dept" style="font-size:.72em;color:#555"></div>
    </div>
    <button onclick="document.getElementById('agent-panel').style.display='none'" style="background:none;border:none;color:#444;cursor:pointer;font-size:1.1em;padding:0;line-height:1">✕</button>
  </div>
  <div id="ap-status" style="margin-bottom:8px"></div>
  <div style="font-size:.68em;letter-spacing:.08em;color:#444;margin-bottom:4px">CURRENT TASK</div>
  <div id="ap-task" style="font-size:.82em;color:#9ec8f8;background:#0a1422;border-radius:5px;padding:8px;margin-bottom:12px;min-height:32px;line-height:1.4"></div>
  <div id="ap-history" style="margin-bottom:12px;max-height:140px;overflow-y:auto"></div>
  <button id="ap-assign-btn" style="width:100%;padding:7px;background:rgba(42,74,106,.4);border:1px solid #2a5a8a;color:#7ec8e3;border-radius:6px;cursor:pointer;font-size:.82em;letter-spacing:.04em">＋ Assign New Task</button>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


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


# /pending is readable without auth (view-only), but approve/reject require auth
# since they write modified Python files to disk — unauthenticated code writes
# through the tunnel would be a critical vulnerability.
_PUBLIC_PATHS.add("/pending")


@app.get("/self/review")
def get_self_review(area: str = ""):
    result, message = _safe_self_review(area=area or None)
    return {"ok": result.get("ok", False), "message": message, "result": result}


@app.post("/self/review")
def post_self_review(req: SelfReviewRequest):
    result, message = _safe_self_review(area=req.area or None)
    return {"ok": result.get("ok", False), "message": message, "result": result}


@app.get("/osint")
async def osint_dashboard():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OSINT Worldview — Jarvis</title>
<style>
:root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--accent:#6c8cff;--text:#e0e2ec;--sub:#8b8fa8;--danger:#ff6b6b;--warn:#ffa94d;--ok:#5cb85c;--card:#1e2132}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;min-height:100vh;padding:24px}
h1{font-size:1.4rem;font-weight:600;letter-spacing:.02em;color:#fff}
.subtitle{color:var(--sub);font-size:.85rem;margin-top:4px}
header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:24px}
.status-bar{display:flex;gap:12px;flex-wrap:wrap;margin-top:6px}
.status-pill{font-size:.72rem;padding:3px 8px;border-radius:10px;background:var(--surface);border:1px solid var(--border)}
.status-pill.ok{border-color:var(--ok);color:var(--ok)}
.status-pill.missing{border-color:var(--danger);color:var(--danger)}
.search-row{display:flex;gap:10px;margin-bottom:28px}
#query{flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:1rem;outline:none;transition:border .15s}
#query:focus{border-color:var(--accent)}
button#run{background:var(--accent);color:#fff;border:none;padding:10px 22px;border-radius:8px;cursor:pointer;font-size:.95rem;font-weight:500;white-space:nowrap}
button#run:disabled{opacity:.5;cursor:default}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.panel-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.panel-title{font-size:.85rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--sub)}
.badge{font-size:.75rem;background:var(--surface);border:1px solid var(--border);padding:2px 8px;border-radius:8px}
.panel-body{padding:14px;max-height:400px;overflow-y:auto}
.placeholder{color:var(--sub);font-size:.85rem;text-align:center;padding:24px 0}
.loading{text-align:center;padding:24px;color:var(--sub)}
.dot-spin::after{content:"⠋";animation:spin .8s linear infinite}
@keyframes spin{0%{content:"⠋"}12%{content:"⠙"}25%{content:"⠹"}37%{content:"⠸"}50%{content:"⠼"}62%{content:"⠴"}75%{content:"⠦"}87%{content:"⠧"}100%{content:"⠇"}}
.profile-row,.domain-row,.sub-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:.85rem}
.profile-row:last-child,.domain-row:last-child,.sub-row:last-child{border-bottom:none}
.site-name{font-weight:500;min-width:90px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.risk{font-size:.7rem;padding:2px 6px;border-radius:6px;background:var(--surface)}
.risk-3{color:var(--danger)}.risk-2{color:var(--warn)}.risk-1{color:var(--ok)}
.whois-grid{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:.83rem}
.whois-key{color:var(--sub);white-space:nowrap}
.whois-val{word-break:break-word}
.error-msg{color:var(--danger);font-size:.85rem;padding:12px;background:#1a0f0f;border-radius:6px;border:1px solid #400}
.target-type{font-size:.75rem;color:var(--sub);margin-bottom:8px}
.export-btn{font-size:.75rem;background:none;border:1px solid var(--border);color:var(--sub);padding:3px 10px;border-radius:6px;cursor:pointer}
.export-btn:hover{border-color:var(--accent);color:var(--accent)}
</style>
</head>
<body>
<header>
  <div>
    <h1>OSINT Worldview</h1>
    <div class="subtitle">Username &amp; domain intelligence aggregator</div>
    <div class="status-bar" id="toolStatus">Loading tools…</div>
  </div>
  <button class="export-btn" onclick="exportJSON()">Export JSON</button>
</header>
<div class="search-row">
  <input id="query" placeholder="Enter username or domain (e.g. johndoe or example.com)" autocomplete="off" spellcheck="false">
  <button id="run" onclick="runScan()">Scan</button>
</div>
<div id="targetType" class="target-type"></div>
<div class="panels">
  <div class="panel" id="panelProfiles">
    <div class="panel-header"><span class="panel-title">Username Profiles</span><span class="badge" id="badgeProfiles">—</span></div>
    <div class="panel-body" id="bodyProfiles"><div class="placeholder">Submit a username scan to see results</div></div>
  </div>
  <div class="panel" id="panelTypos">
    <div class="panel-header"><span class="panel-title">Typo-squatting Domains</span><span class="badge" id="badgeTypos">—</span></div>
    <div class="panel-body" id="bodyTypos"><div class="placeholder">Submit a domain scan to see results</div></div>
  </div>
  <div class="panel" id="panelSubs">
    <div class="panel-header"><span class="panel-title">Subdomains</span><span class="badge" id="badgeSubs">—</span></div>
    <div class="panel-body" id="bodySubs"><div class="placeholder">Submit a domain scan to see results</div></div>
  </div>
  <div class="panel" id="panelWhois">
    <div class="panel-header"><span class="panel-title">WHOIS</span><span class="badge" id="badgeWhois">—</span></div>
    <div class="panel-body" id="bodyWhois"><div class="placeholder">Submit a domain scan to see results</div></div>
  </div>
</div>
<script>
const API = 'http://127.0.0.1:8765';
let lastResult = null;

async function loadStatus() {
  try {
    const r = await fetch(API + '/osint/status');
    const d = await r.json();
    const bar = document.getElementById('toolStatus');
    bar.innerHTML = Object.entries(d.status || {}).map(([k,v]) =>
      `<span class="status-pill ${v.available?'ok':'missing'}">${k} ${v.available?'✓':'✗'}</span>`
    ).join('');
  } catch(e) { document.getElementById('toolStatus').textContent = 'Cannot reach Jarvis API'; }
}

async function runScan() {
  const q = document.getElementById('query').value.trim();
  if (!q) return;
  const btn = document.getElementById('run');
  btn.disabled = true;
  setLoading();
  try {
    const res = await fetch(API + '/osint/worldview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target:q, timeout_seconds:90, max_results_per_tool:50})
    });
    const d = await res.json();
    lastResult = d;
    renderResults(d);
  } catch(e) {
    showError(String(e));
  } finally { btn.disabled = false; }
}

function setLoading() {
  ['Profiles','Typos','Subs','Whois'].forEach(p => {
    document.getElementById('body'+p).innerHTML = '<div class="loading"><span class="dot-spin"></span> Scanning…</div>';
    document.getElementById('badge'+p).textContent = '…';
  });
  document.getElementById('targetType').textContent = '';
}

function renderResults(d) {
  const tt = d.target_type === 'domain' ? '🌐 Domain target' : '👤 Username target';
  document.getElementById('targetType').textContent = tt + ' — tools run: ' + (d.tools_run||[]).join(', ') + ' — elapsed: ' + d.elapsed_seconds + 's';

  const intel = d.intelligence || {};

  // Profiles
  const pr = intel.profiles;
  if (pr && pr.ok) {
    const profiles = pr.profiles || [];
    document.getElementById('badgeProfiles').textContent = profiles.length;
    document.getElementById('bodyProfiles').innerHTML = profiles.length
      ? profiles.map(p => `<div class="profile-row"><span class="site-name">${esc(p.site)}</span><a href="${esc(p.url)}" target="_blank">↗</a></div>`).join('')
      : '<div class="placeholder">No profiles found</div>';
  } else {
    document.getElementById('badgeProfiles').textContent = d.target_type === 'domain' ? 'n/a' : 'err';
    document.getElementById('bodyProfiles').innerHTML = d.target_type === 'domain'
      ? '<div class="placeholder">Not applicable for domain targets</div>'
      : '<div class="placeholder">No results</div>';
  }

  // Typos
  const ty = intel.typos;
  if (ty && ty.ok) {
    const cands = ty.candidates || [];
    document.getElementById('badgeTypos').textContent = cands.length;
    document.getElementById('bodyTypos').innerHTML = cands.length
      ? cands.map(c => `<div class="domain-row"><span class="site-name">${esc(c.domain)}</span><span class="risk risk-${Math.min(3,c.risk_score)}">${['','low','med','high'][Math.min(3,c.risk_score)]}</span><span style="color:var(--sub);font-size:.75rem">${esc(c.fuzzer)}</span></div>`).join('')
      : '<div class="placeholder">No typo-squatting candidates found</div>';
  } else {
    document.getElementById('badgeTypos').textContent = d.target_type !== 'domain' ? 'n/a' : 'err';
    document.getElementById('bodyTypos').innerHTML = d.target_type !== 'domain'
      ? '<div class="placeholder">Not applicable for username targets</div>'
      : '<div class="placeholder">No results</div>';
  }

  // Subdomains
  const sub = intel.subdomains;
  if (sub && sub.ok) {
    const subs = sub.subdomains || [];
    document.getElementById('badgeSubs').textContent = sub.count ?? subs.length;
    document.getElementById('bodySubs').innerHTML = subs.length
      ? subs.map(s => `<div class="sub-row"><span>${esc(s.host)}</span>${s.ip?`<span style="color:var(--sub);font-size:.75rem">${esc(s.ip)}</span>`:''}</div>`).join('')
      : '<div class="placeholder">No subdomains found</div>';
  } else {
    document.getElementById('badgeSubs').textContent = d.target_type !== 'domain' ? 'n/a' : 'err';
    document.getElementById('bodySubs').innerHTML = d.target_type !== 'domain'
      ? '<div class="placeholder">Not applicable for username targets</div>'
      : '<div class="placeholder">subfinder not available or no results</div>';
  }

  // WHOIS
  const wh = intel.whois;
  if (wh && wh.ok) {
    document.getElementById('badgeWhois').textContent = '✓';
    const rows = [
      ['Registrar', wh.registrar],['Created', wh.created],['Expires', wh.expires],
      ['Registrant', wh.registrant_org],['Status', (wh.status||[]).join(', ')],
      ['Name Servers', (wh.name_servers||[]).join(', ')],
    ].filter(([,v]) => v);
    document.getElementById('bodyWhois').innerHTML = rows.length
      ? `<div class="whois-grid">${rows.map(([k,v])=>`<span class="whois-key">${esc(k)}</span><span class="whois-val">${esc(String(v))}</span>`).join('')}</div>`
      : '<div class="placeholder">No structured WHOIS data extracted</div>';
  } else {
    document.getElementById('badgeWhois').textContent = d.target_type !== 'domain' ? 'n/a' : 'err';
    document.getElementById('bodyWhois').innerHTML = d.target_type !== 'domain'
      ? '<div class="placeholder">Not applicable for username targets</div>'
      : '<div class="placeholder">whois not available or no results</div>';
  }
}

function showError(msg) {
  ['Profiles','Typos','Subs','Whois'].forEach(p => {
    document.getElementById('body'+p).innerHTML = `<div class="error-msg">Error: ${esc(msg)}</div>`;
    document.getElementById('badge'+p).textContent = '!';
  });
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function exportJSON() {
  if (!lastResult) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(lastResult, null, 2)], {type:'application/json'}));
  a.download = 'osint-worldview-' + (lastResult.target||'result') + '.json';
  a.click();
}

document.getElementById('query').addEventListener('keydown', e => { if (e.key==='Enter') runScan(); });

loadStatus();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


class _OsintRateLimiter:
    """Sliding-window per-caller rate limit for heavy OSINT scans."""

    def __init__(self, max_calls: int = 5, window_seconds: int = 60) -> None:
        self._max = max_calls
        self._window = window_seconds
        self._history: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, caller: str) -> None:
        now = time.time()
        with self._lock:
            ts = self._history.get(caller, [])
            cutoff = now - self._window
            ts = [t for t in ts if t > cutoff]
            if len(ts) >= self._max:
                retry_after = int(self._window - (now - ts[0]))
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit: {self._max} OSINT scans per {self._window}s. "
                           f"Retry after {retry_after}s.",
                )
            ts.append(now)
            self._history[caller] = ts


_osint_rl = _OsintRateLimiter(max_calls=5, window_seconds=60)


@app.get("/osint/status")
def osint_status():
    return {"ok": True, "status": osint_tools.status()}


@app.post("/osint/username")
def osint_username(request: Request, req: OsintUsernameRequest):
    caller = request.client.host if request.client else "unknown"
    _osint_rl.check(caller)
    result = osint_tools.username_lookup(
        req.username,
        timeout_seconds=req.timeout_seconds,
        top_sites=req.top_sites,
        max_results=req.max_results,
    )
    return result


@app.post("/osint/domain-typos")
def osint_domain_typos(request: Request, req: OsintDomainTyposRequest):
    caller = request.client.host if request.client else "unknown"
    _osint_rl.check(caller)
    result = osint_tools.domain_typo_scan(
        req.domain,
        timeout_seconds=req.timeout_seconds,
        max_results=req.max_results,
        registered_only=req.registered_only,
    )
    return result


@app.post("/osint/subdomains")
def osint_subdomains(request: Request, req: OsintSubdomainRequest):
    caller = request.client.host if request.client else "unknown"
    _osint_rl.check(caller)
    return osint_tools.subdomain_enum(
        domain=req.domain,
        timeout_seconds=req.timeout_seconds,
        max_results=req.max_results,
        passive_only=req.passive_only,
    )


@app.post("/osint/whois")
def osint_whois(request: Request, req: OsintWhoisRequest):
    caller = request.client.host if request.client else "unknown"
    _osint_rl.check(caller)
    return osint_tools.whois_lookup(
        domain=req.domain,
        timeout_seconds=req.timeout_seconds,
    )


@app.post("/osint/worldview")
def osint_worldview(req: OsintWorldviewRequest):
    import concurrent.futures
    import time as _time
    target = req.target.strip()
    timeout = req.timeout_seconds
    max_r = req.max_results_per_tool
    start = _time.time()

    intelligence: dict = {}
    tools_run: list[str] = []
    tools_failed: list[str] = []

    is_domain = "." in target and _normalize_domain_for_worldview(target)

    def _run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if is_domain:
        sub_timeout = min(timeout, 30)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            if req.include_typos:
                futures["typos"] = pool.submit(_run, osint_tools.domain_typo_scan, target, timeout, max_r, True)
            futures["subdomains"] = pool.submit(_run, osint_tools.subdomain_enum, target, sub_timeout, max_r, True)
            futures["whois"] = pool.submit(_run, osint_tools.whois_lookup, target, min(timeout, 15))

            for name, fut in futures.items():
                res = fut.result()
                if res.get("ok"):
                    intelligence[name] = res
                    tools_run.append(name)
                else:
                    tools_failed.append(name)
    else:
        res = _run(osint_tools.username_lookup, target, timeout, 200, max_r)
        if res.get("ok"):
            intelligence["profiles"] = res
            tools_run.append("username_lookup")
        else:
            tools_failed.append("username_lookup")

    return {
        "ok": True,
        "target": target,
        "target_type": "domain" if is_domain else "username",
        "intelligence": intelligence,
        "tools_run": tools_run,
        "tools_failed": tools_failed,
        "elapsed_seconds": round(_time.time() - start, 2),
    }


def _normalize_domain_for_worldview(value: str) -> str:
    import osint_tools as _ot
    return _ot._normalize_domain(value)


@app.get("/osint/cache")
def osint_cache_list():
    return osint_tools.list_cache()


@app.delete("/osint/cache")
def osint_cache_clear(tool: str = "", target: str = ""):
    return osint_tools.clear_cache(tool=tool or None, target=target or None)


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
        log.debug("[API] voice log tail read failed", exc_info=True)
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


@app.get("/monitor")
def get_monitor(force: bool = False):
    """Unified monitoring report: health + skill usage + integration inventory + agent roster."""
    return _skill_monitor.get_report(force=force)


@app.get("/monitor/stats")
def get_monitor_stats():
    """Per-skill invocation counters, success/failure rates, and latency."""
    return _skill_monitor.get_stats()


@app.get("/monitor/integrations")
def get_monitor_integrations(force: bool = False):
    """Availability check for all 3rd-party integrations and local tools."""
    return _skill_monitor.integration_status(force=force)


@app.get("/monitor/log")
def get_monitor_log(n: int = 50):
    """Recent invocation log (last n entries)."""
    n = max(1, min(200, n))
    return {"entries": _skill_monitor.get_recent_log(n)}


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
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


# ── Project Manager (pm) routes ───────────────────────────────────────────────
# Persistent, agent-assigned, sequentially-executed projects. Lives under /pm/
# to avoid collision with the wave-based /projects/ orchestrator above.

class _PMCreateRequest(BaseModel):
    title: str
    description: str = ""
    agent_id: str = "backend-engineer"
    tasks: list[str | dict] = []
    dispatch: bool = False


def _safe_projects_status() -> dict:
    try:
        import project_manager as _pm
        rows = _pm.collect_status()
        return {"ok": True, "projects": rows}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "projects": []}


@app.post("/pm/projects")
async def pm_create_project(req: _PMCreateRequest, request: Request):
    """Create a project and optionally dispatch it to the assigned agent."""
    from infra.rbac import registry
    registry.caller_from_request(request)
    if not req.title.strip():
        return JSONResponse(status_code=400, content={"ok": False, "error": "title required"})
    if not req.tasks:
        return JSONResponse(status_code=400, content={"ok": False, "error": "tasks required"})
    import project_manager as _pm
    proj = _pm.create_project(
        title=req.title,
        description=req.description,
        agent_id=req.agent_id,
        tasks=req.tasks,
    )
    if req.dispatch:
        try:
            _pm.dispatch_project(proj["id"])
        except Exception as exc:
            proj["dispatch_error"] = str(exc)
    return {"ok": True, "project": proj}


@app.get("/pm/projects")
def pm_list_projects(status: str = "", limit: int = 50, request: Request = None):
    """List all projects with status summary."""
    import project_manager as _pm
    return {"ok": True, "projects": _pm.list_projects(status=status, limit=limit)}


@app.get("/pm/projects/{project_id}")
def pm_get_project(project_id: str, include_events: int = 0, request: Request = None):
    """Return a project with its tasks. Pass include_events=N to include last N events."""
    import project_manager as _pm
    proj = _pm.get_project(project_id, include_events=include_events)
    if proj is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
    return {"ok": True, "project": proj}


@app.get("/pm/projects/{project_id}/results")
def pm_project_results(project_id: str, request: Request = None):
    """Return each task's title, status, and full result text."""
    import project_manager as _pm
    proj = _pm.get_project(project_id)
    if proj is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
    tasks = [
        {
            "seq": t.get("seq"),
            "title": t.get("title"),
            "status": t.get("status"),
            "result": t.get("result") or "",
        }
        for t in proj.get("tasks", [])
    ]
    return {"ok": True, "project_id": project_id, "project_status": proj["status"], "tasks": tasks}


@app.post("/pm/projects/{project_id}/dispatch")
def pm_dispatch_project(project_id: str, request: Request):
    """Start autonomous execution of a project."""
    import project_manager as _pm
    try:
        started = _pm.dispatch_project(project_id)
        return {"ok": True, "started": started}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.post("/pm/projects/{project_id}/cancel")
def pm_cancel_project(project_id: str, request: Request):
    """Cancel a running project."""
    import project_manager as _pm
    try:
        ok = _pm.cancel_project(project_id)
        return {"ok": True, "cancelled": ok}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.post("/pm/projects/{project_id}/retry")
def pm_retry_project(project_id: str, request: Request):
    """Re-queue failed tasks in a failed/cancelled project and re-dispatch."""
    import project_manager as _pm
    try:
        proj = _pm.retry_project(project_id)
        return {"ok": True, "project": proj}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.get("/pm/projects/{project_id}/events")
async def pm_project_events_stream(project_id: str, since_id: int = 0, request: Request = None):
    """SSE stream of project events. Poll every 2 s; sends [DONE] when project reaches terminal state."""
    import asyncio as _asyncio
    import project_manager as _pm

    # Validate project exists.
    if _pm.get_project(project_id) is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})

    _terminal = {"done", "failed", "cancelled"}

    async def _gen():
        current_since = since_id
        while True:
            if request and await request.is_disconnected():
                break
            events = _pm.tail_events(project_id, since_id=current_since)
            for ev in events:
                yield f"data: {json.dumps(dict(ev))}\n\n"
                current_since = max(current_since, ev["id"])
            proj = _pm.get_project(project_id)
            if proj and proj.get("status") in _terminal and not events:
                yield f"data: {json.dumps({'event_type': 'stream_done', 'status': proj['status']})}\n\n"
                break
            await _asyncio.sleep(2)

    return StreamingResponse(_gen(), media_type="text/event-stream")


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
        logging.warning("[API] failed to write runtime endpoint — UI/CLI may not find Jarvis", exc_info=True)

    def _run():
        uvicorn.run(app, host=_host, port=_port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="JarvisAPI")
    t.start()
    if os.getenv("JARVIS_QUIET_BOOT", "").lower() not in {"1", "true", "yes"}:
        print(f"[API] Jarvis API running at http://{_host}:{_port}")

    # Pre-load local models after the API is already serving. Keep this
    # sequential so boot does not try to load text, vision, STT, and TTS at once.
    import model_router as _mr
    if _mr.is_open_source_mode():
        def _warm_local_caches():
            try:
                delay = float(os.getenv("JARVIS_LOCAL_WARMUP_DELAY_SECONDS", "8"))
            except ValueError:
                delay = 8.0
            if delay > 0:
                time.sleep(delay)
            try:
                from brains.brain_ollama import warm_model_cache, warm_vision_cache
                warm_model_cache()
                if os.getenv("JARVIS_WARM_VISION_ON_BOOT", "1").lower() not in {"0", "false", "no", "off"}:
                    time.sleep(3)
                    warm_vision_cache()
            except Exception as exc:
                log.warning("[Ollama] deferred cache warm failed (non-fatal): %s", exc)

        warm_thread = threading.Thread(target=_warm_local_caches, daemon=True, name="OllamaWarm")
        warm_thread.start()

    sched_thread = threading.Thread(target=_routines_scheduler, daemon=True, name="RoutinesScheduler")
    sched_thread.start()

    return t
