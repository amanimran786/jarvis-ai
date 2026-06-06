"""
Secure shell and testing tools for the Jarvis backend engineer.

Confines pytest execution to the workspace/ directory.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.tools.shell_tools")

# Resolve workspace directory relative to this file: jarvis-ai/workspace
WORKSPACE_DIR = (Path(__file__).resolve().parent.parent / "workspace").resolve()


def run_tests(pytest_args: str = "") -> str:
    """
    Execute pytest securely within the workspace directory.

    Parses pytest_args safely and runs pytest with cwd set to workspace/.
    """
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Securely tokenize arguments to prevent command injection
        args = shlex.split(pytest_args)

        # Enforce command is 'pytest' and run without shell=True
        cmd = ["pytest"] + args

        log.info("Executing test command: %s (cwd=%s)", cmd, WORKSPACE_DIR)
        result = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )

        return result.stdout
    except subprocess.TimeoutExpired:
        log.warning("run_tests timed out (30s limit)")
        return "Error: Test execution timed out (30s limit)."
    except Exception as exc:
        log.warning("run_tests failed: %s", exc)
        return f"Error running tests: {exc}"
