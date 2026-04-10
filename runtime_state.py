from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.request
import urllib.error


@dataclass
class RuntimeState:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "INIT"
    mode: str = "auto"
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_running: bool = False
    api_thread_name: str = ""
    boot_reason: str = ""
    last_error: str = ""
    call_assist: dict[str, Any] = field(default_factory=dict)
    call_assist_updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


_STATE = RuntimeState()
_CALL_ASSIST_CACHE_TTL = 1.0


def get_state() -> RuntimeState:
    return _STATE


def update(**kwargs: Any) -> RuntimeState:
    for key, value in kwargs.items():
        if hasattr(_STATE, key):
            setattr(_STATE, key, value)
        else:
            _STATE.meta[key] = value
    return _STATE


def snapshot() -> dict[str, Any]:
    try:
        import task_runtime

        managed_runtime = task_runtime.runtime_snapshot()
    except Exception as exc:
        managed_runtime = {"ok": False, "error": str(exc)}
    persisted_api_endpoint = read_api_endpoint()
    public_endpoint = None
    if persisted_api_endpoint:
        public_endpoint = {
            "host": persisted_api_endpoint.get("host"),
            "port": persisted_api_endpoint.get("port"),
            "pid": persisted_api_endpoint.get("pid"),
            "written_at": persisted_api_endpoint.get("written_at"),
            "base_url": persisted_api_endpoint.get("base_url"),
        }
    return {
        "started_at": _STATE.started_at,
        "status": _STATE.status,
        "mode": _STATE.mode,
        "api_host": _STATE.api_host,
        "api_port": _STATE.api_port,
        "api_running": _STATE.api_running,
        "api_thread_name": _STATE.api_thread_name,
        "boot_reason": _STATE.boot_reason,
        "last_error": _STATE.last_error,
        "call_assist": dict(_STATE.call_assist),
        "call_assist_updated_at": _STATE.call_assist_updated_at,
        "managed_runtime": managed_runtime,
        "persistence": {
            "app_data_dir": str(app_data_dir()),
            "port_file": str(port_file_path()),
            "runtime_meta_file": str(runtime_meta_path()),
            "persisted_api_endpoint": public_endpoint,
        },
        "meta": dict(_STATE.meta),
    }


def mark_started(host: str, port: int, thread_name: str = "", reason: str = "") -> RuntimeState:
    return update(
        api_host=host,
        api_port=port,
        api_running=True,
        api_thread_name=thread_name,
        boot_reason=reason,
        status="ONLINE",
    )


def mark_stopped(reason: str = "") -> RuntimeState:
    return update(api_running=False, status="STOPPED", boot_reason=reason)


def mark_error(error: str) -> RuntimeState:
    return update(status="ERROR", last_error=error)


def app_data_dir() -> Path:
    override = os.getenv("JARVIS_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / "Library" / "Application Support" / "Jarvis"
    path.mkdir(parents=True, exist_ok=True)
    return path


def crash_log_path() -> Path:
    return app_data_dir() / ".jarvis_crash.log"


def port_file_path() -> Path:
    return app_data_dir() / ".jarvis_port"


def runtime_meta_path() -> Path:
    return app_data_dir() / ".jarvis_runtime.json"


def write_api_endpoint(host: str, port: int, *, pid: int | None = None) -> None:
    metadata = {
        "host": host,
        "port": int(port),
        "pid": int(pid or os.getpid()),
        "token": os.getenv("JARVIS_API_TOKEN", "").strip(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = runtime_meta_path()
    path.write_text(json.dumps(metadata), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_api_endpoint() -> None:
    for path in (runtime_meta_path(), port_file_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def read_api_endpoint() -> dict[str, Any] | None:
    try:
        payload = json.loads(runtime_meta_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    host = str(payload.get("host") or "").strip() or "127.0.0.1"
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        return None
    return {
        "host": host,
        "port": port,
        "pid": payload.get("pid"),
        "token": str(payload.get("token") or "").strip(),
        "written_at": payload.get("written_at"),
        "base_url": f"http://{host}:{port}",
    }


def discover_api_endpoint() -> dict[str, Any] | None:
    candidates: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def add_candidate(host: str, port: int) -> None:
        key = (host.strip() or "127.0.0.1", int(port))
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    env_host = (os.getenv("JARVIS_API_HOST", "127.0.0.1") or "").strip() or "127.0.0.1"
    env_port = os.getenv("JARVIS_API_PORT", "").strip()
    if env_port:
        try:
            add_candidate(env_host, int(env_port))
        except ValueError:
            pass

    metadata = read_api_endpoint()
    if metadata:
        add_candidate(str(metadata["host"]), int(metadata["port"]))

    try:
        raw_port = port_file_path().read_text(encoding="utf-8").strip()
        if raw_port:
            add_candidate("127.0.0.1", int(raw_port))
    except (FileNotFoundError, OSError, ValueError):
        pass

    for port in range(8765, 8775):
        add_candidate("127.0.0.1", port)

    for host, port in candidates:
        base_url = f"http://{host}:{port}"
        req = urllib.request.Request(base_url + "/status")
        try:
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                payload = json.load(resp)
            if payload.get("status") == "online":
                return {
                    "host": host,
                    "port": port,
                    "base_url": base_url,
                    "status": payload,
                }
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
            continue
    return None


def _summarize_call_assist(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = snapshot or {}
    preferred = snapshot.get("preferred") or {}
    active = snapshot.get("active_source") or {}
    active_name = str(snapshot.get("active_device_name") or active.get("device_name") or "").strip()
    preferred_name = str(preferred.get("device_name") or "").strip()
    meeting_label = snapshot.get("meeting_label")
    last_error = str(snapshot.get("last_error") or "").strip()
    silence_streak = int(snapshot.get("silence_streak") or 0)
    empty_transcript_streak = int(snapshot.get("empty_transcript_streak") or 0)
    caption_fallback_active = bool(snapshot.get("caption_fallback_active"))
    running = bool(snapshot.get("running"))
    has_audio_source = bool(active_name or preferred_name)
    last_transcript = str(snapshot.get("last_transcript") or "").strip()
    last_caption = str(snapshot.get("last_caption") or "").strip()
    last_suggestion = str(snapshot.get("last_suggestion") or "").strip()
    suggestion_source = str(snapshot.get("last_suggestion_source") or "").strip()
    interpreted_question = str(snapshot.get("last_interpreted_question") or "").strip()

    if interpreted_question:
        active_question = interpreted_question
        active_question_source = "interpreted"
    elif last_transcript:
        active_question = last_transcript
        active_question_source = "captions" if (caption_fallback_active and last_caption and last_caption == last_transcript) else "audio"
    elif last_caption:
        active_question = last_caption
        active_question_source = "captions"
    else:
        active_question = ""
        active_question_source = "none"

    transcript_text = active_question.strip()
    if not transcript_text:
        latest_transcript_state = "absent"
        latest_transcript_partial = None
    else:
        terminal = transcript_text[-1]
        if terminal in ".?!…":
            latest_transcript_state = "complete"
            latest_transcript_partial = False
        elif terminal in ",;:-" or transcript_text.lower().endswith((" and", " or", "but", "because", "so")):
            latest_transcript_state = "partial"
            latest_transcript_partial = True
        else:
            latest_transcript_state = "partial"
            latest_transcript_partial = True

    healthy = running and has_audio_source and not last_error
    degraded_reasons: list[str] = []
    if not running:
        degraded_reasons.append("not_running")
    if not has_audio_source:
        degraded_reasons.append("no_audio_source")
    if last_error:
        degraded_reasons.append("last_error")
    if silence_streak >= 3 and not caption_fallback_active:
        degraded_reasons.append("sustained_silence")
    if empty_transcript_streak >= 2 and not caption_fallback_active:
        degraded_reasons.append("empty_transcripts")

    if healthy:
        state = "HEALTHY"
    elif running:
        state = "DEGRADED"
    else:
        state = "STOPPED"

    summary_bits: list[str] = []
    if meeting_label:
        summary_bits.append(f"meeting={meeting_label}")
    if active_name:
        summary_bits.append(f"audio={active_name}")
    elif preferred_name:
        summary_bits.append(f"audio={preferred_name}")
    if caption_fallback_active:
        summary_bits.append("captions=active")
    if silence_streak:
        summary_bits.append(f"silence={silence_streak}")
    if empty_transcript_streak:
        summary_bits.append(f"empty={empty_transcript_streak}")
    if last_error:
        summary_bits.append(f"error={last_error}")

    return {
        "healthy": healthy,
        "state": state,
        "running": running,
        "meeting_label": meeting_label,
        "active_source_name": active_name,
        "active_source_kind": active.get("kind"),
        "preferred_source_name": preferred_name,
        "preferred_source_kind": preferred.get("kind"),
        "active_question": active_question,
        "active_question_source": active_question_source,
        "latest_transcript_state": latest_transcript_state,
        "latest_transcript_partial": latest_transcript_partial,
        "caption_fallback_active": caption_fallback_active,
        "silence_streak": silence_streak,
        "empty_transcript_streak": empty_transcript_streak,
        "last_error": last_error,
        "summary": " | ".join(summary_bits) if summary_bits else state,
        "degraded_reasons": degraded_reasons,
        "last_transcript": last_transcript,
        "last_caption": last_caption,
        "last_suggestion": last_suggestion,
        "suggestion_source": suggestion_source or ("captions" if caption_fallback_active else ("audio" if last_transcript else "unknown")),
        "sample_rate": snapshot.get("sample_rate"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def refresh_call_assist(force_refresh: bool = False) -> dict[str, Any]:
    if not force_refresh and _STATE.call_assist and _STATE.call_assist_updated_at:
        try:
            updated = datetime.fromisoformat(_STATE.call_assist_updated_at)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            if age < _CALL_ASSIST_CACHE_TTL:
                return dict(_STATE.call_assist)
        except Exception:
            pass

    try:
        import meeting_listener

        live_snapshot = meeting_listener.status_snapshot()
    except Exception as exc:
        live_snapshot = {
            "running": False,
            "last_error": str(exc),
        }

    summary = _summarize_call_assist(live_snapshot)
    update(call_assist=summary, call_assist_updated_at=summary.get("updated_at", ""))
    return summary


def call_assist_snapshot(force_refresh: bool = False) -> dict[str, Any]:
    if force_refresh or not _STATE.call_assist:
        return refresh_call_assist(force_refresh=force_refresh)
    return dict(_STATE.call_assist)
