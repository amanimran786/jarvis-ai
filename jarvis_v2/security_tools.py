"""Owner-authorized, read-only security analysis tools for Jarvis V2."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .tools import LocalToolError, READ_ONLY_TOOLS, ReadOnlyLocalTools, model_tool_schemas
from .auth_logs import MAX_LOG_BYTES, analyze_auth_log


SECURITY_ACTIONS = frozenset({"hash_file", "scan_python", "triage_auth_log"})
_MAX_HASH_BYTES = 100 * 1024 * 1024
_MAX_SCAN_BYTES = 2 * 1024 * 1024

_SECURITY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "security",
        "description": (
            "Perform an owner-authorized, read-only security check on one file "
            "inside the configured workspace. triage_auth_log analyzes normalized "
            "authentication JSONL and returns bounded alerts with source line evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(SECURITY_ACTIONS),
                },
                "path": {"type": "string", "maxLength": 4096},
            },
            "required": ["action", "path"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class OwnerSecurityGrant:
    """Explicit, expiring authorization for a narrow security engagement."""

    engagement_id: str
    purpose: str
    allowed_actions: frozenset[str]
    expires_at_epoch: float
    authorized_by_owner: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))
        if self.authorized_by_owner is not True:
            raise LocalToolError("security tools require explicit owner authorization")
        if not self.engagement_id.strip() or self.engagement_id != self.engagement_id.strip():
            raise LocalToolError("engagement id must be non-empty and trimmed")
        if not self.purpose.strip() or self.purpose != self.purpose.strip():
            raise LocalToolError("security purpose must be non-empty and trimmed")
        if not self.allowed_actions:
            raise LocalToolError("security grant must allow at least one action")
        unknown = sorted(self.allowed_actions - SECURITY_ACTIONS)
        if unknown:
            raise LocalToolError(f"unknown security action(s): {', '.join(unknown)}")
        if not math.isfinite(self.expires_at_epoch) or self.expires_at_epoch <= 0:
            raise LocalToolError("security grant expiry must be positive")

    @property
    def sha256(self) -> str:
        payload = {
            "allowed_actions": sorted(self.allowed_actions),
            "authorized_by_owner": self.authorized_by_owner,
            "engagement_id": self.engagement_id,
            "expires_at_epoch": self.expires_at_epoch,
            "purpose": self.purpose,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def authorized_security_tool_schemas() -> list[dict[str, Any]]:
    """Return the default read-only tools plus the security-analysis contract."""
    return [*model_tool_schemas(), json.loads(json.dumps(_SECURITY_SCHEMA))]


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_false(keyword: ast.keyword) -> bool:
    return keyword.arg == "verify" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False


def _literal_true(keyword: ast.keyword) -> bool:
    return keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True


class _PythonSecurityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []

    def _add(self, node: ast.AST, rule_id: str, severity: str, summary: str) -> None:
        self.findings.append(
            {
                "rule_id": rule_id,
                "severity": severity,
                "line": int(getattr(node, "lineno", 0)),
                "column": int(getattr(node, "col_offset", 0)) + 1,
                "summary": summary,
            }
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name in {"eval", "exec", "builtins.eval", "builtins.exec"}:
            self._add(node, "PY-DYNAMIC-CODE", "high", "dynamic code execution")
        if name == "os.system":
            self._add(node, "PY-OS-SYSTEM", "high", "shell command through os.system")
        if name.startswith("subprocess.") and any(_literal_true(item) for item in node.keywords):
            self._add(node, "PY-SHELL-TRUE", "high", "subprocess enables shell parsing")
        if name in {"pickle.load", "pickle.loads"}:
            self._add(node, "PY-PICKLE", "high", "unsafe pickle deserialization")
        if name == "yaml.load":
            self._add(node, "PY-YAML-LOAD", "medium", "YAML load requires a safe loader")
        if name == "tempfile.mktemp":
            self._add(node, "PY-INSECURE-TEMP", "medium", "insecure temporary path creation")
        if any(_literal_false(item) for item in node.keywords):
            self._add(node, "PY-TLS-VERIFY-OFF", "high", "TLS certificate verification disabled")
        self.generic_visit(node)


class AuthorizedSecurityTools:
    """Add deterministic defensive analysis without adding shell or network access."""

    def __init__(
        self,
        workspace: Path,
        grant: OwnerSecurityGrant,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.local_tools = ReadOnlyLocalTools(workspace)
        self.workspace = self.local_tools.workspace
        self.grant = grant
        self.clock = clock

    def _path(self, raw_path: str) -> Path:
        try:
            candidate = (self.workspace / raw_path).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise LocalToolError("requested security path does not exist") from exc
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise LocalToolError("path escapes the configured workspace") from exc
        if not candidate.is_file():
            raise LocalToolError("requested path is not a regular file")
        return candidate

    def _authorize(self, action: str) -> None:
        if self.clock() >= self.grant.expires_at_epoch:
            raise LocalToolError("security authorization has expired")
        if action not in self.grant.allowed_actions:
            raise LocalToolError(f"security action is outside the owner grant: {action}")

    @staticmethod
    def _validate_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(arguments, dict):
            raise LocalToolError("security tool arguments must be an object")
        unknown = sorted(set(arguments) - {"action", "path"})
        if unknown:
            raise LocalToolError(f"unknown security argument(s): {', '.join(unknown)}")
        action = arguments.get("action")
        path = arguments.get("path")
        if not isinstance(action, str) or action not in SECURITY_ACTIONS:
            raise LocalToolError("unsupported security action")
        if not isinstance(path, str) or not path.strip():
            raise LocalToolError("security path must be a non-empty string")
        if len(path) > 4096:
            raise LocalToolError("security path is too long")
        return action, path

    def _read_snapshot(self, path: Path, limit: int) -> bytes:
        """Open relative to pinned directory descriptors; reject symlink swaps."""
        directory = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.relative_to(self.workspace).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise LocalToolError("security artifact is not regular or exceeds the size limit")
                data = handle.read(limit + 1)
                after = os.fstat(handle.fileno())
                if len(data) > limit:
                    raise LocalToolError("security artifact exceeds the size limit")
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise LocalToolError("security artifact changed during reading; retry on a stable copy")
                return data
        except OSError as exc:
            raise LocalToolError("security artifact could not be opened safely") from exc
        finally:
            os.close(directory)

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        if name in READ_ONLY_TOOLS:
            return self.local_tools(name, arguments)
        if name != "security":
            raise LocalToolError(f"tool is not enabled in authorized security profile: {name}")
        action, raw_path = self._validate_arguments(arguments)
        self._authorize(action)
        path = self._path(raw_path)
        limit = _MAX_HASH_BYTES if action == "hash_file" else MAX_LOG_BYTES if action == "triage_auth_log" else _MAX_SCAN_BYTES
        data = self._read_snapshot(path, limit)
        size = len(data)
        artifact_digest = hashlib.sha256(data).hexdigest()
        self._authorize(action)
        relative_path = str(path.relative_to(self.workspace))
        if action == "hash_file":
            if size > _MAX_HASH_BYTES:
                raise LocalToolError("file exceeds the security hash size limit")
            payload = {
                "action": action,
                "engagement_id": self.grant.engagement_id,
                "grant_sha256": self.grant.sha256,
                "path": relative_path,
                "sha256": artifact_digest,
                "size_bytes": size,
            }
        elif action == "triage_auth_log":
            payload = {
                **analyze_auth_log(data), "action": action,
                "engagement_id": self.grant.engagement_id,
                "grant_sha256": self.grant.sha256, "path": relative_path,
            }
        else:
            if path.suffix.lower() not in {".py", ".pyw"}:
                raise LocalToolError("scan_python accepts Python source files only")
            if size > _MAX_SCAN_BYTES:
                raise LocalToolError("file exceeds the Python scan size limit")
            try:
                tree = ast.parse(data.decode("utf-8", errors="strict"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                raise LocalToolError("Python source could not be parsed safely") from exc
            visitor = _PythonSecurityVisitor()
            visitor.visit(tree)
            findings = sorted(
                visitor.findings,
                key=lambda item: (item["line"], item["column"], item["rule_id"]),
            )
            payload = {
                "action": action,
                "engagement_id": self.grant.engagement_id,
                "grant_sha256": self.grant.sha256,
                "path": relative_path,
                "sha256": artifact_digest,
                "findings": findings,
                "finding_count": len(findings),
                "scanner": "jarvis-v2-python-ast-v1",
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
