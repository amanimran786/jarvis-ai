"""Small, auditable V2 tool surface for local code inspection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class LocalToolError(RuntimeError):
    """Raised when a proposed local tool call is invalid or unsafe."""


READ_ONLY_TOOLS = ("file", "git")

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "file": {
        "description": "Read one UTF-8 text file inside the configured workspace.",
        "properties": {
            "action": {"type": "string", "enum": ["read"]},
            "path": {"type": "string", "maxLength": 4096},
        },
        "required": ["action", "path"],
    },
    "git": {
        "description": "Inspect the configured Git workspace without modifying it.",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "diff", "log", "branch", "show"],
            },
            "n": {"type": "integer", "minimum": 1, "maximum": 50},
            "ref": {"type": "string", "maxLength": 200},
        },
        "required": ["action"],
    },
}


def model_tool_schemas() -> list[dict[str, Any]]:
    """Expose only the self-contained V2 read-only tool contract."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _TOOL_SCHEMAS[name]["description"],
                "parameters": {
                    "type": "object",
                    "properties": _TOOL_SCHEMAS[name]["properties"],
                    "required": _TOOL_SCHEMAS[name]["required"],
                    "additionalProperties": False,
                },
            },
        }
        for name in READ_ONLY_TOOLS
    ]


def _validate_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in _TOOL_SCHEMAS:
        raise LocalToolError(f"tool is not enabled in V2 bootstrap: {name}")
    if not isinstance(arguments, dict):
        raise LocalToolError("tool arguments must be an object")
    contract = _TOOL_SCHEMAS[name]
    properties = contract["properties"]
    action = arguments.get("action")
    if name == "file" and action != "read":
        raise LocalToolError("V2 bootstrap permits file reads only")
    if name == "git" and action not in properties["action"]["enum"]:
        raise LocalToolError("V2 bootstrap permits read-only git actions")
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise LocalToolError(
            f"unknown argument(s) for tool '{name}': {', '.join(unknown)}"
        )
    missing = [
        key
        for key in contract["required"]
        if key not in arguments or not str(arguments[key]).strip()
    ]
    if missing:
        raise LocalToolError(f"missing required argument '{missing[0]}' for tool '{name}'")
    normalized = dict(arguments)
    if "path" in normalized:
        normalized["path"] = str(normalized["path"])
        if len(normalized["path"]) > properties["path"]["maxLength"]:
            raise LocalToolError("file path is too long")
    if "ref" in normalized:
        normalized["ref"] = str(normalized["ref"])
        if len(normalized["ref"]) > properties["ref"]["maxLength"]:
            raise LocalToolError("git ref is too long")
    if "n" in normalized:
        value = normalized["n"]
        if isinstance(value, bool):
            raise LocalToolError("git log count must be an integer")
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise LocalToolError("git log count must be an integer") from exc
        if not properties["n"]["minimum"] <= value <= properties["n"]["maximum"]:
            raise LocalToolError("git log count must be between 1 and 50")
        normalized["n"] = value
    return normalized


class ReadOnlyLocalTools:
    """Execute a deliberately narrow set of workspace-local inspection tools."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise LocalToolError("workspace must be a directory")

    def _workspace_path(self, raw_path: str) -> Path:
        candidate = (self.workspace / raw_path).resolve(strict=False)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise LocalToolError("path escapes the configured workspace") from exc
        return candidate

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        normalized = _validate_args(name, arguments)
        if name == "file":
            if normalized.get("action") != "read":
                raise LocalToolError("V2 bootstrap permits file reads only")
            path = self._workspace_path(str(normalized["path"]))
            if not path.is_file():
                raise LocalToolError("requested file does not exist")
            return path.read_text(encoding="utf-8", errors="replace")[:100_000]
        if name == "git":
            action = str(normalized.get("action"))
            if action not in {"status", "diff", "log", "branch", "show"}:
                raise LocalToolError("V2 bootstrap permits read-only git actions")
            command = [
                "/usr/bin/git",
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                action,
            ]
            if action == "status":
                command.append("--short")
            elif action == "diff":
                command.extend(["--no-ext-diff", "--no-textconv"])
            elif action == "log":
                command.extend(["--oneline", f"-{normalized.get('n', 10)}"])
            elif action == "show":
                ref = str(normalized.get("ref", "HEAD"))
                if ref.startswith("-"):
                    raise LocalToolError("git ref cannot begin with a dash")
                command.extend(["--no-ext-diff", "--no-textconv", "--stat", ref])
            git_environment = {
                key: value for key, value in os.environ.items() if not key.startswith("GIT_")
            }
            git_environment.update(
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                }
            )
            completed = subprocess.run(
                command,
                cwd=self.workspace,
                env=git_environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            output = (completed.stdout + completed.stderr).strip()
            if completed.returncode != 0:
                raise LocalToolError(output or f"git {action} failed")
            return output[:100_000] or "(no output)"
        raise LocalToolError(f"tool is not enabled in V2 bootstrap: {name}")
