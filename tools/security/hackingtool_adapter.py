"""
tools/security/hackingtool_adapter.py
======================================

Defensive security toolbox — allowlisted wrapper around AKCodez/hackingtool-plugin.

Jarvis NEVER calls the plugin's own runner. Each tool is invoked directly via
subprocess with list args (shell=False). Every run is:

  1. Checked against the blocklist (refused immediately on match)
  2. Validated against the per-tool allowlist (refused on unknown flag)
  3. Gated on human approval for active scans or external targets
  4. Reviewed by security_reviewer for any external target
  5. Executed with a 5-minute hard timeout, results written to workspace/
  6. Audit-logged to the project memory layer regardless of outcome

Vendored source: third_party/hackingtool-plugin/
Pinning instructions: third_party/hackingtool-plugin/VENDOR.md
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("jarvis.tools.security.hackingtool")

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _REPO_ROOT / "workspace" / "security_results"

# ── Allowlist ─────────────────────────────────────────────────────────────────
#
# Each entry defines:
#   executable     — binary name looked up via PATH
#   needs_approval — True = blocks until approved_by is set; applies to active
#                    scans or anything touching external infra
#   description    — shown to the human in approval requests
#   allowed_args   — regex; every non-target, non-path arg is checked against it

_ALLOWED_TOOLS: dict[str, dict] = {
    "gitleaks": {
        "executable": "gitleaks",
        "needs_approval": True,
        "description": "Scan local git repo for leaked secrets/credentials",
        "allowed_args": re.compile(
            r"^(detect|protect|version|--source|-s|--report-format|--report-path|"
            r"--log-level|--no-git|--redact|-v|--verbose|--config|-c|"
            r"--exit-code|-f|json|sarif|csv|junit|template)$"
        ),
    },
    "trufflehog": {
        "executable": "trufflehog",
        "needs_approval": True,
        "description": "Scan local filesystem for secrets/credentials",
        "allowed_args": re.compile(
            r"^(filesystem|git|--json|--only-verified|--concurrency|"
            r"--no-update|--fail|--log-level|-v|--include-paths|"
            r"--exclude-paths|--exclude-globs)$"
        ),
    },
    "testssl.sh": {
        "executable": "testssl.sh",
        "needs_approval": True,
        "description": "Inspect TLS/SSL configuration of a host:port",
        "allowed_args": re.compile(
            r"^(--quiet|--json|--html|--logfile|-oJ|-oH|--severity|--color|0|1|2|3|"
            r"--connect-timeout|--openssl-timeout|-p|-s|-h|-f|-e|"
            r"MEDIUM|HIGH|CRITICAL|LOW|WARN)$"
        ),
    },
    "wafw00f": {
        "executable": "wafw00f",
        "needs_approval": True,
        "description": "Detect WAF in front of a web target",
        "allowed_args": re.compile(
            r"^(-a|-v|--verbose|-o|--output|-f|--format|json|csv|-t|--test)$"
        ),
    },
    "dnstwist": {
        "executable": "dnstwist",
        "needs_approval": True,
        "description": "Detect typosquatting / look-alike domains (OSINT, passive)",
        "allowed_args": re.compile(
            r"^(--json|--format|--threads|-t|--registered|--unregistered|"
            r"--ssdeep|--mxcheck|--nameservers|-n|--tld|-d|-r|-u)$"
        ),
    },
    "subfinder": {
        "executable": "subfinder",
        "needs_approval": True,
        "description": "Passive subdomain enumeration via public OSINT sources",
        "allowed_args": re.compile(
            r"^(-d|--domain|-dL|--list|-passive|--silent|-o|--output|-json|"
            r"--oJ|-t|--timeout|-v|--verbose|-nW|--no-wildcard|-all)$"
        ),
    },
    "amass": {
        "executable": "amass",
        "needs_approval": True,
        "description": "Passive DNS enumeration via OSINT",
        "allowed_args": re.compile(
            r"^(enum|-passive|-d|-df|-o|-json|-config|-timeout|-silent|-v|-ip)$"
        ),
    },
    "httpx": {
        "executable": "httpx",
        "needs_approval": True,
        "description": "HTTP probing for live host detection",
        "allowed_args": re.compile(
            r"^(-l|--list|-u|--url|-sc|--status-code|-title|-td|--tech-detect|"
            r"-json|-o|--output|-timeout|-t|--threads|-silent|-v|--verbose|"
            r"-fr|--follow-redirects|-cl|--content-length|-nc|--no-color)$"
        ),
    },
    "nmap": {
        "executable": "nmap",
        "needs_approval": True,
        "description": "Port, service, and approved NSE assessment scan — owned hosts only",
        "allowed_args": re.compile(
            r"^(-sV|-sT|--open|-p|--top-ports|-T[0-5]|-oN|-oJ|-oX|"
            r"--script|--script=(safe|default|vuln|exploit|intrusive|malware|fuzzer)"
            r"([,()+ a-zA-Z_-]*(safe|default|vuln|exploit|intrusive|malware|fuzzer))*|-Pn|-n|--host-timeout|-v|"
            r"--version-intensity)$"
        ),
    },
}

# ── Blocklist ─────────────────────────────────────────────────────────────────
#
# Tool names or args containing any of these patterns are refused at the gate.
# The adapter cannot be configured to enable them — change requires a code review.

_BLOCKED_PATTERNS: tuple[str, ...] = (
    # post-exploitation / C2 / payloads
    "metasploit", "msfconsole", "msfvenom", "empire", "covenant",
    "cobalt", "sliver", "havoc", "pupy", "merlin",
    # credential attacks
    "hydra", "medusa", "hashcat", "john", "patator", "crowbar",
    "crunch", "cewl", "cupp", "spray", "brute",
    # wireless
    "aircrack", "wifite", "fluxion", "fern", "wifiphisher",
    "airmon", "airodump", "aireplay",
    # MITM / sniffing
    "ettercap", "bettercap", "arpspoof", "responder",
    "wireshark", "tcpdump", "mitmproxy",
    # phishing / social eng
    "beef", "gophish", "evilginx", "king-phisher", "setoolkit", "se-toolkit",
    # DDoS
    "slowloris", "goldeneye", "loic", "hoic", "hping",
    # aggresive mass-scanners
    "masscan", "zmap",
    # recon-ng has active exploit modules
    "recon-ng",
)

# These flags are refused regardless of tool
_BLOCKED_FLAGS: frozenset[str] = frozenset({
    "--sudo", "sudo",
    "--privileged",          # Docker privileged mode
    "--cap-add",             # Docker capability escalation
    "--network=host",        # Docker host networking
    "--force",               # destructive overwrite
    "-S",                    # sudo stdin read
    "--password",            # credential on CLI
    "--creds",
    "--exploit",
    "--payload",
    "--lhost",
    "--lport",
})

_NMAP_ALLOWED_SCRIPT_CATEGORIES: frozenset[str] = frozenset({
    "safe",
    "default",
    "vuln",
    "exploit",
    "intrusive",
    "malware",
    "fuzzer",
})

_NMAP_BLOCKED_SCRIPT_CATEGORIES: frozenset[str] = frozenset({
    "brute",
    "dos",
})

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ToolRunRequest:
    tool: str
    args: list[str]
    target: str
    agent_id: str
    session_id: str          = ""
    project_id: str          = ""
    approved_by: str | None  = None
    approval_token: str | None = None


@dataclass
class ToolRunResult:
    tool: str
    target: str
    exit_code: int            = 0
    stdout: str               = ""
    stderr: str               = ""
    blocked: bool             = False
    block_reason: str | None  = None
    output_path: str | None   = None
    audit_id: str             = field(default_factory=lambda: str(uuid.uuid4()))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_local_path(target: str) -> bool:
    return target.startswith(("/", "./", "~/")) or Path(target).exists()


def _expected_human_approval_token() -> str:
    """
    Return the configured approval token from the environment.

    Fails closed: if JARVIS_SECURITY_APPROVAL_TOKEN is not set or is empty,
    raises ValueError rather than falling back to a known static string that
    could be guessed or leaked via source code.
    """
    token = os.getenv("JARVIS_SECURITY_APPROVAL_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "JARVIS_SECURITY_APPROVAL_TOKEN must be set to use security tools"
        )
    return token


def _has_human_approval(request: ToolRunRequest) -> bool:
    approved_by = (request.approved_by or "").strip().lower()
    token = (request.approval_token or "").strip()
    if not approved_by or not token:
        return False
    if approved_by in {"ai", "agent", "jarvis", request.agent_id.strip().lower()}:
        return False
    try:
        expected = _expected_human_approval_token()
    except ValueError:
        return False
    return token == expected


def _check_blocklist(tool: str, args: list[str]) -> str | None:
    """Return a block reason if tool or args match the blocklist, else None."""
    tl = tool.lower()
    for pat in _BLOCKED_PATTERNS:
        if pat in tl:
            return f"tool '{tool}' matches blocked pattern '{pat}'"
    for arg in args:
        if arg in _BLOCKED_FLAGS:
            return f"blocked flag '{arg}'"
        al = arg.lower()
        for pat in _BLOCKED_PATTERNS:
            if pat in al:
                return f"blocked pattern '{pat}' in argument '{arg}'"
    return None


def _nmap_script_value_from_arg(arg: str) -> str | None:
    if arg.startswith("--script="):
        return arg.split("=", 1)[1].strip()
    return None


def _validate_nmap_script_expression(expr: str) -> str | None:
    """Validate approved NSE category expressions for nmap."""
    if not expr:
        return "--script requires an explicit category expression"
    lowered = expr.lower()
    if not re.fullmatch(r"[a-z0-9_,+()!&|.* -]+", lowered):
        return f"nmap --script expression contains unsupported characters: {expr}"

    tokens = [t for t in re.split(r"[^a-z0-9_-]+", lowered) if t]
    if not tokens:
        return "--script requires at least one category"

    for token in tokens:
        if token in {"and", "or", "not"}:
            continue
        bare = token.rstrip("*")
        if bare in _NMAP_BLOCKED_SCRIPT_CATEGORIES:
            return f"nmap --script category '{bare}' is not permitted"
        if bare not in _NMAP_ALLOWED_SCRIPT_CATEGORIES:
            return (
                "nmap --script is limited to category expressions using "
                "safe, default, vuln, exploit, intrusive, malware, or fuzzer"
            )
    return None


def _check_nmap_script_args(args: list[str]) -> str | None:
    for idx, arg in enumerate(args):
        value = _nmap_script_value_from_arg(arg)
        if value is not None:
            reason = _validate_nmap_script_expression(value)
            if reason:
                return reason
            continue
        if arg == "--script":
            if idx + 1 >= len(args):
                return "--script requires an explicit category expression"
            reason = _validate_nmap_script_expression(args[idx + 1])
            if reason:
                return reason
    return None


def _check_allowlist(tool: str, args: list[str], target: str) -> str | None:
    """Validate flag args against the per-tool allowed_args regex.

    Only args starting with '-' are validated — value args (port numbers,
    counts, format names, output paths) are positional and safe to pass
    through with shell=False. The security intent is to block dangerous
    flags, not their associated values.
    """
    spec = _ALLOWED_TOOLS.get(tool)
    if spec is None:
        return f"tool '{tool}' is not in the defensive allowlist"
    if tool == "nmap":
        reason = _check_nmap_script_args(args)
        if reason:
            return reason
    pattern: re.Pattern = spec["allowed_args"]
    for arg in args:
        if arg == target:
            continue
        if arg.startswith(("/", "./", "~/")):
            continue
        if not arg.startswith("-"):
            # Value arg (port number, count, format string, subcommand verb).
            # Not a flag — no pattern validation needed. Injection is not
            # possible here because shell=False prevents shell interpretation.
            continue
        if not pattern.match(arg):
            return f"flag '{arg}' is not permitted for tool '{tool}'"
    return None


def _run_security_gate(request: ToolRunRequest, spec: dict) -> str | None:
    """
    Returns None (pass) or a block reason string.

    Every enabled security tool → explicit human approval required.
    Local passive tool → skips AI reviewer after human approval.
    External target → also routes through security_reviewer, which can block but cannot approve alone.
    """
    needs_approval: bool = spec.get("needs_approval", True)
    is_external = not _is_local_path(request.target)

    if (needs_approval or is_external) and not _has_human_approval(request):
        reason = "active scan" if needs_approval else "external target"
        return (
            f"'{request.tool}' on '{request.target}' requires explicit human approval "
            f"(reason: {reason}). Set approved_by plus a valid approval_token before calling run_tool()."
        )

    if is_external:
        try:
            from agents.security_reviewer import review  # type: ignore
            verdict = review(
                payload={
                    "tool": request.tool,
                    "args": request.args,
                    "target": request.target,
                    "agent_id": request.agent_id,
                    "action": "security_tool_run",
                },
                task_id=f"hackingtool_{request.tool}_{int(time.time())}",
                stage=0,
            )
            verdict_str = str(getattr(verdict, "verdict", verdict))
            if verdict_str == "FAIL":
                summary = getattr(verdict, "summary", "no details")
                return f"security_reviewer blocked run: {summary}"
        except Exception as exc:
            logger.warning("security_reviewer unavailable; blocking security tool run: %s", exc)
            return f"security_reviewer unavailable; blocked run: {exc}"

    return None


def _audit_log(result: ToolRunResult, request: ToolRunRequest) -> None:
    """Write structured audit entries to the project memory layer and audit stream."""
    try:
        from infra.security_audit import audit_event, SECURITY_TOOL_EXEC
        audit_event(
            SECURITY_TOOL_EXEC,
            actor=request.agent_id,
            target=f"{request.tool} {request.target}",
            decision="blocked" if result.blocked else "ran",
            severity="warning" if result.blocked or result.exit_code else "notice",
            reason=result.block_reason or (result.stderr[:300] if result.stderr else ""),
            task_id=result.audit_id,
            rollback_ref=result.output_path or result.audit_id,
            extra={
                "tool": result.tool,
                "target": result.target,
                "approved_by": request.approved_by or "",
                "exit_code": result.exit_code,
            },
        )
    except Exception as exc:
        logger.warning("security audit stream write failed (non-fatal): %s", exc)

    try:
        from infra.memory import store  # type: ignore
        if store is None:
            return
        content = (
            f"[SECURITY_TOOL_AUDIT] audit_id={result.audit_id}\n"
            f"tool={result.tool}  target={result.target}\n"
            f"agent_id={request.agent_id}  session={request.session_id}\n"
            f"approved_by={request.approved_by or 'none'}\n"
            f"exit_code={result.exit_code}  blocked={result.blocked}\n"
            f"block_reason={result.block_reason or 'none'}\n"
            f"output_path={result.output_path or 'none'}"
        )
        store.write(
            "project",
            content=content,
            agent_id=request.agent_id,
            session_id=request.session_id,
            project_id=request.project_id,
            key=f"security_audit_{result.audit_id}",
            importance=8,
            metadata={
                "type": "security_audit",
                "tool": result.tool,
                "target": result.target,
                "blocked": result.blocked,
                "approved_by": request.approved_by,
                "audit_id": result.audit_id,
            },
            force=False,
        )
    except Exception as exc:
        logger.warning("audit_log write failed (non-fatal): %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────

def run_tool(request: ToolRunRequest) -> ToolRunResult:
    """
    Execute one allowlisted defensive tool.

    Returns a ToolRunResult regardless of outcome; blocked=True means the run
    was refused at some gate. An audit entry is always written.
    """
    result = ToolRunResult(tool=request.tool, target=request.target)

    # 1. Blocklist
    reason = _check_blocklist(request.tool, request.args)
    if reason:
        logger.warning("run_tool blocked (blocklist): %s", reason)
        result.blocked = True
        result.block_reason = reason
        _audit_log(result, request)
        return result

    # 2. Allowlist + per-tool arg validation
    reason = _check_allowlist(request.tool, request.args, request.target)
    if reason:
        logger.warning("run_tool blocked (allowlist): %s", reason)
        result.blocked = True
        result.block_reason = reason
        _audit_log(result, request)
        return result

    spec = _ALLOWED_TOOLS[request.tool]

    # 3. Approval / security_reviewer gate
    reason = _run_security_gate(request, spec)
    if reason:
        logger.warning("run_tool blocked (gate): %s", reason)
        result.blocked = True
        result.block_reason = reason
        _audit_log(result, request)
        return result

    # 4. Tool installed?
    executable = spec["executable"]
    if not shutil.which(executable):
        result.blocked = True
        result.block_reason = f"'{executable}' not found in PATH"
        _audit_log(result, request)
        return result

    # 5. Run
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = _RESULTS_DIR / f"{result.audit_id}_{request.tool}.txt"

    cmd = [executable] + request.args
    if request.target and request.target not in request.args:
        cmd.append(request.target)

    logger.info(
        "run_tool: %s target=%s agent=%s approved_by=%s",
        executable, request.target, request.agent_id, request.approved_by,
    )

    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(_REPO_ROOT),
        )
        result.exit_code = proc.returncode
        result.stdout = proc.stdout
        result.stderr = proc.stderr
        output_path.write_text(
            f"# {request.tool}  target={request.target}  audit={result.audit_id}\n"
            f"# exit_code={result.exit_code}\n\n{result.stdout}"
        )
        result.output_path = str(output_path)
    except subprocess.TimeoutExpired:
        result.exit_code = -1
        result.stderr = "timed out after 300s"
        logger.warning("run_tool: %s timed out", executable)
    except Exception as exc:
        result.exit_code = -1
        result.stderr = str(exc)
        logger.error("run_tool: %s raised: %s", executable, exc)

    # 6. Audit
    _audit_log(result, request)
    return result
