"""
Self-improvement engine for Jarvis.

Jarvis can read its own source code, identify weaknesses, propose improvements,
apply changes, and restart itself. Every change is backed up first.

Triggered by:
  - "improve yourself"
  - "upgrade your routing"
  - "fix your memory system"
  - "make yourself better at coding tasks"
  - Automatically after detecting repeated failures in a conversation
"""

import os
import shutil
import subprocess
import sys
import difflib
import tempfile
from datetime import datetime
from brain_claude import ask_claude
from config import OPUS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, ".jarvis_backups")

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
    "ui.py":            "graphical interface, chat window, layout, colors, and controls",
    "self_improve.py":  "self-improvement and code editing engine",
    "stealth.py":       "screen share invisibility",
    "main.py":          "startup logic and entry point",
}


# ── Backup system ─────────────────────────────────────────────────────────────

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


# ── Diff and apply ────────────────────────────────────────────────────────────

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


def apply_improvement(filename: str, new_code: str) -> tuple[str, str]:
    """
    Apply new code to a file. Creates backup first.
    Returns (backup_path, diff_summary).
    """
    path = os.path.join(BASE_DIR, filename)
    with open(path, encoding="utf-8") as f:
        original = f.read()

    _validate_python_code(filename, new_code)
    backup_path = _backup(filename)
    diff = _diff(original, new_code, filename)
    fd, temp_path = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=BASE_DIR, text=True)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_code)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    return backup_path, diff


# ── Self-analysis ─────────────────────────────────────────────────────────────

def read_source(filename: str) -> str:
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        return f"File not found: {filename}"
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_all_source() -> str:
    """Read all improvable source files into a single context string."""
    parts = []
    for filename, description in IMPROVABLE_FILES.items():
        content = read_source(filename)
        parts.append(f"### {filename} ({description})\n```python\n{content}\n```")
    return "\n\n".join(parts)


def analyze_weakness(area: str = None) -> str:
    """
    Ask Opus to analyze Jarvis's source code and identify the most impactful improvements.
    area: optional specific area to focus on (e.g. "routing", "memory", "voice")
    """
    focus = f"Focus specifically on: {area}." if area else "Identify the most impactful improvement across any file."

    # Read relevant files
    if area:
        relevant = {k: v for k, v in IMPROVABLE_FILES.items() if area.lower() in v.lower() or area.lower() in k.lower()}
        if not relevant:
            relevant = IMPROVABLE_FILES
    else:
        relevant = IMPROVABLE_FILES

    source_parts = []
    for filename in list(relevant.keys())[:5]:  # limit context
        content = read_source(filename)
        source_parts.append(f"### {filename}\n```python\n{content[:3000]}\n```")
    source = "\n\n".join(source_parts)

    prompt = f"""You are analyzing the source code of Jarvis, a personal AI assistant.
{focus}

Source code:
{source}

Identify the single most impactful improvement that would make Jarvis more capable,
efficient, or useful. Be specific about:
1. Which file to change
2. What the problem is
3. What the improvement is
4. Why it matters

Keep your response concise — 3-4 sentences max."""

    return ask_claude(prompt, model=OPUS)


def generate_improvement(filename: str, instruction: str) -> str:
    """
    Ask Opus to rewrite a specific file with an improvement applied.
    Returns the new code as a string.
    """
    current_code = read_source(filename)
    all_context = ""

    # Include related files for context
    for f in list(IMPROVABLE_FILES.keys())[:4]:
        if f != filename:
            content = read_source(f)
            all_context += f"\n\n### {f} (for context)\n```python\n{content[:1500]}\n```"

    prompt = f"""You are improving Jarvis, a personal AI assistant.

Task: {instruction}

Current {filename}:
```python
{current_code}
```

Related files for context:{all_context}

Rewrite {filename} with the improvement applied.
Rules:
- Keep all existing functionality intact unless explicitly told to remove it
- Make the improvement clean and well-integrated
- Do not add unnecessary complexity
- Return ONLY the complete new Python code for {filename}, nothing else
- No explanation, no markdown, just the raw Python code"""

    new_code = ask_claude(prompt, model=OPUS)

    # Strip markdown code fences — handle leading blank lines and language tags
    new_code = new_code.strip()
    if new_code.startswith("```"):
        lines = new_code.split("\n")
        # Drop opening fence (e.g. ```python)
        lines = lines[1:]
        # Drop closing fence if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        new_code = "\n".join(lines)

    return new_code


def self_improve(instruction: str = None, filename: str = None) -> dict:
    """
    Full self-improvement pipeline:
    1. Analyze code to find what to improve (if no instruction given)
    2. Generate improved code using Opus
    3. Back up original
    4. Apply changes
    5. Return summary for user approval/review

    Returns dict with: file, backup, diff, summary
    """
    # Step 1: figure out what to improve
    if not instruction:
        analysis = analyze_weakness()
        # Extract filename from analysis if not provided
        if not filename:
            for f in IMPROVABLE_FILES:
                if f in analysis:
                    filename = f
                    break
        if not filename:
            return {"error": analysis, "action_needed": "specify a file"}
        instruction = analysis
    elif not filename:
        # Try to infer filename from instruction
        for f in IMPROVABLE_FILES:
            name = f.replace(".py", "")
            if name in instruction.lower() or f in instruction.lower():
                filename = f
                break
        if not filename:
            filename = "router.py"  # default to routing improvements

    print(f"[Self-Improve] Improving {filename}: {instruction[:80]}...")

    # Step 2: generate improved code
    new_code = generate_improvement(filename, instruction)

    # Step 3 & 4: backup and apply
    try:
        backup_path, diff = apply_improvement(filename, new_code)
    except Exception as e:
        return {
            "error": f"Self-improve aborted before applying changes: {e}",
            "file": filename,
            "instruction": instruction,
        }

    return {
        "file": filename,
        "backup": os.path.basename(backup_path),
        "diff": diff,
        "instruction": instruction,
        "lines_changed": diff.count("\n+") + diff.count("\n-"),
    }


def restart_jarvis() -> None:
    """Restart the Jarvis process to load updated code."""
    print("[Self-Improve] Restarting Jarvis to apply changes...")
    subprocess.Popen([sys.executable] + sys.argv)
    sys.exit(0)
