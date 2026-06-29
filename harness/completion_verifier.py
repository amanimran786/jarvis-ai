"""Collect deterministic, loop-owned evidence for task completion."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from harness.task_contract import TaskSpec


PREVIEW_BYTES = 4096
_HASH_CHUNK_BYTES = 64 * 1024


class CompletionEvidenceError(RuntimeError):
    """Raised when completion evidence cannot be collected from a worktree."""


def _run_git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CompletionEvidenceError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _repo_root(repo_path: str | Path) -> Path:
    try:
        candidate = Path(repo_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CompletionEvidenceError(f"invalid worktree path: {repo_path}") from exc
    if not candidate.is_dir():
        raise CompletionEvidenceError(f"worktree path is not a directory: {candidate}")

    root_text = _run_git(candidate, "rev-parse", "--show-toplevel").decode(
        "utf-8", errors="surrogateescape"
    )
    return Path(root_text.rstrip("\n")).resolve(strict=True)


def _resolve_base(repo: Path, base_ref: str) -> str:
    if not base_ref or "\x00" in base_ref:
        raise CompletionEvidenceError("base ref must be a non-empty Git reference")
    output = _run_git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_ref}^{{commit}}",
    )
    return output.decode("ascii").strip()


def _nul_paths(output: bytes) -> set[str]:
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\x00")
        if item
    }


def _changed_files(repo: Path, base_commit: str) -> list[str]:
    tracked = _run_git(
        repo,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_commit,
        "--",
    )
    untracked = _run_git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(_nul_paths(tracked) | _nul_paths(untracked))


def _stream_summary(stream: BinaryIO) -> dict[str, Any]:
    stream.flush()
    stream.seek(0)
    digest = hashlib.sha256()
    preview = bytearray()
    size = 0

    while chunk := stream.read(_HASH_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if len(preview) < PREVIEW_BYTES:
            preview.extend(chunk[: PREVIEW_BYTES - len(preview)])

    return {
        "sha256": digest.hexdigest(),
        "preview": bytes(preview).decode("utf-8", errors="replace"),
        "bytes": size,
        "truncated": size > PREVIEW_BYTES,
    }


def _bytes_summary(value: bytes) -> dict[str, Any]:
    with tempfile.TemporaryFile() as stream:
        stream.write(value)
        return _stream_summary(stream)


def _command_result(
    command: str,
    exit_code: int,
    stdout: dict[str, Any],
    stderr: dict[str, Any],
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    return {
        "command": command,
        "exit_code": exit_code,
        "stdout_sha256": stdout["sha256"],
        "stdout_preview": stdout["preview"],
        "stdout_bytes": stdout["bytes"],
        "stdout_truncated": stdout["truncated"],
        "stderr_sha256": stderr["sha256"],
        "stderr_preview": stderr["preview"],
        "stderr_bytes": stderr["bytes"],
        "stderr_truncated": stderr["truncated"],
        "timed_out": timed_out,
    }


def _failed_command(command: str, exit_code: int, message: str) -> dict[str, Any]:
    return _command_result(
        command,
        exit_code,
        _bytes_summary(b""),
        _bytes_summary(message.encode("utf-8", errors="replace")),
    )


def _execute_command(command: str, repo: Path, timeout: float) -> dict[str, Any]:
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return _failed_command(command, 2, f"invalid verification command: {exc}")
    if not argv:
        return _failed_command(command, 2, "verification command is empty")
    if timeout <= 0:
        result = _failed_command(command, 124, "verification budget exhausted")
        result["timed_out"] = True
        return result

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv,
                cwd=repo,
                stdout=stdout,
                stderr=stderr,
                shell=False,
            )
        except FileNotFoundError as exc:
            return _failed_command(command, 127, str(exc))
        except OSError as exc:
            return _failed_command(command, 126, str(exc))

        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()

        return _command_result(
            command,
            124 if timed_out else process.returncode,
            _stream_summary(stdout),
            _stream_summary(stderr),
            timed_out=timed_out,
        )


def collect_completion_evidence(
    spec: TaskSpec,
    repo_path: str | Path,
    base_ref: str,
) -> dict[str, Any]:
    """Collect the evidence envelope consumed by ``evaluate_completion``.

    Verification commands run in contract order within the task's total wall-time
    budget. They are parsed into argument lists and never evaluated by a shell.
    """
    repo = _repo_root(repo_path)
    base_commit = _resolve_base(repo, base_ref)
    deadline = time.monotonic() + spec.budget.wall_time_seconds
    commands = [
        _execute_command(command, repo, deadline - time.monotonic())
        for command in spec.verification_commands
    ]
    return {
        "observer": "loop",
        "changed_files": _changed_files(repo, base_commit),
        "commands": commands,
        "policy_findings": [],
    }
