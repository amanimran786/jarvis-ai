from __future__ import annotations

import os
import time
import threading
import json
import urllib.request
import urllib.error

import api
import runtime_state
import task_runtime


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
    url = f"http://{host}:{port}/status"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                payload = json.load(resp)
            if payload.get("status") == "online":
                return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            last_error = str(exc)
            time.sleep(0.1)
    if last_error:
        runtime_state.update(last_error=f"api readiness timeout: {last_error}")
    return False


def _is_another_instance_running() -> bool:
    """Return True if a healthy Jarvis API is already running (different process)."""
    if os.getenv("JARVIS_ALLOW_PARALLEL_INSTANCE", "").lower() in {"1", "true", "yes", "on"}:
        return False
    try:
        existing = runtime_state.read_api_endpoint()
        if not existing:
            return False
        pid = existing.get("pid")
        if pid and pid != os.getpid():
            base = existing.get("base_url", "")
            if base:
                with urllib.request.urlopen(f"{base}/status", timeout=1.5) as r:
                    payload = json.load(r)
                    return payload.get("status") == "online"
    except Exception:
        pass
    return False


def start_daemon(host: str | None = None, port: int | None = None, reason: str = "bootstrap") -> threading.Thread:
    """
    Start the local API daemon once and record basic runtime state.
    """
    global _BOOT_THREAD

    resolved_host, resolved_port = _resolve_host_port(host=host, port=port)
    task_runtime.bootstrap()

    # Guard: don't clobber the runtime meta if a healthy instance already exists
    if _is_another_instance_running():
        print("[Daemon] Another Jarvis instance is already running. Skipping startup.")
        return threading.current_thread()

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
        port_file = runtime_state.port_file_path()
        port_file.write_text(str(actual_port), encoding="utf-8")
        try:
            os.chmod(port_file, 0o600)
        except OSError:
            pass
        os.environ["JARVIS_API_TOKEN"] = api.get_api_token()
        runtime_state.write_api_endpoint(actual_host, actual_port)
        if actual_host in {"0.0.0.0", "::"}:
            try:
                import hardware as _hw
                lan_ips = _hw.local_ipv4_addresses()
                if lan_ips:
                    print(f"[API] LAN approval page: http://{lan_ips[0]}:{actual_port}/pending")
            except Exception:
                pass
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
