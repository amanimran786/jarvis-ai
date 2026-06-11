"""
agents/ai_safety_agent.py — AI safety and risk triage agent.

Provides: harm classification, threat scoring, and pre-execution risk
assessment for tasks submitted through the agent pipeline. Integrates
with security_reviewer for structural code analysis.

All verdicts are advisory — execution gates are enforced by callers.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("jarvis.agent.ai_safety")

# ── Threat taxonomy ───────────────────────────────────────────────────────────

HARM_CATEGORIES = {
    "prompt_injection": {
        "description": "Text attempting to override AI system instructions",
        "severity": "critical",
        "signals": [
            r"ignore\s+(?:previous|prior|all)\s+instructions",
            r"you\s+are\s+now\s+(?:DAN|an?\s+unaligned|unrestricted)",
            r"new\s+system\s+prompt",
            r"forget\s+everything\s+above",
            r"disregard\s+your\s+guidelines",
            r"pretend\s+you\s+have\s+no\s+restrictions",
        ],
    },
    "privilege_escalation": {
        "description": "Requests exceeding caller's granted permissions",
        "severity": "high",
        "signals": [
            r"\bsudo\b|\broot\b|\badmin\b",
            r"access\s+other\s+agents",
            r"bypass\s+(?:auth|rbac|security|approval)",
            r"grant\s+(?:yourself|me)\s+(?:admin|full|elevated)",
        ],
    },
    "unauthorized_execution": {
        "description": "Code execution embedded in what should be data",
        "severity": "high",
        "signals": [
            r"\beval\s*\(|\bexec\s*\(",
            r"__import__\s*\(",
            r"subprocess\.(?:run|call|Popen).*shell\s*=\s*True",
            r"os\.system\s*\(",
        ],
    },
    "data_exfiltration": {
        "description": "Routing internal data to unauthorized external endpoints",
        "severity": "high",
        "signals": [
            r"send\s+(?:memory|history|conversation|vault)\s+to\b",
            r"curl\s+.*\|\s*nc\b",
            r"webhook.*(?:memory|session|token|key)",
        ],
    },
    "destructive_action": {
        "description": "Irreversible operations not explicitly authorized",
        "severity": "high",
        "signals": [
            r"rm\s+-rf\b",
            r"git\s+push\s+--force\s+.*(?:main|master)",
            r"DROP\s+TABLE|TRUNCATE\s+TABLE",
            r"kubectl\s+delete\s+--all",
            r"format\s+(?:disk|drive|volume)",
        ],
    },
    "credential_exposure": {
        "description": "API keys, tokens, or passwords in payload",
        "severity": "critical",
        "signals": [
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bsk-[a-zA-Z0-9]{20,}\b",
            r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b",
            r"-----BEGIN (?:RSA )?PRIVATE KEY-----",
            r"password\s*[:=]\s*['\"][^'\"]{6,}",
        ],
    },
}

SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


# ── Task dispatcher ───────────────────────────────────────────────────────────

def process_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the AI safety agent.

    Supported task types (task["type"]):
      - "risk_triage"      : classify harm categories in a payload
      - "threat_score"     : compute numeric risk score (0–100)
      - "pre_exec_review"  : full pre-execution safety assessment
      - "policy_check"     : check against Jarvis security ROE

    The agent always returns a verdict: "PASS", "FLAG", or "BLOCK".
    """
    task_type = task.get("type", "risk_triage")
    ctx = task.get("context", {})

    handlers = {
        "risk_triage": _handle_risk_triage,
        "threat_score": _handle_threat_score,
        "pre_exec_review": _handle_pre_exec_review,
        "policy_check": _handle_policy_check,
    }

    handler = handlers.get(task_type)
    if handler is None:
        return {
            "ok": False,
            "error": f"Unknown ai_safety task type: {task_type!r}",
            "supported_types": list(handlers),
        }

    try:
        result = handler(ctx)
        return {"ok": True, "result": result}
    except Exception as exc:
        logger.exception("ai_safety_agent failed on %s", task_type)
        # Safety agent failures must not silently pass — return BLOCK
        return {
            "ok": False,
            "error": str(exc),
            "result": {"verdict": "BLOCK", "reason": f"Safety agent internal error: {exc}"},
        }


# ── Handlers ──────────────────────────────────────────────────────────────────

def _handle_risk_triage(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Classify harm categories present in a payload string.

    ctx keys:
      - payload (str): text to analyze
    """
    payload = ctx.get("payload", "")
    if not payload:
        return {"verdict": "PASS", "findings": [], "reason": "Empty payload"}

    findings = _scan_payload(payload)
    verdict = _derive_verdict(findings)

    return {
        "verdict": verdict,
        "findings": findings,
        "categories_flagged": [f["category"] for f in findings],
        "max_severity": findings[0]["severity"] if findings else "none",
    }


def _handle_threat_score(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Compute a 0–100 numeric risk score.

    ctx keys:
      - payload (str): text to score
    """
    payload = ctx.get("payload", "")
    findings = _scan_payload(payload)

    score = 0
    for f in findings:
        rank = SEVERITY_RANK.get(f["severity"], 0)
        score += rank * 25  # critical=100, high=75, medium=50, low=25

    score = min(score, 100)
    level = "critical" if score >= 90 else "high" if score >= 60 else "medium" if score >= 30 else "low" if score > 0 else "none"

    return {
        "threat_score": score,
        "level": level,
        "findings_count": len(findings),
        "verdict": "BLOCK" if score >= 75 else "FLAG" if score >= 25 else "PASS",
    }


def _handle_pre_exec_review(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Full pre-execution safety review: triage + structural code check.

    ctx keys:
      - task_title       (str): short task description
      - task_description (str): full task description
      - code             (str, optional): code to be executed
      - agent            (str, optional): which agent will execute
    """
    title = ctx.get("task_title", "")
    desc = ctx.get("task_description", "")
    code = ctx.get("code", "")
    agent = ctx.get("agent", "unknown")

    combined_payload = f"{title}\n{desc}\n{code}"

    # Rule-based triage
    findings = _scan_payload(combined_payload)

    # Code-specific checks
    code_findings = _check_code_safety(code) if code else []
    all_findings = findings + code_findings

    verdict = _derive_verdict(all_findings)

    # Delegate to security_reviewer for FAIL/critical findings
    security_verdict = None
    if any(f["severity"] == "critical" for f in all_findings):
        security_verdict = _delegate_security_review(combined_payload)

    return {
        "verdict": verdict,
        "agent": agent,
        "findings": all_findings,
        "security_reviewer_result": security_verdict,
        "requires_human_approval": verdict != "PASS",
        "summary": _summarize(all_findings, verdict),
    }


def _handle_policy_check(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Check a proposed action against Jarvis security ROE policies.

    ctx keys:
      - action         (str): what the agent wants to do
      - allowed_tools  (list[str]): tools the agent is permitted to use
      - requested_tool (str): what the agent is actually requesting
    """
    action = ctx.get("action", "")
    allowed = set(ctx.get("allowed_tools", []))
    requested = ctx.get("requested_tool", "")

    violations = []

    # Tool authorization check
    if requested and requested not in allowed:
        violations.append({
            "policy": "tool_authorization",
            "severity": "high",
            "detail": f"{requested!r} not in agent's allowed tool list: {sorted(allowed)}",
        })

    # Hardcoded forbidden actions
    forbidden_patterns = [
        (r"\bshell\s*=\s*True\b", "shell=True subprocess call"),
        (r"\beval\s*\(", "eval() call"),
        (r"\bexec\s*\(", "exec() call"),
    ]
    for pattern, label in forbidden_patterns:
        if re.search(pattern, action):
            violations.append({
                "policy": "forbidden_operation",
                "severity": "critical",
                "detail": f"Forbidden operation detected: {label}",
            })

    verdict = "BLOCK" if any(v["severity"] == "critical" for v in violations) else \
              "FLAG" if violations else "PASS"

    return {
        "verdict": verdict,
        "violations": violations,
        "requested_tool": requested,
        "allowed_tools": sorted(allowed),
    }


# ── Scanning helpers ──────────────────────────────────────────────────────────

def _scan_payload(text: str) -> list[dict]:
    """Scan text against all harm category signal patterns."""
    findings = []
    for category, spec in HARM_CATEGORIES.items():
        for pattern in spec["signals"]:
            if re.search(pattern, text, re.IGNORECASE):
                findings.append({
                    "category": category,
                    "severity": spec["severity"],
                    "description": spec["description"],
                    "matched_pattern": pattern,
                })
                break  # one finding per category

    # Sort by severity descending
    findings.sort(key=lambda f: SEVERITY_RANK.get(f["severity"], 0), reverse=True)
    return findings


def _check_code_safety(code: str) -> list[dict]:
    """Additional checks specific to code payloads."""
    findings = []
    checks = [
        (r"pickle\.loads?\(", "high", "pickle deserialization — unsafe with untrusted data"),
        (r"yaml\.load\((?!.*Loader)", "medium", "yaml.load() without Loader — use yaml.safe_load()"),
        (r"open\s*\(\s*[^,)]+,\s*['\"]w", "low", "direct open() for write — validate path first"),
    ]
    for pattern, severity, detail in checks:
        if re.search(pattern, code, re.IGNORECASE):
            findings.append({
                "category": "code_safety",
                "severity": severity,
                "description": detail,
                "matched_pattern": pattern,
            })
    return findings


def _derive_verdict(findings: list[dict]) -> str:
    if not findings:
        return "PASS"
    max_sev = max(SEVERITY_RANK.get(f["severity"], 0) for f in findings)
    if max_sev >= SEVERITY_RANK["high"]:
        return "BLOCK"
    if max_sev >= SEVERITY_RANK["medium"]:
        return "FLAG"
    return "FLAG"


def _summarize(findings: list[dict], verdict: str) -> str:
    if not findings:
        return "No safety concerns detected."
    cats = ", ".join(f["category"] for f in findings[:3])
    return f"{verdict}: {len(findings)} finding(s) — {cats}."


def _delegate_security_review(payload: str) -> dict | None:
    """Ask the security_reviewer agent for a structural code review."""
    try:
        from agents.security_reviewer import review
        return review(payload)
    except Exception as exc:
        logger.warning("security_reviewer delegation failed: %s", exc)
        return {"verdict": "FAIL", "reason": f"Delegation error: {exc}"}
