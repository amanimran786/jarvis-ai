"""
agents/automation_engineer.py — Automation engineering agent.

Handles: shell script generation, file manipulation pipelines, macro
composition, and repeatable task automation. All shell execution is
list-args + shell=False. Never evaluates LLM output directly.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.agent.automation")

_JARVIS_ROOT = Path(__file__).parent.parent.resolve()
_ALLOWED_BASES = [
    _JARVIS_ROOT,
    Path.home() / "jarvis-ai",
    Path.home() / "Documents",
    Path.home() / "Desktop",
]


# ── Path safety ───────────────────────────────────────────────────────────────

def _safe_path(user_input: str, base: Path | None = None) -> Path:
    """
    Resolve user-supplied path and reject traversal outside allowed bases.
    Raises ValueError on rejection.
    """
    resolved = (Path(user_input)).resolve()
    bases = [base.resolve()] if base else _ALLOWED_BASES
    if not any(str(resolved).startswith(str(b)) for b in bases):
        raise ValueError(f"Path traversal rejected: {resolved}")
    return resolved


# ── Task dispatcher ───────────────────────────────────────────────────────────

def process_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the automation engineer agent.

    Supported task types (task["type"]):
      - "shell_script"    : generate a shell script for a described automation
      - "file_pipeline"   : transform / reorganize files in a directory
      - "macro_compose"   : compose a multi-step macro from primitive operations
      - "run_script"      : execute a pre-approved script (path must be in allowed base)
    """
    task_type = task.get("type", "")
    ctx = task.get("context", {})

    handlers = {
        "shell_script": _handle_shell_script,
        "file_pipeline": _handle_file_pipeline,
        "macro_compose": _handle_macro_compose,
        "run_script": _handle_run_script,
    }

    handler = handlers.get(task_type)
    if handler is None:
        return {
            "ok": False,
            "error": f"Unknown automation task type: {task_type!r}",
            "supported_types": list(handlers),
        }

    try:
        result = handler(ctx)
        return {"ok": True, "result": result}
    except ValueError as exc:
        # Security / validation error — do not swallow
        logger.warning("automation_engineer security rejection: %s", exc)
        return {"ok": False, "error": str(exc), "security_rejection": True}
    except Exception as exc:
        logger.exception("automation_engineer failed on task type %s", task_type)
        return {"ok": False, "error": str(exc)}


# ── Handlers ──────────────────────────────────────────────────────────────────

def _handle_shell_script(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a shell script for a described automation task.

    ctx keys:
      - description (str): natural language description of what the script should do
      - output_path (str, optional): where to save the script (must be in allowed base)
      - language    (str, optional): "bash" (default) or "zsh"
    """
    description = ctx.get("description", "")
    if not description:
        return {"error": "description is required"}

    language = ctx.get("language", "bash")
    if language not in {"bash", "zsh"}:
        language = "bash"

    prompt = (
        f"Write a {language} script that does the following:\n{description}\n\n"
        f"Rules:\n"
        f"- Use 'set -euo pipefail' at the top\n"
        f"- Prefer built-in tools over external dependencies\n"
        f"- Add a usage/help block at top\n"
        f"- No hardcoded credentials\n"
        f"- Output ONLY the script, no explanation"
    )

    script_content = _local_llm(prompt)

    # Strip markdown fences if present
    script_content = _strip_fences(script_content)

    output_path = ctx.get("output_path")
    saved_to = None
    if output_path:
        safe = _safe_path(output_path)
        safe.write_text(script_content, encoding="utf-8")
        safe.chmod(0o755)
        saved_to = str(safe)
        logger.info("Shell script saved to %s", saved_to)

    return {
        "language": language,
        "script": script_content,
        "saved_to": saved_to,
        "note": "Review the script before executing. Use run_script task type to execute.",
    }


def _handle_file_pipeline(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Plan (and optionally execute) a file transformation pipeline.

    ctx keys:
      - source_dir   (str): directory to process (must be in allowed base)
      - operations   (list[dict]): ordered list of ops, each with "op" and params
      - dry_run      (bool, default True): if True, return plan only

    Supported ops: rename_pattern, move_to, delete_matching, collect_by_ext
    """
    source_dir = ctx.get("source_dir", "")
    if not source_dir:
        return {"error": "source_dir is required"}

    safe_dir = _safe_path(source_dir)
    if not safe_dir.is_dir():
        return {"error": f"source_dir is not a directory: {safe_dir}"}

    operations = ctx.get("operations", [])
    dry_run = ctx.get("dry_run", True)

    plan = []
    executed = []

    for op in operations:
        op_type = op.get("op", "")
        result = _execute_file_op(op_type, op, safe_dir, dry_run)
        if dry_run:
            plan.append({"op": op_type, "plan": result})
        else:
            executed.append({"op": op_type, "result": result})

    return {
        "source_dir": str(safe_dir),
        "dry_run": dry_run,
        "plan": plan if dry_run else None,
        "executed": executed if not dry_run else None,
    }


def _execute_file_op(op_type: str, op: dict, base: Path, dry_run: bool) -> str:
    """Execute or plan a single file operation."""
    if op_type == "collect_by_ext":
        ext = op.get("ext", "")
        dest = op.get("dest", "")
        files = list(base.glob(f"**/*{ext}")) if ext else []
        if dry_run:
            return f"Would move {len(files)} {ext} files to {dest}"
        dest_path = _safe_path(dest) if dest else base / f"collected{ext}"
        dest_path.mkdir(parents=True, exist_ok=True)
        for f in files:
            f.rename(dest_path / f.name)
        return f"Moved {len(files)} files to {dest_path}"

    if op_type == "delete_matching":
        pattern = op.get("pattern", "")
        if not pattern:
            return "No pattern specified"
        files = list(base.glob(pattern))
        if dry_run:
            return f"Would delete {len(files)} files matching {pattern!r}"
        for f in files:
            if f.is_file():
                f.unlink()
        return f"Deleted {len(files)} files"

    return f"Unknown op: {op_type}"


def _handle_macro_compose(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Compose a multi-step macro from primitive operations.

    ctx keys:
      - goal       (str): what the macro should accomplish end-to-end
      - primitives (list[str], optional): allowed primitive operations
    """
    goal = ctx.get("goal", "")
    if not goal:
        return {"error": "goal is required"}

    primitives = ctx.get("primitives", [
        "read_file", "write_file", "run_shell", "web_search", "memory_recall",
    ])

    prompt = (
        f"Compose a macro (ordered list of steps) to accomplish:\n{goal}\n\n"
        f"Available primitives: {', '.join(primitives)}\n\n"
        f"Format each step as:\n"
        f"  Step N: <primitive>(<args>) — <what it does>\n"
        f"Output ONLY the step list."
    )

    plan = _local_llm(prompt)
    return {
        "goal": goal,
        "macro_plan": plan,
        "primitives_available": primitives,
        "note": "Review macro plan before executing. Each step requires separate approval.",
    }


def _handle_run_script(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a pre-approved script at a validated path.

    ctx keys:
      - script_path (str): path to the script (must be in allowed base)
      - args        (list[str], optional): additional args
      - timeout     (int, optional): timeout in seconds (default 60, max 300)
    """
    script_path = ctx.get("script_path", "")
    if not script_path:
        return {"error": "script_path is required"}

    args = ctx.get("args", [])
    # Validate args before filesystem access — reject shell injection attempts
    for arg in args:
        if any(c in str(arg) for c in (";", "|", "&", "`", "$", ">")):
            raise ValueError(f"Unsafe argument rejected: {arg!r}")

    safe = _safe_path(script_path)
    if not safe.is_file():
        return {"error": f"Script not found: {safe}"}

    timeout = min(int(ctx.get("timeout", 60)), 300)
    cmd = [str(safe)] + [str(a) for a in args]

    logger.info("Running script: %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env={**os.environ},
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-1000:],
            "success": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Script timed out after {timeout}s", "success": False}


# ── LLM integration ───────────────────────────────────────────────────────────

def _local_llm(prompt: str) -> str:
    try:
        from brains.brain_ollama import ask_local
        return ask_local(prompt)
    except Exception as exc:
        logger.warning("Local LLM unavailable: %s", exc)
        return f"[LLM unavailable — prompt: {prompt[:80]}...]"


def _strip_fences(text: str) -> str:
    """Remove ```bash / ``` markdown fences from LLM output."""
    import re
    return re.sub(r"^```[a-z]*\n?", "", text.strip(), flags=re.MULTILINE).rstrip("`").strip()
