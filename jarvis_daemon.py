from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import threading
import json
import urllib.request
import urllib.error

import api
import runtime_state
import task_runtime


_log = logging.getLogger(__name__)

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
    test_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{test_host}:{port}/status"
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
    try:
        existing = runtime_state.read_api_endpoint()
        if not existing:
            return False
        pid = existing.get("pid")
        if not pid or pid == os.getpid():
            return False
        # Verify the PID is actually alive before making an HTTP call.
        # This prevents a race where a freshly killed process's runtime.json
        # is still on disk while the new process starts.
        try:
            os.kill(pid, 0)  # signal 0 = liveness check, no signal sent
        except (ProcessLookupError, PermissionError):
            return False  # process is dead or unreadable
        base = existing.get("base_url", "")
        if base:
            with urllib.request.urlopen(f"{base}/status", timeout=1.5) as r:
                payload = json.load(r)
                return payload.get("status") == "online"
    except Exception:
        logging.debug("[Daemon] silent failure in _is_another_instance_running", exc_info=True)
    return False


# ── Apple Foundation Model (apfel) startup probe ───────────────────────────────

_APFEL_PORT = 11438
_APFEL_PROBE_URL = f"http://localhost:{_APFEL_PORT}/v1/models"


def _apfel_is_running() -> bool:
    """Return True if apfel is already serving on its expected port."""
    try:
        with urllib.request.urlopen(_APFEL_PROBE_URL, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _maybe_start_apfel() -> None:
    """Start apfel if enabled, installed, and not already running."""
    if os.getenv("JARVIS_APPLE_FOUNDATION_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return
    apfel_bin = shutil.which("apfel")
    if not apfel_bin:
        return
    if _apfel_is_running():
        return
    try:
        subprocess.Popen(
            [apfel_bin, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log.info("[AppleFoundation] Started apfel on :%d", _APFEL_PORT)
        print(f"[AppleFoundation] Started apfel on :{_APFEL_PORT}")
    except Exception as exc:
        _log.warning("[AppleFoundation] Failed to start apfel: %s", exc)


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

        try:
            import tunnel_manager
            tunnel_manager.start_tunnel(actual_port)
        except Exception as e:
            print(f"[Tunnel] Failed to initialize secure global tunnel: {e}")
        port_file = runtime_state.port_file_path()
        port_file.write_text(str(actual_port), encoding="utf-8")
        try:
            os.chmod(port_file, 0o600)
        except OSError:
            pass
        api_token = api.get_api_token()
        os.environ["JARVIS_API_TOKEN"] = api_token
        runtime_state.write_api_endpoint(actual_host, actual_port, token=api_token)
        if actual_host in {"0.0.0.0", "::"}:
            try:
                import hardware as _hw
                lan_ips = _hw.local_ipv4_addresses()
                if lan_ips:
                    print(f"[API] LAN approval page: http://{lan_ips[0]}:{actual_port}/pending")
            except Exception:
                logging.debug("[Daemon] silent failure in unknown", exc_info=True)
        runtime_state.mark_started(
            host=actual_host,
            port=actual_port,
            thread_name=getattr(_BOOT_THREAD, "name", ""),
            reason=reason,
        )
        _maybe_start_apfel()
        runtime_state.refresh_call_assist(force_refresh=True)
        return _BOOT_THREAD


def bootstrap_snapshot() -> dict:
    return runtime_state.snapshot()


def is_running() -> bool:
    return bool(_BOOT_THREAD and _BOOT_THREAD.is_alive())
