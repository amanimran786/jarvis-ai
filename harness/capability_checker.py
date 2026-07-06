"""Runtime capability probes for typed task contracts."""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.error
import urllib.request

from harness.task_contract import Capability, TaskContract


log = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 3
_SUBPROCESS_TIMEOUT_SECONDS = 5
_UNPROBEABLE = {Capability.CALENDAR, Capability.IMESSAGE, Capability.SCREEN}


def _check_http(url: str, method: str, *, accept_http_error: bool = False) -> bool:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS):
            return True
    except urllib.error.HTTPError as exc:
        if accept_http_error:
            log.debug("Capability HTTP probe reached %s with status %s", url, exc.code)
            return True
        log.debug("Capability HTTP probe failed for %s: %s", url, exc)
        return False
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        log.debug("Capability HTTP probe failed for %s: %s", url, exc)
        return False


def _check_filesystem(working_directory: str) -> bool:
    return os.access(working_directory, os.R_OK) and os.access(
        working_directory, os.W_OK
    )


def _check_subprocess(args: list[str]) -> bool:
    try:
        result = subprocess.run(
            args,
            shell=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("Capability subprocess probe failed for %s: %s", args[0], exc)
        return False
    return result.returncode == 0


def check_capability(cap: Capability) -> bool:
    """Return whether a capability is currently available."""
    if cap is Capability.OLLAMA:
        return _check_http("http://localhost:11434/api/tags", "GET")
    if cap is Capability.FILESYSTEM:
        return _check_filesystem(os.getcwd())
    if cap is Capability.INTERNET:
        return _check_http(
            "https://api.openai.com",
            "HEAD",
            accept_http_error=True,
        )
    if cap is Capability.GIT:
        return _check_subprocess(["git", "--version"])
    if cap is Capability.PYTHON:
        return _check_subprocess(["python3", "--version"])
    if cap is Capability.VOICE:
        try:
            import sounddevice  # type: ignore[import-not-found]

            sounddevice.query_devices()
            return True
        except Exception as exc:
            log.debug("Voice capability probe failed: %s", exc)
            return False
    if cap in _UNPROBEABLE:
        log.info("Capability %s is not yet probeable", cap.value)
        return False
    return False


def check_contract_capabilities(contract: TaskContract) -> dict[str, bool]:
    """Probe each capability required by a typed task contract."""
    results: dict[str, bool] = {}
    for cap in contract.requires_capabilities:
        try:
            if cap is Capability.FILESYSTEM:
                results[cap.value] = _check_filesystem(contract.working_directory)
            else:
                results[cap.value] = check_capability(cap)
        except Exception as exc:
            log.warning("Capability probe failed for %s: %s", cap.value, exc)
            results[cap.value] = False
    return results
