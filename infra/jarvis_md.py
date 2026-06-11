"""
infra/jarvis_md.py — Automated JARVIS.md read/append system.

Agents call this after a QA failure or novel fix to propose a regression entry.
The entry is returned as a string (never auto-written) — callers must explicitly
append via append_regression() after human approval.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger("jarvis.infra.jarvis_md")

_JARVIS_MD = Path(__file__).parent.parent / "JARVIS.md"

_REGRESSION_MARKER = "## Regression Log"
_FAILURES_MARKER = "## Known Failure Patterns"


def load() -> str:
    """Return full JARVIS.md contents, or empty string if unavailable."""
    try:
        return _JARVIS_MD.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("JARVIS.md not readable: %s", exc)
        return ""


def get_invariants() -> str:
    """Extract the Architecture Invariants section for agent briefing."""
    text = load()
    match = re.search(r"## Architecture Invariants\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def get_failure_patterns() -> str:
    """Extract Known Failure Patterns section."""
    text = load()
    match = re.search(r"## Known Failure Patterns\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def propose_failure_entry(*, agent: str, pattern: str, fix: str) -> str:
    """
    Format a new Known Failure Patterns entry.
    Returns a markdown string; never writes to disk.
    """
    return (
        f"\n### [{agent}] {pattern[:80]}\n"
        f"Fix: {fix}\n"
    )


def propose_regression_entry(*, agent: str, what_failed: str, fix_applied: str, when: str | None = None) -> str:
    """
    Format a Regression Log entry in the canonical format.
    Returns a markdown string; never writes to disk.
    """
    when = when or date.today().isoformat()
    return f"- [{when}] [{agent}] {what_failed} → {fix_applied}\n"


def append_failure_pattern(entry: str) -> bool:
    """
    Append a failure pattern entry to JARVIS.md under Known Failure Patterns.
    Returns True on success.

    Caller must have already obtained human approval before calling this.
    """
    text = load()
    if _FAILURES_MARKER not in text:
        logger.error("JARVIS.md missing '%s' marker — cannot append.", _FAILURES_MARKER)
        return False

    # Insert before the Regression Log section (or at end of failures section)
    insert_before = _REGRESSION_MARKER if _REGRESSION_MARKER in text else None
    if insert_before:
        idx = text.index(insert_before)
        new_text = text[:idx] + entry.rstrip("\n") + "\n\n" + text[idx:]
    else:
        new_text = text.rstrip("\n") + "\n" + entry + "\n"

    try:
        _JARVIS_MD.write_text(new_text, encoding="utf-8")
        return True
    except OSError as exc:
        logger.error("Failed to write JARVIS.md: %s", exc)
        return False


def append_regression(entry: str) -> bool:
    """
    Append a regression log entry to JARVIS.md under Regression Log.
    Returns True on success.

    Caller must have already obtained human approval before calling this.
    """
    text = load()
    if _REGRESSION_MARKER not in text:
        logger.error("JARVIS.md missing '%s' marker — cannot append.", _REGRESSION_MARKER)
        return False

    new_text = text.rstrip("\n") + "\n" + entry
    try:
        _JARVIS_MD.write_text(new_text, encoding="utf-8")
        return True
    except OSError as exc:
        logger.error("Failed to write JARVIS.md: %s", exc)
        return False


def log_regression_auto(*, agent: str, what_failed: str, fix_applied: str) -> bool:
    """
    Compose and immediately append a regression entry.
    Use only from automated post-QA hooks where human already approved the fix.
    """
    entry = propose_regression_entry(agent=agent, what_failed=what_failed, fix_applied=fix_applied)
    return append_regression(entry)
