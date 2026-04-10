"""
Deterministic behavior gates for dangerous Jarvis actions.

These are not advisory prompts. They run before and after specific tool
boundaries such as shell execution, file writes, and self-improve entry.
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOOK_LOG = ROOT / "hook_events.jsonl"
_LOCK = threading.Lock()

PROTECTED_PREFIXES = (
    "/System",
    "/Library",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/etc",
)

BLOCKED_COMMAND_PATTERNS = (
    "rm -rf",
    "rm -fr",
    "mkfs",
    "dd if=",
    "wipefs",
    "shred",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    ":(){:|:&};:",
)

HARD_BLOCKED_COMMAND_PATTERNS = (
    "mkfs",
    "dd if=",
    "wipefs",
    "shred",
    ":(){:|:&};:",
    "chmod -r 777 /",
    "chmod -r 000 /",
    "chown -r /",
)

MODIFY_VERBS = (
    "rm ",
    "mv ",
    "cp ",
    "chmod ",
    "chown ",
    "ln ",
    "tee ",
    ">",
    ">>",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(event: dict) -> None:
    payload = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        **event,
    }
    with _LOCK:
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path or ""))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def max_permissive_profile_enabled() -> bool:
    return _env_flag("JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE", False)


def _is_protected_path(path: str) -> bool:
    normalized = _normalize_path(path)
    return any(normalized == prefix or normalized.startswith(prefix + os.sep) for prefix in PROTECTED_PREFIXES)


def pre_shell_command(command: str, cwd: str | None = None, admin: bool = False) -> dict:
    lower = (command or "").lower().strip()
    patterns = HARD_BLOCKED_COMMAND_PATTERNS if max_permissive_profile_enabled() else BLOCKED_COMMAND_PATTERNS
    for pattern in patterns:
        if pattern in lower:
            result = {
                "ok": False,
                "reason": f"Blocked by behavior gate: '{pattern}' is not allowed.",
                "rule": "blocked_pattern",
            }
            _record({"phase": "pre_shell", "admin": admin, "command": command, "cwd": cwd or "", **result})
            return result

    if any(verb in lower for verb in MODIFY_VERBS):
        for prefix in PROTECTED_PREFIXES:
            if prefix.lower() in lower:
                if max_permissive_profile_enabled() and admin:
                    continue
                result = {
                    "ok": False,
                    "reason": (
                        f"Blocked by behavior gate: modifying protected system path {prefix} "
                        f"{'requires admin approval.' if max_permissive_profile_enabled() else 'is not allowed through Jarvis shell tools.'}"
                    ),
                    "rule": "protected_path_shell_requires_admin" if max_permissive_profile_enabled() else "protected_path_shell",
                }
                _record({"phase": "pre_shell", "admin": admin, "command": command, "cwd": cwd or "", **result})
                return result

    result = {"ok": True, "reason": "", "rule": "allowed"}
    _record({"phase": "pre_shell", "admin": admin, "command": command, "cwd": cwd or "", **result})
    return result


def post_shell_command(command: str, output: str, admin: bool = False) -> None:
    _record(
        {
            "phase": "post_shell",
            "admin": admin,
            "command": command,
            "ok": True,
            "output_preview": (output or "")[:300],
        }
    )


def pre_file_write(path: str, source: str = "", allow_protected: bool = False) -> dict:
    normalized = _normalize_path(path)
    permissive_allow_protected = max_permissive_profile_enabled() and _env_flag("JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES", False)
    if not allow_protected and not permissive_allow_protected and _is_protected_path(normalized):
        result = {
            "ok": False,
            "reason": f"Blocked by behavior gate: writing to protected system path {normalized} is not allowed.",
            "rule": "protected_path_write",
        }
        _record({"phase": "pre_write", "path": normalized, "source": source, **result})
        return result
    result = {"ok": True, "reason": "", "rule": "allowed"}
    _record({"phase": "pre_write", "path": normalized, "source": source, **result})
    return result


def post_file_write(path: str, source: str = "") -> dict:
    normalized = _normalize_path(path)
    if normalized.endswith(".py"):
        try:
            py_compile.compile(normalized, doraise=True)
        except Exception as exc:
            result = {
                "ok": False,
                "reason": f"Blocked by behavior gate: Python validation failed for {os.path.basename(normalized)}: {exc}",
                "rule": "python_validation",
            }
            _record({"phase": "post_write", "path": normalized, "source": source, **result})
            return result
    result = {"ok": True, "reason": "", "rule": "validated"}
    _record({"phase": "post_write", "path": normalized, "source": source, **result})
    return result


def pre_self_improve(target: str = "") -> dict:
    text = (target or "").strip()
    if text:
        mentioned = re.findall(r"\b[a-zA-Z0-9_/-]+\.py\b", text)
        disallowed = [name for name in mentioned if os.path.basename(name) not in {
            "router.py", "model_router.py", "memory.py", "learner.py", "brain.py",
            "brain_claude.py", "brain_ollama.py", "brain_gemini.py", "tools.py", "voice.py", "config.py",
            "briefing.py", "notes.py", "terminal.py", "google_services.py", "camera.py",
            "vault.py", "wiki_builder.py", "source_ingest.py", "skill_factory.py",
            "skills.py", "ui.py", "self_improve.py", "stealth.py", "main.py",
            "local_beta.py", "local_model_automation.py", "local_model_benchmark.py",
            "local_model_eval.py", "local_stt.py", "local_training.py", "local_tts.py",
        }]
        if disallowed:
            result = {
                "ok": False,
                "reason": f"Blocked by behavior gate: self-improve cannot target {', '.join(disallowed)}.",
                "rule": "self_improve_scope",
            }
            _record({"phase": "pre_self_improve", "target": target, **result})
            return result
    result = {"ok": True, "reason": "", "rule": "allowed"}
    _record({"phase": "pre_self_improve", "target": target, **result})
    return result


def summary(hours: int = 24) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    if not HOOK_LOG.exists():
        return {"hours": hours, "event_count": 0, "blocked_count": 0, "by_phase": {}}

    rows = []
    with _LOCK:
        with HOOK_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = datetime.fromisoformat(row["timestamp"])
                if ts >= cutoff:
                    rows.append(row)

    by_phase: dict[str, int] = {}
    blocked_count = 0
    for row in rows:
        phase = row.get("phase", "unknown")
        by_phase[phase] = by_phase.get(phase, 0) + 1
        if row.get("ok") is False:
            blocked_count += 1

    return {
        "hours": hours,
        "event_count": len(rows),
        "blocked_count": blocked_count,
        "by_phase": by_phase,
        "protected_prefixes": list(PROTECTED_PREFIXES),
    }


def status_text(hours: int = 24) -> str:
    data = summary(hours=hours)
    profile = "max-permissive local profile is ON. " if max_permissive_profile_enabled() else ""
    if data["event_count"] == 0:
        return (
            f"Behavior gates are active. {profile}I am guarding shell commands, admin shell commands, file writes, and self-improve entry, "
            "but no hook events have been recorded in that window yet."
        )
    phases = ", ".join(f"{name} {count}" for name, count in sorted(data["by_phase"].items()))
    return (
        f"Behavior gates are active. {profile}In the last {hours} hours I recorded {data['event_count']} hook events and blocked "
        f"{data['blocked_count']} of them. Activity by phase: {phases}. Protected system paths include "
        f"{', '.join(data['protected_prefixes'])}."
    )
