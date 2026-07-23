"""Immutable Git-object enforcement for Jarvis's automated REVIEW.md gate."""

from __future__ import annotations

import ast
import collections
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from harness.pre_commit_check import run_checks


INFRASTRUCTURE_RULES = frozenset(
    {
        "INVALID_GIT_EVIDENCE",
        "GIT_INSPECTION_ERROR",
        "DIFF_ERROR",
        "BLOB_READ_ERROR",
        "BLOB_NOT_FOUND",
        "CHECKER_ERROR",
    }
)


@dataclass(frozen=True)
class CommitGateFinding:
    """A redacted gate finding safe to persist in queue state and logs."""

    path: str
    line: int
    rule: str

    def summary(self) -> str:
        safe_path = self.path.encode("unicode_escape").decode("ascii")
        return f"{safe_path}:{self.line}: [{self.rule}]"


@dataclass
class CommitGateResult:
    """Result of checking changed Python blobs in one immutable commit range."""

    passed: bool
    base_commit: str = ""
    completion_commit: str = ""
    files_checked: list[str] = field(default_factory=list)
    findings: list[CommitGateFinding] = field(default_factory=list)

    def reasons(self) -> list[str]:
        return [finding.summary() for finding in self.findings]

    @property
    def infrastructure_failed(self) -> bool:
        return any(
            finding.rule in INFRASTRUCTURE_RULES for finding in self.findings
        )


class CommitGateError(RuntimeError):
    """Raised internally when Git evidence cannot be collected safely."""


def _run_git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
    )
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            timeout=30,
            shell=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitGateError(f"Git command failed: {type(exc).__name__}") from exc
    if check and result.returncode != 0:
        raise CommitGateError(f"Git command failed with exit {result.returncode}")
    return result


def _resolve_repo(repo_path: str | Path) -> Path:
    candidate = Path(repo_path).expanduser().resolve(strict=True)
    result = _run_git(candidate, "rev-parse", "--show-toplevel")
    root = Path(os.fsdecode(result.stdout).strip()).resolve(strict=True)
    if root != candidate:
        raise CommitGateError("repo_path must be the Git worktree root")
    return root


def _resolve_commit(repo: Path, ref: str) -> str:
    raw_ref = str(ref or "").strip()
    if not raw_ref or "\x00" in raw_ref:
        raise CommitGateError("Git commit reference is missing")
    result = _run_git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{raw_ref}^{{commit}}",
    )
    commit = os.fsdecode(result.stdout).strip()
    if len(commit) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in commit
    ):
        raise CommitGateError("Git did not return a canonical commit SHA")
    return commit


def _changed_paths(
    repo: Path,
    base_commit: str,
    completion_commit: str,
) -> list[tuple[str, str]]:
    result = _run_git(
        repo,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        base_commit,
        completion_commit,
        "--",
    )
    fields = [os.fsdecode(item) for item in result.stdout.split(b"\x00") if item]
    if len(fields) % 2:
        raise CommitGateError("Git returned malformed changed-file metadata")
    changed: list[tuple[str, str]] = []
    for index in range(0, len(fields), 2):
        status, path = fields[index], fields[index + 1]
        changed.append((status, path))
    return changed


def _is_python_source(path: str, mode: str, content: bytes) -> bool:
    if path.endswith((".py", ".pyw")):
        return True
    lines = content.splitlines()
    first_line = lines[0].lower() if lines else b""
    return mode == "100755" and first_line.startswith(b"#!") and b"python" in first_line


_SUBPROCESS_CALLS = frozenset(
    {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "run",
    }
)
_SHELL_APIS = frozenset({"getoutput", "getstatusoutput"})
_POSITIONAL_SHELL_APIS = frozenset(
    {"Popen", "call", "check_call", "check_output", "run"}
)


def _call_aliases(
    tree: ast.AST,
) -> tuple[set[str], dict[str, str], set[str], set[str]]:
    module_aliases = {"subprocess"}
    call_aliases: dict[str, str] = {}
    functools_aliases = {"functools"}
    partial_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
                elif alias.name == "functools":
                    functools_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_CALLS:
                    call_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "functools":
            for alias in node.names:
                if alias.name == "partial":
                    partial_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in module_aliases
            and node.value.attr in _SUBPROCESS_CALLS
        ):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    call_aliases[target.id] = node.value.attr
    return module_aliases, call_aliases, functools_aliases, partial_aliases


def _subprocess_call_name(
    func: ast.AST,
    module_aliases: set[str],
    call_aliases: dict[str, str],
) -> str | None:
    if isinstance(func, ast.Name):
        return call_aliases.get(func.id)
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in module_aliases
        and func.attr in _SUBPROCESS_CALLS
    ):
        return func.attr
    return None


def _is_partial_call(
    func: ast.AST,
    functools_aliases: set[str],
    partial_aliases: set[str],
) -> bool:
    if isinstance(func, ast.Name):
        return func.id in partial_aliases
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in functools_aliases
        and func.attr == "partial"
    )


def _is_literal_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _expanded_shell_value(node: ast.AST) -> tuple[bool, ast.AST | None]:
    if not isinstance(node, ast.Dict):
        return False, None
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None or not isinstance(key, ast.Constant):
            return False, None
        if key.value == "shell":
            return True, value
    return True, None


def _ast_security_findings(path: str, content: bytes) -> list[CommitGateFinding]:
    try:
        tree = ast.parse(content, filename=path)
    except (SyntaxError, ValueError):
        return []
    (
        module_aliases,
        call_aliases,
        functools_aliases,
        partial_aliases,
    ) = _call_aliases(tree)
    os_aliases = {"os"}
    posix_aliases = {"posix"}
    hard_exit_aliases: set[str] = set()
    process_exec_aliases: set[str] = set()
    native_modules = {"ctypes", "_ctypes", "cffi"}
    importlib_aliases = {"importlib"}
    findings: list[CommitGateFinding] = []
    for imported in ast.walk(tree):
        if isinstance(imported, ast.Import):
            for alias in imported.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or alias.name)
                elif alias.name == "posix":
                    posix_aliases.add(alias.asname or alias.name)
                elif alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                if alias.name.split(".", 1)[0] in native_modules:
                    findings.append(
                        CommitGateFinding(
                            path,
                            getattr(imported, "lineno", 0),
                            "NATIVE_FFI",
                        )
                    )
        elif isinstance(imported, ast.ImportFrom) and imported.module == "os":
            for alias in imported.names:
                if alias.name == "_exit":
                    hard_exit_aliases.add(alias.asname or alias.name)
                elif alias.name.startswith("exec"):
                    process_exec_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(imported, ast.ImportFrom)
            and imported.module == "posix"
        ):
            for alias in imported.names:
                if alias.name == "_exit":
                    hard_exit_aliases.add(alias.asname or alias.name)
                elif alias.name.startswith("exec"):
                    process_exec_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(imported, ast.ImportFrom)
            and str(imported.module or "").split(".", 1)[0]
            in native_modules
        ):
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(imported, "lineno", 0),
                    "NATIVE_FFI",
                )
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        hard_exit = (
            isinstance(node.func, ast.Name)
            and node.func.id in hard_exit_aliases
        ) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in os_aliases
            and node.func.attr == "_exit"
        ) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in posix_aliases
            and node.func.attr == "_exit"
        )
        if hard_exit:
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(node, "lineno", 0),
                    "HARD_PROCESS_EXIT",
                )
            )
        process_replacement = (
            isinstance(node.func, ast.Name)
            and node.func.id in process_exec_aliases
        ) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in (os_aliases | posix_aliases)
            and node.func.attr.startswith("exec")
        )
        if process_replacement:
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(node, "lineno", 0),
                    "PROCESS_REPLACEMENT",
                )
            )
        dynamic_module = None
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
        ):
            dynamic_module = node.args[0]
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_aliases
            and node.func.attr == "import_module"
            and node.args
        ):
            dynamic_module = node.args[0]
        if (
            isinstance(dynamic_module, ast.Constant)
            and isinstance(dynamic_module.value, str)
            and dynamic_module.value.split(".", 1)[0] in native_modules
        ):
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(node, "lineno", 0),
                    "NATIVE_FFI",
                )
            )
        call_name = _subprocess_call_name(
            node.func,
            module_aliases,
            call_aliases,
        )
        partial_target = None
        if (
            _is_partial_call(node.func, functools_aliases, partial_aliases)
            and node.args
        ):
            partial_target = _subprocess_call_name(
                node.args[0],
                module_aliases,
                call_aliases,
            )
        subprocess_call = call_name is not None or partial_target is not None
        effective_call = call_name or partial_target
        if effective_call in _SHELL_APIS:
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(node, "lineno", 0),
                    "SHELL_API",
                )
            )
        if (
            effective_call in _POSITIONAL_SHELL_APIS
            and len(node.args) > (9 if partial_target else 8)
            and not _is_literal_false(
                node.args[9 if partial_target else 8]
            )
        ):
            shell_argument = node.args[9 if partial_target else 8]
            findings.append(
                CommitGateFinding(
                    path,
                    getattr(
                        shell_argument,
                        "lineno",
                        getattr(node, "lineno", 0),
                    ),
                    "SHELL_TRUE",
                )
            )
        for keyword in node.keywords:
            if keyword.arg == "shell" and not _is_literal_false(keyword.value):
                findings.append(
                    CommitGateFinding(
                        path,
                        getattr(keyword, "lineno", getattr(node, "lineno", 0)),
                        "SHELL_TRUE",
                    )
                )
            elif keyword.arg is None and subprocess_call:
                fully_known, shell_value = _expanded_shell_value(keyword.value)
                if not fully_known:
                    findings.append(
                        CommitGateFinding(
                            path,
                            getattr(keyword, "lineno", getattr(node, "lineno", 0)),
                            "DYNAMIC_SUBPROCESS_KWARGS",
                        )
                    )
                elif shell_value is not None and not _is_literal_false(shell_value):
                    findings.append(
                        CommitGateFinding(
                            path,
                            getattr(shell_value, "lineno", getattr(node, "lineno", 0)),
                            "SHELL_TRUE",
                        )
                    )
    return findings


def _tree_blob(repo: Path, commit: str, path: str) -> tuple[str, bytes] | None:
    result = _run_git(
        repo,
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        commit,
        "--",
        path,
    )
    record = result.stdout.removesuffix(b"\x00")
    if not record:
        return None
    metadata, separator, recorded_path = record.partition(b"\t")
    if not separator or os.fsdecode(recorded_path) != path:
        raise CommitGateError("Git returned malformed tree metadata")
    parts = metadata.split()
    if len(parts) != 3:
        raise CommitGateError("Git returned malformed tree entry")
    mode, object_type, blob_sha = (os.fsdecode(part) for part in parts)
    if object_type != "blob":
        return mode, b""
    content = _run_git(repo, "cat-file", "blob", blob_sha).stdout
    return mode, content


def _new_suppressions(base: bytes, completion: bytes) -> list[int]:
    marker = b"# pre-commit-ok"
    remaining = collections.Counter(
        line for line in base.splitlines() if marker in line
    )
    introduced: list[int] = []
    for line_number, line in enumerate(completion.splitlines(), start=1):
        if marker not in line:
            continue
        if remaining[line]:
            remaining[line] -= 1
        else:
            introduced.append(line_number)
    return introduced


def _infrastructure_failure(rule: str) -> CommitGateResult:
    return CommitGateResult(
        passed=False,
        findings=[CommitGateFinding("<commit-gate>", 0, rule)],
    )


def capture_clean_head(repo_path: str | Path) -> str:
    """Return the exact clean worktree HEAD or raise a redacted gate error."""
    repo = _resolve_repo(repo_path)
    head = _resolve_commit(repo, "HEAD")
    status = _run_git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status.stdout:
        raise CommitGateError("worktree is dirty")
    return head


def run_commit_gate(
    repo_path: str | Path,
    base_ref: str,
    completion_ref: str,
) -> CommitGateResult:
    """Check Python blobs changed between two commits and fail closed."""
    try:
        repo = _resolve_repo(repo_path)
        base_commit = _resolve_commit(repo, base_ref)
        completion_commit = _resolve_commit(repo, completion_ref)
    except (CommitGateError, OSError, RuntimeError, ValueError):
        return _infrastructure_failure("INVALID_GIT_EVIDENCE")

    result = CommitGateResult(
        passed=False,
        base_commit=base_commit,
        completion_commit=completion_commit,
    )

    try:
        ancestor = _run_git(
            repo,
            "merge-base",
            "--is-ancestor",
            base_commit,
            completion_commit,
            check=False,
        )
        if ancestor.returncode == 1:
            result.findings.append(
                CommitGateFinding("<commit-range>", 0, "BASE_NOT_ANCESTOR")
            )
        elif ancestor.returncode != 0:
            raise CommitGateError("could not verify commit ancestry")

        current_head = _resolve_commit(repo, "HEAD")
        if current_head != completion_commit:
            result.findings.append(
                CommitGateFinding("<worktree>", 0, "HEAD_MISMATCH")
            )

        status = _run_git(
            repo,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if status.stdout:
            result.findings.append(
                CommitGateFinding("<worktree>", 0, "DIRTY_WORKTREE")
            )
        changed = _changed_paths(repo, base_commit, completion_commit)
    except (CommitGateError, OSError, RuntimeError, ValueError):
        result.findings.append(
            CommitGateFinding("<commit-gate>", 0, "GIT_INSPECTION_ERROR")
        )
        return result

    with tempfile.TemporaryDirectory(prefix="jarvis-commit-gate-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, (status_code, path) in enumerate(changed):
            if status_code.startswith("D"):
                continue
            try:
                completion_entry = _tree_blob(repo, completion_commit, path)
                base_entry = _tree_blob(repo, base_commit, path)
            except CommitGateError:
                result.findings.append(
                    CommitGateFinding(path, 0, "BLOB_READ_ERROR")
                )
                continue
            if completion_entry is None:
                result.findings.append(
                    CommitGateFinding(path, 0, "BLOB_NOT_FOUND")
                )
                continue
            completion_mode, completion_blob = completion_entry
            if completion_mode not in {"100644", "100755"}:
                result.findings.append(
                    CommitGateFinding(path, 0, "UNSAFE_GIT_MODE")
                )
                continue
            if not _is_python_source(path, completion_mode, completion_blob):
                continue

            base_blob = base_entry[1] if base_entry is not None else b""
            for line_number in _new_suppressions(base_blob, completion_blob):
                result.findings.append(
                    CommitGateFinding(path, line_number, "UNAUTHORIZED_SUPPRESSION")
                )
            result.findings.extend(_ast_security_findings(path, completion_blob))

            source = temp_root / f"{index:05d}.py"
            source.write_bytes(completion_blob)
            try:
                check_result = run_checks([source])
            except Exception:
                result.findings.append(
                    CommitGateFinding(path, 0, "CHECKER_ERROR")
                )
                continue
            result.files_checked.append(path)
            result.findings.extend(
                CommitGateFinding(path, finding.line, finding.rule)
                for finding in check_result.findings
            )
            result.findings.extend(
                CommitGateFinding(path, 0, "SYNTAX_ERROR")
                for _ in check_result.syntax_errors
            )

    unique_findings = dict.fromkeys(
        (finding.path, finding.line, finding.rule)
        for finding in result.findings
    )
    result.findings = [
        CommitGateFinding(path, line, rule)
        for path, line, rule in unique_findings
    ]
    result.passed = not result.findings
    return result


def write_gate_log(
    log_path: str | Path,
    task_id: str,
    session_id: str,
    result: CommitGateResult,
    *,
    timestamp: str,
) -> None:
    """Append redacted gate findings to an owner-only log."""
    path = Path(log_path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or not parent.is_dir():
        raise OSError("gate log parent must be a real directory")
    parent.chmod(0o700)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(parent, directory_flags)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("gate log target must be a regular file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            safe_timestamp = _escaped_log_field(timestamp)
            safe_task_id = _escaped_log_field(task_id)
            safe_session_id = _escaped_log_field(session_id)
            stream.write(
                f"[{safe_timestamp}] task={safe_task_id} "
                f"session={safe_session_id}\n"
            )
            for reason in result.reasons():
                stream.write(f"  {reason}\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _escaped_log_field(value: object) -> str:
    return str(value).encode("unicode_escape").decode("ascii")
