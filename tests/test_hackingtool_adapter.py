"""
Tests for tools/security/hackingtool_adapter.py.

No external services, no real subprocess calls. subprocess.run and shutil.which
are patched throughout. security_reviewer and infra.memory are mocked only while
tests execute so collection does not poison unrelated test modules.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ── Stub external deps before import ─────────────────────────────────────────

# Module-level objects only — NOT installed into sys.modules at collection time.
# sys.modules["infra"] = MagicMock() here would break test_backend_engineer.py's
# collection-time `from infra.memory import store` when we're listed first.
# sys.modules["agents.security_reviewer"] here would break test_manager.py's
# module-level `from agents.security_reviewer import SecurityVerdict` when collected after us.
# The autouse fixture below installs and tears down both around each test instead.
_mock_store = MagicMock()
_mock_memory = MagicMock()
_mock_reviewer = MagicMock()
_TEST_APPROVAL_TOKEN = "I_APPROVE_THIS_AUTHORIZED_SECURITY_TEST"

# Clean up any cached adapter module so our stubs take effect
for _mod in ["tools.security", "tools.security.hackingtool_adapter"]:
    sys.modules.pop(_mod, None)

import pytest


@pytest.fixture(autouse=True)
def _restore_stubs(monkeypatch):
    """Install stubs before each test; remove them after.

    Not set at module/collection level because they would corrupt the `infra`
    and `agents` packages for test files collected after us (e.g. when this
    file is collected first alphabetically or in reverse order).

    Also sets JARVIS_SECURITY_APPROVAL_TOKEN so that _approved_req() tests
    work after the fail-closed fix (unset env var → no static fallback).
    """
    _mock_store.reset_mock()
    _mock_store.write = MagicMock()
    _mock_memory.store = _mock_store
    _mock_reviewer.reset_mock()
    _mock_reviewer.review = MagicMock()
    monkeypatch.setitem(sys.modules, "infra.memory", _mock_memory)
    monkeypatch.setitem(sys.modules, "agents.security_reviewer", _mock_reviewer)
    monkeypatch.setenv("JARVIS_SECURITY_APPROVAL_TOKEN", _TEST_APPROVAL_TOKEN)
    yield


from tools.security.hackingtool_adapter import (
    ToolRunRequest,
    ToolRunResult,
    _ALLOWED_TOOLS,
    _BLOCKED_FLAGS,
    _check_allowlist,
    _check_blocklist,
    run_tool,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _req(
    tool: str = "gitleaks",
    args: list[str] | None = None,
    target: str = "/workspace",
    agent_id: str = "security_reviewer",
    approved_by: str | None = None,
    approval_token: str | None = None,
) -> ToolRunRequest:
    return ToolRunRequest(
        tool=tool,
        args=args or [],
        target=target,
        agent_id=agent_id,
        session_id="sess-1",
        project_id="proj-1",
        approved_by=approved_by,
        approval_token=approval_token,
    )


def _approved_req(**kwargs) -> ToolRunRequest:
    kwargs.setdefault("approved_by", "aman")
    kwargs.setdefault("approval_token", _TEST_APPROVAL_TOKEN)
    return _req(**kwargs)


# ── Blocklist checks ──────────────────────────────────────────────────────────

def test_blocked_tool_is_refused():
    reason = _check_blocklist("hydra", [])
    assert reason is not None
    assert "hydra" in reason


def test_blocked_flag_in_args_is_refused():
    reason = _check_blocklist("nmap", ["--sudo", "-sV"])
    assert reason is not None
    assert "--sudo" in reason


def test_blocked_pattern_in_arg_value():
    reason = _check_blocklist("nmap", ["-p", "80", "--script=metasploit"])
    assert reason is not None


def test_allowed_tool_passes_blocklist():
    assert _check_blocklist("gitleaks", ["detect", "--source", "/workspace"]) is None


def test_blocked_flag_set_covers_known_dangerous_flags():
    for flag in ("--sudo", "sudo", "--privileged", "--force", "--payload", "--lhost", "--lport"):
        assert flag in _BLOCKED_FLAGS, f"{flag} missing from _BLOCKED_FLAGS"


# ── Allowlist / arg validation ────────────────────────────────────────────────

def test_unknown_tool_is_refused():
    reason = _check_allowlist("ncrack", [], "target")
    assert reason is not None
    assert "allowlist" in reason


def test_allowed_tool_known_flag_passes():
    assert _check_allowlist("gitleaks", ["detect", "--source"], "/workspace") is None


def test_disallowed_flag_for_tool_is_refused():
    reason = _check_allowlist("gitleaks", ["--exploit"], "/workspace")
    assert reason is not None
    assert "--exploit" in reason


def test_target_in_args_is_not_double_validated():
    # The target itself appearing as an arg should not trigger the flag check
    assert _check_allowlist("nmap", ["/my/target"], "/my/target") is None


# ── Approval gate ─────────────────────────────────────────────────────────────

def test_active_tool_without_approval_is_blocked():
    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(tool="nmap", args=["-sV"], target="192.168.1.1"))
    assert result.blocked
    assert "approval" in result.block_reason.lower()


def test_active_tool_with_approval_is_not_blocked_by_gate():
    """With approval set, the gate passes (execution may still fail if tool absent)."""
    with patch("shutil.which", return_value=None):  # not installed → different block
        result = run_tool(_approved_req(tool="nmap", args=["-sV"], target="192.168.1.1"))
    assert result.blocked
    assert "PATH" in result.block_reason  # blocked by "not in PATH", not by gate


def test_approval_string_without_token_is_blocked():
    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(tool="nmap", args=["-sV"], target="192.168.1.1", approved_by="aman"))
    assert result.blocked
    assert "approval_token" in result.block_reason


def test_agent_cannot_self_approve_security_tool():
    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(
            tool="nmap",
            args=["-sV"],
            target="192.168.1.1",
            agent_id="security_reviewer",
            approved_by="security_reviewer",
            approval_token=_TEST_APPROVAL_TOKEN,
        ))
    assert result.blocked
    assert "human approval" in result.block_reason


def test_passive_local_tool_requires_human_approval():
    """Even passive local security tools require explicit approval."""
    with patch("shutil.which", return_value="/usr/bin/gitleaks"):
        result = run_tool(_req(tool="gitleaks", args=["detect"], target="/workspace"))
    assert result.blocked
    assert "approval" in result.block_reason.lower()


def test_passive_local_tool_runs_with_human_approval():
    """gitleaks on a local path should not need AI reviewer, but does need human approval."""
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"findings": []}'
        mock_run.return_value.stderr = ""
        result = run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace"))
    assert not result.blocked


def test_external_target_without_approval_is_blocked():
    with patch("shutil.which", return_value="/usr/bin/subfinder"):
        result = run_tool(_req(
            tool="subfinder",
            args=["-passive"],
            target="example.com",  # external — not a local path
        ))
    # subfinder is passive but target is external → needs approval
    assert result.blocked
    assert "approval" in result.block_reason.lower()


def test_external_target_with_approval_routes_through_reviewer():
    """An external target with approval should call security_reviewer.review()."""
    mock_verdict = MagicMock()
    mock_verdict.verdict = "PASS"
    _mock_reviewer.review = MagicMock(return_value=mock_verdict)

    with (
        patch("shutil.which", return_value="/usr/bin/subfinder"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        result = run_tool(_approved_req(
            tool="subfinder",
            args=["-passive"],
            target="example.com",
        ))

    _mock_reviewer.review.assert_called_once()
    call_payload = _mock_reviewer.review.call_args.kwargs["payload"]
    assert call_payload["tool"] == "subfinder"
    assert call_payload["target"] == "example.com"
    assert not result.blocked


def test_reviewer_fail_blocks_run():
    mock_verdict = MagicMock()
    mock_verdict.verdict = "FAIL"
    mock_verdict.summary = "target is a public server, not owned"
    _mock_reviewer.review = MagicMock(return_value=mock_verdict)

    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(
            tool="nmap", args=["-sV", "-p", "80"],
            target="8.8.8.8",
            approved_by="human",
            approval_token=_TEST_APPROVAL_TOKEN,
        ))
    assert result.blocked
    assert "security_reviewer" in result.block_reason.lower()


def test_reviewer_exception_fails_closed():
    """If security_reviewer raises, the tool is blocked."""
    _mock_reviewer.review = MagicMock(side_effect=RuntimeError("reviewer down"))

    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(
            tool="nmap", args=["-sV", "-p", "443"],
            target="10.0.0.1",
            approved_by="human",
            approval_token=_TEST_APPROVAL_TOKEN,
        ))
    assert result.blocked
    assert "security_reviewer unavailable" in result.block_reason


# ── Tool not installed ────────────────────────────────────────────────────────

def test_tool_not_installed_is_blocked():
    with patch("shutil.which", return_value=None):
        result = run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace"))
    assert result.blocked
    assert "PATH" in result.block_reason


def test_env_approval_token_is_required_when_configured(monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_APPROVAL_TOKEN", "session-secret")
    with patch("shutil.which", return_value="/usr/bin/gitleaks"):
        result = run_tool(_req(
            tool="gitleaks",
            args=["detect"],
            target="/workspace",
            approved_by="aman",
            approval_token=_TEST_APPROVAL_TOKEN,
        ))
    assert result.blocked
    assert "approval_token" in result.block_reason


def test_env_approval_token_accepts_matching_human_token(monkeypatch):
    monkeypatch.setenv("JARVIS_SECURITY_APPROVAL_TOKEN", "session-secret")
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        result = run_tool(_req(
            tool="gitleaks",
            args=["detect"],
            target="/workspace",
            approved_by="aman",
            approval_token="session-secret",
        ))
    assert not result.blocked


# ── Subprocess execution ──────────────────────────────────────────────────────

def test_successful_run_captures_stdout():
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"leaks": 0}'
        mock_run.return_value.stderr = ""
        result = run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace"))

    assert not result.blocked
    assert result.exit_code == 0
    assert result.stdout == '{"leaks": 0}'
    # subprocess called with shell=False and list args
    call = mock_run.call_args
    assert call.kwargs.get("shell") is False
    assert call.args[0][0] == "gitleaks"


def test_target_appended_to_cmd_when_not_in_args():
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace/repo"))

    cmd = mock_run.call_args.args[0]
    assert cmd[-1] == "/workspace/repo"


def test_timeout_returns_exit_code_minus_one():
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("gitleaks", 300)),
        patch("pathlib.Path.mkdir"),
    ):
        result = run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace"))

    assert result.exit_code == -1
    assert "timed out" in result.stderr


# ── Audit logging ─────────────────────────────────────────────────────────────

def test_audit_log_always_written_on_block():
    """Audit entry is written even when the run is refused at the blocklist."""
    _mock_store.write = MagicMock()
    with patch("shutil.which", return_value="/usr/bin/hydra"):
        result = run_tool(_req(tool="hydra", args=[]))
    assert result.blocked
    _mock_store.write.assert_called_once()


def test_audit_log_written_on_success():
    _mock_store.write = MagicMock()
    with (
        patch("shutil.which", return_value="/usr/bin/gitleaks"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        run_tool(_approved_req(tool="gitleaks", args=["detect"], target="/workspace"))

    _mock_store.write.assert_called_once()
    kwargs = _mock_store.write.call_args.kwargs
    assert kwargs["metadata"]["type"] == "security_audit"
    assert kwargs["metadata"]["tool"] == "gitleaks"


def test_audit_log_includes_approved_by():
    _mock_store.write = MagicMock()
    mock_verdict = MagicMock()
    mock_verdict.verdict = "PASS"
    _mock_reviewer.review = MagicMock(return_value=mock_verdict)

    with (
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""
        run_tool(_approved_req(
            tool="nmap", args=["-sV", "-p", "443"],
            target="10.0.0.5",
        ))

    kwargs = _mock_store.write.call_args.kwargs
    assert kwargs["metadata"]["approved_by"] == "aman"


# ── Allowlist completeness ────────────────────────────────────────────────────

def test_all_allowed_tools_have_required_keys():
    for name, spec in _ALLOWED_TOOLS.items():
        assert "executable" in spec, f"{name} missing 'executable'"
        assert "needs_approval" in spec, f"{name} missing 'needs_approval'"
        assert "description" in spec, f"{name} missing 'description'"
        assert "allowed_args" in spec, f"{name} missing 'allowed_args'"
        assert hasattr(spec["allowed_args"], "match"), f"{name}.allowed_args must be a compiled regex"


def test_nmap_aggressive_timing_flags_pass_allowlist():
    for flag in ("-T4", "-T5"):
        reason = _check_blocklist("nmap", [flag])
        assert reason is None, f"{flag} should not be in blocklist"
        reason = _check_allowlist("nmap", [flag], "127.0.0.1")
        assert reason is None, f"{flag} should be allowed for nmap"


def test_nmap_safe_timing_flags_pass_allowlist():
    for flag in ("-T0", "-T1", "-T2", "-T3"):
        reason = _check_allowlist("nmap", [flag], "127.0.0.1")
        assert reason is None, f"{flag} should be allowed for nmap"


def test_nmap_vuln_exploit_intrusive_script_categories_pass_allowlist():
    args_list = [
        ["--script=vuln"],
        ["--script=exploit"],
        ["--script=intrusive"],
        ["--script=malware"],
        ["--script=fuzzer"],
        ["--script=vuln,exploit,intrusive"],
        ["--script=vuln,malware,fuzzer"],
        ["--script", "vuln,exploit"],
        ["--script", "malware,fuzzer"],
        ["--script", "vuln and intrusive"],
    ]
    for args in args_list:
        assert _check_blocklist("nmap", args) is None
        reason = _check_allowlist("nmap", args, "127.0.0.1")
        assert reason is None, f"{args} should be allowed for approved nmap scans"


def test_nmap_disallowed_script_categories_are_refused():
    args_list = [
        ["--script", "brute"],
        ["--script=vuln,dos"],
    ]
    for args in args_list:
        reason = _check_blocklist("nmap", args) or _check_allowlist("nmap", args, "127.0.0.1")
        assert reason is not None, f"{args} should be refused"


def test_nmap_aggressive_script_scan_requires_approval():
    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(
            tool="nmap",
            args=["-sV", "-T5", "--script=vuln,exploit,intrusive"],
            target="192.168.1.10",
        ))
    assert result.blocked
    assert "approval" in result.block_reason.lower()


def test_nmap_malware_fuzzer_scan_requires_approval():
    with patch("shutil.which", return_value="/usr/bin/nmap"):
        result = run_tool(_req(
            tool="nmap",
            args=["-sV", "-T4", "--script=malware,fuzzer"],
            target="192.168.1.10",
        ))
    assert result.blocked
    assert "approval" in result.block_reason.lower()


def test_nmap_aggressive_script_scan_executes_with_approval_and_reviewer_pass():
    mock_verdict = MagicMock()
    mock_verdict.verdict = "PASS"
    _mock_reviewer.review = MagicMock(return_value=mock_verdict)

    with (
        patch("shutil.which", return_value="/usr/bin/nmap"),
        patch("subprocess.run") as mock_run,
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.write_text"),
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "nmap output"
        mock_run.return_value.stderr = ""
        result = run_tool(_approved_req(
            tool="nmap",
            args=["-sV", "-T5", "--script=vuln,exploit,intrusive"],
            target="192.168.1.10",
        ))

    assert not result.blocked
    cmd = mock_run.call_args.args[0]
    assert "-T5" in cmd
    assert "--script=vuln,exploit,intrusive" in cmd
