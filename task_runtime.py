from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import conversation_context as ctx
import evals
from model_router import smart_stream
import task_persistence
import usage_tracker
import worktree_manager
from infra.security_audit import (
    audit_event, APPROVAL_GRANTED, APPROVAL_DENIED, SECURITY_VERDICT,
)

log = logging.getLogger(__name__)


_LOCK = threading.RLock()
# Per-agent execution locks — agents with different IDs run concurrently; same agent serializes.
_AGENT_LOCKS: dict[str, threading.Lock] = {}
_AGENT_LOCKS_MUTEX = threading.Lock()
# Global concurrency cap — prevents overwhelming a single Ollama instance with too many parallel
# model calls. Cloud providers (OpenAI/Gemini) are not affected by this limit in practice.
def _parse_max_concurrent(raw: str | None) -> int:
    # Clamp to [1, 32]: 0 would wedge the semaphore forever (silent DoS) and
    # garbage must not crash module import.
    try:
        return max(1, min(int(raw), 32))
    except (TypeError, ValueError):
        log.warning("invalid JARVIS_MAX_CONCURRENT_TASKS=%r — using 6", raw)
        return 6


_MODEL_SEMAPHORE = threading.Semaphore(
    _parse_max_concurrent(os.getenv("JARVIS_MAX_CONCURRENT_TASKS", "6"))
)


def _get_agent_lock(agent_id: str) -> threading.Lock:
    with _AGENT_LOCKS_MUTEX:
        if agent_id not in _AGENT_LOCKS:
            _AGENT_LOCKS[agent_id] = threading.Lock()
        return _AGENT_LOCKS[agent_id]

_AGENTS: dict[str, dict[str, Any]] = {}
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_EVENTS: dict[str, list[dict[str, Any]]] = {}
_TASK_THREADS: dict[str, threading.Thread] = {}

_TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
_WAITING_APPROVAL_STATUS = "waiting_approval"
_DEFAULT_CONFIDENCE_THRESHOLD = float(os.getenv("JARVIS_AGENT_AUTO_CONFIDENCE_THRESHOLD", "0.74"))

_DEBRIEF_CONTRACT = (
    "\n\nJarvis managed-agent debrief contract:\n"
    "- Act as the assigned specialist, while Jarvis remains the manager.\n"
    "- If the task changes files, settings, data, or external state, include a Debrief section.\n"
    "- Debrief must name files/actions touched, approval state, verification performed, and rollback or recall notes.\n"
    "- If confidence drops, automation becomes unreliable, or sensitive/destructive work is needed, stop and ask for human review.\n"
    "- Do not claim edits, tests, pushes, submissions, or deployments happened unless they actually happened.\n"
)

_TERSE_PREFIXES = {
    "lite": "CAVEMAN LITE",
    "full": "CAVEMAN FULL",
    "ultra": "CAVEMAN ULTRA",
}

_PERSIST_REDACT_SOURCES_DEFAULT = "webhook,github_webhook"
_PERSIST_REDACTED_PROMPT = "[REDACTED_PROMPT]"
_PERSIST_REDACTED_EFFECTIVE_PROMPT = "[REDACTED_EFFECTIVE_PROMPT]"
_PERSIST_REDACTED_RESULT = "[REDACTED_RESULT]"
_SENSITIVE_META_BLOB_KEYS = {
    "payload",
    "raw_payload",
    "payload_raw",
    "event_payload",
    "body",
    "raw_body",
    "request_body",
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


def _persist_redact_sources() -> set[str]:
    raw = os.getenv("JARVIS_PERSIST_REDACT_SOURCES", _PERSIST_REDACT_SOURCES_DEFAULT)
    return {
        source.strip().lower()
        for source in str(raw).split(",")
        if source and source.strip()
    }


def _strip_meta_payload_blobs(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _SENSITIVE_META_BLOB_KEYS:
                continue
            sanitized[key] = _strip_meta_payload_blobs(item)
        return sanitized
    if isinstance(value, list):
        return [_strip_meta_payload_blobs(item) for item in value]
    return value


def _sanitize_task_for_persistence(task: dict[str, Any]) -> dict[str, Any]:
    persisted = _copy(task)
    source = str(persisted.get("source") or "").strip().lower()
    if source not in _persist_redact_sources():
        return persisted
    persisted["prompt"] = _PERSIST_REDACTED_PROMPT
    persisted["effective_prompt"] = _PERSIST_REDACTED_EFFECTIVE_PROMPT
    persisted["result"] = _PERSIST_REDACTED_RESULT
    meta = persisted.get("meta")
    if isinstance(meta, (dict, list)):
        persisted["meta"] = _strip_meta_payload_blobs(meta)
    return persisted


def _default_agents() -> list[dict[str, Any]]:
    return [
        {
            "id": "jarvis-manager",
            "label": "Jarvis Manager",
            "kind": "manager",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["triage", "delegation", "review", "approval_gates", "debrief"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "manager"},
        },
        {
            "id": "backend-engineer",
            "label": "Backend Engineer",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["python", "api", "runtime", "integration", "tests"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
        },
        {
            "id": "frontend-designer",
            "label": "Frontend Designer",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["ui", "visual_design", "desktop", "responsive_layout"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
        },
        {
            "id": "ux-researcher",
            "label": "UX Researcher",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["user_flows", "interaction_review", "copy", "friction_audit"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
        },
        {
            "id": "security-reviewer",
            "label": "Security Reviewer",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["security", "abuse_paths", "auth", "secrets", "permissions"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "reviewer", "review": "human_first_for_sensitive"},
        },
        {
            "id": "qa-tester",
            "label": "QA Tester",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["tests", "smoke", "regression", "packaged_app"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
        },
        {
            "id": "researcher",
            "label": "Researcher",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["web_research", "repo_research", "source_synthesis"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "cite_sources"},
        },
        {
            "id": "devops-release",
            "label": "DevOps Release",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["git", "github", "builds", "packaging", "release_notes"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "human_first_for_publish"},
        },
        {
            "id": "memory-librarian",
            "label": "Memory Librarian",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["memory", "vault", "obsidian", "retrieval", "summaries"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "write_policy_aware"},
        },
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
            "id": "skill-builder",
            "label": "Skill Builder",
            "kind": "system",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["skills", "proposal", "self_improve"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "skill_factory", "mode": "proposal_first"},
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
        {
            "id": "pipeline-monitor",
            "label": "Pipeline Monitor",
            "kind": "monitor",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["task_health", "stall_detection", "status_tracking", "pipeline_alerts"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "monitor"},
        },
        {
            "id": "output-quality-checker",
            "label": "Output Quality Checker",
            "kind": "monitor",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["output_validation", "code_review", "garbage_detection", "quality_scoring"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "monitor"},
        },
        {
            "id": "automation-engineer",
            "label": "Automation Engineer",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["scripting", "ci_cd", "testing_automation", "workflow_automation"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
        },
        {
            "id": "ai-evaluator",
            "label": "AI Evaluator",
            "kind": "monitor",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["model_evaluation", "output_scoring", "benchmark", "quality_rubric"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "monitor"},
        },
        {
            "id": "ai-safety-agent",
            "label": "AI Safety Agent",
            "kind": "monitor",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["safety_review", "risk_assessment", "policy_enforcement", "guardrails"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "reviewer", "review": "human_first_for_sensitive"},
        },
        {
            "id": "career-agent",
            "label": "Career Agent",
            "kind": "specialist",
            "owner": "jarvis",
            "status": "idle",
            "capabilities": ["career_advice", "skill_gap_analysis", "job_market", "learning_paths"],
            "current_task_id": "",
            "last_heartbeat_at": _now(),
            "last_error": "",
            "meta": {"source": "task_runtime", "mode": "specialist", "review": "confidence_gated"},
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
            if task.get("status") in _TERMINAL_TASK_STATUSES or task.get("status") == _WAITING_APPROVAL_STATUS:
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
    task_persistence.upsert_task(_sanitize_task_for_persistence(task))


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
    if requested_agent_id:
        # Try exact match first, then normalize underscore↔hyphen (AGENT_ROSTER uses _ , _AGENTS uses -)
        if requested_agent_id in _AGENTS:
            return requested_agent_id
        normalized_id = requested_agent_id.replace("_", "-")
        if normalized_id in _AGENTS:
            return normalized_id
    normalized = (kind or "chat").strip().lower()
    if normalized in {"code", "coding", "fix", "implementation", "backend", "api", "runtime"}:
        return "backend-engineer"
    if normalized in {"ui", "frontend", "design", "desktop"}:
        return "frontend-designer"
    if normalized in {"ux", "user", "interaction", "copy"}:
        return "ux-researcher"
    if normalized in {"security", "auth", "permissions", "threat_model", "threat-model"}:
        return "security-reviewer"
    if normalized in {"qa", "test", "tests", "verification", "smoke"}:
        return "qa-tester"
    if normalized in {"research", "repo_research", "web_research"}:
        return "researcher"
    if normalized in {"release", "devops", "github", "build", "packaging"}:
        return "devops-release"
    if normalized == "meeting":
        return "meeting-assist"
    if normalized in {"memory", "obsidian"}:
        return "memory-librarian"
    if normalized in {"knowledge", "vault"}:
        return "knowledge-vault"
    if normalized in {"skill", "skills", "skill_builder", "skill-builder"}:
        return "skill-builder"
    if normalized in {"bridge", "devices"}:
        return "bridge"
    if normalized in {"task", "project", "plan", "manager"}:
        return "jarvis-manager"
    return "chat-router"


def _agent_role_preamble(agent_id: str) -> str:
    labels = {
        "jarvis-manager": "You are Jarvis Manager: triage, delegate, coordinate, review, and keep Aman in control.",
        "backend-engineer": "You are Backend Engineer: make tight Python/API/runtime changes with narrow verification.",
        "frontend-designer": "You are Frontend Designer: improve UI behavior, layout, controls, and visual polish.",
        "ux-researcher": "You are UX Researcher: find interaction friction, clarify workflows, and improve user-facing copy.",
        "security-reviewer": "You are Security Reviewer: inspect trust boundaries, secrets, auth, permissions, and abuse paths.",
        "qa-tester": "You are QA Tester: reproduce, verify, smoke test, and report residual risk.",
        "researcher": "You are Researcher: gather source-backed evidence and separate facts from uncertainty.",
        "devops-release": "You are DevOps Release: handle builds, packaging, git hygiene, GitHub, and release notes.",
        "memory-librarian": "You are Memory Librarian: organize Obsidian/vault/memory work without overwriting canonical notes carelessly.",
        "knowledge-vault": "You are Knowledge Vault: ground responses in stored memory and vault notes.",
        "skill-builder": "You are Skill Builder: propose and validate skills before mutation.",
    }
    return labels.get(agent_id, "You are a Jarvis specialist agent. Stay scoped, auditable, and conservative.")


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
    return _task_prompt_for_kind(prompt, kind="chat", terse_mode=terse_mode)


def _task_prompt_for_kind(prompt: str, *, kind: str = "chat", terse_mode: str = "", agent_id: str = "") -> str:
    normalized_kind = (kind or "chat").strip().lower() or "chat"
    base_prompt = prompt
    if normalized_kind in {"vault", "knowledge", "memory"}:
        lower = prompt.lower()
        if "vault curator" not in lower:
            base_prompt = (
                "Use the vault curator to handle this vault task. "
                "Prefer explicit note refs, heading-level edits, template-backed note creation, "
                "and the agent inbox when the work should stay queued instead of mutating a canonical note immediately. "
                f"{prompt}"
            )
    if normalized_kind in {"skill", "skills", "skill_builder", "skill-builder"}:
        lower = prompt.lower()
        if "skill builder" not in lower:
            base_prompt = (
                "Use the skill builder to handle this skill task. "
                "Draft and validate a proposal by default; do not mutate skills/index.json or create skill files "
                "unless the user has explicitly requested the separate create/promote skill command. "
                f"{prompt}"
            )
    if agent_id:
        base_prompt = f"{_agent_role_preamble(agent_id)}\n\nTask:\n{base_prompt}"
    base_prompt = f"{base_prompt}{_DEBRIEF_CONTRACT}"
    normalized = _normalize_terse_mode(terse_mode)
    if not normalized:
        return base_prompt
    prefix = _TERSE_PREFIXES[normalized]
    return f"{prefix}: {base_prompt}"


def _should_isolate_workspace(kind: str, isolated_workspace: bool | None) -> bool:
    if isolated_workspace is not None:
        return bool(isolated_workspace)
    normalized = (kind or "").strip().lower()
    return normalized in {"code", "coding", "review", "refactor", "fix", "implementation"}


def _approval_reason_for_task(prompt: str, *, kind: str = "", source: str = "") -> str:
    lower = (prompt or "").lower()
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind in {"skill", "skills", "skill_builder", "skill-builder"} and re.search(
        r"\b(create|generate|make|build|promote)\b.*\bskill\b", lower
    ) and not re.search(r"\b(propose|proposal|draft|plan|review)\b", lower):
        return "skill registry mutation"
    markers = (
        ("git push", "repo push"),
        ("push to github", "repo push"),
        ("deploy", "deployment"),
        ("release", "release action"),
        ("merge pull request", "merge action"),
        ("merge pr", "merge action"),
        ("delete ", "destructive file action"),
        ("remove ", "destructive file action"),
        ("rm -", "destructive shell action"),
        ("reset --hard", "destructive git action"),
        ("git reset", "destructive git action"),
        ("git clean", "destructive git action"),
        ("install ", "environment-changing action"),
        ("pip install", "environment-changing action"),
        ("npm install", "environment-changing action"),
        ("brew install", "environment-changing action"),
        ("sudo ", "privileged action"),
        ("/etc", "protected system path"),
        ("/system", "protected system path"),
        ("/library", "protected system path"),
    )
    for marker, reason in markers:
        if marker in lower:
            return reason
    return ""


def _confidence_threshold(meta: dict[str, Any] | None = None) -> float:
    meta = meta or {}
    raw = meta.get("confidence_threshold")
    if raw is None:
        return max(0.0, min(1.0, _DEFAULT_CONFIDENCE_THRESHOLD))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return max(0.0, min(1.0, _DEFAULT_CONFIDENCE_THRESHOLD))


def _estimate_task_confidence(prompt: str, *, kind: str = "", source: str = "", agent_id: str = "", meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = meta or {}
    if meta.get("confidence_score") is not None:
        try:
            score = max(0.0, min(1.0, float(meta["confidence_score"])))
        except (TypeError, ValueError):
            score = 0.55
        return {
            "score": score,
            "threshold": _confidence_threshold(meta),
            "source": "meta",
            "factors": ["caller_supplied_confidence"],
        }

    text = (prompt or "").strip()
    lower = text.lower()
    normalized_kind = (kind or "").strip().lower()
    score = 0.72
    factors: list[str] = ["baseline"]

    clear_action = bool(re.search(r"\b(fix|add|update|write|create|test|verify|review|inspect|research|summarize|list|show|explain|check)\b", lower))
    if clear_action:
        score += 0.08
        factors.append("clear_action")
    if len(text) >= 40:
        score += 0.04
        factors.append("specific_prompt")
    if normalized_kind in {"qa", "test", "tests", "verification", "review", "research", "vault", "knowledge", "memory"}:
        score += 0.05
        factors.append("low_mutation_kind")
    if normalized_kind in {"code", "coding", "fix", "implementation", "backend", "api", "runtime", "ui", "frontend", "design"}:
        score += 0.02
        factors.append("isolated_workspace_kind")

    vague_markers = (
        "whatever",
        "anything",
        "everything",
        "all of it",
        "make it better",
        "fix it all",
        "full take over",
        "do everything",
        "clean up everything",
    )
    if any(marker in lower for marker in vague_markers):
        score -= 0.24
        factors.append("vague_or_broad_scope")

    sensitive_markers = (
        "password",
        "api key",
        "secret",
        "token",
        "credential",
        "private key",
        "oauth",
        "payment",
        "submit application",
        "social security",
    )
    if any(marker in lower for marker in sensitive_markers):
        score -= 0.18
        factors.append("sensitive_data_or_auth")

    external_state_markers = (
        "git push",
        "push to github",
        "deploy",
        "release",
        "merge pull request",
        "submit",
        "apply",
        "send email",
        "delete",
        "remove",
        "install",
        "sudo",
    )
    if any(marker in lower for marker in external_state_markers):
        score -= 0.26
        factors.append("external_or_destructive_state")

    if source in {"webhook", "github_webhook"}:
        score -= 0.08
        factors.append("remote_trigger")

    return {
        "score": round(max(0.0, min(1.0, score)), 2),
        "threshold": _confidence_threshold(meta),
        "source": "heuristic",
        "factors": factors,
    }


def _task_requires_approval(prompt: str, *, kind: str = "", source: str = "", agent_id: str = "", meta: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
    meta = meta or {}
    if meta.get("approval_required") is True:
        confidence = _estimate_task_confidence(prompt, kind=kind, source=source, agent_id=agent_id, meta=meta)
        return True, str(meta.get("approval_reason") or "explicit request"), confidence
    confidence = _estimate_task_confidence(prompt, kind=kind, source=source, agent_id=agent_id, meta=meta)
    # Internally-generated orchestrator tasks (source="project") with an explicit
    # confidence score bypass the content-marker scan — the prompt contains prior
    # agent outputs which may include destructive-sounding words (install, remove)
    # that are analysis findings, not commands being issued.
    skip_content_scan = (source == "project" and meta.get("confidence_score") is not None)
    if not skip_content_scan:
        reason = _approval_reason_for_task(prompt, kind=kind, source=source)
        if reason:
            return True, reason, confidence
    if confidence["score"] < confidence["threshold"]:
        return True, "confidence below safe autonomy threshold", confidence
    return False, "", confidence


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


def _start_task_thread(task_id: str) -> None:
    thread = threading.Thread(target=_run_task, args=(task_id,), daemon=True, name=f"JarvisTask-{task_id}")
    _TASK_THREADS[task_id] = thread
    thread.start()


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


# ── Agent tool context injection ──────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.resolve()
_SAFE_READ_EXTENSIONS = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".txt"}

# ── Phase 3: verification loop + self-improvement ─────────────────────────────

_LESSONS_DIR = Path.home() / ".jarvis" / "agent_lessons"
_VERIFIABLE_AGENTS = {
    "security-reviewer", "qa-tester", "backend-engineer",
    "researcher", "output-quality-checker", "automation-engineer",
}
_AUTO_VERIFY = os.getenv("JARVIS_AUTO_VERIFY", "1").strip() == "1"


def _load_agent_lesson(agent_id: str) -> str:
    """Read active (approved) lesson for this agent, if any."""
    if not re.match(r'^[a-z0-9-]+$', agent_id):
        return ""
    path = _LESSONS_DIR / f"{agent_id}.md"
    if not path.exists():
        return ""
    try:
        full = path.read_text(encoding="utf-8").strip()
        if full.startswith("# PENDING_APPROVAL"):
            return ""  # not yet approved
        # Lessons accumulate over time — when over budget, keep the newest
        # (tail), not the oldest.
        content = full[-4000:]
        return f"\n\n=== Learned behaviors for {agent_id} ===\n{content}"
    except Exception:
        return ""


# First-person past-tense execution claims. Deliberately narrow: "you can run X"
# or "next, run pytest" must NOT match — only claims that execution already happened.
_EXEC_CLAIM_PATTERNS = re.compile(
    r"(?:\bthe command\s+`?[^`\n]{1,80}`?\s+(?:returned|showed|output|reported|confirmed|produced|gave))"
    r"|(?:\bI\s+(?:ran|executed|invoked)\b)"
    r"|(?:\b(?:running|executing)\s+`[^`\n]{1,80}`\s+(?:returned|produced|gave|shows|showed|confirms|confirmed))"
    r"|(?:\bcommand output (?:indicates|shows|showed|confirms|confirmed|reveals|revealed))"
    r"|(?:\bafter running\s+`?[^`\n]{1,80}`?,)"
    r"|(?:\bthe output of\s+`[^`\n]{1,80}`\s+(?:is|was|shows|showed))",
    re.IGNORECASE,
)


def _detect_fabricated_execution(response: str, tool_calls: int) -> str:
    """
    Deterministic fabrication check: the response claims a command was executed,
    but the runtime made zero tool calls for this task. Returns the matched
    claim text, or "" if clean.
    """
    if tool_calls > 0:
        return ""
    m = _EXEC_CLAIM_PATTERNS.search(response)
    return m.group(0).strip() if m else ""


def _auto_verify(task_id: str, agent_id: str, prompt: str, result: str,
                 tool_calls: int = -1, tool_transcript: str = "",
                 inherited_transcript: str = "") -> dict:
    """
    Score a completed task output using a lightweight LLM call.
    Returns {"pass": bool, "score": float, "reason": str}.
    Fails open (pass=True) on any error so verification never blocks valid tasks.
    """
    import json as _json
    try:
        from brains.brain import ask_stream
        from config import GPT_MINI
        tool_fact = ""
        if tool_calls == 0 and inherited_transcript:
            # Retry attempts inherit the prior attempt's runtime-captured
            # outputs — citing those is grounded, not fabricated.
            tool_fact = (
                "\nRUNTIME FACT (ground truth, not from the agent): the agent executed "
                "ZERO new tool calls in this attempt, but the runtime carried over REAL "
                "tool outputs from the previous attempt (shown below). Claims consistent "
                "with those outputs are grounded and must NOT be treated as fabricated. "
                "Only claims going beyond them are unsupported.\n"
            )
        elif tool_calls == 0:
            tool_fact = (
                "\nRUNTIME FACT (ground truth, not from the agent): the agent executed "
                "ZERO tool calls during this task. If the response cites command output, "
                "file listings, or test results as if it ran them, those claims are "
                "FABRICATED — set the overall score to 0.0 regardless of the other "
                "criteria and say so in the reason.\n"
            )
        elif tool_calls > 0:
            tool_fact = (
                f"\nRUNTIME FACT (ground truth, not from the agent): the agent executed "
                f"{tool_calls} real tool call(s); their outputs are shown below, captured "
                "by the runtime. The response may summarize them without quoting raw "
                "output — do NOT penalize that. Instead check that its claims are "
                "CONSISTENT with these outputs.\n"
            )
        transcript_block = ""
        if tool_transcript and tool_calls > 0:
            transcript_block = (
                "\nRUNTIME-CAPTURED TOOL OUTPUTS (ground truth):\n"
                f"{tool_transcript[-3000:]}\n"
            )
        elif inherited_transcript and tool_calls == 0:
            transcript_block = (
                "\nRUNTIME-CAPTURED TOOL OUTPUTS (from the previous attempt, ground truth):\n"
                f"{inherited_transcript[-3000:]}\n"
            )
        verify_prompt = (
            f"You are an output quality evaluator. Score this agent response.\n\n"
            f"Agent: {agent_id}\n"
            f"Task: {prompt[:500]}\n"
            f"{tool_fact}{transcript_block}\n"
            f"Response (first 1500 chars):\n{result[:1500]}\n\n"
            "Score on three criteria (0-1 each):\n"
            "1. Does it directly address the task?\n"
            "2. Are claims grounded in evidence vs. speculation?\n"
            "3. Is it complete FOR WHAT THE TASK ASKED? A narrow task deserves a "
            "short answer — a correct, direct answer to a simple question scores 1. "
            "Do NOT demand extra insights, recommendations, or detail the task did "
            "not request.\n\n"
            "A response fails ONLY for: wrong or fabricated content, not addressing "
            "the task, or claims unsupported by the evidence. Stylistic preferences, "
            "phrasing choices, or extra caution are NOT failures — at most a minor "
            "score reduction.\n\n"
            "Return ONLY valid JSON (no prose, no markdown):\n"
            '{"score": 0.0, "reason": "one sentence"}\n'
            "Score = average of the three criteria."
        )
        raw = "".join(ask_stream(verify_prompt, model=GPT_MINI, track_context=False,
                                 bypass_local=True, bare_system=True))
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"pass": True, "score": 1.0, "reason": "parse error — failing open"}
        verdict = _json.loads(m.group(0))
        score = float(verdict.get("score", 1.0))
        # Pass is computed from the score, never taken from the model — mini
        # models set pass=false over stylistic nitpicks while scoring high.
        return {
            "pass": score >= 0.65,
            "score": score,
            "reason": str(verdict.get("reason", "")),
        }
    except Exception as exc:
        log.warning("auto_verify error for %s: %s", task_id, exc)
        return {"pass": True, "score": 1.0, "reason": f"verify error: {exc}"}


_VERDICTS_PATH = Path.home() / ".jarvis" / "verifier_verdicts.jsonl"


def _record_verdict(task_id: str, agent_id: str, verdict: dict,
                    tool_calls: int, retry_count: int,
                    result: str = "", tool_transcript: str = "",
                    inherited_transcript: str = "",
                    fabricated_claim: str = "") -> None:
    """Append the verifier verdict to a JSONL log so calibration is measurable
    (pass rate per agent, score distribution, retry effectiveness). Provenance
    hashes let the independent pipeline auditor detect post-hoc tampering."""
    import hashlib as _hashlib
    import json as _json
    try:
        _VERDICTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "agent_id": agent_id,
            "score": verdict.get("score"),
            "pass": verdict.get("pass"),
            "reason": verdict.get("reason", ""),
            "tool_calls": tool_calls,
            "retry_count": retry_count,
            "fabrication_flag": fabricated_claim,
            "had_inherited_evidence": bool(inherited_transcript and tool_calls == 0),
            "result_sha256": _hashlib.sha256(result.encode("utf-8")).hexdigest() if result else "",
            "result_len": len(result or ""),
            "transcript_sha256": _hashlib.sha256(tool_transcript.encode("utf-8")).hexdigest() if tool_transcript else "",
            "transcript_len": len(tool_transcript or ""),
        }
        with open(_VERDICTS_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        log.exception("failed to record verifier verdict for %s", task_id)
    audit_event(
        SECURITY_VERDICT, actor=agent_id, target=task_id,
        decision="pass" if verdict.get("pass") else "fail", task_id=task_id,
    )


def _propose_skill_update(agent_id: str, prompt: str, result: str, reason: str) -> None:
    """
    Propose a lesson for this agent: LLM generates a behavior rule,
    written as a pending file for human approval.
    """
    if not re.match(r'^[a-z0-9-]+$', agent_id):
        log.warning("propose_skill_update: rejected invalid agent_id %r", agent_id)
        return
    try:
        from model_router import smart_stream
        _LESSONS_DIR.mkdir(parents=True, exist_ok=True)
        lesson_prompt = (
            f"An agent named '{agent_id}' failed to produce a good response.\n\n"
            f"Task: {prompt[:400]}\n\n"
            f"Response: {result[:800]}\n\n"
            f"Failure reason: {reason}\n\n"
            "Write ONE specific behavior rule (2-4 sentences) this agent should follow "
            "next time it sees a similar task. Be concrete and actionable. "
            "No markdown headers, no preamble — just the rule text."
        )
        stream, _ = smart_stream(lesson_prompt, tool="chat", prefer_local=True, skip_dynamic_context=True)
        rule = "".join(chunk for chunk in stream).strip()
        if not rule:
            return
        ts = datetime.now(timezone.utc).isoformat()
        content = (
            f"# PENDING_APPROVAL\n"
            f"# Agent: {agent_id}\n"
            f"# Proposed: {ts}\n"
            f"# Failure reason: {reason}\n\n"
            f"{rule}\n"
        )
        path = _LESSONS_DIR / f"{agent_id}.pending.md"
        path.write_text(content, encoding="utf-8")
        log.info("skill proposal written for %s: %s", agent_id, path)
    except Exception as exc:
        log.warning("propose_skill_update error for %s: %s", agent_id, exc)

_BASH_ALLOWED_CMDS = {
    # test/lint runners — scoped to project, don't allow arbitrary -c eval
    "pytest", "ruff", "mypy", "flake8",
    # read-only filesystem inspection
    "grep", "find", "ls", "cat", "head", "tail", "wc",
    # git — argument-filtered below to read-only subcommands
    "git",
    # safe output
    "echo",
}
# git subcommands agents may use; all others are denied
_GIT_ALLOWED_SUBCOMMANDS = {"log", "diff", "status", "show", "ls-files", "shortlog", "blame"}
_BASH_MAX_OUTPUT = 4096
_BASH_TIMEOUT = 30


_BASH_MAX_PIPE_STAGES = 4

# macOS Seatbelt sandbox — kernel-enforced confinement for agent-executed
# commands, inherited by child processes (so pytest's subprocesses stay
# confined too). Deprecated CLI but still the only stable userland entry
# point to Seatbelt; Apple's own tooling relies on the same facility.
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _sandbox_available() -> bool:
    import sys
    if os.getenv("JARVIS_DISABLE_SANDBOX") == "1":
        return False
    return sys.platform == "darwin" and os.path.exists(_SANDBOX_EXEC)


def _sandbox_profile(write_roots: "list[Path]") -> str:
    """Seatbelt profile: reads allowed, network denied, writes denied except
    `write_roots` plus tmp dirs and /dev (TMPDIR resolves under
    /private/var/folders on macOS)."""
    allows = [
        '(subpath "/private/tmp")',
        '(subpath "/private/var/folders")',
        '(subpath "/dev")',
    ]
    for r in write_roots:
        p = str(Path(r).resolve()).replace("\\", "\\\\").replace('"', '\\"')
        allows.append(f'(subpath "{p}")')
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        f"(allow file-write* {' '.join(allows)})\n"
    )


def _sandbox_wrap(argv: "list[str]", write_roots: "list[Path]") -> "list[str]":
    if not _sandbox_available():
        return argv
    return [_SANDBOX_EXEC, "-p", _sandbox_profile(write_roots)] + argv


def _validate_bash_segment(parts: list[str], root: "Path | None" = None) -> "list[str] | str":
    """Validate one pipeline segment against `root` (defaults to repo root).
    Returns the (possibly glob-expanded) argv list, or an error string
    starting with '[bash'."""
    root = root or _REPO_ROOT
    if not parts:
        return "[bash error: empty command segment]"
    exe = Path(parts[0]).name  # strip any path prefix
    if exe not in _BASH_ALLOWED_CMDS:
        return f"[bash denied: '{exe}' is not in the allowed command list]"
    # Rebuild with just the basename to prevent executable path traversal
    parts = [exe] + parts[1:]
    # git: restrict to read-only subcommands
    if exe == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub not in _GIT_ALLOWED_SUBCOMMANDS:
            return f"[bash denied: 'git {sub}' is not in the allowed git subcommand list]"
    # Block path traversal in file arguments for filesystem commands.
    # shell=False does no glob expansion, so expand globs here (relative to repo
    # root) — expanded paths then go through the same traversal check.
    _PATH_CMDS = {"ls", "cat", "head", "tail", "grep", "find", "wc"}
    if exe in _PATH_CMDS and root is not None:
        import glob as _glob
        expanded: list[str] = [parts[0]]
        for arg in parts[1:]:
            if arg.startswith("-"):
                expanded.append(arg)
                continue  # skip flags
            if any(c in arg for c in ("*", "?", "[")):
                # find uses glob args as -name patterns — pass through unexpanded
                if exe == "find":
                    expanded.append(arg)
                    continue
                matches = sorted(_glob.glob(str(root / arg)))
                matches = [m for m in matches
                           if str(Path(m).resolve()).startswith(str(root))]
                if not matches:
                    return f"[bash error: glob '{arg}' matched no files in repo root]"
                # Relative paths — shorter output, and never silently cap below
                # the real match count (agents draw wrong conclusions otherwise)
                expanded.extend(
                    str(Path(m).relative_to(root)) for m in matches[:500]
                )
                continue
            try:
                resolved = (Path(arg) if Path(arg).is_absolute() else root / arg).resolve()
                if not str(resolved).startswith(str(root)):
                    return f"[bash denied: path '{arg}' resolves outside repo root]"
            except Exception:
                pass  # malformed paths will fail naturally at runtime
            expanded.append(arg)
        parts = expanded
    return parts


def _tool_bash(command_str: str, root: "Path | None" = None) -> str:
    """Execute a bash command string with safety guardrails. Returns stdout+stderr truncated.

    Supports pipelines (cmd1 | cmd2) between allowlisted commands — each stage
    is validated independently and chained with shell=False. Shell redirection
    is rejected with an explanatory error so agents self-correct.
    `root` scopes path validation and cwd (defaults to repo root; tasks with an
    isolated workspace pass their worktree)."""
    import shlex
    import subprocess
    root = root or _REPO_ROOT
    try:
        tokens = shlex.split(command_str.strip())
    except ValueError as e:
        return f"[bash error: could not parse command: {e}]"
    if not tokens:
        return "[bash error: empty command]"
    for tok in tokens:
        if tok != "|" and any(c in tok for c in (">", "<", ";", "&", "`", "$(")):
            return (f"[bash denied: shell operator in '{tok}' is not supported. "
                    "Pipes between allowed commands work (e.g. ls *.py | wc -l); "
                    "redirection, chaining and substitution do not]")
    # Split into pipeline segments on '|' tokens
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            segments.append([])
        else:
            segments[-1].append(tok)
    if len(segments) > _BASH_MAX_PIPE_STAGES:
        return f"[bash denied: pipelines are limited to {_BASH_MAX_PIPE_STAGES} stages]"
    # Isolated worktrees are writable inside the sandbox; the main repo never is —
    # repo-root tasks get read-only enforcement at the kernel level.
    write_roots = [root] if root != _REPO_ROOT else []
    validated: list[list[str]] = []
    for seg in segments:
        v = _validate_bash_segment(seg, root)
        if isinstance(v, str):
            return v
        # Bare `pytest` does not put cwd on sys.path; `python -m pytest` does —
        # without this, tests in a worktree can't import top-level modules.
        if v and v[0] in ("pytest", "mypy", "flake8"):
            import sys as _sys
            v = [_sys.executable, "-m", v[0]] + v[1:]
        validated.append(_sandbox_wrap(v, write_roots))
    try:
        if len(validated) == 1:
            result = subprocess.run(
                validated[0],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=_BASH_TIMEOUT,
                shell=False,
            )
            out = (result.stdout + result.stderr).strip()
        else:
            procs: list[subprocess.Popen] = []
            prev_stdout = None
            for seg in validated:
                p = subprocess.Popen(
                    seg,
                    cwd=str(root),
                    stdin=prev_stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                )
                if prev_stdout is not None:
                    prev_stdout.close()  # let upstream see SIGPIPE
                prev_stdout = p.stdout
                procs.append(p)
            try:
                stdout, last_err = procs[-1].communicate(timeout=_BASH_TIMEOUT)
            except subprocess.TimeoutExpired:
                for p in procs:
                    p.kill()
                return f"[bash timeout: command exceeded {_BASH_TIMEOUT}s]"
            errs = []
            for p in procs[:-1]:
                p.wait(timeout=5)
                if p.stderr is not None:
                    e = p.stderr.read()
                    p.stderr.close()
                    if e.strip():
                        errs.append(e.strip())
            if last_err and last_err.strip():
                errs.append(last_err.strip())
            out = (stdout + ("\n" + "\n".join(errs) if errs else "")).strip()
        if len(out) > _BASH_MAX_OUTPUT:
            out = out[:_BASH_MAX_OUTPUT] + f"\n[truncated — {len(out)} chars total]"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[bash timeout: command exceeded {_BASH_TIMEOUT}s]"
    except Exception as e:
        return f"[bash error: {e}]"


def _execute_bash_tool_calls(result_text: str) -> str:
    """Find <bash>...</bash> tags in result, execute each, append outputs."""
    pattern = re.compile(r'<bash>(.*?)</bash>', re.DOTALL)
    calls = pattern.findall(result_text)
    if not calls:
        return result_text
    outputs = []
    for cmd in calls[:5]:  # max 5 tool calls per response
        cmd = cmd.strip()
        out = _tool_bash(cmd)
        outputs.append(f"$ {cmd}\n{out}")
    suffix = "\n\n--- Tool Outputs ---\n" + "\n\n".join(outputs)
    return result_text + suffix


def _extract_bash_calls(text: str) -> list[str]:
    """Return list of bash command strings from <bash>...</bash> tags in text."""
    return [m.strip() for m in re.findall(r'<bash>(.*?)</bash>', text, re.DOTALL)]


_WRITE_MAX_BYTES = 262144
_WRITE_TAG_RE = re.compile(r'<write_file path="([^"]+)">\n?(.*?)</write_file>', re.DOTALL)


def _extract_write_calls(text: str) -> list[tuple[str, str]]:
    """Return (path, content) pairs from <write_file path="...">...</write_file> tags."""
    return [(m.group(1), m.group(2)) for m in _WRITE_TAG_RE.finditer(text)]


def _tool_write_file(rel_path: str, content: str, root: Path) -> str:
    """Write a file inside an isolated workspace. Only ever called with a
    per-task git worktree as root — never the live repo."""
    p = rel_path.strip()
    if not p or Path(p).is_absolute():
        return f"[write denied: path must be relative to the workspace root, got '{p}']"
    parts = Path(p).parts
    if ".." in parts or ".git" in parts:
        return f"[write denied: path '{p}' may not contain '..' or '.git']"
    try:
        target = (root / p).resolve()
    except Exception as e:
        return f"[write error: bad path '{p}': {e}]"
    if not str(target).startswith(str(root)):
        return f"[write denied: path '{p}' resolves outside the workspace]"
    if len(content.encode("utf-8", errors="replace")) > _WRITE_MAX_BYTES:
        return f"[write denied: content exceeds {_WRITE_MAX_BYTES} bytes]"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[wrote {len(content)} chars to {p}]"
    except Exception as e:
        return f"[write error: {e}]"


def _workspace_diff(root: Path) -> str:
    """Stage everything in the worktree and return the cached diff (truncated).
    The branch is never merged by the runtime — this diff is the review surface."""
    import subprocess
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True,
                       text=True, timeout=30, shell=False)
        r = subprocess.run(["git", "diff", "--cached"], cwd=str(root),
                           capture_output=True, text=True, timeout=30, shell=False)
        diff = r.stdout.strip()
        if len(diff) > 6000:
            diff = diff[:6000] + f"\n[diff truncated — {len(diff)} chars total]"
        return diff
    except Exception as e:
        log.warning("workspace diff failed for %s: %s", root, e)
        return ""


_TASK_RESULT_MAX = 8000


def _tool_task_result(task_id: str) -> str:
    """Return the stored result of a completed task. On-demand context retrieval."""
    tid = task_id.strip()
    if not re.match(r'^task_[a-f0-9]{6,32}$', tid):
        return "[task_result denied: invalid task id format]"
    with _LOCK:
        t = _TASKS.get(tid)
        if t is None:
            return f"[task_result error: task {tid} not found]"
        status = t.get("status", "")
        result = (t.get("result") or "").strip()
    if status not in _TERMINAL_TASK_STATUSES:
        return f"[task_result: task {tid} is still '{status}' — no result yet]"
    if not result:
        return f"[task_result: task {tid} finished with empty result]"
    if len(result) > _TASK_RESULT_MAX:
        result = result[:_TASK_RESULT_MAX] + f"\n[truncated — {len(result)} chars total]"
    return result


def _extract_task_result_calls(text: str) -> list[str]:
    """Return list of task ids from <task_result>...</task_result> tags in text."""
    return [m.strip() for m in re.findall(r'<task_result>(.*?)</task_result>', text, re.DOTALL)]


def _run_tool_loop(
    task_id: str,
    original_prompt: str,
    agent_ctx: str,
    initial_response: str,
    initial_model: str,
    start_index: int,
    work_root: "Path | None" = None,
    allow_write: bool = False,
    prefer_local: bool = True,
) -> tuple[str, str, int, int, str]:
    """
    Multi-turn tool-call loop.

    If the LLM response contains <bash>...</bash> tags, execute them, then re-call
    the LLM with full history so it can reason about the results. Repeats up to
    4 more times (5 total iterations including the initial call in _run_task).

    Returns (final_response_text, model_used, last_chunk_index, tool_calls_made,
    tool_transcript). tool_transcript is the runtime-captured tool output history —
    ground truth for the verifier, since the final response may summarize rather
    than quote it. The returned text is ONLY the last continuation response —
    callers accumulate the full transcript themselves via SSE events already emitted.
    """
    response = initial_response
    model = initial_model
    index = start_index
    tool_calls_made = 0
    conversation: list[tuple[str, str]] = []  # (assistant_text, tool_results_text)

    def _transcript() -> str:
        return "\n\n".join(tools for _, tools in conversation)

    def _history_blocks() -> list[str]:
        """Compact prior steps so the continuation prompt does not grow
        quadratically: the latest step stays rich, older steps are truncated
        hard, and the total history is budget-capped (oldest elided first)."""
        blocks: list[str] = []
        last = len(conversation) - 1
        for i, (prev_resp, prev_tools) in enumerate(conversation):
            resp_cap, tools_cap = (2000, 4000) if i == last else (400, 800)
            resp = prev_resp if len(prev_resp) <= resp_cap else (
                prev_resp[:resp_cap] + "\n[...truncated]")
            # Tool output tails carry the verdict (test summaries, error lines)
            tools = prev_tools if len(prev_tools) <= tools_cap else (
                "[...truncated]\n" + prev_tools[-tools_cap:])
            blocks.append(
                f"[Step {i + 1} — your response]:\n{resp}\n\n"
                f"[Step {i + 1} — tool results]:\n{tools}"
            )
        elided = 0
        while len(blocks) > 1 and sum(len(b) for b in blocks) > 12000:
            blocks.pop(0)
            elided += 1
        if elided:
            blocks[0] = f"[{elided} earlier step(s) elided]\n\n" + blocks[0]
        return blocks

    for _iter in range(4):  # up to 4 continuation rounds after the initial call
        bash_calls = _extract_bash_calls(response)
        result_calls = _extract_task_result_calls(response)
        write_calls = _extract_write_calls(response) if (allow_write and work_root) else []
        if not bash_calls and not result_calls and not write_calls:
            break

        # Execute tools
        tool_parts: list[str] = []
        for path, content in write_calls[:5]:
            out = _tool_write_file(path, content, work_root)
            tool_calls_made += 1
            tool_parts.append(f"[write_file {path}]\n{out}")
        for cmd in bash_calls[:5]:
            out = _tool_bash(cmd, root=work_root)
            tool_calls_made += 1
            tool_parts.append(f"$ {cmd}\n{out}")
        for tid in result_calls[:5]:
            out = _tool_task_result(tid)
            tool_calls_made += 1
            tool_parts.append(f"[task_result {tid}]\n{out}")
        tool_output = "\n\n".join(tool_parts)
        conversation.append((response, tool_output))

        # Emit tool results as SSE chunks so the client sees them live
        suffix = "\n\n--- Tool Outputs ---\n" + tool_output + "\n"
        index += 1
        _append_event(task_id, "chunk", chunk=suffix, index=index, model=model)

        # Check for cancel before making another LLM call
        with _LOCK:
            t = _TASKS.get(task_id, {})
            if t.get("cancel_requested"):
                return response, model, index, tool_calls_made, _transcript()

        # Build continuation prompt: original task + compacted prior history
        history_blocks = _history_blocks()
        continuation_prompt = (
            f"{original_prompt}\n\n"
            "---\n"
            "You used tools in a previous step. Results:\n\n"
            + "\n\n---\n\n".join(history_blocks)
            + "\n\n---\n\n"
            "Continue your analysis with these results. "
            "Write your final complete answer. "
            "Only use "
            + ("<bash>, <write_file> or <task_result>" if allow_write else "<bash> or <task_result>")
            + " tags again if you need one more step."
        )

        # Stream continuation response
        stream, model = smart_stream(
            continuation_prompt, tool="chat", extra_system=agent_ctx,
            prefer_local=prefer_local, skip_dynamic_context=True,
        )
        cont_chunks: list[str] = []
        for chunk in stream:
            with _LOCK:
                t = _TASKS.get(task_id, {})
                if t.get("cancel_requested"):
                    return "".join(cont_chunks), model, index, tool_calls_made, _transcript()
                if t.get("status") != "streaming":
                    _set_task_status(task_id, "streaming")
            cont_chunks.append(chunk)
            index += 1
            _append_event(task_id, "chunk", chunk=chunk, index=index, model=model)

        response = "".join(cont_chunks)

    # Budget exhausted but the agent still wants tools: force a final answer
    # so the stored result is never a dangling, unexecuted tool tag.
    if conversation and (_extract_bash_calls(response) or _extract_task_result_calls(response)
                         or (allow_write and _extract_write_calls(response))):
        history_blocks = _history_blocks()
        wrapup_prompt = (
            f"{original_prompt}\n\n"
            "---\n"
            "Tool history so far:\n\n"
            + "\n\n---\n\n".join(history_blocks)
            + "\n\n---\n\n"
            "Your tool budget is EXHAUSTED — no more <bash> or <task_result> tags "
            "will be executed. Using ONLY the tool results already shown above, "
            "write your final complete answer now. If the results are insufficient, "
            "say exactly what is known and what could not be determined."
        )
        with _LOCK:
            t = _TASKS.get(task_id, {})
            if t.get("cancel_requested"):
                return response, model, index, tool_calls_made, _transcript()
        stream, model = smart_stream(
            wrapup_prompt, tool="chat", extra_system=agent_ctx,
            prefer_local=prefer_local, skip_dynamic_context=True,
        )
        final_chunks: list[str] = []
        for chunk in stream:
            final_chunks.append(chunk)
            index += 1
            _append_event(task_id, "chunk", chunk=chunk, index=index, model=model)
        final = "".join(final_chunks)
        if final.strip():
            # Strip any tags the model emitted anyway — they will not run
            final = re.sub(r'<bash>.*?</bash>', '[tool budget exhausted — not executed]',
                           final, flags=re.DOTALL)
            final = re.sub(r'<task_result>.*?</task_result>',
                           '[tool budget exhausted — not executed]', final, flags=re.DOTALL)
            final = _WRITE_TAG_RE.sub('[tool budget exhausted — not executed]', final)
            response = final

    # A continuation round can stream zero chunks — never store an empty
    # result when real tool outputs exist.
    if not response.strip():
        if conversation:
            last_text = next((r for r, _ in reversed(conversation) if r.strip()), "")
            response = (
                (last_text + "\n\n" if last_text else "")
                + "[runtime note: the model returned no final text after tool "
                "execution; the tool outputs below are runtime-captured ground truth]\n\n"
                "--- Tool Outputs ---\n" + _transcript()[-4000:]
            ).strip()
        else:
            response = "[runtime note: the model returned no text]"

    return response, model, index, tool_calls_made, _transcript()


# Agents that get live task pipeline state injected before they run
_MONITOR_AGENTS = {
    "pipeline-monitor",
    "output-quality-checker",
    "ai-evaluator",
    "ai-safety-agent",
}

# Agents that get repo file contents injected based on filenames in their prompt
_FILE_READER_AGENTS = {
    "security-reviewer",
    "backend-engineer",
    "qa-tester",
    "automation-engineer",
    "researcher",
}


def _tool_read_files(prompt: str) -> str:
    """Extract .py/.md filenames from prompt, read them from the repo, return as context block."""
    if _REPO_ROOT is None:
        return ""
    candidates = re.findall(
        r'\b([\w/._-]+\.(?:py|md|json|yaml|yml|toml|sh|txt))\b', prompt
    )
    seen: set[str] = set()
    sections: list[str] = []
    for name in candidates[:5]:
        if name in seen:
            continue
        seen.add(name)
        for base in [_REPO_ROOT, _REPO_ROOT / "brains", _REPO_ROOT / "infra",
                     _REPO_ROOT / "tests", _REPO_ROOT / "tools"]:
            candidate = (base / name).resolve()
            if not str(candidate).startswith(str(_REPO_ROOT)):
                continue
            if candidate.suffix not in _SAFE_READ_EXTENSIONS:
                continue
            if not candidate.exists():
                continue
            try:
                content = candidate.read_text(errors="replace")[:3000]
                rel = str(candidate.relative_to(_REPO_ROOT))
                sections.append(f"[{rel}  —  {len(content)} chars]\n{content}")
                break
            except Exception:
                pass
    if not sections:
        return ""
    return "=== Repo file contents (read-only context) ===\n\n" + "\n\n---\n\n".join(sections)


def _tool_live_tasks(limit: int = 20) -> str:
    """Return a formatted snapshot of the current task pipeline state."""
    try:
        tasks = list_tasks(limit=limit)
    except Exception:
        return ""
    lines = []
    for t in tasks:
        result_len = len(t.get("result") or "")
        preview = ((t.get("result") or "")[:100]).replace("\n", " ")
        line = (
            f"  {t['id']}  {(t.get('assigned_agent_id') or '?'):<24}"
            f"  [{t.get('status','?'):<16}]  {result_len:>5}b"
        )
        if preview:
            line += f'  "{preview}"'
        lines.append(line)
    header = f"=== Live task pipeline ({len(tasks)} tasks) ==="
    return header + "\n" + "\n".join(lines)


def _tool_fetch_task_results(prompt: str) -> str:
    """Find task_* IDs mentioned in the prompt and inject their results."""
    task_ids = re.findall(r'\btask_[a-f0-9]{12}\b', prompt)
    if not task_ids:
        return ""
    sections = []
    for tid in task_ids[:6]:
        t = get_task(tid)
        if not t:
            continue
        result = (t.get("result") or "").strip()
        if not result:
            continue
        agent = t.get("assigned_agent_id", "?")
        status = t.get("status", "?")
        sections.append(f"[{tid}  /  {agent}  /  {status}]\n{result[:800]}")
    if not sections:
        return ""
    return "=== Referenced task results ===\n\n" + "\n\n".join(sections)


def _build_agent_tool_context(agent_id: str, prompt: str) -> str:
    """Assemble live context to inject before the agent runs its task."""
    parts: list[str] = []

    if agent_id in _MONITOR_AGENTS:
        live = _tool_live_tasks()
        if live:
            parts.append(live)

    referenced = _tool_fetch_task_results(prompt)
    if referenced:
        parts.append(referenced)

    if agent_id in _FILE_READER_AGENTS:
        files = _tool_read_files(prompt)
        if files:
            parts.append(files)

    return "\n\n" + "\n\n".join(parts) if parts else ""


def _run_task(task_id: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return
        agent_id = task["assigned_agent_id"]
        _set_task_status(task_id, "assigned", assigned_at=_now())
        _touch_agent(agent_id, status="busy", current_task_id=task_id, last_error="")

    try:
        with _get_agent_lock(agent_id):
            with _MODEL_SEMAPHORE:
                with _LOCK:
                    task = _TASKS[task_id]
                    if task.get("cancel_requested"):
                        _set_task_status(task_id, "cancelled", finished_at=_now())
                        return
                    _set_task_status(task_id, "running", started_at=_now())
                    prompt = task.get("effective_prompt") or task["prompt"]
                    original_prompt = task["prompt"]
                    source = task["source"]
                    original_kind = task.get("kind", "chat")

                start_seq = usage_tracker.current_seq()
                _bash_tool_hint = (
                    "Bash tool: you may execute read-only shell commands by wrapping them in "
                    "<bash>command</bash> tags in your response. "
                    "The runtime will execute the command and return the output so you can continue. "
                    "Allowed commands: grep, find, ls, cat, head, tail, wc, git log/diff/status/show, pytest, ruff, echo. "
                    "IMPORTANT constraints: (1) Pipes between allowed commands work "
                    "(e.g. ls *.py | wc -l, max 4 stages); redirection (>, 2>), command "
                    "chaining (;, &&) and substitution ($(), backticks) are rejected. "
                    "(2) Paths must be within the workspace root. Globs like *.py are expanded relative to it. "
                    "MANDATORY: if the task asks you to run, check, count, or inspect anything in the repo, "
                    "you MUST emit a <bash> tag and wait for the real output. NEVER fabricate or imagine "
                    "command output — fabricated output is a critical failure."
                )
                # Isolated workspace (git worktree on its own branch): unlocks
                # the write tool. Bash/pytest run inside it; diff is surfaced
                # for review and the branch is NEVER merged by the runtime.
                with _LOCK:
                    _ws = dict(_TASKS[task_id].get("workspace") or {})
                work_root: Path | None = None
                if _ws.get("ok") and _ws.get("enabled") and _ws.get("worktree_path"):
                    _wr = Path(_ws["worktree_path"]).resolve()
                    if _wr.is_dir():
                        work_root = _wr
                _write_tool_hint = ""
                if work_root is not None:
                    _write_tool_hint = (
                        " Write tool: you are in an ISOLATED git worktree (branch "
                        f"{_ws.get('branch', '?')}) — you may create or modify files with "
                        '<write_file path="relative/path.py">full file content</write_file> tags. '
                        "Always write the COMPLETE file content, not a fragment. "
                        "After writing, verify with <bash>pytest ...</bash> or ruff in the same "
                        "or next step. Your changes land on the isolated branch only; a human "
                        "reviews the diff before anything is merged."
                    )
                agent_ctx = (
                    f"You are the '{agent_id}' agent in Jarvis, a local-first macOS AI runtime. "
                    "Produce a direct, complete response to the task. Do not produce a plan unless explicitly asked. "
                    + _bash_tool_hint
                    + _write_tool_hint
                )
                tool_ctx = _build_agent_tool_context(agent_id, original_prompt)
                if tool_ctx:
                    agent_ctx = agent_ctx + tool_ctx
                lesson = _load_agent_lesson(agent_id)
                if lesson:
                    agent_ctx = agent_ctx + lesson
                # Verified-failure escalation: first attempt runs local-first
                # (cheap); once the verifier has rejected an attempt, retries
                # go to the stronger cloud tier instead of repeating the same
                # local-model failure mode.
                with _LOCK:
                    _retry_count = int((_TASKS.get(task_id, {}).get("meta") or {}).get("retry_count", 0))
                prefer_local_run = _retry_count == 0
                stream, model = smart_stream(prompt, tool="chat", extra_system=agent_ctx, prefer_local=prefer_local_run)
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

                initial_response = "".join(chunks)
                # Phase 2: multi-turn tool-call loop. If the initial response
                # contains <bash> calls, execute them and feed results back
                # to the LLM for a genuine continuation (up to 4 more rounds).
                response, model, index, tool_calls_made, tool_transcript = _run_tool_loop(
                    task_id,
                    original_prompt,
                    agent_ctx,
                    initial_response,
                    model,
                    index,
                    work_root=work_root,
                    allow_write=work_root is not None,
                    prefer_local=prefer_local_run,
                )
                # Deterministic fabrication check: response claims execution
                # but zero tool calls actually ran. Runs for ALL agents — no
                # LLM needed, the runtime knows the ground truth. Skipped when
                # the task inherited verified outputs from a prior attempt —
                # the LLM verifier judges those against the evidence instead.
                with _LOCK:
                    task_meta = _TASKS.get(task_id, {}).get("meta") or {}
                inherited_transcript = str(task_meta.get("inherited_transcript", ""))
                fabricated_claim = (
                    "" if (inherited_transcript and tool_calls_made == 0)
                    else _detect_fabricated_execution(response, tool_calls_made)
                )
                if fabricated_claim:
                    warning = (
                        "\n\n[RUNTIME WARNING: this response claims command execution "
                        f'("{fabricated_claim}") but the agent made ZERO tool calls. '
                        "The claimed output is fabricated and must not be trusted.]"
                    )
                    response += warning
                    index += 1
                    _append_event(task_id, "chunk", chunk=warning, index=index, model=model)
                    log.warning(
                        "task %s (%s): fabricated execution claim detected: %r",
                        task_id, agent_id, fabricated_claim,
                    )
                # Surface workspace changes as a reviewable diff. The branch is
                # never merged by the runtime — review and merge are human steps.
                if work_root is not None:
                    ws_diff = _workspace_diff(work_root)
                    if ws_diff:
                        diff_block = (
                            f"\n\n--- Workspace Diff (branch {_ws.get('branch', '?')}, "
                            "NOT merged — review required) ---\n" + ws_diff
                        )
                        response += diff_block
                        index += 1
                        _append_event(task_id, "chunk", chunk=diff_block, index=index, model=model)
                        _append_event(task_id, "workspace_diff",
                                      branch=_ws.get("branch", ""), chars=len(ws_diff))
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
            # Verification runs OUTSIDE the lock — LLM calls cannot hold _LOCK
            if _AUTO_VERIFY and agent_id in _VERIFIABLE_AGENTS:
                retry_count = int(task_meta.get("retry_count", 0))
                verdict = _auto_verify(
                    task_id, agent_id, original_prompt, response,
                    tool_calls=tool_calls_made,
                    tool_transcript=tool_transcript,
                    inherited_transcript=inherited_transcript,
                )
                _record_verdict(task_id, agent_id, verdict, tool_calls_made, retry_count,
                                result=response, tool_transcript=tool_transcript,
                                inherited_transcript=inherited_transcript,
                                fabricated_claim=fabricated_claim)
                with _LOCK:
                    if task_id in _TASKS:
                        (_TASKS[task_id].setdefault("meta", {}))["verification"] = verdict
                if not verdict["pass"] and retry_count < 2:
                    log.info(
                        "task %s failed verification (score=%.2f): %s — retrying (%d/2)",
                        task_id, verdict["score"], verdict["reason"], retry_count + 1,
                    )
                    # Carry real evidence forward — without it, retries parrot
                    # the prior answer with zero tool calls and fail fabrication
                    # checks every time.
                    evidence = (tool_transcript or inherited_transcript)[-2500:]
                    evidence_block = (
                        "\nVerified tool outputs from the previous attempt "
                        "(runtime-captured — you may cite these as evidence):\n"
                        f"{evidence}\n" if evidence else ""
                    )
                    retry_prompt = (
                        f"{original_prompt}\n\n"
                        f"---\nPrevious attempt failed quality check: {verdict['reason']}\n"
                        f"{evidence_block}"
                        "Produce a better response that addresses this specific weakness. "
                        "If you need facts beyond the outputs above, execute fresh <bash> "
                        "commands — do not invent results."
                    )
                    submit_task(
                        retry_prompt,
                        kind=original_kind,
                        source="retry",
                        assigned_agent_id=agent_id,
                        meta={"retry_count": retry_count + 1, "confidence_score": 0.9,
                              "original_task_id": task_id,
                              "inherited_transcript": evidence},
                    )
                elif not verdict["pass"] and retry_count >= 2:
                    log.warning(
                        "task %s failed verification after max retries: %s",
                        task_id, verdict["reason"],
                    )
                    _propose_skill_update(agent_id, original_prompt, response, verdict["reason"])
    except Exception as exc:
        with _LOCK:
            _fail_task(task_id, str(exc))
        _ap = locals().get("original_prompt", "")
        _ai = locals().get("agent_id", "")
        if _ai in _VERIFIABLE_AGENTS and _ap:
            _propose_skill_update(_ai, _ap, str(exc), f"exception: {exc}")
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
    approval_required, approval_reason, confidence = _task_requires_approval(
        prompt,
        kind=normalized_kind,
        source=source,
        agent_id=chosen_agent_id,
        meta=meta,
    )
    workspace = worktree_manager.prepare_isolated_workspace(
        task_id,
        prompt,
        enabled=_should_isolate_workspace(normalized_kind, isolated_workspace),
    )
    task = {
        "id": task_id,
        "kind": normalized_kind,
        "source": source or "api",
        "status": _WAITING_APPROVAL_STATUS if approval_required else "queued",
        "prompt": prompt,
        "effective_prompt": _task_prompt_for_kind(
            prompt,
            kind=normalized_kind,
            terse_mode=normalized_terse_mode,
            agent_id=chosen_agent_id,
        ),
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
        "approval_required": approval_required,
        "approval_reason": approval_reason,
        "confidence": _copy(confidence),
        "autonomy": "human_review" if approval_required else "auto_edit",
        "debrief_required": True,
        "audit": {
            "manager_agent_id": "jarvis-manager",
            "assigned_agent_id": chosen_agent_id,
            "approval_policy": "confidence_gated_human_in_the_loop",
            "approval_reason": approval_reason,
            "created_at": created_at,
            "approved_at": "",
            "denied_at": "",
            "reversible_note": "Task output must include rollback or recall notes for any applied change.",
        },
        "approved_at": "",
        "denied_at": "",
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
            status=task["status"],
            agent_id=chosen_agent_id,
            kind=task["kind"],
            source=task["source"],
            terse_mode=normalized_terse_mode,
            approval_required=approval_required,
            approval_reason=approval_reason,
            confidence=_copy(confidence),
            autonomy=task["autonomy"],
        )
        if workspace.get("enabled"):
            _append_event(task_id, "workspace", workspace=_copy(workspace))
        if not approval_required:
            _start_task_thread(task_id)
        return _copy(task)


def approve_task(task_id: str) -> dict[str, Any] | None:
    bootstrap()
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        if task.get("status") != _WAITING_APPROVAL_STATUS:
            return _copy(task)
        task["approval_required"] = False
        task["approved_at"] = _now()
        task["autonomy"] = "human_approved"
        audit = task.get("audit")
        if isinstance(audit, dict):
            audit["approved_at"] = task["approved_at"]
            audit["approval_reason"] = task.get("approval_reason", "")
        _set_task_status(task_id, "queued", approved_at=task["approved_at"])
        _start_task_thread(task_id)
        snapshot = _copy(task)
    audit_event(
        APPROVAL_GRANTED, actor="operator",
        target=str(snapshot.get("prompt") or "")[:80], task_id=task_id,
        rollback_ref=str((snapshot.get("workspace") or {}).get("branch") or ""),
    )
    return snapshot


def deny_task(task_id: str) -> dict[str, Any] | None:
    bootstrap()
    with _LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return None
        if task["status"] in _TERMINAL_TASK_STATUSES:
            return _copy(task)
        task["denied_at"] = _now()
        task["cancel_requested"] = True
        task["autonomy"] = "human_denied"
        audit = task.get("audit")
        if isinstance(audit, dict):
            audit["denied_at"] = task["denied_at"]
        _set_task_status(task_id, "cancelled", finished_at=_now(), denied_at=task["denied_at"])
        snapshot = _copy(task)
    audit_event(
        APPROVAL_DENIED, actor="operator",
        target=str(snapshot.get("prompt") or "")[:80], task_id=task_id,
        rollback_ref=str((snapshot.get("workspace") or {}).get("branch") or ""),
    )
    return snapshot


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
        if task["status"] in {"queued", _WAITING_APPROVAL_STATUS}:
            _set_task_status(task_id, "cancelled", finished_at=_now())
            agent_id = task.get("assigned_agent_id", "")
            if agent_id:
                _touch_agent(agent_id, status="idle", current_task_id="")
        return _copy(task)


def purge_terminal_tasks() -> int:
    """Remove all succeeded/failed/cancelled tasks from memory and persistence. Returns count removed."""
    bootstrap()
    with _LOCK:
        to_remove = [
            tid for tid, task in _TASKS.items()
            if task.get("status") in _TERMINAL_TASK_STATUSES
        ]
        for tid in to_remove:
            _TASKS.pop(tid, None)
            _TASK_EVENTS.pop(tid, None)
        try:
            import task_persistence
            task_persistence.save_snapshot({"tasks": list(_TASKS.values()), "events": dict(_TASK_EVENTS)})
        except Exception:
            pass
    return len(to_remove)


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
                "waiting_approval": sum(1 for task in tasks if task.get("status") == _WAITING_APPROVAL_STATUS),
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
