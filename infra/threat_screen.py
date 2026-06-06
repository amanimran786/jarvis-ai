"""
Deterministic pre-screening layer for the Security Reviewer agent.

Runs before any LLM call. Catches obvious attacks and credential leaks
with regex + Shannon entropy so we don't waste inference on known-bad payloads.

Returns a ScreenResult. If blocked=True the caller must halt immediately.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class ScreenResult:
    blocked:  bool
    findings: list[dict] = field(default_factory=list)

    def add(self, type_: str, location: str, description: str):
        self.findings.append({"type": type_, "location": location, "description": description})
        self.blocked = True


# ─── Patterns ─────────────────────────────────────────────────────────────────

# Known API key prefixes / formats — each tuple is (name, compiled_pattern)
_KEY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key",   re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key",   re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"]([A-Za-z0-9/+=]{40})['\"]")),
    ("openai_key",       re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,100}\b")),
    ("anthropic_key",    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{40,120}\b")),
    ("github_token",     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("google_api_key",   re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("slack_token",      re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,100}\b")),
    ("stripe_key",       re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b")),
    ("pem_header",       re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("db_conn_string",   re.compile(
        r"(?i)(postgres|mysql|mongodb)://[^:]+:[^@]{4,}@",
    )),
    ("jwt_token",        re.compile(r"\beyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\b")),
]

# Prompt injection signals — any match is a finding (not necessarily blocking alone)
_INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore_instructions",   re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+instructions?")),
    ("disregard_guidelines",  re.compile(r"(?i)disregard\s+(your\s+)?(previous\s+)?instructions?|guidelines?|rules?")),
    ("new_system_prompt",     re.compile(r"(?i)(new|updated?)\s+system\s+prompt")),
    ("forget_everything",     re.compile(r"(?i)forget\s+(everything|all\s+previous)")),
    ("you_are_now_dan",       re.compile(r"(?i)you\s+are\s+now\s+(DAN|an?\s+AI\s+without|unrestricted)")),
    ("act_as_jailbreak",      re.compile(r"(?i)(pretend|act|behave)\s+(you\s+are|as\s+if)\s+(you\s+have\s+no|without)\s+(restrictions?|limits?|rules?|guidelines?)")),
    ("override_prompt",       re.compile(r"(?i)(override|bypass|circumvent)\s+(your\s+)?(instructions?|safety|restrictions?|guidelines?)")),
    ("encoded_injection",     re.compile(r"(?i)(base64|rot13|hex)\s*(decode|encoded?)\s*(this|the\s+following|:)")),
]

# Destructive action patterns
_DESTRUCTIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sql_drop",          re.compile(r"(?i)\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX)\b")),
    ("sql_truncate",      re.compile(r"(?i)\bTRUNCATE\s+TABLE\b")),
    # DELETE without WHERE (rough heuristic — no WHERE within 80 chars after FROM clause)
    ("sql_delete_all",    re.compile(r"(?i)\bDELETE\s+FROM\s+\w+\s*(?:LIMIT\s+\d+\s*)?;")),
    ("git_force_push",    re.compile(r"(?i)git\s+push\s+.*--force(?:-with-lease)?.*(?:main|master|prod)")),
    ("rm_rf",             re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b")),
    ("kubectl_delete_all",re.compile(r"(?i)kubectl\s+delete\s+.*--all")),
    ("dd_destructive",    re.compile(r"\bdd\s+.*of=/dev/[a-z]")),
]

# Embedded code execution in data context
_CODE_EXEC_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("eval_exec",         re.compile(r"\b(eval|exec)\s*\(")),
    ("import_injection",  re.compile(r"__import__\s*\(")),
    ("os_system",         re.compile(r"\bos\.(system|popen|exec[lv]pe?)\s*\(")),
    ("subprocess_shell",  re.compile(r"subprocess\.(run|call|Popen)\s*\(.*shell\s*=\s*True")),
]


# ─── Shannon entropy ──────────────────────────────────────────────────────────

def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((f / n) * math.log2(f / n) for f in freq.values())


# High-entropy token finder: words ≥ 24 chars, entropy ≥ 4.8 bits/char,
# not a UUID, not a URL, not an integer.
_UUID_RE   = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_URL_RE    = re.compile(r"^https?://")
_INT_RE    = re.compile(r"^\d+$")
_TOKEN_RE  = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")

def _find_high_entropy_tokens(text: str) -> list[str]:
    candidates = _TOKEN_RE.findall(text)
    return [
        t for t in candidates
        if (
            _shannon_entropy(t) >= 4.8
            and not _UUID_RE.match(t)
            and not _URL_RE.match(t)
            and not _INT_RE.match(t)
        )
    ]


# ─── Public API ───────────────────────────────────────────────────────────────

def screen_payload(text: str) -> ScreenResult:
    """
    Run all deterministic checks against `text`.
    Returns ScreenResult(blocked=True) if any critical pattern matches.
    Prompt injection alone sets blocked=True; combined with other signals = critical.
    """
    result = ScreenResult(blocked=False)

    # 1 — API key patterns (always blocking)
    for name, pattern in _KEY_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = m.group(0)[:40] + ("…" if len(m.group(0)) > 40 else "")
            result.add(
                type_="CREDENTIAL_EXPOSURE",
                location=f"matched pattern '{name}'",
                description=f"Detected credential pattern [{name}]: {snippet}",
            )

    # 2 — High-entropy tokens (blocking — likely unrecognised credential)
    hi_ent = _find_high_entropy_tokens(text)
    for token in hi_ent[:3]:  # cap at 3 to avoid noise on binary blobs
        result.add(
            type_="CREDENTIAL_EXPOSURE",
            location="high_entropy_string",
            description=f"High-entropy token (≥4.8 bits/char, ≥24 chars): {token[:20]}…",
        )

    # 3 — Prompt injection (blocking)
    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            result.add(
                type_="PROMPT_INJECTION",
                location=f"matched pattern '{name}'",
                description=f"Prompt injection signal detected: {name}",
            )

    # 4 — Destructive actions (blocking)
    for name, pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(text):
            result.add(
                type_="DESTRUCTIVE_ACTION",
                location=f"matched pattern '{name}'",
                description=f"Destructive operation pattern detected: {name}",
            )

    # 5 — Code execution in data (blocking)
    for name, pattern in _CODE_EXEC_PATTERNS:
        if pattern.search(text):
            result.add(
                type_="UNAUTHORIZED_TOOL_EXECUTION",
                location=f"matched pattern '{name}'",
                description=f"Code execution pattern in payload: {name}",
            )

    return result
