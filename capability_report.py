"""Truthful, runtime-aware capability reporting for Jarvis."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


log = logging.getLogger(__name__)


HealthSnapshot = Mapping[str, Mapping[str, Any]]


def _load_health() -> HealthSnapshot:
    try:
        import jarvis_health

        return jarvis_health.check_all(force=False)
    except Exception:
        log.debug("Capability health snapshot failed", exc_info=True)
        return {}


def _load_message_history_status() -> Mapping[str, Any]:
    try:
        import messages

        return messages.messages_history_access_status()
    except Exception:
        log.debug("Messages history capability probe failed", exc_info=True)
        return {}


def _component_state(health: HealthSnapshot, name: str) -> str:
    status = health.get(name)
    if not status:
        return "not verified"
    return "ready" if status.get("ok") else "needs attention"


def _voice_state(health: HealthSnapshot) -> str:
    stt = _component_state(health, "stt")
    tts = _component_state(health, "tts")
    if stt == tts == "ready":
        return "ready (local speech-to-text and text-to-speech)"
    if "needs attention" in {stt, tts}:
        missing = [name.upper() for name, state in (("stt", stt), ("tts", tts)) if state != "ready"]
        return f"needs attention ({', '.join(missing)})"
    return "not verified"


def _watcher_state(health: HealthSnapshot) -> str:
    status = health.get("watcher")
    if not status:
        return "not verified"
    detail = str(status.get("detail") or "").lower()
    if "disabled" in detail:
        return "disabled"
    return "active" if status.get("ok") else "needs attention"


def _message_history_state(status: Mapping[str, Any]) -> str:
    if not status:
        return "not verified"
    if status.get("ok"):
        return "ready"
    return "not ready (Full Disk Access or local Messages data may be required)"


def capabilities_reply(
    *,
    health: HealthSnapshot | None = None,
    message_history: Mapping[str, Any] | None = None,
) -> str:
    """Describe implemented capability scope and this runtime's current readiness."""
    snapshot = _load_health() if health is None else health
    history_status = _load_message_history_status() if message_history is None else message_history

    google_state = _component_state(snapshot, "google")
    if google_state == "ready":
        google_line = "ready"
    elif google_state == "needs attention":
        google_line = "needs attention (authorization or connectivity failed)"
    else:
        google_line = "not verified (requires a healthy Google connection)"

    return "\n".join(
        [
            "Here is what I can do on this Mac:",
            "",
            "Implemented capabilities",
            "- Agentic execution: plan and run bounded multi-step tasks, call approved tools, track progress, and report evidence.",
            "- Python code loop: generate code in an isolated workspace, run pytest, diagnose failures, patch, and rerun.",
            "- Specialist passes: planner, executor, reviewer, researcher, debugger, vault, and defensive security roles.",
            "- Research and knowledge: web search, cited deep research, source ingestion, notes, memory, artifacts, and Obsidian vault work.",
            "- Mac assistance: browser and app control, terminal and files, screenshots, timers, weather, Calendar, Gmail, and iMessage.",
            "- Voice and meetings: wake word, local transcription, speech, and Smart Listen.",
            "- Guarded improvement: prepare skills and local-model candidates for evaluation; training and promotion are never silent.",
            "",
            "Current readiness",
            f"- Local models: {_component_state(snapshot, 'ollama')}.",
            f"- Voice: {_voice_state(snapshot)}.",
            f"- Vault: {_component_state(snapshot, 'vault')}. Semantic/episodic memory: {_component_state(snapshot, 'mem0')}.",
            f"- Calendar and Gmail: {google_line}.",
            f"- iMessage: draft/send is confirmation-gated; history is {_message_history_state(history_status)}.",
            f"- Proactive alerts: {_watcher_state(snapshot)}.",
            "",
            "Sensitive external or privileged actions require confirmation or explicit approval. "
            "Live web and weather results require network access. Say 'health check' for detailed diagnostics.",
        ]
    )


def mobile_system_guidance() -> str:
    """Return non-stale capability guidance for model-driven mobile replies."""
    return (
        "Jarvis can route bounded agentic tasks, an isolated Python test/fix loop, "
        "specialist passes, research and vault work, Mac tools, communication tools, voice, and meetings. "
        "Integration availability depends on live health, account authorization, network access, and macOS permissions. "
        "Never claim that Calendar, Gmail, iMessage history, proactive alerts, voice, memory, or local models are ready "
        "unless runtime evidence verifies them. Distinguish implemented capabilities from current readiness, and describe "
        "local-model training or self-improvement as approval-gated rather than autonomous. "
    )
