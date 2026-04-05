"""
Self-improvement engine for Jarvis.

Jarvis can read its own source code, identify weaknesses, propose improvements,
apply changes, and restart itself. Every change is backed up first.

Triggered by:
"improve yourself"
"upgrade your routing"
"fix your memory system"
"make yourself better at coding tasks"
Automatically after detecting repeated failures in a conversation
"""

import os
import shutil
import subprocess
import sys
import difflib
import tempfile
import re
import traceback
from datetime import datetime
from brain_claude import ask_claude
from config import OPUS
import evals

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, ".jarvis_backups")
CRASH_LOG = os.path.join(BASE_DIR, ".jarvis_crash.log")
# Source files Jarvis is allowed to improve
IMPROVABLE_FILES = {
    "router.py":        "intent detection and tool routing logic",
    "model_router.py":  "smart model selection and cost optimization",
    "memory.py":        "memory storage and context retrieval",
    "learner.py":       "self-learning and knowledge extraction",
    "brain.py":         "OpenAI GPT interface",
    "brain_claude.py":  "Anthropic Claude interface",
    "brain_ollama.py":  "local Ollama model interface",
    "tools.py":         "system tools (search, apps, timers, system control)",
    "voice.py":         "speech recognition and text-to-speech",
    "config.py":        "model configuration and system prompt",
    "briefing.py":      "morning briefing and startup logic",
    "notes.py":         "note taking system",
    "terminal.py":      "file system and terminal execution",
    "google_services.py": "Google Calendar and Gmail integration",
    "camera.py":        "webcam and vision capabilities",
    "vault.py":         "local markdown vault indexing and retrieval",
    "wiki_builder.py":  "deterministic vault wiki compiler",
    "source_ingest.py": "source ingestion into the local vault",
    "skill_factory.py": "skill generation and eval promotion",
    "skills.py":        "local skill registry and loading",
    "ui.py":            "graphical interface, chat window, layout, colors, and controls",
    "self_improve.py":  "self-improvement and code editing engine",
    "stealth.py":       "screen share invisibility",
    "main.py":          "startup logic and entry point",
}
# -- Backup system -------------------------------------------------------------

def _backup(filename: str) -> str:
    """Create a timestamped backup of a file before modifying it."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = os.path.join(BASE_DIR, filename)
    dst = os.path.join(BACKUP_DIR, f"{filename}.{ts}.bak")
    shutil.copy2(src, dst)
    return dst


def list_backups(filename: str = None) -> list[str]:
    """List all backups, optionally filtered by filename."""
    if not os.path.exists(BACKUP_DIR):
        return []
    files = os.listdir(BACKUP_DIR)
    if filename:
        files = [f for f in files if f.startswith(filename)]
    return sorted(files, reverse=True)


def restore_backup(backup_name: str) -> str:
    """Restore a file from backup."""
    src = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.exists(src):
        return f"Backup not found: {backup_name}"
# Extract original filename (everything before the timestamp)
    parts = backup_name.rsplit(".", 3)
    original = parts[0] + "." + parts[1] if len(parts) >= 3 else backup_name
    dst = os.path.join(BASE_DIR, original)
    shutil.copy2(src, dst)
    return f"Restored {original} from {backup_name}."
# -- Diff and apply ------------------------------------------------------------

def _diff(original: str, updated: str, filename: str) -> str:
    """Generate a human-readable diff."""
    orig_lines = original.splitlines(keepends=True)
    new_lines = updated.splitlines(keepends=True)
    diff = difflib.unified_diff(orig_lines, new_lines, fromfile=f"{filename} (before)",
                                 tofile=f"{filename} (after)", lineterm="")
    return "".join(diff)


def _validate_python_code(filename: str, code: str) -> None:
    """Reject empty or syntactically invalid model output before touching disk."""
    if not code or not code.strip():
        raise ValueError(f"Generated code for {filename} was empty.")

    try:
        compile(code, f"<generated {filename}>", "exec")
    except SyntaxError as e:
        detail = f"line {e.lineno}: {e.msg}"
        lines = code.splitlines()
        if e.lineno and 0 < e.lineno <= len(lines):
            detail += f" near {lines[e.lineno - 1].strip()[:120]!r}"
        raise ValueError(f"Generated code for {filename} failed syntax validation at {detail}.") from e


def _sanitize_generated_code(code: str) -> str:
    """
    Normalize common model-output artifacts before syntax validation.
    This keeps decorative Unicode separators or smart punctuation from breaking
    otherwise valid generated Python.
    """
    replacements = str.maketrans({
        "\u2500": "-",
        "\u2014": "-",
        "\u2013": "-",
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
    })
    lines = []
    for raw_line in code.splitlines():
        line = raw_line.translate(replacements)
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and re.match(r"^[-]{2,}\s[A-Za-z].*$", stripped):
            line = "# " + stripped
        lines.append(line)
    return "\n".join(lines)


def _extract_syntax_error_line(error_text: str) -> int | None:
    match = re.search(r"line\s+(\d+)", error_text or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _heuristic_comment_fix(code: str, error_text: str, attempts: int = 3) -> str:
    """
    If the model emits plain-English section headings as bare code lines,
    comment those lines and retry locally before giving up.
    """
    lines = code.splitlines()
    for _ in range(attempts):
        try:
            compile("\n".join(lines), "<heuristic_fix>", "exec")
            return "\n".join(lines)
        except SyntaxError as exc:
            lineno = exc.lineno or _extract_syntax_error_line(error_text)
            if not lineno or lineno < 1 or lineno > len(lines):
                break
            candidate = lines[lineno - 1]
            stripped = candidate.strip()
            if not stripped or stripped.startswith("#"):
                break
            if re.match(r"^[A-Z][A-Za-z0-9 ,:_()/-]+$", stripped):
                lines[lineno - 1] = "# " + stripped
                continue
            break
    return "\n".join(lines)


def apply_improvement(filename: str, new_code: str) -> tuple[str, str]:
    """
    Apply new code to a file. Creates backup first.
    Returns (backup_path, diff_summary).
    """
    path = os.path.join(BASE_DIR, filename)
    with open(path, encoding="utf-8") as f:
        original = f.read()

    backup_path = _