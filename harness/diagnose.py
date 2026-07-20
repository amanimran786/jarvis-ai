"""
harness/diagnose.py — Jarvis /diagnose command: full subsystem health check.

Runs 8 checks and returns an emoji-prefixed status table.  Each row shows
✅ healthy / ⚠️ degraded / ❌ broken with a one-line detail and, when broken,
an inline fix instruction.

Checks:
  1. Ollama        — reachable at localhost:11434, models loaded
  2. Memory files  — working/ episodic/ kb/ dirs exist and are non-empty
  3. Audit log     — logs/audit.jsonl exists, last entry < 1 h ago
  4. Google auth   — token.json valid; refresh token present if expired
  5. Budget status — harness.budget.status_text() summary
  6. Self-eval     — entry count + avg quality score of last 20
  7. Adaptive router — route_quality_report() from harness.adaptive_router
  8. Test suite    — pytest tests/ -q --tb=no; report pass/fail counts

Public API:
  run_diagnose() -> DiagnoseReport
  diagnose_text() -> str          — formatted output for /diagnose command
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ── Status constants ───────────────────────────────────────────────────────────

OK   = "ok"
WARN = "warn"
FAIL = "fail"

_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}

AUDIT_STALE_HOURS = 1   # flag audit log if last entry older than this


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    status: str        # "ok" | "warn" | "fail"
    detail: str        # one-line status shown in the table
    fix: str = ""      # shown only when status != ok
    extra: str = ""    # multi-line extra block (budget / router report)


@dataclass
class DiagnoseReport:
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


def _kb_dir() -> Path:
    return _base_dir() / "kb"


# ── 1. Ollama ──────────────────────────────────────────────────────────────────

def _check_ollama() -> CheckResult:
    """Verify Ollama is reachable and has at least one local model pulled."""
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
        short = [m.split(":")[0] for m in models[:4]]
        suffix = f" (+{len(models) - 4} more)" if len(models) > 4 else ""
        return CheckResult(
            name="Ollama",
            status=OK,
            detail=f"{len(models)} model(s): {', '.join(short)}{suffix}",
        )
    except Exception as exc:
        return CheckResult(
            name="Ollama",
            status=FAIL,
            detail=f"Unreachable — {_short(exc)}",
            fix="Start Ollama: open the Ollama app or run `ollama serve`",
        )


# ── 2. Memory files ────────────────────────────────────────────────────────────

def _check_memory() -> CheckResult:
    """Check that working/, episodic/ memory dirs and kb/ exist and are non-empty."""
    issues: list[str] = []
    counts: list[str] = []

    mem = _memory_dir()
    for subdir in ("working", "episodic"):
        d = mem / subdir
        if not d.exists():
            issues.append(f"memory/{subdir}/ missing")
        else:
            n = sum(1 for _ in d.iterdir())
            if n == 0:
                issues.append(f"memory/{subdir}/ empty")
            else:
                counts.append(f"{subdir}={n}")

    kb = _kb_dir()
    if not kb.exists():
        issues.append("kb/ directory missing")
    else:
        n = sum(1 for _ in kb.rglob("*") if _.is_file())
        if n == 0:
            issues.append("kb/ empty")
        else:
            counts.append(f"kb={n} files")

    if issues:
        return CheckResult(
            name="Memory",
            status=WARN,
            detail="; ".join(issues),
            fix="Run: python main.py --init  or restore from backup",
        )
    return CheckResult(
        name="Memory",
        status=OK,
        detail="  ".join(counts),
    )


# ── 3. Audit log ───────────────────────────────────────────────────────────────

def _check_audit_log() -> CheckResult:
    """Check audit.jsonl exists and last entry is within AUDIT_STALE_HOURS."""
    path = _logs_dir() / "audit.jsonl"
    if not path.exists():
        return CheckResult(
            name="Audit log",
            status=WARN,
            detail="logs/audit.jsonl not found",
            fix="Jarvis creates audit.jsonl automatically on first run",
        )

    # Find last valid timestamp
    last_ts: datetime | None = None
    total_lines = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    rec = json.loads(line)
                    ts_str = rec.get("ts") or rec.get("timestamp") or rec.get("time") or ""
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                except (json.JSONDecodeError, ValueError):
                    pass
    except OSError as exc:
        return CheckResult(
            name="Audit log",
            status=FAIL,
            detail=f"Cannot read — {_short(exc)}",
            fix=f"Check permissions: chmod 644 {path}",
        )

    if last_ts is None:
        size_kb = path.stat().st_size // 1024
        return CheckResult(
            name="Audit log",
            status=WARN,
            detail=f"Present ({size_kb} KB, {total_lines} lines) but no parseable timestamps",
        )

    age = datetime.now(timezone.utc) - last_ts
    age_mins = int(age.total_seconds() / 60)
    if age > timedelta(hours=AUDIT_STALE_HOURS):
        return CheckResult(
            name="Audit log",
            status=WARN,
            detail=f"Last entry {age_mins} min ago (stale >{AUDIT_STALE_HOURS}h)",
            fix="Jarvis may not be running — start the daemon to resume audit logging",
        )

    return CheckResult(
        name="Audit log",
        status=OK,
        detail=f"{total_lines} entries  last {age_mins} min ago",
    )


# ── 4. Google auth ─────────────────────────────────────────────────────────────

def _check_google_auth() -> CheckResult:
    """Check Google OAuth token without triggering a re-auth flow."""
    try:
        import google_services as _gs  # type: ignore[import]
        token_file  = Path(_gs.TOKEN_FILE)
        creds_file  = Path(_gs.CREDENTIALS_FILE)

        if not creds_file.exists():
            return CheckResult(
                name="Google auth",
                status=FAIL,
                detail="credentials.json not found",
                fix="Download OAuth credentials from Google Cloud Console and save to credentials.json",
            )
        if not token_file.exists():
            return CheckResult(
                name="Google auth",
                status=WARN,
                detail="token.json missing — auth required",
                fix="Run: python3 google_services.py --reauth",
            )

        try:
            from google.oauth2.credentials import Credentials  # type: ignore[import]
            creds = Credentials.from_authorized_user_file(str(token_file), _gs.SCOPES)
            if creds.valid:
                expiry = f"  expires {creds.expiry.strftime('%Y-%m-%d %H:%M UTC')}" if creds.expiry else ""
                return CheckResult(
                    name="Google auth",
                    status=OK,
                    detail=f"Valid{expiry}",
                )
            if creds.expired and creds.refresh_token:
                return CheckResult(
                    name="Google auth",
                    status=WARN,
                    detail="Token expired — refresh_token present, will auto-refresh on next call",
                    fix="Run a calendar/email query to trigger token refresh",
                )
            return CheckResult(
                name="Google auth",
                status=FAIL,
                detail="Token invalid and no refresh_token",
                fix="Run: python3 google_services.py --reauth",
            )
        except Exception as exc:
            return CheckResult(
                name="Google auth",
                status=WARN,
                detail=f"Could not parse token.json — {_short(exc)}",
                fix="Run: python3 google_services.py --reauth",
            )

    except ImportError:
        return CheckResult(
            name="Google auth",
            status=WARN,
            detail="google_services not importable",
        )
    except Exception as exc:
        return CheckResult(
            name="Google auth",
            status=WARN,
            detail=f"Check failed — {_short(exc)}",
        )


# ── 5. Budget status ───────────────────────────────────────────────────────────

def _check_budget() -> CheckResult:
    """Call budget.status_text() and condense to a one-line summary."""
    try:
        from harness.budget import status_text, check
        full_text = status_text()

        # Build a one-line summary: highlight any hard-limited providers
        hard_limited = []
        for provider in ("anthropic", "openai", "gemini", "ollama_cloud"):
            r = check(provider)
            if r.get("hard"):
                hard_limited.append(provider)

        if hard_limited:
            detail = f"HARD LIMIT reached: {', '.join(hard_limited)}"
            status = WARN
        else:
            detail = "All providers within limits"
            status = OK

        return CheckResult(
            name="Budget",
            status=status,
            detail=detail,
            extra=full_text,
        )
    except Exception as exc:
        return CheckResult(
            name="Budget",
            status=WARN,
            detail=f"Could not read budget — {_short(exc)}",
            fix="Check harness/budget.py and logs/budget.jsonl",
        )


# ── 6. Self-eval health ────────────────────────────────────────────────────────

def _check_self_eval() -> CheckResult:
    """Count self_eval.jsonl entries and report avg quality of last 20."""
    path = _logs_dir() / "self_eval.jsonl"
    if not path.exists():
        return CheckResult(
            name="Self-eval",
            status=WARN,
            detail="logs/self_eval.jsonl not found",
            fix="Run a few queries to generate self-eval data",
        )

    total = 0
    quality_scores: list[float] = []
    try:
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
                    q = rec.get("response_quality")
                    if q is not None:
                        try:
                            quality_scores.append(float(q))
                        except (TypeError, ValueError):
                            pass
                except (json.JSONDecodeError, ValueError):
                    pass
    except OSError as exc:
        return CheckResult(
            name="Self-eval",
            status=FAIL,
            detail=f"Cannot read — {_short(exc)}",
            fix=f"Check permissions: chmod 644 {path}",
        )

    if total == 0:
        return CheckResult(
            name="Self-eval",
            status=WARN,
            detail="File exists but no valid scored entries",
            fix="Run a few queries to generate self-eval data",
        )

    last20 = quality_scores[-20:] if quality_scores else []
    if last20:
        avg = sum(last20) / len(last20)
        avg_str = f"{avg:.2f}"
        avg_status = OK if avg >= 0.65 else WARN
    else:
        avg_str = "—"
        avg_status = WARN

    detail = f"{total} entries  avg quality (last 20): {avg_str}"
    fix = "Check worst interactions: /reflect or /diagnose --verbose" if avg_status == WARN else ""
    return CheckResult(
        name="Self-eval",
        status=avg_status,
        detail=detail,
        fix=fix,
    )


# ── 7. Adaptive router ─────────────────────────────────────────────────────────

def _check_adaptive_router() -> CheckResult:
    """Fetch route quality report; flag any demoted routes."""
    try:
        from harness.adaptive_router import route_quality_report, get_demoted_routes
        demoted = get_demoted_routes()
        report_text = route_quality_report(n=200)

        if demoted:
            detail = f"{len(demoted)} route(s) demoted: {', '.join(sorted(demoted))}"
            status = WARN
        else:
            detail = "All routes within quality threshold"
            status = OK

        return CheckResult(
            name="Router",
            status=status,
            detail=detail,
            extra=report_text,
        )
    except Exception as exc:
        return CheckResult(
            name="Router",
            status=WARN,
            detail=f"Could not read router quality — {_short(exc)}",
            fix="Check harness/adaptive_router.py and logs/self_eval.jsonl",
        )


# ── 8. Test suite ──────────────────────────────────────────────────────────────

def _check_tests() -> CheckResult:
    """Run pytest -q --tb=no and parse pass/fail counts. Timeout 60s."""
    try:
        project_root = str(_base_dir())
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", "--no-header"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=project_root,
        )
        output = result.stdout + result.stderr

        # Parse summary line: "5 passed, 2 failed in 3.14s" or "7 passed in 2.01s"
        passed = failed = errors = 0
        for line in output.splitlines():
            line_l = line.lower()
            if "passed" in line_l or "failed" in line_l or "error" in line_l:
                import re
                m_pass  = re.search(r"(\d+)\s+passed",  line_l)
                m_fail  = re.search(r"(\d+)\s+failed",  line_l)
                m_error = re.search(r"(\d+)\s+error",   line_l)
                if m_pass:  passed  = int(m_pass.group(1))
                if m_fail:  failed  = int(m_fail.group(1))
                if m_error: errors  = int(m_error.group(1))

        total_bad = failed + errors
        if total_bad > 0:
            return CheckResult(
                name="Tests",
                status=WARN,
                detail=f"{passed} passed  {failed} failed  {errors} errors",
                fix=f"Run: python3 -m pytest tests/ -q --tb=short  ({total_bad} need attention)",
            )

        if passed == 0:
            # Could be collection error
            return CheckResult(
                name="Tests",
                status=WARN,
                detail="pytest ran but collected 0 tests",
                fix="Check tests/ directory and pytest configuration",
            )

        return CheckResult(
            name="Tests",
            status=OK,
            detail=f"{passed} passed  0 failed",
        )

    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Tests",
            status=WARN,
            detail="pytest timed out after 60s",
            fix="Run manually: python3 -m pytest tests/ -q --tb=no",
        )
    except Exception as exc:
        return CheckResult(
            name="Tests",
            status=FAIL,
            detail=f"Could not run pytest — {_short(exc)}",
            fix="Ensure pytest is installed: pip install pytest",
        )


# ── Runner ─────────────────────────────────────────────────────────────────────

# Store function names as strings so patch() replacements are picked up at runtime.
_CHECKS: list[tuple[str, str]] = [
    ("Ollama",    "_check_ollama"),
    ("Memory",    "_check_memory"),
    ("Audit",     "_check_audit_log"),
    ("Google",    "_check_google_auth"),
    ("Budget",    "_check_budget"),
    ("Self-eval", "_check_self_eval"),
    ("Router",    "_check_adaptive_router"),
    ("Tests",     "_check_tests"),
]


def run_diagnose() -> DiagnoseReport:
    """Run all 8 subsystem checks and return a DiagnoseReport. Never raises."""
    import harness.diagnose as _self  # late import so patch() is visible
    results: list[CheckResult] = []
    for display_name, fn_name in _CHECKS:
        fn: Callable[[], CheckResult] = getattr(_self, fn_name)
        try:
            results.append(fn())
        except Exception as exc:
            log.warning("[diagnose] %s check raised: %s", display_name, exc)
            results.append(CheckResult(
                name=display_name,
                status=FAIL,
                detail=f"Check crashed — {_short(exc)}",
                fix="See logs for full traceback",
            ))
    return DiagnoseReport(
        results=results,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


# ── Formatter ──────────────────────────────────────────────────────────────────

def diagnose_text(include_extras: bool = True) -> str:
    """Run checks and return the full /diagnose text output."""
    report = run_diagnose()

    col = 10
    lines = [
        f"Jarvis /diagnose — {report.generated_at}",
        "",
        f"{'Subsystem':<{col}}  {'Status':<10}  Detail",
        "─" * 72,
    ]

    fixes: list[str] = []
    extras: list[tuple[str, str]] = []

    for r in report.results:
        icon = _ICON[r.status]
        lines.append(f"{r.name:<{col}}  {icon:<11}  {r.detail}")
        if r.fix:
            fixes.append(f"  [{r.name}] {r.fix}")
        if r.extra:
            extras.append((r.name, r.extra))

    lines.append("─" * 72)
    overall_icon = _ICON[report.overall]
    lines.append(f"Overall: {overall_icon} {report.overall.upper()}")

    if fixes:
        lines.append("")
        lines.append("Fix instructions:")
        lines.extend(fixes)

    if include_extras and extras:
        for name, body in extras:
            lines.append("")
            lines.append(f"── {name} detail ──")
            lines.extend(body.splitlines())

    return "\n".join(lines)


def _short(exc: Exception) -> str:
    return str(exc)[:80].rstrip()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="harness.diagnose")
    parser.add_argument("--no-extras", action="store_true",
                        help="Omit budget/router detail blocks")
    args = parser.parse_args(argv)
    print(diagnose_text(include_extras=not args.no_extras))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
