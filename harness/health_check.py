"""
harness/health_check.py — Jarvis subsystem health checker.

Checks six subsystems and reports ✅/⚠️/❌ status with fix instructions.

Subsystems checked:
  1. Ollama        — reachable, models loaded
  2. Memory        — memory/ dirs exist and readable
  3. Audit log     — logs/audit.jsonl writable
  4. Google auth   — token.json present and not expired
  5. Budget log    — logs/budget.jsonl present
  6. Self-eval     — logs/self_eval.jsonl has recent entries (< 48h)

Public API:
  run_checks() -> HealthReport
  health_text() -> str            — for /diagnose command
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ── Status constants ───────────────────────────────────────────────────────────

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    status: str          # "ok" | "warn" | "fail"
    detail: str          # one-line detail shown in the table
    fix: str = ""        # fix instruction shown only when status != ok


@dataclass
class HealthReport:
    results: list[CheckResult] = field(default_factory=list)
    generated_at: str = ""

    @property
    def overall(self) -> str:
        if any(r.status == FAIL for r in self.results):
            return FAIL
        if any(r.status == WARN for r in self.results):
            return WARN
        return OK

    def by_name(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)


# ── Path helpers ───────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    try:
        from harness.audit import _base_dir as _ab
        return _ab()
    except Exception:
        return Path(__file__).resolve().parent.parent


def _logs_dir() -> Path:
    return _base_dir() / "logs"


def _memory_dir() -> Path:
    return _base_dir() / "memory"


# ── Individual checks ──────────────────────────────────────────────────────────

def _check_ollama() -> CheckResult:
    """Check that Ollama is reachable and has at least one model loaded."""
    try:
        from brains.brain_ollama import list_local_models
        models = list_local_models()
        if not models:
            return CheckResult(
                name="Ollama",
                status=WARN,
                detail="Reachable but no models loaded",
                fix="Run: ollama pull qwen3:30b-a3b",
            )
        short_names = [m.split(":")[0] for m in models[:3]]
        suffix = f" (+{len(models) - 3} more)" if len(models) > 3 else ""
        return CheckResult(
            name="Ollama",
            status=OK,
            detail=f"{len(models)} model(s): {', '.join(short_names)}{suffix}",
        )
    except Exception as exc:
        return CheckResult(
            name="Ollama",
            status=FAIL,
            detail=f"Unreachable — {_short(exc)}",
            fix="Start Ollama: open the Ollama app or run `ollama serve`",
        )


def _check_memory() -> CheckResult:
    """Check that memory/ directories exist and identity.md is present."""
    base = _base_dir()
    mem_dir = _memory_dir()
    identity = base / "kb" / "core" / "identity.md"

    issues = []
    if not mem_dir.exists():
        issues.append("memory/ directory missing")
    else:
        required_subdirs = ("episodic", "semantic", "working")
        missing = [d for d in required_subdirs if not (mem_dir / d).exists()]
        if missing:
            issues.append(f"missing subdirs: {', '.join(missing)}")

    if not identity.exists():
        issues.append("kb/core/identity.md missing")

    if issues:
        return CheckResult(
            name="Memory",
            status=WARN,
            detail="; ".join(issues),
            fix="Run: python main.py --init  or restore from backup",
        )

    # Count episodic files as a rough proxy for memory health
    ep_count = len(list((mem_dir / "episodic").glob("*.json")))
    sem_count = len(list((mem_dir / "semantic").glob("*")))
    return CheckResult(
        name="Memory",
        status=OK,
        detail=f"identity.md ok  episodic={ep_count}  semantic={sem_count}",
    )


def _check_audit_log() -> CheckResult:
    """Check that logs/audit.jsonl is writable."""
    path = _logs_dir() / "audit.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Probe write: open in append mode and write nothing
        with open(path, "a", encoding="utf-8"):
            pass
        # Check size / last modified
        if path.exists():
            size_kb = path.stat().st_size // 1024
            return CheckResult(
                name="Audit log",
                status=OK,
                detail=f"Writable ({size_kb} KB)",
            )
        return CheckResult(
            name="Audit log",
            status=OK,
            detail="Writable (empty)",
        )
    except OSError as exc:
        return CheckResult(
            name="Audit log",
            status=FAIL,
            detail=f"Not writable — {_short(exc)}",
            fix=f"Check permissions: chmod 644 {path}",
        )


def _check_google_auth() -> CheckResult:
    """Check Google OAuth token validity without triggering a re-auth flow."""
    try:
        import google_services as _gs
        token_file = Path(_gs.TOKEN_FILE)
        creds_file = Path(_gs.CREDENTIALS_FILE)

        if not creds_file.exists():
            return CheckResult(
                name="Google auth",
                status=FAIL,
                detail="credentials.json not found",
                fix=(
                    f"Download OAuth credentials from Google Cloud Console "
                    f"and save to {creds_file}"
                ),
            )

        if not token_file.exists():
            return CheckResult(
                name="Google auth",
                status=WARN,
                detail="token.json missing — auth required",
                fix="Run: python google_services.py --reauth",
            )

        # Load the token and inspect validity without actually refreshing
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(token_file), _gs.SCOPES)
            if creds.valid:
                expiry_str = ""
                if creds.expiry:
                    expiry_str = f"  expires {creds.expiry.strftime('%Y-%m-%d %H:%M UTC')}"
                return CheckResult(
                    name="Google auth",
                    status=OK,
                    detail=f"Valid{expiry_str}",
                )
            if creds.expired and creds.refresh_token:
                return CheckResult(
                    name="Google auth",
                    status=WARN,
                    detail="Token expired but refresh_token present — will auto-refresh",
                    fix="Run a calendar or email query to trigger refresh",
                )
            return CheckResult(
                name="Google auth",
                status=FAIL,
                detail="Token invalid and no refresh_token",
                fix="Run: python google_services.py --reauth",
            )
        except Exception as exc:
            return CheckResult(
                name="Google auth",
                status=WARN,
                detail=f"Could not parse token.json — {_short(exc)}",
                fix="Run: python google_services.py --reauth",
            )

    except ImportError:
        return CheckResult(
            name="Google auth",
            status=WARN,
            detail="google_services not available (packaged app?)",
        )
    except Exception as exc:
        return CheckResult(
            name="Google auth",
            status=WARN,
            detail=f"Check failed — {_short(exc)}",
        )


def _check_budget_log() -> CheckResult:
    """Check that logs/budget.jsonl exists and is readable."""
    path = _logs_dir() / "budget.jsonl"
    if not path.exists():
        return CheckResult(
            name="Budget log",
            status=WARN,
            detail="logs/budget.jsonl not found — no budget tracking yet",
            fix="Budget is created automatically when Jarvis runs queries",
        )
    try:
        size_kb = path.stat().st_size // 1024
        # Try reading last line to confirm parseable
        last_entry = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last_entry = line
        if last_entry:
            json.loads(last_entry)  # validate last entry is valid JSON
        return CheckResult(
            name="Budget log",
            status=OK,
            detail=f"Present and readable ({size_kb} KB)",
        )
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="Budget log",
            status=WARN,
            detail=f"Present but last entry malformed — {_short(exc)}",
            fix="Budget log will self-repair on next query",
        )


def _check_self_eval() -> CheckResult:
    """Check that self_eval.jsonl exists and has entries within the last 48h."""
    path = _logs_dir() / "self_eval.jsonl"
    if not path.exists():
        return CheckResult(
            name="Self-eval",
            status=WARN,
            detail="logs/self_eval.jsonl not found",
            fix="Run a few queries to generate self-eval data",
        )
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        total = 0
        recent = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("error"):
                        continue
                    total += 1
                    ts_str = rec.get("ts", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            recent += 1
                except (json.JSONDecodeError, ValueError):
                    pass

        if total == 0:
            return CheckResult(
                name="Self-eval",
                status=WARN,
                detail="File exists but no valid scored entries",
                fix="Run a few queries to generate self-eval data",
            )
        if recent == 0:
            return CheckResult(
                name="Self-eval",
                status=WARN,
                detail=f"{total} total entries, none in last 48h",
                fix="Jarvis may not have been active recently — run a query to verify",
            )
        return CheckResult(
            name="Self-eval",
            status=OK,
            detail=f"{total} total entries  {recent} in last 48h",
        )
    except OSError as exc:
        return CheckResult(
            name="Self-eval",
            status=FAIL,
            detail=f"Cannot read — {_short(exc)}",
            fix=f"Check permissions: chmod 644 {path}",
        )


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_checks() -> HealthReport:
    """Run all subsystem checks and return a HealthReport. Never raises."""
    # Build inside the function so patches to module-level names take effect.
    _check_pairs = [
        ("Ollama", _check_ollama),
        ("Memory", _check_memory),
        ("Audit log", _check_audit_log),
        ("Google auth", _check_google_auth),
        ("Budget log", _check_budget_log),
        ("Self-eval", _check_self_eval),
    ]
    results: list[CheckResult] = []
    for _name, check_fn in _check_pairs:
        try:
            results.append(check_fn())
        except Exception as exc:
            log.warning("[health_check] %s raised unexpectedly: %s", _name, exc)
            results.append(CheckResult(
                name=_name,
                status=FAIL,
                detail=f"Check crashed — {_short(exc)}",
                fix="See logs for traceback",
            ))
    return HealthReport(
        results=results,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


# ── Formatting ─────────────────────────────────────────────────────────────────

def health_text(include_score_report: bool = True) -> str:
    """Full /diagnose output: health table + optional worst-interactions summary."""
    report = run_checks()

    # Table header
    col_w = 12
    lines = [
        f"Jarvis health check — {report.generated_at}",
        "",
        f"{'Subsystem':<{col_w}}  {'Status':<8}  Detail",
        "─" * 70,
    ]

    fixes: list[str] = []
    for r in report.results:
        icon = _ICON[r.status]
        lines.append(f"{r.name:<{col_w}}  {icon:<9}  {r.detail}")
        if r.fix:
            fixes.append(f"  [{r.name}] {r.fix}")

    lines.append("─" * 70)
    overall_icon = _ICON[report.overall]
    lines.append(f"Overall: {overall_icon} {report.overall.upper()}")

    if fixes:
        lines.append("")
        lines.append("Fix:")
        lines.extend(fixes)

    # Append self-eval quality report as a second section
    if include_score_report:
        try:
            from harness import self_eval_log
            score_text = self_eval_log.diagnose_report(n=50, worst_n=3)
            if score_text:
                lines.append("")
                lines.append("─" * 70)
                lines.append(score_text)
        except Exception:
            pass

    return "\n".join(lines)


def _short(exc: Exception) -> str:
    return str(exc)[:80].rstrip()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="harness.health_check")
    parser.add_argument("--no-score", action="store_true",
                        help="Skip self-eval quality section")
    parser.parse_args(argv)
    print(health_text(include_score_report=not parser.parse_args(argv).no_score))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
