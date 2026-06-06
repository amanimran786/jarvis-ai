"""
Secure filesystem tools for the Jarvis backend engineer.

Confines all reads and writes to the workspace/ directory.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("jarvis.tools.fs_tools")

# Resolve workspace directory relative to this file: jarvis-ai/workspace
WORKSPACE_DIR = (Path(__file__).resolve().parent.parent / "workspace").resolve()


def _secure_path(filepath: str | Path) -> Path:
    """
    Resolve path relative to WORKSPACE_DIR and verify it stays inside.
    Raises PermissionError if path traversal is detected.
    """
    # Ensure workspace directory exists
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    target = Path(filepath)
    if not target.is_absolute():
        target = WORKSPACE_DIR / target

    resolved_target = target.resolve()
    resolved_workspace = WORKSPACE_DIR.resolve()

    try:
        resolved_target.relative_to(resolved_workspace)
    except ValueError as exc:
        raise PermissionError(
            f"Access denied: Path '{filepath}' resolves to '{resolved_target}', "
            f"which is outside the workspace directory '{resolved_workspace}'."
        ) from exc

    return resolved_target


def read_file(filepath: str) -> str:
    """
    Read the contents of a file inside the workspace directory.

    Enforces path traversal checks.
    """
    try:
        target = _secure_path(filepath)
        if not target.exists():
            return f"Error: File '{filepath}' not found."
        if not target.is_file():
            return f"Error: '{filepath}' is not a file."
        return target.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("read_file failed for '%s': %s", filepath, exc)
        return f"Error reading file '{filepath}': {exc}"


def write_file(filepath: str, content: str) -> str:
    """
    Write or overwrite content to a file inside the workspace directory.

    Enforces path traversal checks.
    """
    try:
        target = _secure_path(filepath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Success: Wrote to '{filepath}'."
    except Exception as exc:
        log.warning("write_file failed for '%s': %s", filepath, exc)
        return f"Error writing file '{filepath}': {exc}"
