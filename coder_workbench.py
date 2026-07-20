"""
Repo-grounded coding workbench for the Jarvis terminal console.

This gives Jarvis a native way to answer "what should I verify next?" from the
actual git state instead of generic coding-agent advice.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parent
BENCHMARK_LOG = ROOT / "training" / "benchmarks.jsonl"


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


def _run_shell(command: str, *, cwd: Path = ROOT, timeout_seconds: int = 120) -> tuple[int, str, float]:
    """Run a verification command without invoking a command shell."""
    started = time.monotonic()
    try:
        if re.search(r"(?:&&|\|\||[;|<>`]|\$\(|[\r\n])", command):
            raise ValueError("Shell control operators are not allowed in verification commands")
        args = shlex.split(command)
        env = os.environ.copy()
        while args and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", args[0]):
            name, value = args.pop(0).split("=", 1)
            if (name, value) != ("JARVIS_RUN_PACKAGED_SMOKE", "1"):
                raise ValueError(f"Unsupported verification environment override: {name}")
            env[name] = value
        if not args:
            raise ValueError("Verification command is empty")

        executable = Path(args[0]).name
        allowed = {"echo", "git", "pytest", "python", "python3"}
        installer = (ROOT / "scripts" / "install_jarvis_app.sh").resolve()
        requested = Path(args[0]).expanduser()
        if "/" in args[0] and requested.resolve(strict=False) != installer:
            raise ValueError(f"Executable paths are not allowed here: {args[0]}")
        if executable == "git" and args[1:] != ["diff", "--check"]:
            raise ValueError("Only 'git diff --check' is allowed as a verification command")
        vault_check = ["python3", "-c", "import vault; print(vault.build_wiki_text())"]
        if executable in {"python", "python3"} and "-c" in args and args != vault_check:
            raise ValueError("Inline Python is not allowed as a verification command")
        if executable not in allowed and requested.resolve(strict=False) != installer:
            raise ValueError(f"Unsupported verification executable: {args[0]}")

        completed = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            shell=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode, completed.stdout.rstrip(), time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return 124, output.rstrip() or f"Timed out after {timeout_seconds}s", time.monotonic() - started
    except Exception as exc:
        return 1, str(exc), time.monotonic() - started


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
            "Execute the required verify plan when Jarvis needs to close the loop itself.",
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
                "command": "python3 -c 'import vault; print(vault.build_wiki_text())'",
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


def run_verification_plan(
    paths: list[str] | None = None,
    *,
    required_only: bool = True,
    stop_on_failure: bool = True,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    commands = verification_plan(paths)
    results: list[dict[str, Any]] = []
    for item in commands:
        if required_only and not item.get("required"):
            results.append({**item, "skipped": True, "ok": True, "output": ""})
            continue
        code, output, elapsed = _run_shell(item["command"], timeout_seconds=timeout_seconds)
        result = {
            **item,
            "skipped": False,
            "ok": code == 0,
            "returncode": code,
            "elapsed_seconds": round(elapsed, 2),
            "output": output[-4000:],
        }
        results.append(result)
        if code != 0 and stop_on_failure:
            break

    failed = [item for item in results if not item.get("ok")]
    return {
        "ok": not failed,
        "required_only": required_only,
        "stop_on_failure": stop_on_failure,
        "timeout_seconds": timeout_seconds,
        "commands": results,
        "failed_count": len(failed),
        "ran_count": sum(1 for item in results if not item.get("skipped")),
        "skipped_count": sum(1 for item in results if item.get("skipped")),
    }


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


def _latest_benchmark() -> dict[str, Any]:
    if not BENCHMARK_LOG.exists():
        return {}
    latest: dict[str, Any] = {}
    try:
        for line in BENCHMARK_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            latest = json.loads(line)
    except Exception:
        return {}
    return latest


# ── Git write operations ──────────────────────────────────────────────────────

def git_diff(args: list[str] | None = None) -> str:
    """Return git diff for the working tree (or staged if args includes --cached)."""
    return _git(["diff", *(args or [])])


def git_commit(msg: str, paths: list[str] | None = None) -> str:
    """Stage specified paths (or all changes) and commit with msg.

    Uses list args throughout and never enables shell execution.  # pre-commit-ok
    """
    if paths:
        code, out = _run(["git", "add", "--", *paths])
    else:
        code, out = _run(["git", "add", "-A"])
    if code != 0:
        return f"git add failed: {out}"
    code, out = _run(["git", "commit", "-m", msg])
    if code != 0:
        return f"git commit failed: {out}"
    return out


# ── Code fix loop (P2 — Cursor/Codex gap) ────────────────────────────────────

_CODER_WRITE_SYSTEM = """\
You are a precise Python coding agent. When given a task:
- Write all necessary Python files (implementation + tests).
- Return ONLY a valid JSON object — no markdown, no explanation, no preamble.
Format:
{"files": [{"path": "relative/path.py", "content": "...full code..."}], "test_command": "python -m pytest test_foo.py -q"}

Rules:
- Write tests in a file prefixed with 'test_'
- Use pytest for testing
- The runtime derives the pytest command from validated test-file paths; command text is never executed
- paths are relative to the working directory
- Output ONLY the JSON object\
"""

_CODER_FIX_SYSTEM = """\
You are a Python debugging agent. Fix the failing code so the tests pass.
Return ONLY a valid JSON object — no markdown, no explanation.
Format:
{"files": [{"path": "relative/path.py", "content": "...complete fixed code..."}]}

Output ONLY the JSON object with the fixed file(s).\
"""


def _parse_coder_json(raw: str) -> dict:
    """Extract JSON object from coder model response."""
    text = raw.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]).strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        return json.loads(obj_match.group())
    raise ValueError(f"No JSON object in coder response (first 200 chars): {text[:200]}")


def fix_loop(
    task: str,
    *,
    workspace: Path | None = None,
    max_iterations: int = 5,
    execution_approved: bool = False,
) -> dict[str, Any]:
    """Write code with devstral, run tests, patch on failure, repeat.

    LOOP:
      ask devstral to write code → run test_command → capture output
      if exit 0 → return success
      if exit ≠ 0 → send failure + current files back to devstral → apply patch
      if max_iterations hit → return best attempt with failure log

    Args:
        task: Natural language description (e.g. "write a function that reverses a string and test it")
        workspace: Working directory; defaults to <jarvis-root>/workspace
        max_iterations: Max write-test-fix cycles (default 5)
        execution_approved: Trusted caller confirms generated pytest execution.

    Returns:
        {
          "ok": bool,
          "iterations": int,
          "files": {path: content},
          "test_command": str,
          "output": str,
          "history": [{"iteration": int, "output": str, "ok": bool, "elapsed_seconds": float}],
        }
    """
    import ollama as _ollama_lib
    from brains.brain_ollama import get_best_available
    from config import LOCAL_CODER, LOCAL_ORNITH_35B

    cwd = (workspace or ROOT / "workspace").resolve()
    cwd.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    test_command = ""
    test_output = ""
    history: list[dict[str, Any]] = []

    def _safe_write(rel_path: str, content: str) -> None:
        target = (cwd / rel_path).resolve()
        if not target.is_relative_to(cwd):
            raise PermissionError(f"Path traversal rejected: {rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # Use a direct Ollama client with coding-appropriate timeouts (code gen can be slow)
    try:
        import httpx
        _code_timeout = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0)
        _ollama_client = _ollama_lib.Client(timeout=_code_timeout)
    except ImportError:
        _ollama_client = _ollama_lib.Client(timeout=300.0)

    # Prefer ornith-35b (agentic coding specialist, SWE-bench 82.4); fall back to LOCAL_CODER.
    # The check avoids get_best_available's default-to-first-model behaviour when the
    # preferred model isn't pulled — we want an explicit presence test.
    try:
        _avail = {m.model for m in _ollama_client.list().models}
        if any(LOCAL_ORNITH_35B in m for m in _avail):
            model = LOCAL_ORNITH_35B
        else:
            model = get_best_available(LOCAL_CODER)
    except Exception:
        model = get_best_available(LOCAL_CODER)
    log.info("[fix_loop] Starting — model=%s cwd=%s task=%s", model, cwd, task[:80])

    for iteration in range(1, max_iterations + 1):
        if iteration == 1:
            prompt = (
                f"Task: {task}\n\n"
                "Write all necessary Python files (implementation + tests) to complete this task. "
                "Return JSON with 'files' list and 'test_command'."
            )
            system = _CODER_WRITE_SYSTEM
        else:
            files_summary = "\n\n".join(
                f"File: {p}\n```python\n{c}\n```" for p, c in files.items()
            )
            prompt = (
                f"Task: {task}\n\n"
                f"Current files:\n{files_summary}\n\n"
                f"Test command: {test_command}\n"
                f"Failure output:\n{test_output[:3000]}\n\n"
                "Fix the code so all tests pass."
            )
            system = _CODER_FIX_SYSTEM

        try:
            response = _ollama_client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
                options={"temperature": 0},
            )
            raw = (response.message.content or "").strip()
            parsed = _parse_coder_json(raw)
        except Exception as exc:
            log.warning("[fix_loop] iteration %d parse error: %s", iteration, exc)
            history.append({"iteration": iteration, "output": f"Model/parse error: {exc}", "ok": False, "elapsed_seconds": 0.0})
            continue

        # Write files from this iteration
        for fspec in parsed.get("files", []):
            rel_path = str(fspec.get("path", "output.py")).strip()
            content = str(fspec.get("content", ""))
            try:
                _safe_write(rel_path, content)
                files[rel_path] = content
            except PermissionError as exc:
                return {"ok": False, "error": str(exc), "iterations": iteration, "files": files, "history": history}

        # Never execute model-supplied command text. Derive a fixed pytest command
        # solely from generated paths that already passed the workspace boundary.
        test_files = sorted(
            path for path in files
            if Path(path).name.startswith("test_") and Path(path).suffix == ".py"
        )
        if not test_files:
            test_output = "Generated code did not include a pytest test file."
            history.append({
                "iteration": iteration,
                "output": test_output,
                "ok": False,
                "elapsed_seconds": 0.0,
            })
            continue
        test_command = f"python -m pytest {_quote_paths(test_files)} -q"
        if not execution_approved:
            return {
                "ok": False,
                "iterations": iteration,
                "files": files,
                "test_command": test_command,
                "output": "",
                "history": history,
                "error": "Explicit approval is required before running generated tests.",
            }

        returncode, test_output, elapsed = _run_shell(test_command, cwd=cwd, timeout_seconds=60)
        ok = returncode == 0

        log.info("[fix_loop] iteration %d exit=%d elapsed=%.1fs", iteration, returncode, elapsed)
        history.append({"iteration": iteration, "output": test_output, "ok": ok, "elapsed_seconds": round(elapsed, 2)})

        if ok:
            return {
                "ok": True,
                "iterations": iteration,
                "files": files,
                "test_command": test_command,
                "output": test_output,
                "history": history,
            }

    return {
        "ok": False,
        "iterations": max_iterations,
        "files": files,
        "test_command": test_command,
        "output": test_output,
        "history": history,
        "error": f"Still failing after {max_iterations} iterations",
    }


def improvement_text() -> str:
    """Return repo-grounded improvement suggestions from current git and eval state."""
    payload = status()
    benchmark = _latest_benchmark()
    categories = benchmark.get("categories", {}) if isinstance(benchmark, dict) else {}
    weak_categories = []
    for name, info in categories.items():
        failed = int(info.get("failed") or 0) + int(info.get("errors") or 0)
        total = int(info.get("total") or 0)
        if total and failed:
            weak_categories.append(f"{name} {info.get('passed', 0)}/{total}")

    changed = payload.get("changed_files", [])
    runtime_dirty = [
        item["path"] for item in changed
        if _touches_runtime([item["path"]])
    ]

    lines = [
        "I inspected the repo state, recent benchmark data, and verification plan.",
        f"Current branch is {payload['branch']} at {payload['head']}.",
    ]
    if changed:
        lines.append(f"The worktree has {len(changed)} changed paths, so the first improvement is change control: isolate or commit the current work before adding more features.")
    else:
        lines.append("The worktree is clean, so the next improvement can be selected from benchmark evidence rather than cleanup pressure.")

    suggestions: list[str] = []
    if weak_categories:
        suggestions.append(
            "Tighten the failing benchmark categories next: "
            + ", ".join(weak_categories[:4])
            + "."
        )
    if runtime_dirty:
        suggestions.append(
            "Run the packaged-app verification path because runtime surfaces are dirty: "
            + ", ".join(runtime_dirty[:5])
            + "."
        )
    suggestions.append(
        "Promote codebase review into a real fast path: inspect git state, benchmark failures, and verification commands before giving architectural advice."
    )
    suggestions.append(
        "Keep overnight training evidence-gated: training completion should not count as learning unless eval totals, adapter path, examples, and promotion status are all visible."
    )

    lines.append("Highest-value improvements: " + " ".join(suggestions[:4]))
    verify = payload.get("recommended_next", [])
    if verify:
        required = [item["command"] for item in verify if item.get("required")]
        if required:
            lines.append("Verification I would run first: " + required[0])
    return "\n".join(lines)
