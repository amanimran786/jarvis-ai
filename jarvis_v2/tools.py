"""Small, auditable V2 tool surface for local code inspection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import tool_registry


class LocalToolError(RuntimeError):
    """Raised when a proposed local tool call is invalid or unsafe."""


READ_ONLY_TOOLS = ("file", "git")


def model_tool_schemas() -> list[dict[str, Any]]:
    """Expose only the read-only portions of the existing typed registry."""
    schemas: list[dict[str, Any]] = []
    for name in READ_ONLY_TOOLS:
        spec = tool_registry.get_tool_spec(name)
        if spec is None:
            continue
        properties: dict[str, Any] = {}
        for arg_name, meta in spec.args_schema.items():
            schema_type = {
                "bool": "boolean",
                "int": "integer",
                "float": "number",
            }.get(meta.get("type", "string"), meta.get("type", "string"))
            prop: dict[str, Any] = {"type": schema_type}
            choices = meta.get("choices")
            if choices is not None:
                allowed = list(choices)
                if name == "file" and arg_name == "action":
                    allowed = ["read"]
                if name == "git" and arg_name == "action":
                    allowed = ["status", "diff", "log", "branch", "show"]
                prop["enum"] = allowed
            properties[arg_name] = prop
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(spec.required),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return schemas


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
        valid, normalized, error = tool_registry.validate_args(name, arguments)
        if not valid:
            raise LocalToolError(error)
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
