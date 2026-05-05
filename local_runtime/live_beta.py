"""
Live beta feedback loop for Jarvis.

This module records runtime interactions and UI feedback into a lightweight
JSONL stream, and (optionally) triggers a small local beta suite in the
background to keep the loop tight while the user is actively testing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import evals
from local_runtime import local_beta


_LOCK = threading.Lock()
_STATE = {
    "enabled": os.getenv("JARVIS_LIVE_BETA", "1").strip().lower() in {"1", "true", "yes", "on"},
    "autorun": os.getenv("JARVIS_LIVE_BETA_AUTORUN", "0").strip().lower() in {"1", "true", "yes", "on"},
    "train_pack": os.getenv("JARVIS_LIVE_BETA_TRAIN_PACK", "0").strip().lower() in {"1", "true", "yes", "on"},
    "last_run": 0.0,
    "running": False,
    "events": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root_dir() -> Path:
    try:
        return local_beta.ROOT
    except Exception:
        return Path.home() / "Library" / "Application Support" / "Jarvis" / "training" / "beta"


def _ensure_dir() -> Path:
    root = _root_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _events_path() -> Path:
    return _ensure_dir() / "live_feedback.jsonl"


def _runs_path() -> Path:
    return _ensure_dir() / "live_runs.jsonl"


def is_enabled() -> bool:
    return bool(_STATE.get("enabled"))


def set_enabled(enabled: bool) -> dict:
    with _LOCK:
        _STATE["enabled"] = bool(enabled)
    return status()


def set_autorun(enabled: bool) -> dict:
    with _LOCK:
        _STATE["autorun"] = bool(enabled)
    return status()


def status() -> dict:
    with _LOCK:
        last_run = _STATE.get("last_run", 0.0)
        running = _STATE.get("running", False)
        events = _STATE.get("events", 0)
        autorun = _STATE.get("autorun", False)
        enabled = _STATE.get("enabled", False)
    since = round(time.time() - last_run, 1) if last_run else None
    return {
        "ok": True,
        "enabled": enabled,
        "autorun": autorun,
        "training_pack": bool(_STATE.get("train_pack")),
        "running": running,
        "events": events,
        "last_run_seconds_ago": since,
        "root": str(_root_dir()),
    }


def _append_jsonl(path: Path, payload: dict) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def record_interaction(entry: dict, *, source: str = "live_beta") -> None:
    if not is_enabled() or not entry:
        return
    payload = {
        "timestamp": _now_iso(),
        "event": "interaction",
        "source": source,
        "interaction_id": entry.get("id"),
        "user_input": entry.get("user_input"),
        "response": entry.get("response"),
        "model": entry.get("model"),
        "context": entry.get("context", {}),
    }
    _append_jsonl(_events_path(), payload)
    _bump_events()


def record_feedback(
    *,
    interaction: dict | None,
    rating: int,
    issue: str = "",
    expected: str = "",
    source: str = "live_beta_ui",
) -> None:
    if not is_enabled():
        return
    payload = {
        "timestamp": _now_iso(),
        "event": "feedback",
        "source": source,
        "rating": int(rating),
        "issue": issue,
        "expected": expected,
        "interaction_id": interaction.get("id") if interaction else "",
        "model": interaction.get("model") if interaction else "",
        "user_input": interaction.get("user_input") if interaction else "",
    }
    _append_jsonl(_events_path(), payload)
    _bump_events()

    if rating < 0:
        evals.log_failure(
            issue=issue or "Thumbs down rating",
            interaction_id=interaction.get("id") if interaction else None,
            expected=expected,
            source=source,
        )
        _maybe_run_beta(reason="negative_feedback")


def _bump_events() -> None:
    with _LOCK:
        _STATE["events"] = int(_STATE.get("events", 0)) + 1
    if _STATE.get("autorun"):
        if _STATE["events"] % 6 == 0:
            _maybe_run_beta(reason="event_batch")


def _maybe_run_beta(*, reason: str = "manual", suite: str = "engineering") -> None:
    if not _STATE.get("autorun"):
        return
    with _LOCK:
        if _STATE.get("running"):
            return
        _STATE["running"] = True
        _STATE["last_run"] = time.time()

    def _runner():
        try:
            result = local_beta.run_beta_suite(
                suite=suite,
                limit=10,
                include_browser=False,
                log_failures=True,
                build_training_pack=bool(_STATE.get("train_pack")),
            )
            payload = {
                "timestamp": _now_iso(),
                "event": "beta_run",
                "reason": reason,
                "suite": suite,
                "result": {
                    "ok": result.get("ok"),
                    "passed": result.get("passed"),
                    "failed": result.get("failed"),
                    "path": result.get("path"),
                },
            }
            _append_jsonl(_runs_path(), payload)
        finally:
            with _LOCK:
                _STATE["running"] = False

    threading.Thread(target=_runner, daemon=True, name="JarvisLiveBeta").start()
