from __future__ import annotations

import os
import socket
import time
import threading

import api
import runtime_state


_BOOT_LOCK = threading.Lock()
_BOOT_THREAD: threading.Thread | None = None


def _resolve_host_port(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    resolved_host = (host or os.getenv("JARVIS_API_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    raw_port = port if port is not None else os.getenv("JARVIS_API_PORT", "8765")
    try:
        resolved_port = int(raw_port)
    except (TypeError, ValueError):
        resolved_port = 8765
    return resolved_host, resolved_port


def _wait_for_api_ready(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.1)
    if last_error:
        runtime_state.update(last_error=f"api readiness timeout: {last_error}")
    return False


def start_daemon(host: str | None = None, port: int | None = None, reason: str = "bootstrap") -> threading.Thread:
    """
    Start the local API daemon once and record basic runtime state.
    """
    global _BOOT_THREAD

    resolved_host, resolved_port = _resolve_host_port(host=host, port=port)

    with _BOOT_LOCK:
        if _BOOT_THREAD and _BOOT_THREAD.is_alive():
            actual_host = api.get_host()
            actual_port = api.get_port()
            if not _wait_for_api_ready(actual_host, actual_port):
                runtime_state.update(
                    status="STARTING",
                    api_host=actual_host,
                    api_port=actual_port,
                    api_running=True,
                    api_thread_name=_BOOT_THREAD.name,
                    boot_reason=reason,
                )
                return _BOOT_THREAD
            runtime_state.update(
                status="ONLINE",
                api_host=actual_host,
                api_port=actual_port,
                api_running=True,
                api_thread_name=_BOOT_THREAD.name,
                boot_reason=reason,
            )
            return _BOOT_THREAD

        runtime_state.update(
            status="STARTING",
            api_host=resolved_host,
            api_port=resolved_port,
            api_running=False,
            api_thread_name="",
            boot_reason=reason,
            last_error="",
        )

        _BOOT_THREAD = api.start(host=resolved_host, port=resolved_port)
        actual_host = api.get_host()
        actual_port = api.get_port()
        if not _wait_for_api_ready(actual_host, actual_port):
            runtime_state.mark_error(f"API did not become ready at http://{actual_host}:{actual_port}")
            return _BOOT_THREAD
        runtime_state.mark_started(
            host=actual_host,
            port=actual_port,
            thread_name=getattr(_BOOT_THREAD, "name", ""),
            reason=reason,
        )
        runtime_state.refresh_call_assist(force_refresh=True)
        return _BOOT_THREAD


def bootstrap_snapshot() -> dict:
    return runtime_state.snapshot()


def is_running() -> bool:
    return bool(_BOOT_THREAD and _BOOT_THREAD.is_alive())
