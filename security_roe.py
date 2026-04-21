"""
Defensive security rules of engagement and task templates.

This keeps Jarvis useful as a cybersecurity engineering companion without
turning dual-use prompts into autonomous offensive execution.
"""

from __future__ import annotations

from typing import Any


_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "scope_gate",
        "name": "Authorization and Scope Gate",
        "best_for": "Any security task before analysis or tooling.",
        "must_have": [
            "written authorization or clear owned-system context",
            "target assets and boundaries",
            "allowed actions and prohibited actions",
            "data handling and credential rules",
            "stop condition and escalation owner",
        ],
        "output": [
            "state whether the task is in scope",
            "name missing authorization or scope details",
            "continue with safe defensive analysis if active testing is not authorized",
        ],
    },
    {
        "id": "threat_model",
        "name": "Threat Model",
        "best_for": "Architecture, product, AI-agent, browser, memory, or integration designs.",
        "must_have": [
            "assets",
            "actors",
            "entry points",
            "trust boundaries",
            "abuse paths",
            "existing controls",
            "missing controls",
            "verification plan",
        ],
        "output": [
            "lead with the highest-impact abuse path",
            "separate confirmed risks from assumptions",
            "recommend the smallest control that changes the outcome",
        ],
    },
    {
        "id": "code_security_review",
        "name": "Code Security Review",
        "best_for": "PRs, auth flows, API handlers, local automation, and file/network tooling.",
        "must_have": [
            "authn/authz boundary",
            "input validation and parsing",
            "secret handling",
            "path traversal and file write gates",
            "SSRF and outbound network controls",
            "injection and deserialization risks",
            "audit logging and rate limits",
        ],
        "output": [
            "findings first, ranked by exploitability and impact",
            "exact file/function references when code is available",
            "tests or probes that would prove the risk",
        ],
    },
    {
        "id": "incident_triage",
        "name": "Security Incident Triage",
        "best_for": "Suspicious activity, leaked secret, account abuse, production vulnerability, or active misuse signal.",
        "must_have": [
            "signal and source",
            "blast radius",
            "affected assets/users",
            "timeline",
            "containment option",
            "evidence preservation",
            "communications owner",
        ],
        "output": [
            "classify severity from evidence, not anxiety",
            "separate contain, eradicate, recover, and learn steps",
            "name the first reversible action",
        ],
    },
    {
        "id": "ai_misuse",
        "name": "AI Misuse and Prompt-Injection Review",
        "best_for": "Agents, tool use, memory retrieval, browser control, jailbreaks, and model-boundary failures.",
        "must_have": [
            "trusted vs untrusted inputs",
            "tool permission boundary",
            "memory read/write boundary",
            "prompt-injection path",
            "data exfiltration path",
            "classifier or policy gap",
            "eval case that reproduces the failure",
        ],
        "output": [
            "identify the override path",
            "decide whether the failure is model-boundary, policy-boundary, or tooling-boundary",
            "recommend a control plus an eval",
        ],
    },
    {
        "id": "browser_source_gate",
        "name": "Browser and Source Ingestion Gate",
        "best_for": "Scraping, browser automation, OSINT, repo ingestion, and external content pipelines.",
        "must_have": [
            "user consent",
            "terms/robots constraints",
            "credential boundary",
            "rate limit",
            "data minimization",
            "storage and deletion rule",
            "human approval for risky actions",
        ],
        "output": [
            "prefer read-only inspection first",
            "do not bypass access controls or anti-abuse systems",
            "log provenance for anything stored in memory or vault",
        ],
    },
    {
        "id": "prompt_leakage",
        "name": "Prompt Leakage and System-Prompt Extraction Review",
        "best_for": "Public jailbreak repos, system-prompt leakage attempts, prompt-injection evals, and model-boundary tests.",
        "must_have": [
            "trusted instruction boundary",
            "untrusted prompt corpus provenance",
            "secret or system-prompt exposure path",
            "tool or memory exfiltration path",
            "safe expected refusal or containment behavior",
            "eval case and regression owner",
            "no secret reproduction rule",
        ],
        "output": [
            "state whether the request is defensive analysis or extraction",
            "summarize the attack pattern without reproducing sensitive prompts",
            "add an eval or control instead of copying jailbreak text into production",
        ],
    },
)

_ALIASES = {
    "scope": "scope_gate",
    "authorization": "scope_gate",
    "roe": "scope_gate",
    "threat": "threat_model",
    "review": "code_security_review",
    "code": "code_security_review",
    "incident": "incident_triage",
    "ai": "ai_misuse",
    "prompt": "ai_misuse",
    "jailbreak": "ai_misuse",
    "leak": "prompt_leakage",
    "leakage": "prompt_leakage",
    "prompt_leak": "prompt_leakage",
    "prompt_leakage": "prompt_leakage",
    "system_prompt": "prompt_leakage",
    "system_prompt_leak": "prompt_leakage",
    "cl4r1t4s": "prompt_leakage",
    "browser": "browser_source_gate",
    "scraping": "browser_source_gate",
    "source": "browser_source_gate",
}


def list_templates() -> list[dict[str, Any]]:
    return [dict(template) for template in _TEMPLATES]


def get_template(template_id: str | None) -> dict[str, Any] | None:
    if not template_id:
        return None
    normalized = str(template_id).strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _ALIASES.get(normalized, normalized)
    for template in _TEMPLATES:
        if template["id"] == normalized:
            return dict(template)
    return None


def status(template_id: str | None = None) -> dict[str, Any]:
    selected = get_template(template_id)
    return {
        "ok": True,
        "mode": "defensive-only",
        "purpose": "Give Jarvis repeatable rules of engagement for defensive cybersecurity work.",
        "templates": [selected] if selected else list_templates(),
        "guardrails": [
            "Do not run exploitation, credential harvesting, persistence, evasion, or lateral movement.",
            "Do not target third-party systems without explicit authorization.",
            "When scope is missing, provide safe analysis and ask for authorization details before active testing.",
            "Prefer read-only inspection, threat modeling, code review, and control design.",
        ],
    }


def summary_text(template_id: str | None = None) -> str:
    payload = status(template_id)
    lines = [
        "Defensive security ROE: use written scope, safe analysis, and explicit stop conditions before any security task.",
        f"Mode: {payload['mode']}",
        "",
    ]
    for template in payload["templates"]:
        must = ", ".join(template["must_have"][:4])
        lines.append(f"- {template['id']}: {template['name']} -> require {must}.")
    return "\n".join(lines)
