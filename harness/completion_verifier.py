"""Collect deterministic, loop-owned evidence for task completion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.commit_review_gate import (
    CommitGateResult,
    capture_clean_head,
    run_commit_gate,
)
from harness.task_contract import CompletionVerdict, TaskSpec, evaluate_completion


PREVIEW_BYTES = 4096
MAX_CAPTURE_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 10.0
_READ_CHUNK_BYTES = 64 * 1024
_POLICY_VERSION = 1
_SHELL_SYNTAX = frozenset(";|&<>`$\n\r\x00")
_FULL_OBJECT_ID = re.compile(r"[0-9a-fA-F]{40,64}")
_SENSITIVE_ENV_FRAGMENTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_PYTEST_FAILURE_SUMMARY = re.compile(
    rb"\b(?:[1-9][0-9]*) (?:failed|error|errors)\b",
    re.IGNORECASE,
)
_PYTEST_RESULT_PREFIX = b"JARVIS_PYTEST_RESULT "
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_GIT_CANDIDATES = (
    Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
    Path("/Library/Developer/CommandLineTools/usr/bin/git"),
    Path("/usr/bin/git"),
)
_GIT_EXECUTABLE = str(
    next(
        (candidate for candidate in _GIT_CANDIDATES if candidate.is_file()),
        Path(shutil.which("git") or "git"),
    )
)
_PYTHON_MODULE_RUNNER = """
import json
import runpy
import sys

module, repo, *args = sys.argv[1:]
if module == "pytest":
    import pytest

    sys.path.insert(0, repo)

    class JarvisPytestBootstrap:
        def pytest_configure(self, config):
            for module_name in (
                "tools",
                "brains.brain_apple_foundation",
                "brains.brain_ollama",
            ):
                try:
                    __import__(module_name)
                except Exception:
                    pass

    class JarvisPytestObserver:
        def __init__(self):
            self.collected = 0
            self.terminal = 0
            self.passed = 0
            self.failed = 0

        def pytest_collection_finish(self, session):
            self.collected = len(session.items)

        def pytest_runtest_logreport(self, report):
            terminal = report.when == "call"
            terminal = terminal or (
                report.when == "setup"
                and (report.failed or report.skipped)
            )
            if terminal:
                self.terminal += 1
            if report.when == "call" and report.passed:
                self.passed += 1
            if report.failed:
                self.failed += 1

    observer = JarvisPytestObserver()
    result = int(
        pytest.main(
            ["--noconftest", *args],
            plugins=[JarvisPytestBootstrap(), observer],
        )
    )
    record = {
        "collected": observer.collected,
        "terminal": observer.terminal,
        "passed": observer.passed,
        "failed": observer.failed,
        "pytest_exit_code": result,
    }
    print("JARVIS_PYTEST_RESULT " + json.dumps(record, sort_keys=True))
    raise SystemExit(result)
if module == "__script__":
    script, *script_args = args
    sys.path.insert(0, repo)
    sys.argv = [script, *script_args]
    runpy.run_path(script, run_name="__main__")
    raise SystemExit(0)
sys.argv = [module, *args]
runpy.run_module(module, run_name="__main__", alter_sys=True)
""".strip()


class CompletionEvidenceError(RuntimeError):
    """Raised when completion evidence cannot be collected from a worktree."""


@dataclass(frozen=True)
class CompletionAssessment:
    """Completion evidence, immutable commit gate, and final verdict."""

    base_commit: str
    completion_commit: str
    evidence: Mapping[str, Any]
    gate: CommitGateResult
    verdict: CompletionVerdict


@dataclass
class _BoundedCapture:
    digest: Any = field(default_factory=hashlib.sha256)
    preview: bytearray = field(default_factory=bytearray)
    data: bytearray = field(default_factory=bytearray)
    size: int = 0
    overflowed: bool = False

    def add(self, chunk: bytes) -> None:
        remaining = MAX_CAPTURE_BYTES - self.size
        accepted = chunk[:remaining]
        self.digest.update(accepted)
        self.data.extend(accepted)
        self.size += len(accepted)
        if len(self.preview) < PREVIEW_BYTES:
            self.preview.extend(accepted[: PREVIEW_BYTES - len(self.preview)])
        if len(chunk) > remaining:
            self.overflowed = True

    def summary(self) -> dict[str, Any]:
        return {
            "sha256": self.digest.hexdigest(),
            "preview": bytes(self.preview).decode("utf-8", errors="replace"),
            "bytes": self.size,
            "truncated": self.overflowed or self.size > PREVIEW_BYTES,
            "_data": bytes(self.data),
        }


def _empty_summary(value: bytes = b"") -> dict[str, Any]:
    capture = _BoundedCapture()
    capture.add(value[:MAX_CAPTURE_BYTES])
    capture.overflowed = len(value) > MAX_CAPTURE_BYTES
    return capture.summary()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _capture_process(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> tuple[int, dict[str, Any], dict[str, Any], bool, bool]:
    captures = {"stdout": _BoundedCapture(), "stderr": _BoundedCapture()}
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    for name, stream in streams.items():
        if stream is None:
            continue
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    deadline = time.monotonic() + timeout
    timed_out = False
    output_overflow = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
                break
            for key, _ in selector.select(timeout=min(remaining, 0.1)):
                capture = captures[key.data]
                read_size = min(
                    _READ_CHUNK_BYTES,
                    max(1, MAX_CAPTURE_BYTES - capture.size + 1),
                )
                try:
                    chunk = os.read(key.fileobj.fileno(), read_size)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                capture.add(chunk)
                if capture.overflowed:
                    output_overflow = True
                    _kill_process_group(process)
                    break
            if output_overflow:
                break

        if not timed_out and not output_overflow:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group(process)
    finally:
        selector.close()
        for stream in streams.values():
            if stream is not None and not stream.closed:
                stream.close()

    exit_code = 125 if output_overflow else 124 if timed_out else process.returncode
    return (
        exit_code,
        captures["stdout"].summary(),
        captures["stderr"].summary(),
        timed_out,
        output_overflow,
    )


def _process_env(*, git: bool = False) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SENSITIVE_ENV_FRAGMENTS)
    }
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if git:
        env.update(
            {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
            }
        )
    return env


def _verification_env(sandbox_root: Path) -> dict[str, str]:
    home = sandbox_root / "home"
    temp = sandbox_root / "tmp"
    cache = sandbox_root / "cache"
    for directory in (home, temp, cache):
        directory.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "TMPDIR": str(temp),
        "XDG_CACHE_HOME": str(cache),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "JARVIS_AUTO_VERIFY": "0",
        "JARVIS_NATIVE_TOOL_LOOP": "0",
        "JARVIS_SECURITY_AUDIT_PATH": str(sandbox_root / "security_audit.jsonl"),
        "JARVIS_TASK_DB_PATH": str(sandbox_root / "jarvis_tasks.sqlite3"),
        "JARVIS_OLLAMA_LIVENESS_DISABLED": "1",
        "JARVIS_API_ALLOW_NO_AUTH": "1",
    }


def _sandbox_path(path: Path) -> str:
    resolved = str(path.resolve(strict=True))
    if any(ord(char) < 32 or ord(char) == 127 for char in resolved):
        raise CompletionEvidenceError("sandbox path contains control characters")
    escaped = resolved.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _verification_sandbox_profile(repo: Path, sandbox_root: Path) -> str:
    read_roots = {
        repo.resolve(strict=True),
        sandbox_root.resolve(strict=True),
        Path(sys.prefix).resolve(strict=True),
        Path(sys.base_prefix).resolve(strict=True),
    }
    for system_root in (
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/Library",
        "/private/etc",
        "/private/var/db/timezone",
        "/dev",
        "/Applications/Xcode.app/Contents/Developer",
    ):
        path = Path(system_root)
        if path.exists():
            read_roots.add(path.resolve(strict=True))
    read_roots.update(_git_metadata_roots(repo))
    allow_reads = " ".join(
        f"(subpath {_sandbox_path(path)})"
        for path in sorted(read_roots, key=str)
    )
    allow_writes = " ".join(
        f"(subpath {_sandbox_path(path)})"
        for path in (repo, sandbox_root)
    )
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-read*)\n"
        "(deny file-write*)\n"
        "(allow file-read-metadata)\n"
        f"(allow file-read* (literal \"/\") {allow_reads})\n"
        f"(allow file-write* {allow_writes} (subpath \"/dev\"))\n"
    )


def _git_metadata_roots(repo: Path) -> set[Path]:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return {dot_git.resolve(strict=True)}
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CompletionEvidenceError(
            "could not resolve verification Git metadata"
        ) from exc
    prefix = "gitdir: "
    if not marker.startswith(prefix):
        raise CompletionEvidenceError("verification Git metadata is malformed")
    git_dir = Path(marker[len(prefix):])
    if not git_dir.is_absolute():
        git_dir = dot_git.parent / git_dir
    git_dir = git_dir.resolve(strict=True)
    roots = {git_dir}
    common_file = git_dir / "commondir"
    if common_file.is_file():
        common = Path(common_file.read_text(encoding="utf-8").strip())
        if not common.is_absolute():
            common = git_dir / common
        roots.add(common.resolve(strict=True))
    return roots


def _sandboxed_argv(
    argv: Sequence[str],
    repo: Path,
    sandbox_root: Path,
) -> list[str]:
    if sys.platform != "darwin" or not _SANDBOX_EXEC.is_file():
        raise CompletionEvidenceError(
            "kernel verification sandbox is unavailable"
        )
    return [
        str(_SANDBOX_EXEC),
        "-p",
        _verification_sandbox_profile(repo, sandbox_root),
        *argv,
    ]


def _spawn(
    argv: Sequence[str],
    repo: Path,
    timeout: float,
    *,
    git: bool = False,
    sandbox_root: Path | None = None,
) -> tuple[int, dict[str, Any], dict[str, Any], bool, bool]:
    command = list(argv)
    env = _process_env(git=git)
    if sandbox_root is not None:
        command = _sandboxed_argv(command, repo, sandbox_root)
        env = _verification_env(sandbox_root)
    process = subprocess.Popen(
        command,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        env=env,
    )
    return _capture_process(process, timeout)


def _run_git(repo: Path, *args: str) -> bytes:
    try:
        exit_code, stdout, stderr, timed_out, overflowed = _spawn(
            ["git", *args], repo, GIT_TIMEOUT_SECONDS, git=True
        )
    except OSError as exc:
        raise CompletionEvidenceError(f"could not run Git: {exc}") from exc
    if timed_out:
        raise CompletionEvidenceError(f"git {' '.join(args)} timed out")
    if overflowed:
        raise CompletionEvidenceError(f"git {' '.join(args)} exceeded output limit")
    if exit_code != 0:
        detail = stderr["preview"].strip()
        raise CompletionEvidenceError(detail or f"git {' '.join(args)} failed")
    return stdout["_data"]


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


def _validate_git_trust(repo: Path) -> None:
    replacement_refs = _run_git(
        repo, "for-each-ref", "--format=%(refname)", "refs/replace"
    ).decode("utf-8", errors="replace")
    if replacement_refs.strip():
        raise CompletionEvidenceError("repository contains replacement refs")

    index_entries = _run_git(repo, "ls-files", "-v", "-z")
    hidden_paths = []
    for entry in index_entries.split(b"\x00"):
        if not entry:
            continue
        tag = chr(entry[0])
        if tag == "S" or tag.islower():
            hidden_paths.append(entry[2:].decode("utf-8", errors="surrogateescape"))
    if hidden_paths:
        paths = ", ".join(sorted(hidden_paths)[:10])
        raise CompletionEvidenceError(
            f"index contains skip-worktree or assume-unchanged paths: {paths}"
        )

    excludes_path = _run_git(repo, "rev-parse", "--git-path", "info/exclude").decode().strip()
    excludes = Path(excludes_path)
    if not excludes.is_absolute():
        excludes = repo / excludes
    if excludes.exists():
        active = [
            line for line in excludes.read_text(errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if active:
            raise CompletionEvidenceError("repository info/exclude contains active patterns")
    try:
        code, stdout, _, timed_out, overflowed = _spawn(
            ["git", "config", "--get", "core.excludesfile"],
            repo,
            GIT_TIMEOUT_SECONDS,
            git=True,
        )
    except OSError as exc:
        raise CompletionEvidenceError(f"could not inspect Git excludes: {exc}") from exc
    if timed_out or overflowed or code not in {0, 1}:
        raise CompletionEvidenceError("could not safely inspect Git excludes")
    if code == 0 and stdout["_data"].strip():
        raise CompletionEvidenceError("repository uses a global excludes file")


def _resolve_base(repo: Path, base_ref: str) -> str:
    if not _FULL_OBJECT_ID.fullmatch(base_ref):
        raise CompletionEvidenceError("base ref must be a full Git object ID")
    output = _run_git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_ref}^{{commit}}",
    )
    return output.decode("ascii").strip()


def _current_head(repo: Path) -> str:
    output = _run_git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        "HEAD^{commit}",
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
        "--no-ext-diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_commit,
        "--",
    )
    untracked = _run_git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(_nul_paths(tracked) | _nul_paths(untracked))


def _path_fingerprint(path: Path) -> tuple[Any, ...]:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return ("missing",)
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(_READ_CHUNK_BYTES):
                digest.update(chunk)
        return ("file", stat.st_mode, stat.st_size, digest.hexdigest())
    if path.is_dir():
        return ("directory", stat.st_mode)
    return ("special", stat.st_mode, stat.st_size)


def _worktree_snapshot(repo: Path, base_commit: str) -> dict[str, Any]:
    changed_files = _changed_files(repo, base_commit)
    return {
        "changed_files": changed_files,
        "fingerprints": {
            path: _path_fingerprint(repo / path) for path in changed_files
        },
    }


def _safe_repo_path(repo: Path, raw: str) -> bool:
    path_text = raw.split("::", 1)[0]
    if not path_text or "\x00" in path_text:
        return False
    try:
        resolved = (repo / path_text).resolve(strict=False)
        resolved.relative_to(repo)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _validate_pytest_args(args: Sequence[str], repo: Path) -> str | None:
    no_value = {
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-x",
        "--exitfirst",
        "-s",
        "--disable-warnings",
        "--strict-config",
        "--strict-markers",
        "--no-header",
        "--no-summary",
        "--showlocals",
    }
    value_options = {
        "-k",
        "-m",
        "--tb",
        "--maxfail",
        "--color",
        "--capture",
        "--timeout",
        "--ignore",
        "--ignore-glob",
    }
    value_prefixes = (
        "--tb=",
        "--maxfail=",
        "--color=",
        "--capture=",
        "--timeout=",
        "--ignore=",
        "--ignore-glob=",
    )
    targets = 0
    index = 0
    positional_only = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positional_only = True
        elif not positional_only and arg in no_value:
            pass
        elif not positional_only and arg in value_options:
            index += 1
            if index >= len(args):
                return f"{arg} requires a value"
        elif not positional_only and arg.startswith(value_prefixes):
            pass
        elif not positional_only and arg.startswith("-"):
            return f"pytest option is not allowed: {arg}"
        elif not _safe_repo_path(repo, arg):
            return f"pytest target is outside the repository: {arg}"
        else:
            targets += 1
        index += 1
    return None if targets else "pytest requires an explicit repository target"


def _normalized_pytest_args(args: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--timeout":
            index += 2
            continue
        if arg.startswith("--timeout="):
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return normalized


def _validate_ruff_args(args: Sequence[str], repo: Path) -> str | None:
    if not args or args[0] != "check":
        return "ruff is limited to the read-only check command"
    no_value = {"-q", "--quiet", "--no-cache", "--isolated", "--preview", "--statistics"}
    value_prefixes = (
        "--output-format=",
        "--select=",
        "--ignore=",
        "--extend-select=",
        "--extend-ignore=",
        "--target-version=",
    )
    targets = 0
    for arg in args[1:]:
        if arg in no_value or arg.startswith(value_prefixes):
            continue
        if arg.startswith("-"):
            return f"ruff option is not allowed: {arg}"
        if not _safe_repo_path(repo, arg):
            return f"ruff target is outside the repository: {arg}"
        targets += 1
    return None if targets else "ruff check requires an explicit repository target"


def _validate_compileall_args(args: Sequence[str], repo: Path) -> str | None:
    targets = 0
    for arg in args:
        if arg in {"-q", "-qq"}:
            continue
        if arg.startswith("-"):
            return f"compileall option is not allowed: {arg}"
        if not _safe_repo_path(repo, arg):
            return f"compileall target is outside the repository: {arg}"
        targets += 1
    return None if targets else "compileall requires an explicit repository target"


def _validate_git_args(args: Sequence[str], repo: Path) -> tuple[list[str] | None, str | None]:
    if not args:
        return None, "git requires a read-only subcommand"
    if args[0] == "status":
        allowed = {
            "--short",
            "-s",
            "--branch",
            "-b",
            "--porcelain",
            "--porcelain=v1",
            "--porcelain=v2",
            "--untracked-files=no",
            "--untracked-files=normal",
            "--untracked-files=all",
            "--ignored=no",
            "--ahead-behind",
            "--no-ahead-behind",
        }
        invalid = [arg for arg in args[1:] if arg not in allowed]
        if invalid:
            return None, f"git status option is not allowed: {invalid[0]}"
        return [_GIT_EXECUTABLE, *args], None

    if args[0] != "diff" or "--check" not in args[1:]:
        return None, "git is limited to status and diff --check"
    allowed_options = {"--check", "--cached", "--staged", "--no-renames", "--relative"}
    sanitized = ["git", "diff", "--no-ext-diff"]
    path_mode = False
    revisions = 0
    for arg in args[1:]:
        if arg == "--":
            path_mode = True
            sanitized.append(arg)
        elif not path_mode and arg in allowed_options:
            sanitized.append(arg)
        elif not path_mode and arg.startswith("-"):
            return None, f"git diff option is not allowed: {arg}"
        elif path_mode:
            if not _safe_repo_path(repo, arg):
                return None, f"git diff path is outside the repository: {arg}"
            sanitized.append(arg)
        else:
            revisions += 1
            if revisions > 2 or any(char.isspace() for char in arg):
                return None, f"git diff revision is not allowed: {arg}"
            sanitized.append(arg)
    sanitized[0] = _GIT_EXECUTABLE
    return sanitized, None


def _trusted_argv(command: str, repo: Path) -> tuple[list[str] | None, str | None]:
    if any(char in command for char in _SHELL_SYNTAX):
        return None, "verification command contains shell syntax"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        return None, f"invalid verification command: {exc}"
    if not argv:
        return None, "verification command is empty"

    if argv[0] == "git":
        return _validate_git_args(argv[1:], repo)

    trusted_python = argv[0] in {"python", "python3", sys.executable}
    if not trusted_python:
        return None, "verification executable is not allowed"
    if len(argv) >= 2 and argv[1] != "-m":
        script = argv[1]
        if (
            script.startswith("-")
            or not script.endswith(".py")
            or not _safe_repo_path(repo, script)
        ):
            return None, "Python verification script is not allowed"
        return (
            [
                sys.executable,
                "-I",
                "-c",
                _PYTHON_MODULE_RUNNER,
                "__script__",
                str(repo),
                str((repo / script).resolve(strict=False)),
                *argv[2:],
            ],
            None,
        )
    if len(argv) < 3:
        return None, "Python verification must use an allowed module via -m"

    module = argv[2]
    module_args = argv[3:]
    validators = {
        "pytest": _validate_pytest_args,
        "ruff": _validate_ruff_args,
        "compileall": _validate_compileall_args,
    }
    validator = validators.get(module)
    if validator is None:
        return None, f"Python module is not allowed: {module}"
    error = validator(module_args, repo)
    if error is not None:
        return None, error
    if module == "pytest":
        module_args = _normalized_pytest_args(module_args)
    return (
        [
            sys.executable,
            "-I",
            "-c",
            _PYTHON_MODULE_RUNNER,
            module,
            str(repo),
            *module_args,
        ],
        None,
    )


def _command_result(
    command: str,
    exit_code: int,
    stdout: Mapping[str, Any],
    stderr: Mapping[str, Any],
    *,
    timed_out: bool = False,
    output_overflow: bool = False,
    policy_validated: bool = True,
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
        "output_overflow": output_overflow,
        "policy_validated": policy_validated,
    }


def _failed_command(command: str, exit_code: int, message: str) -> dict[str, Any]:
    return _command_result(
        command,
        exit_code,
        _empty_summary(),
        _empty_summary(message.encode("utf-8", errors="replace")),
        policy_validated=False,
    )


def _pytest_result(stdout: bytes) -> dict[str, int] | None:
    records = [
        line[len(_PYTEST_RESULT_PREFIX):]
        for line in stdout.splitlines()
        if line.startswith(_PYTEST_RESULT_PREFIX)
    ]
    if len(records) != 1:
        return None
    try:
        record = json.loads(records[0])
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    expected = {
        "collected",
        "terminal",
        "passed",
        "failed",
        "pytest_exit_code",
    }
    if set(record) != expected or not all(
        type(record[key]) is int for key in expected
    ):
        return None
    return record


def _pytest_result_is_complete(
    record: Mapping[str, int],
    exit_code: int,
) -> bool:
    return (
        exit_code == 0
        and record["pytest_exit_code"] == 0
        and record["collected"] > 0
        and record["passed"] > 0
        and record["terminal"] == record["collected"]
        and record["failed"] == 0
    )


def _execute_command(command: str, repo: Path, timeout: float) -> dict[str, Any]:
    argv, error = _trusted_argv(command, repo)
    if error is not None or argv is None:
        return _failed_command(command, 126, error or "verification command rejected")
    if timeout <= 0:
        result = _failed_command(command, 124, "verification budget exhausted")
        result["timed_out"] = True
        return result

    try:
        with tempfile.TemporaryDirectory(
            prefix="jarvis-verifier-sandbox-"
        ) as sandbox_dir:
            exit_code, stdout, stderr, timed_out, output_overflow = _spawn(
                argv,
                repo,
                timeout,
                git=argv[0] == _GIT_EXECUTABLE,
                sandbox_root=Path(sandbox_dir),
            )
    except FileNotFoundError as exc:
        return _failed_command(command, 127, str(exc))
    except OSError as exc:
        return _failed_command(command, 126, str(exc))

    if "pytest" in argv and not timed_out and not output_overflow:
        combined_output = stdout["_data"] + b"\n" + stderr["_data"]
        pytest_record = _pytest_result(stdout["_data"])
        if (
            pytest_record is None
            or pytest_record["pytest_exit_code"] != exit_code
            or (
                exit_code == 0
                and (
                    _PYTEST_FAILURE_SUMMARY.search(combined_output)
                    or not _pytest_result_is_complete(
                        pytest_record,
                        exit_code,
                    )
                )
            )
        ):
            exit_code = 86
    return _command_result(
        command,
        exit_code,
        stdout,
        stderr,
        timed_out=timed_out,
        output_overflow=output_overflow,
    )


def _mutation_finding(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any] | None:
    before_paths = set(before["changed_files"])
    after_paths = set(after["changed_files"])
    modified = sorted(
        path
        for path in before_paths & after_paths
        if before["fingerprints"][path] != after["fingerprints"][path]
    )
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    if not (added or removed or modified):
        return None
    return {
        "id": "verifier_worktree_mutation",
        "status": "open",
        "severity": "high",
        "message": "verification commands mutated the Git-visible worktree",
        "added": added,
        "removed": removed,
        "modified": modified,
    }


def collect_completion_evidence(
    spec: TaskSpec,
    repo_path: str | Path,
    base_ref: str,
    *,
    execute_commands: bool = True,
) -> dict[str, Any]:
    """Collect the evidence envelope consumed by ``evaluate_completion``."""
    repo = _repo_root(repo_path)
    _validate_git_trust(repo)
    base_commit = _resolve_base(repo, base_ref)
    completion_commit_before = _current_head(repo)
    before = _worktree_snapshot(repo, base_commit)

    deadline = time.monotonic() + spec.budget.wall_time_seconds
    commands = (
        [
            _execute_command(command, repo, deadline - time.monotonic())
            for command in spec.verification_commands
        ]
        if execute_commands
        else []
    )
    completion_commit_after = _current_head(repo)
    after = _worktree_snapshot(repo, base_commit)
    findings = []
    worktree_finding = _mutation_finding(before, after)
    if worktree_finding:
        findings.append(worktree_finding)
    if completion_commit_before != completion_commit_after:
        findings.append(
            {
                "id": "verifier_head_mutation",
                "status": "open",
                "severity": "critical",
                "message": "verification commands changed the completion commit",
                "before_commit": completion_commit_before,
                "after_commit": completion_commit_after,
            }
        )

    return {
        "observer": "loop",
        "base_commit": base_commit,
        "completion_commit_before_commands": completion_commit_before,
        "completion_commit": completion_commit_after,
        "changed_files": after["changed_files"],
        "changed_files_before_commands": before["changed_files"],
        "changed_files_after_commands": after["changed_files"],
        "commands": commands,
        "policy_findings": findings,
        "evidence_policy": {
            "version": _POLICY_VERSION,
            "trusted_commands_only": True,
            "git_no_replace_objects": True,
            "git_hidden_index_flags_rejected": True,
            "process_group_isolation": True,
            "capture_limit_bytes_per_stream": MAX_CAPTURE_BYTES,
            "full_sandbox": (
                sys.platform == "darwin" and _SANDBOX_EXEC.is_file()
            ),
        },
    }


def compact_completion_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Return durable evidence without command output previews or source text."""
    commands = []
    for item in evidence.get("commands", []):
        if not isinstance(item, Mapping):
            continue
        commands.append(
            {
                "command": item.get("command"),
                "exit_code": item.get("exit_code"),
                "stdout_sha256": item.get("stdout_sha256"),
                "stderr_sha256": item.get("stderr_sha256"),
                "timed_out": item.get("timed_out"),
                "output_overflow": item.get("output_overflow"),
                "policy_validated": item.get("policy_validated"),
            }
        )
    return {
        "observer": evidence.get("observer"),
        "base_commit": evidence.get("base_commit"),
        "completion_commit_before_commands": evidence.get(
            "completion_commit_before_commands"
        ),
        "completion_commit": evidence.get("completion_commit"),
        "changed_files": list(evidence.get("changed_files", [])),
        "commands": commands,
        "policy_findings": list(evidence.get("policy_findings", [])),
        "commit_gate": dict(evidence.get("commit_gate", {})),
        "collected_at": evidence.get("collected_at"),
    }


@contextmanager
def _detached_verification_worktree(
    repo_path: str | Path,
    completion_ref: str,
):
    source_repo = _repo_root(repo_path)
    _validate_git_trust(source_repo)
    completion_commit = _resolve_base(source_repo, completion_ref)
    try:
        current_head = capture_clean_head(source_repo)
    except Exception as exc:
        raise CompletionEvidenceError(
            f"completion worktree is not clean: {type(exc).__name__}"
        ) from exc
    if current_head != completion_commit:
        raise CompletionEvidenceError(
            "completion commit no longer matches the source worktree HEAD"
        )

    with tempfile.TemporaryDirectory(prefix="jarvis-verify-worktree-") as temp_dir:
        worktree = Path(temp_dir) / "checkout"
        _run_git(
            source_repo,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(worktree),
            completion_commit,
        )
        try:
            yield worktree, completion_commit
        finally:
            try:
                _run_git(
                    source_repo,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                )
            except CompletionEvidenceError:
                _run_git(source_repo, "worktree", "prune")


def verify_completion(
    spec: TaskSpec,
    repo_path: str | Path,
    base_ref: str,
    *,
    completion_ref: str,
) -> CompletionAssessment:
    """Collect evidence and enforce REVIEW.md against the pinned completion commit."""
    with _detached_verification_worktree(
        repo_path,
        completion_ref,
    ) as (worktree, completion_commit):
        base_commit = _resolve_base(worktree, base_ref)
        gate = run_commit_gate(worktree, base_commit, completion_commit)
        evidence = collect_completion_evidence(
            spec,
            worktree,
            base_ref,
            execute_commands=gate.passed,
        )
        base_commit = str(evidence["base_commit"])

        policy_findings = list(evidence.get("policy_findings", []))
        policy_findings.extend(
            {
                "id": f"commit_gate_{finding.rule.lower()}",
                "status": "open",
                "severity": "critical",
                "message": finding.summary(),
            }
            for finding in gate.findings
        )
        evidence["policy_findings"] = policy_findings
        evidence["commit_gate"] = {
            "base_commit": gate.base_commit,
            "completion_commit": gate.completion_commit,
            "files_checked": list(gate.files_checked),
            "reasons": gate.reasons(),
            "passed": gate.passed,
        }
        verdict = evaluate_completion(spec, evidence)
    return CompletionAssessment(
        base_commit=base_commit,
        completion_commit=completion_commit,
        evidence=evidence,
        gate=gate,
        verdict=verdict,
    )
