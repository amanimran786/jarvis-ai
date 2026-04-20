"""
Local frontier capability parity scorecard.

The goal is not to claim Jarvis is Claude/GPT/Codex/Gemini/Grok. The goal is
to keep a live, inspectable map of local equivalents and remaining gaps.
"""

from __future__ import annotations

import contextlib
import io
from typing import Any

from config import DEFAULT_MODE, LOCAL_CODER, LOCAL_DEFAULT, LOCAL_REASONING


def _safe(label: str, fn, fallback: Any) -> Any:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return fn()
    except Exception as exc:
        if isinstance(fallback, dict):
            return {**fallback, "error": str(exc), "source": label}
        return fallback


def _feature(
    feature_id: str,
    name: str,
    status: str,
    local_equivalent: str,
    evidence: list[str],
    next_gap: str,
) -> dict[str, Any]:
    return {
        "id": feature_id,
        "name": name,
        "status": status,
        "local_equivalent": local_equivalent,
        "evidence": evidence,
        "next_gap": next_gap,
    }


def _status_from_ready(ready: bool, partial: bool = False) -> str:
    if ready:
        return "ready"
    if partial:
        return "partial"
    return "gap"


def scorecard() -> dict[str, Any]:
    import extension_registry
    import model_router
    import semantic_memory
    import task_runtime
    import vault
    from brains import brain_ollama
    from local_runtime import local_stt, local_tts

    local_available = bool(_safe("model_router", model_router._has_local, False))
    local_caps = _safe("brain_ollama", brain_ollama.local_capabilities, {})
    vault_status = _safe("vault", vault.status, {})
    semantic_status = _safe("semantic_memory", semantic_memory.status, {})
    stt_status = _safe("local_stt", local_stt.status, {})
    tts_status = _safe("local_tts", local_tts.status, {})
    agents = _safe("task_runtime", task_runtime.list_agents, [])
    skills = _safe("extension_registry", extension_registry.list_skills, [])
    connectors = _safe("extension_registry", extension_registry.list_connectors, [])
    plugins = _safe("extension_registry", extension_registry.list_plugins, [])

    vision_state = str(local_caps.get("vision_status") or "").lower()
    vision_ready = vision_state in {"ready", "available", "ok"}
    semantic_ready = bool(semantic_status.get("index_ready"))
    vault_ready = int(vault_status.get("doc_count") or 0) > 0
    stt_ready = bool(stt_status.get("local_available"))
    tts_ready = bool(tts_status.get("ready"))
    negative_skill_count = sum(1 for skill in skills if skill.get("negative_triggers"))
    browser_connector = any(connector.get("id") == "browser_operator" for connector in connectors)
    security_roe_ready = any(skill.get("id") == "defensive_security_roe" for skill in skills)

    features = [
        _feature(
            "chat_reasoning",
            "Claude/GPT/Gemini/Grok-style chat and reasoning",
            _status_from_ready(local_available),
            f"{LOCAL_DEFAULT} with reasoning route {LOCAL_REASONING}",
            [f"default_mode={DEFAULT_MODE}", f"local_available={local_available}", f"reasoning_model={LOCAL_REASONING}"],
            "run recurring local evals against hard reasoning, coding, and instruction-following cases",
        ),
        _feature(
            "coding_agent",
            "Codex/Claude Code-style repo implementation loop",
            _status_from_ready(local_available and bool(LOCAL_CODER), partial=bool(LOCAL_CODER)),
            f"managed /code lane using {LOCAL_CODER}",
            [f"coder_model={LOCAL_CODER}", "isolated_workspace=true", "context_budget=true"],
            "add stronger repo-map and verification helpers so code tasks ground faster",
        ),
        _feature(
            "vision",
            "Gemini/GPT-style image and screen understanding",
            _status_from_ready(vision_ready, partial=bool(local_caps.get("vision_model") or local_caps.get("vision_preferred"))),
            str(local_caps.get("vision_model") or local_caps.get("vision_preferred") or "local vision route"),
            [f"vision_status={local_caps.get('vision_status', 'unknown')}"],
            "verify more real screenshot and UI-understanding tasks through the packaged app",
        ),
        _feature(
            "memory_brain",
            "Persistent brain and retrieval",
            _status_from_ready(vault_ready and semantic_ready, partial=vault_ready or semantic_ready),
            "Obsidian markdown vault plus semantic memory",
            [
                f"vault_docs={vault_status.get('doc_count', 0)}",
                f"semantic_backend={semantic_status.get('retrieval_backend', 'unknown')}",
                f"semantic_ready={semantic_ready}",
            ],
            "keep consolidating inbox/candidate work into curated hubs and verify retrieval quality",
        ),
        _feature(
            "voice",
            "Voice in/out assistant loop",
            _status_from_ready(stt_ready and tts_ready, partial=stt_ready or tts_ready),
            "local faster-whisper STT plus local TTS",
            [
                f"stt_engine={stt_status.get('active_engine', 'unknown')}",
                f"stt_local={stt_ready}",
                f"tts_ready={tts_ready}",
            ],
            "continue packaged end-to-end voice verification, especially mic capture and TTS audibility",
        ),
        _feature(
            "agents",
            "Managed teammate agents",
            _status_from_ready(bool(agents)),
            "managed task runtime with specialist agent profiles",
            [f"agent_count={len(agents)}"],
            "add richer issue-board UX and blocker/status reporting without cloud dependency",
        ),
        _feature(
            "skills",
            "Portable reusable skills",
            _status_from_ready(bool(skills) and negative_skill_count > 0, partial=bool(skills)),
            "local skills registry with positive and negative triggers",
            [f"skill_count={len(skills)}", f"negative_trigger_skills={negative_skill_count}"],
            "add portable export/import compatibility for .agents/skills and .claude/skills layouts",
        ),
        _feature(
            "browser_tools",
            "Browser/tool execution",
            _status_from_ready(browser_connector, partial=True),
            "local browser operator and source ingest surfaces",
            [f"browser_connector={browser_connector}", f"connector_count={len(connectors)}", f"plugin_count={len(plugins)}"],
            "gate scraping/CDP expansion with user consent, ToS, credential, and robots.txt policy checks",
        ),
        _feature(
            "security",
            "Cybersecurity engineering companion",
            _status_from_ready(security_roe_ready, partial=True),
            "threat-modeling brain notes, security agents, permission gates, and defensive ROE templates",
            [
                "defensive_only_external_patterns=true",
                "protected_path_gates=true",
                f"defensive_security_roe={security_roe_ready}",
            ],
            "raise security eval quality with defensive-only incident, code-review, and prompt-injection cases",
        ),
    ]

    counts: dict[str, int] = {}
    for feature in features:
        counts[feature["status"]] = counts.get(feature["status"], 0) + 1
    ready = counts.get("ready", 0)
    total = len(features)
    return {
        "ok": True,
        "goal": "Close local capability gaps against frontier assistant/coding-agent product classes without cloud dependency.",
        "mode": DEFAULT_MODE,
        "score": round(ready / total, 2),
        "status_counts": counts,
        "features": features,
        "next_best_seam": _next_best_seam(features),
    }


def _next_best_seam(features: list[dict[str, Any]]) -> str:
    for preferred in ("coding_agent", "voice", "vision", "skills", "browser_tools"):
        for feature in features:
            if feature["id"] == preferred and feature["status"] != "ready":
                return feature["next_gap"]
    for feature in features:
        if feature["status"] != "ready":
            return feature["next_gap"]
    return "raise eval difficulty and keep packaging verification tight"


def summary_text() -> str:
    card = scorecard()
    lines = [
        f"Local frontier parity: {card['score']:.0%} ready by capability group.",
        f"Mode: {card['mode']}. Next seam: {card['next_best_seam']}",
        "",
    ]
    for feature in card["features"]:
        lines.append(f"- {feature['name']} [{feature['status']}]: {feature['local_equivalent']}.")
    return "\n".join(lines)
