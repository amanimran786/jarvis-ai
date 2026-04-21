"""
Repo-grounded coding workbench for the Jarvis terminal console.

This gives Jarvis a native way to answer "what should I verify next?" from the
actual git state instead of generic coding-agent advice.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


_RUNTIME_SURFACES = {
    "api.py",
    "router.py",
    "jarvis_cli.py",
    "main.py",
    "Jarvis.spec",
    "ui.py",
    "voice.py",
}

_STATUS_MODULES = {
    "capability_evals.py",
    "capability_parity.py",
    "coder_workbench.py",
    "context_budget.py",
    "external_agent_patterns.py",
    "production_readiness.py",
    "security_roe.py",
}


def _run(args: list[str], *, cwd: Path = ROOT) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return 1, str(exc)
    return completed.returncode, completed.stdout.rstrip()


def _git(args: list[str]) -> str:
    code, output = _run(["git", *args])
    return output if code == 0 else ""


def _quote_paths(paths: list[str]) -> str:
    return " ".join(shlex.quote(path) for path in paths)


def changed_files() -> list[dict[str, str]]:
    output = _git(["status", "--short"])
    files: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "?"
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        files.append({"status": status, "path": path})
    return files


def _recommended_compile(files: list[str]) -> str:
    py_files = [path for path in files if path.endswith(".py")]
    if py_files:
        return f"python3 -m compileall {_quote_paths(py_files)}"
    return "python3 -m compileall api.py router.py jarvis_cli.py"


def _touches_runtime(files: list[str]) -> bool:
    return any(path in _RUNTIME_SURFACES or path.startswith("local_runtime/") for path in files)


def _touches_status_or_cli(files: list[str]) -> bool:
    return any(path in _STATUS_MODULES or path in {"api.py", "router.py", "jarvis_cli.py"} for path in files)


def _touches_vault(files: list[str]) -> bool:
    return any(path.startswith("vault/") for path in files)


def _touches_tests(files: list[str]) -> list[str]:
    return [path for path in files if path.startswith("tests/") and path.endswith(".py")]


def status() -> dict[str, Any]:
    files = changed_files()
    branch = _git(["branch", "--show-current"]) or "unknown"
    head = _git(["log", "-1", "--oneline"]) or "unknown"
    root = _git(["rev-parse", "--show-toplevel"]) or str(ROOT)
    file_paths = [item["path"] for item in files]
    return {
        "ok": True,
        "purpose": "Give Jarvis a repo-grounded terminal coding loop like a local Claude/Codex workbench.",
        "root": root,
        "branch": branch,
        "head": head,
        "clean": not files,
        "changed_files": files,
        "recommended_next": verification_plan(file_paths),
        "loop": [
            "Inspect repo state before coding.",
            "Make the smallest correct diff.",
            "Run the verify plan generated from changed files.",
            "Rebuild the packaged app when runtime surfaces change.",
            "Commit and push only after verification passes.",
        ],
    }


def verification_plan(paths: list[str] | None = None) -> list[dict[str, Any]]:
    files = list(paths or [item["path"] for item in changed_files()])
    commands: list[dict[str, Any]] = [
        {
            "id": "diff_check",
            "why": "Catch whitespace and patch hygiene problems before tests.",
            "command": "git diff --check",
            "required": True,
        },
        {
            "id": "compile",
            "why": "Catch syntax/import breakage in touched Python files.",
            "command": _recommended_compile(files),
            "required": True,
        },
    ]
    if _touches_status_or_cli(files):
        commands.append(
            {
                "id": "status_unit_regression",
                "why": "Console/API/router status surfaces changed.",
                "command": (
                    "python3 -m pytest tests/test_unit_coverage.py "
                    "-k 'JarvisCliEndpointTests or ProductionReadinessTests or CapabilityEvalTests or CapabilityParityTests' -q"
                ),
                "required": True,
            }
        )
        commands.append(
            {
                "id": "status_router_regression",
                "why": "Fast-path router or API status endpoints may have changed.",
                "command": (
                    "python3 -m pytest tests/test_jarvis_regression_suite.py "
                    "-k 'production_readiness or capability_evals or capability_parity or security_roe or coder_workbench' -q"
                ),
                "required": True,
            }
        )
    touched_tests = _touches_tests(files)
    if touched_tests:
        commands.append(
            {
                "id": "changed_tests",
                "why": "Changed test files should run directly.",
                "command": f"python3 -m pytest {_quote_paths(touched_tests)} -q",
                "required": True,
            }
        )
    if _touches_vault(files):
        commands.append(
            {
                "id": "vault_index",
                "why": "Vault graph/index changes should regenerate the compiled wiki index.",
                "command": "python3 - <<'PY'\nimport vault\nprint(vault.build_wiki_text())\nPY",
                "required": True,
            }
        )
    if _touches_runtime(files):
        commands.append(
            {
                "id": "package_rebuild",
                "why": "Runtime/API/CLI surfaces must be verified against the installed macOS app.",
                "command": "/Users/truthseeker/jarvis-ai/scripts/install_jarvis_app.sh --applications-only",
                "required": True,
            }
        )
        commands.append(
            {
                "id": "packaged_smoke",
                "why": "Verify the installed bundle, not just source checkout behavior.",
                "command": (
                    "JARVIS_RUN_PACKAGED_SMOKE=1 python3 -m pytest tests/test_jarvis_live_integrations.py "
                    "-k 'packaged_app_starts_and_serves_status or packaged_app_chat_serves_vault_curator_read' -q"
                ),
                "required": True,
            }
        )
    if len(commands) == 2:
        commands.append(
            {
                "id": "default_unit_smoke",
                "why": "No specialized surface detected; run a cheap baseline smoke.",
                "command": "python3 -m pytest tests/test_unit_coverage.py -q",
                "required": False,
            }
        )
    return commands


def summary_text() -> str:
    payload = status()
    lines = [
        "Jarvis coder workbench: repo-grounded terminal loop for local coding.",
        f"Branch: {payload['branch']} | clean={'yes' if payload['clean'] else 'no'}",
        f"Head: {payload['head']}",
        "",
    ]
    if payload["changed_files"]:
        lines.append("Changed files:")
        for item in payload["changed_files"]:
            lines.append(f"- {item['status']} {item['path']}")
        lines.append("")
    lines.append("Verify plan:")
    for item in payload["recommended_next"]:
        required = "required" if item.get("required") else "optional"
        lines.append(f"- {item['id']} [{required}]: {item['command']}")
    return "\n".join(lines)
