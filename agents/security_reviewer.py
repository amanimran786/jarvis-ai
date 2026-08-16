"""
Security Reviewer agent worker.

Consumes a task payload, runs deterministic pre-screening via threat_screen,
then routes to the LLM for deep analysis. Emits a SecurityVerdict to the
DAG review_chains table (or logs it if postgres is unavailable).

Entry point:   review(payload, task_id, stage) -> SecurityVerdict
DAG write:     emit_to_dag(verdict, pg_conn=None)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Literal

from infra.threat_screen import ScreenResult, screen_payload

log = logging.getLogger("jarvis.agent.security_reviewer")

Verdict  = Literal["PASS", "FAIL", "REQUEST_CHANGES"]
Severity = Literal["none", "low", "medium", "high", "critical"]

# postgres review_verdict enum uses lowercase; map from our uppercase
_VERDICT_TO_PG: dict[str, str] = {
    "PASS":             "approved",
    "REQUEST_CHANGES":  "needs_revision",
    "FAIL":             "rejected",
}


# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class SecurityFinding:
    type:           str
    severity:       Severity
    location:       str
    description:    str
    recommendation: str


@dataclass
class SecurityVerdict:
    verdict:    Verdict
    severity:   Severity
    findings:   list[SecurityFinding]
    summary:    str
    task_id:    str = ""
    stage:      int = 0
    screened_by: str = "security_reviewer"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def is_blocking(self) -> bool:
        return not (
            self.verdict == "PASS"
            and self.severity == "none"
            and not self.findings
        )


def _fallback_verdict() -> SecurityVerdict:
    return SecurityVerdict(
        verdict="FAIL",
        severity="critical",
        findings=[SecurityFinding(
            type="PARSE_ERROR",
            severity="critical",
            location="llm_output",
            description="LLM did not return valid security JSON. Treating as FAIL for safety.",
            recommendation="Manual review required.",
        )],
        summary="LLM output could not be validated. Pipeline halted.",
    )


# ─── LLM output parsing ───────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)

def _parse_llm_verdict(raw: str) -> SecurityVerdict:
    raw = _THINK_RE.sub("", raw).strip()

    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        log.warning("No JSON block in security reviewer output")
        return _fallback_verdict()

    try:
        data     = json.loads(raw[start:end])
        findings = [SecurityFinding(**f) for f in data.get("findings", [])]
        verdict = str(data.get("verdict") or "")
        severity = str(data.get("severity") or "")
        if verdict not in {"PASS", "FAIL", "REQUEST_CHANGES"}:
            raise ValueError(f"invalid verdict {verdict!r}")
        if severity not in {"none", "low", "medium", "high", "critical"}:
            raise ValueError(f"invalid severity {severity!r}")
        if any(
            finding.severity not in {"none", "low", "medium", "high", "critical"}
            for finding in findings
        ):
            raise ValueError("invalid finding severity")
        if verdict == "PASS" and (severity != "none" or findings):
            raise ValueError("PASS verdict must have severity=none and no findings")
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("summary must be a string")
        return SecurityVerdict(
            verdict=verdict,
            severity=severity,
            findings=findings,
            summary=summary,
        )
    except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
        log.warning("Failed to parse LLM verdict JSON: %s", exc)
        return _fallback_verdict()


# ─── Structured local review (primary path) ──────────────────────────────────

# Ollama grammar-enforced verdict shape. Forcing JSON at generation time fixes
# the gate's "local LLM returned empty" failures at the root (tracker item 9):
# the old path went through the agentic tool loop (needless 64k-ctx call that
# blew the 45s client timeout under memory pressure) and then through the
# TTS-oriented _strip_markdown, which deletes <think>…</think> blocks — a
# reasoning model that answers inside its think block came back empty.
_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict":  {"type": "string", "enum": ["PASS", "FAIL", "REQUEST_CHANGES"]},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "high", "critical"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type":           {"type": "string"},
                    "severity":       {"type": "string"},
                    "location":       {"type": "string"},
                    "description":    {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": ["type", "severity", "location", "description", "recommendation"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["verdict", "severity", "findings", "summary"],
}


def _local_security_review(payload_str: str) -> str:
    """
    One non-streamed, schema-constrained local call for the verdict.
    No tool loop, no markdown/think stripping, temperature 0, dedicated
    structured-output timeout. Returns raw JSON string, or "" on failure
    (callers fall through to the opt-in cloud path, then fail closed).
    """
    try:
        from brains.brain_ollama import ask_local_structured
        from config import AGENT_ROSTER
        system = AGENT_ROSTER.get("security_reviewer", {}).get("system_prompt", "")
        if not system:
            return ""
        return ask_local_structured(
            payload_str, _VERDICT_SCHEMA, system=system, raise_on_error=False,
        )
    except Exception:
        log.exception("structured local security review failed")
        return ""


# ─── Cloud fallback for security gate ────────────────────────────────────────

# Provider order: Gemini 2.5 Flash Lite (free tier) → GPT-4o-mini → Anthropic Haiku
_CLOUD_SECURITY_PROVIDERS = [
    ("gemini",    "gemini-2.5-flash-lite"),
    ("openai",    "gpt-4o-mini"),
    ("anthropic", "claude-haiku-4-5-20251001"),
]


def _cloud_security_review_allowed() -> bool:
    """
    Cloud security review is opt-in only: requires the explicit operator flag
    AND a mode that permits cloud calls. Review payloads contain task content
    and code — they must never leave the machine on a silent default path.
    """
    import os
    flag = str(os.getenv("JARVIS_ALLOW_CLOUD_SECURITY_REVIEW") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    try:
        from model_router import is_open_source_mode
        return not is_open_source_mode()
    except Exception:
        return False


def _cloud_security_review(payload_str: str) -> str:
    """
    Call a cloud LLM to produce a security verdict when local LLM returns empty.
    Tries Gemini free tier first, then OpenAI, then Anthropic.
    Returns raw JSON string or empty string if all providers fail.
    Gated by JARVIS_ALLOW_CLOUD_SECURITY_REVIEW; the gate itself fails closed
    (empty output → fresh fail-closed verdict = manual review).
    """
    import os
    if not _cloud_security_review_allowed():
        log.info(
            "Cloud security fallback skipped: JARVIS_ALLOW_CLOUD_SECURITY_REVIEW "
            "not enabled or open-source mode active."
        )
        return ""
    from config import AGENT_ROSTER
    system = AGENT_ROSTER.get("security_reviewer", {}).get("system_prompt", "")
    if not system:
        return ""

    _CLOUD_TIMEOUT = 20  # seconds per provider

    for provider, model in _CLOUD_SECURITY_PROVIDERS:
        try:
            if provider == "gemini":
                import os as _os
                key = _os.getenv("GEMINI_API_KEY", "")
                if not key:
                    continue
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=key)
                r = client.models.generate_content(
                    model=model,
                    contents=payload_str,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        max_output_tokens=1024,
                        timeout=_CLOUD_TIMEOUT,
                    ),
                )
                raw = (r.text or "").strip()

            elif provider == "openai":
                key = os.getenv("OPENAI_API_KEY", "")
                if not key:
                    continue
                import openai
                client = openai.OpenAI(api_key=key, timeout=_CLOUD_TIMEOUT)
                r = client.chat.completions.create(
                    model=model, max_tokens=1024,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": payload_str},
                    ],
                    response_format={"type": "json_object"},
                )
                raw = (r.choices[0].message.content or "").strip()

            else:  # anthropic
                key = os.getenv("ANTHROPIC_API_KEY", "")
                if not key:
                    continue
                import anthropic
                client = anthropic.Anthropic(api_key=key, timeout=_CLOUD_TIMEOUT)
                msg = client.messages.create(
                    model=model, max_tokens=1024, system=system,
                    messages=[{"role": "user", "content": payload_str}],
                )
                raw = (msg.content[0].text or "").strip()

            if raw:
                log.info("Security gate used cloud fallback (%s/%s)", provider, model)
                _audit_security_event(
                    "cloud_call",
                    actor="security_reviewer",
                    target="security review fallback",
                    decision="sent",
                    severity="warning",
                    reason="local security reviewer returned empty output",
                    rollback_ref="manual_review",
                    extra={"provider": provider, "model": model},
                )
                return raw

        except Exception as exc:
            log.debug("Cloud security fallback %s/%s failed: %s", provider, model, exc)

    log.warning("All cloud security providers failed — returning empty")
    return ""


def _audit_security_event(action: str, **kwargs) -> None:
    """Best-effort security audit emit; never changes the reviewer verdict."""
    try:
        from infra.security_audit import audit_event
        audit_event(action, **kwargs)
    except Exception:
        log.exception("security audit emit failed")


# ─── Pre-screen → LLM pipeline ───────────────────────────────────────────────

def review(payload: dict, task_id: str = "", stage: int = 0) -> SecurityVerdict:
    """
    Main entry point for the security reviewer DAG stage.

    Flow:
      1. Serialise payload to string for scanning
      2. Run deterministic threat_screen (no LLM) — fast path catches obvious attacks
      3. If screen blocks → return FAIL immediately, no LLM call
      4. Otherwise → dispatch to security_reviewer LLM agent
      5. Parse structured verdict from LLM output
      6. Attach task_id + stage, return SecurityVerdict
    """
    payload_str = json.dumps(payload, ensure_ascii=False)

    # Step 1: deterministic fast path
    screen: ScreenResult = screen_payload(payload_str)
    if screen.blocked:
        log.warning(
            "Pre-screen blocked payload task_id=%s findings=%d",
            task_id, len(screen.findings),
        )
        findings = [
            SecurityFinding(
                type=f["type"],
                severity="critical",
                location=f["location"],
                description=f["description"],
                recommendation="Remove or sanitize before resubmitting.",
            )
            for f in screen.findings
        ]
        verdict = SecurityVerdict(
            verdict="FAIL",
            severity="critical",
            findings=findings,
            summary=(
                f"Pre-screen halted pipeline: {len(findings)} critical finding(s) "
                "detected without LLM call."
            ),
            task_id=task_id,
            stage=stage,
        )
        _audit_security_event(
            "threat_screen_block",
            actor="security_reviewer",
            target=f"task {task_id or 'unknown'}",
            decision=verdict.verdict,
            severity="critical",
            reason=verdict.summary,
            task_id=task_id,
            rollback_ref=task_id or "manual_review",
        )
        return verdict

    # Step 2: LLM deep analysis — structured local first, cloud opt-in fallback
    raw_output = _local_security_review(payload_str)

    if not raw_output.strip():
        raw_output = _cloud_security_review(payload_str)

    verdict = _parse_llm_verdict(raw_output)
    verdict.task_id = task_id
    verdict.stage   = stage

    log.info(
        "Security verdict task_id=%s verdict=%s severity=%s findings=%d",
        task_id, verdict.verdict, verdict.severity, len(verdict.findings),
    )
    _audit_security_event(
        "security_verdict",
        actor="security_reviewer",
        target=f"task {task_id or 'unknown'}",
        decision=verdict.verdict,
        severity="critical" if verdict.severity == "critical" else "warning" if verdict.is_blocking() else "info",
        reason=verdict.summary,
        task_id=task_id,
        rollback_ref=task_id or "manual_review",
        extra={"review_stage": stage, "finding_count": len(verdict.findings)},
    )
    return verdict


# ─── DAG review chain emit ────────────────────────────────────────────────────

def emit_to_dag(verdict: SecurityVerdict, pg_conn=None) -> None:
    """
    Write the verdict to the review_chains table.

    pg_conn: a live psycopg2 connection, or None for local/test mode.
    When None, verdict is logged only — pipeline still receives the verdict
    object and can decide whether to halt.
    """
    pg_verdict = _VERDICT_TO_PG.get(verdict.verdict, "rejected")
    artifacts  = json.dumps({"findings": [asdict(f) for f in verdict.findings]})

    if pg_conn is None:
        log.info(
            "DAG emit (no-pg) task_id=%s pg_verdict=%s severity=%s findings=%d summary=%r",
            verdict.task_id, pg_verdict, verdict.severity,
            len(verdict.findings), verdict.summary[:80],
        )
        return

    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO review_chains
                    (task_id, stage, reviewer_id, verdict, notes, artifacts, completed_at)
                VALUES
                    (%s::uuid, %s, 'security_reviewer', %s::review_verdict, %s, %s::jsonb, now())
                ON CONFLICT (task_id, stage) DO UPDATE
                    SET verdict      = EXCLUDED.verdict,
                        notes        = EXCLUDED.notes,
                        artifacts    = EXCLUDED.artifacts,
                        completed_at = now()
                """,
                (verdict.task_id, verdict.stage, pg_verdict, verdict.summary, artifacts),
            )
        pg_conn.commit()
        log.info("DAG verdict written task_id=%s pg_verdict=%s", verdict.task_id, pg_verdict)
    except Exception:
        log.exception("Failed to write verdict to review_chains — rolling back")
        try:
            pg_conn.rollback()
        except Exception:
            logging.debug("[SecurityReviewer] silent failure in emit_to_dag", exc_info=True)


# ─── Convenience: review + emit in one call ───────────────────────────────────

def review_and_emit(
    payload:  dict,
    task_id:  str = "",
    stage:    int = 0,
    pg_conn=None,
) -> SecurityVerdict:
    verdict = review(payload, task_id=task_id, stage=stage)
    emit_to_dag(verdict, pg_conn=pg_conn)
    return verdict
