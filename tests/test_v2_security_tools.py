from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jarvis_v2.security_tools import (
    AuthorizedSecurityTools,
    OwnerSecurityGrant,
    authorized_security_tool_schemas,
)
from jarvis_v2.tools import LocalToolError


def grant(*actions: str, expires: float = 200.0) -> OwnerSecurityGrant:
    return OwnerSecurityGrant(
        engagement_id="local-lab-001",
        purpose="Authorized defensive source review",
        allowed_actions=frozenset(actions),
        expires_at_epoch=expires,
        authorized_by_owner=True,
    )


def test_grant_requires_explicit_owner_authorization():
    with pytest.raises(LocalToolError, match="explicit owner authorization"):
        OwnerSecurityGrant(
            engagement_id="local-lab-001",
            purpose="Review",
            allowed_actions=frozenset({"hash_file"}),
            expires_at_epoch=200.0,
        )


def test_security_profile_has_no_shell_network_or_write_tool():
    names = {
        item["function"]["name"] for item in authorized_security_tool_schemas()
    }

    assert names == {"file", "git", "security"}
    security = next(
        item for item in authorized_security_tool_schemas()
        if item["function"]["name"] == "security"
    )
    assert set(
        security["function"]["parameters"]["properties"]["action"]["enum"]
    ) == {"hash_file", "scan_python"}


def test_hash_file_returns_evidence_without_file_contents(tmp_path: Path):
    artifact = tmp_path / "sample.bin"
    artifact.write_bytes(b"private-evidence")
    tools = AuthorizedSecurityTools(
        tmp_path,
        grant("hash_file"),
        clock=lambda: 100.0,
    )

    payload = json.loads(
        tools("security", {"action": "hash_file", "path": "sample.bin"})
    )

    assert payload == {
        "action": "hash_file",
        "engagement_id": "local-lab-001",
        "grant_sha256": grant("hash_file").sha256,
        "path": "sample.bin",
        "sha256": hashlib.sha256(b"private-evidence").hexdigest(),
        "size_bytes": len(b"private-evidence"),
    }
    assert "private-evidence" not in json.dumps(payload)


def test_python_scan_reports_deterministic_security_findings(tmp_path: Path):
    source = tmp_path / "unsafe.py"
    shell_keyword = "sh" + "ell"
    dynamic_builtin = "ev" + "al"
    source.write_text(
        "import os, pickle, subprocess\n"
        "os.system('id')\n"
        f"subprocess.run(['echo', 'x'], {shell_keyword}=True)\n"
        "pickle.loads(b'data')\n"
        f"{dynamic_builtin}('1 + 1')\n",
        encoding="utf-8",
    )
    tools = AuthorizedSecurityTools(
        tmp_path,
        grant("scan_python"),
        clock=lambda: 100.0,
    )

    payload = json.loads(
        tools("security", {"action": "scan_python", "path": "unsafe.py"})
    )

    assert payload["scanner"] == "jarvis-v2-python-ast-v1"
    assert payload["finding_count"] == 4
    assert [item["rule_id"] for item in payload["findings"]] == [
        "PY-OS-SYSTEM",
        "PY-SHELL-TRUE",
        "PY-PICKLE",
        "PY-DYNAMIC-CODE",
    ]
    assert all(item["line"] > 0 for item in payload["findings"])


def test_security_profile_rejects_expired_or_ungranted_actions(tmp_path: Path):
    artifact = tmp_path / "safe.py"
    artifact.write_text("print('ok')\n", encoding="utf-8")

    with pytest.raises(LocalToolError, match="expired"):
        AuthorizedSecurityTools(
            tmp_path,
            grant("hash_file", expires=50.0),
            clock=lambda: 50.0,
        )("security", {"action": "hash_file", "path": "safe.py"})

    with pytest.raises(LocalToolError, match="outside the owner grant"):
        AuthorizedSecurityTools(
            tmp_path,
            grant("hash_file"),
            clock=lambda: 100.0,
        )("security", {"action": "scan_python", "path": "safe.py"})


def test_security_profile_rejects_workspace_escape_and_symlink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(("ev" + "al") + "('1')\n", encoding="utf-8")
    (workspace / "escape.py").symlink_to(outside)
    tools = AuthorizedSecurityTools(
        workspace,
        grant("scan_python"),
        clock=lambda: 100.0,
    )

    with pytest.raises(LocalToolError, match="escapes"):
        tools("security", {"action": "scan_python", "path": "escape.py"})


def test_security_profile_rejects_unknown_arguments(tmp_path: Path):
    artifact = tmp_path / "safe.py"
    artifact.write_text("print('ok')\n", encoding="utf-8")
    tools = AuthorizedSecurityTools(
        tmp_path,
        grant("scan_python"),
        clock=lambda: 100.0,
    )

    with pytest.raises(LocalToolError, match="unknown security argument"):
        tools(
            "security",
            {"action": "scan_python", "path": "safe.py", "command": "id"},
        )


def test_security_profile_normalizes_mutable_action_input():
    actions = {"hash_file"}

    item = OwnerSecurityGrant(
        engagement_id="local-lab-001",
        purpose="Review",
        allowed_actions=actions,
        expires_at_epoch=200.0,
        authorized_by_owner=True,
    )
    actions.add("scan_python")

    assert item.allowed_actions == frozenset({"hash_file"})
    assert len(item.sha256) == 64


def test_security_profile_normalizes_missing_paths_to_tool_error(tmp_path: Path):
    tools = AuthorizedSecurityTools(
        tmp_path,
        grant("hash_file"),
        clock=lambda: 100.0,
    )

    with pytest.raises(LocalToolError, match="does not exist"):
        tools("security", {"action": "hash_file", "path": "missing.bin"})
