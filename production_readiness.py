"""
Production-readiness contract for Jarvis.

This module is intentionally stricter than the capability parity scorecard:
parity tracks local feature coverage, while this contract prevents Jarvis from
claiming "100% production ready" for every possible user request.
"""

from __future__ import annotations

import contextlib
import io
import shutil
from pathlib import Path
from typing import Any

from config import DEFAULT_MODE, LOCAL_CODER, LOCAL_DEFAULT, LOCAL_REASONING


APPLICATIONS_APP = Path("/Users/truthseeker/Applications/Jarvis.app")
DESKTOP_APP = Path("/Users/truthseeker/Desktop/Jarvis.app")
CRASH_LOG = Path("/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_crash.log")


def _safe(label: str, fn, fallback: Any) -> Any:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return fn()
    except Exception as exc:
        if isinstance(fallback, dict):
            return {**fallback, "error": str(exc), "source": label}
        return fallback


def _path_exists(path: Path) -> bool:
    return path.exists()


def _which(command: str) -> str:
    return shutil.which(command) or ""


def _status(ready: bool, *, partial: bool = False) -> str:
    if ready:
        return "ready"
    if partial:
        return "partial"
    return "gap"


def _check(
    check_id: str,
    name: str,
    status: str,
    evidence: list[str],
    next_gap: str,
    *,
    critical: bool = True,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "critical": critical,
        "evidence": evidence,
        "next_gap": next_gap,
    }


def contract() -> dict[str, Any]:
    import capability_evals
    import model_router
    import semantic_memory
    import task_runtime
    import vault
    from brains import brain_ollama
    from local_runtime import local_stt, local_tts
    import security_roe

    local_available = bool(_safe("model_router", model_router._has_local, False))
    local_caps = _safe("brain_ollama", brain_ollama.local_capabilities, {})
    vault_status = _safe("vault", vault.status, {})
    semantic_status = _safe("semantic_memory", semantic_memory.status, {})
    stt_status = _safe("local_stt", local_stt.status, {})
    tts_status = _safe("local_tts", local_tts.status, {})
    agents = _safe("task_runtime", task_runtime.list_agents, [])
    evals_status = _safe("capability_evals", capability_evals.status, {})
    roe_status = _safe("security_roe", security_roe.status, {})

    vision_state = str(local_caps.get("vision_status") or "").lower()
    vision_ready = vision_state in {"ready", "available", "ok"}
    semantic_ready = bool(semantic_status.get("index_ready"))
    vault_ready = int(vault_status.get("doc_count") or 0) > 0
    stt_ready = bool(stt_status.get("local_available"))
    tts_ready = bool(tts_status.get("ready"))
    jarvis_command = _which("jarvis")
    app_ready = _path_exists(APPLICATIONS_APP)
    desktop_ready = _path_exists(DESKTOP_APP)
    eval_coverage = float(evals_status.get("coverage_score") or 0.0)
    roe_ready = roe_status.get("mode") == "defensive-only"

    checks = [
        _check(
            "local_model_routing",
            "Local model routing",
            _status(local_available),
            [f"default_mode={DEFAULT_MODE}", f"default_model={LOCAL_DEFAULT}", f"reasoning_model={LOCAL_REASONING}"],
            "keep open-source/local mode as the default and fail closed if Ollama is unavailable",
        ),
        _check(
            "coding_agent",
            "Local coding-agent lane",
            _status(local_available and bool(LOCAL_CODER), partial=bool(LOCAL_CODER)),
            [f"coder_model={LOCAL_CODER}", "managed_tasks=true", "isolated_code_workspace=true"],
            "add harder repo-grounded implementation and verification evals",
        ),
        _check(
            "voice_loop",
            "Voice input and output",
            _status(stt_ready and tts_ready, partial=stt_ready or tts_ready),
            [
                f"stt_engine={stt_status.get('active_engine', 'unknown')}",
                f"stt_local={stt_ready}",
                f"tts_ready={tts_ready}",
            ],
            "run a packaged mic-capture plus audible-TTS smoke, not just status checks",
        ),
        _check(
            "vision_loop",
            "Local vision",
            _status(vision_ready, partial=bool(local_caps.get("vision_model") or local_caps.get("vision_preferred"))),
            [
                f"vision_model={local_caps.get('vision_model') or local_caps.get('vision_preferred') or 'local vision route'}",
                f"vision_status={local_caps.get('vision_status', 'unknown')}",
            ],
            "verify text-heavy screenshot handling through the packaged app",
        ),
        _check(
            "memory_brain",
            "Vault and semantic memory",
            _status(vault_ready and semantic_ready, partial=vault_ready or semantic_ready),
            [
                f"vault_docs={vault_status.get('doc_count', 0)}",
                f"semantic_backend={semantic_status.get('retrieval_backend', 'unknown')}",
                f"semantic_ready={semantic_ready}",
            ],
            "keep retrieval quality tied to local golden cases and curated vault hubs",
        ),
        _check(
            "managed_agents",
            "Managed smart agents",
            _status(bool(agents)),
            [f"agent_count={len(agents)}"],
            "add stronger issue-board/status UX and blocker reporting",
        ),
        _check(
            "terminal_console",
            "Jarvis terminal console",
            _status(bool(jarvis_command)),
            [f"jarvis_path={jarvis_command or 'missing'}"],
            "keep the installed jarvis command synced with the daemon and packaged app",
        ),
        _check(
            "packaged_app",
            "Packaged macOS app surface",
            _status(app_ready and desktop_ready, partial=app_ready or desktop_ready),
            [f"applications_app={app_ready}", f"desktop_app={desktop_ready}"],
            "rebuild the installed bundle after runtime/API/router changes and verify the installed target",
        ),
        _check(
            "safety_contract",
            "Safety and permission contract",
            _status(roe_ready),
            [f"security_mode={roe_status.get('mode', 'unknown')}", "protected_paths=true"],
            "keep dual-use work constrained to defensive scope and explicit authorization",
        ),
        _check(
            "eval_harness",
            "Capability eval harness",
            _status(eval_coverage >= 1.0, partial=eval_coverage > 0),
            [f"coverage={eval_coverage:.0%}", str(evals_status.get("live_command") or "")],
            "schedule live golden runs and raise difficulty as cases become easy",
        ),
        _check(
            "zero_cost_default",
            "Zero-cost default route",
            _status(DEFAULT_MODE == "open-source"),
            [f"default_mode={DEFAULT_MODE}", "paid_cloud_fallback=false"],
            "keep paid/cloud providers opt-in rather than fallback behavior",
        ),
    ]

    critical_ready = all(check["status"] == "ready" for check in checks if check.get("critical"))
    go_live_gates = _go_live_gates(
        console_ready=bool(jarvis_command),
        app_ready=app_ready and desktop_ready,
        crash_log_present=_path_exists(CRASH_LOG),
    )
    gates_ready = all(gate["status"] == "ready" for gate in go_live_gates)

    constraints = [
        "No local assistant can satisfy every possible request for free; some tasks require live internet, third-party accounts, paid services, or unavailable local tools.",
        "Live/current information still requires network access and source verification.",
        "Account actions require the user to be logged in and to grant app, browser, connector, or macOS permissions.",
        "Frontier-quality output is bounded by installed local models, hardware, context window, and latency.",
        "Unsafe, illegal, credential-theft, evasion, persistence, or unauthorized offensive requests must be blocked or converted to defensive guidance.",
        "macOS mic, screen, automation, and accessibility permissions can drift outside Jarvis code and must be verified on the real packaged app.",
        "Zero API cost applies to the default local/open-source core path, not to optional external services the user explicitly chooses.",
    ]

    return {
        "ok": True,
        "production_ready": False,
        "daily_local_core_ready": critical_ready,
        "free_local_core_ready": critical_ready and DEFAULT_MODE == "open-source",
        "unbounded_free_use": False,
        "go_live_gates_ready": gates_ready,
        "summary": _summary(critical_ready, gates_ready),
        "checks": checks,
        "constraints": constraints,
        "go_live_gates": go_live_gates,
        "next_best_seam": _next_best_seam(checks, go_live_gates),
    }


def _go_live_gates(*, console_ready: bool, app_ready: bool, crash_log_present: bool) -> list[dict[str, Any]]:
    return [
        _check(
            "installer_and_path",
            "Installed console and packaged app",
            _status(console_ready and app_ready, partial=console_ready or app_ready),
            [f"console_ready={console_ready}", f"packaged_app_ready={app_ready}"],
            "keep installer/path sync verified after runtime changes",
            critical=False,
        ),
        _check(
            "packaged_voice_ui_smoke",
            "Packaged voice/UI end-to-end smoke",
            "partial",
            ["API/package smoke exists", "audible TTS and live mic smoke still manual"],
            "add a safe installed-app smoke that verifies mic capture, STT text, and audible TTS",
            critical=False,
        ),
        _check(
            "recurring_live_golden_suite",
            "Recurring live golden suite",
            "gap",
            ["golden cases exist", "no recurring production gate is enforced yet"],
            "schedule or automate live golden runs for console, memory, voice, vision, coding, and safety",
            critical=False,
        ),
        _check(
            "backup_restore",
            "Brain and runtime backup/restore",
            "gap",
            ["vault persists as markdown", "no restore drill recorded"],
            "add a restore drill for vault, memory db, runtime state, and installed app config",
            critical=False,
        ),
        _check(
            "crash_recovery",
            "Crash recovery and runtime observability",
            _status(False, partial=crash_log_present),
            [f"crash_log_present={crash_log_present}"],
            "add a clear crash monitor/restart path and a clean no-crash packaged smoke",
            critical=False,
        ),
        _check(
            "permission_onboarding",
            "macOS permission onboarding",
            "partial",
            ["doctor reports capability status", "system permission prompts are still external"],
            "make permission failures actionable from the app and console",
            critical=False,
        ),
    ]


def _summary(core_ready: bool, gates_ready: bool) -> str:
    core = "yes" if core_ready else "not yet"
    gates = "yes" if gates_ready else "not yet"
    return (
        "No. Jarvis is not 100% production-ready for every possible request, and it cannot be unbounded-free regardless of request. "
        f"The local zero-cost core is ready for daily use: {core}. Production go-live gates are all ready: {gates}."
    )


def _next_best_seam(checks: list[dict[str, Any]], go_live_gates: list[dict[str, Any]]) -> str:
    for item in checks:
        if item["status"] != "ready":
            return item["next_gap"]
    for item in go_live_gates:
        if item["status"] != "ready":
            return item["next_gap"]
    return "keep production readiness honest with recurring live evals and packaged-app smokes"


def summary_text() -> str:
    payload = contract()
    lines = [
        payload["summary"],
        f"Free local core ready: {'yes' if payload['free_local_core_ready'] else 'no'}",
        f"Unbounded free use: {'yes' if payload['unbounded_free_use'] else 'no'}",
        f"Next seam: {payload['next_best_seam']}",
        "",
        "Core checks:",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['name']} [{check['status']}]: {', '.join(check['evidence'][:2])}.")
    lines.append("")
    lines.append("Hard constraints:")
    for constraint in payload["constraints"][:4]:
        lines.append(f"- {constraint}")
    return "\n".join(lines)
