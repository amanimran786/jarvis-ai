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
        return self.verdict in {"FAIL", "REQUEST_CHANGES"}


_FALLBACK_VERDICT = SecurityVerdict(
    verdict="FAIL",
    severity="critical",
    findings=[SecurityFinding(
        type="PARSE_ERROR",
        severity="critical",
        location="llm_output",
        description="LLM did not return parseable JSON. Treating as FAIL for safety.",
        recommendation="Manual review required.",
    )],
    summary="LLM output could not be parsed. Pipeline halted.",
)


# ─── LLM output parsing ───────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)

def _parse_llm_verdict(raw: str) -> SecurityVerdict:
    raw = _THINK_RE.sub("", raw).strip()

    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        log.warning("No JSON block in security reviewer output")
        return _FALLBACK_VERDICT

    try:
        data     = json.loads(raw[start:end])
        findings = [SecurityFinding(**f) for f in data.get("findings", [])]
        return SecurityVerdict(
            verdict=data.get("verdict", "FAIL"),
            severity=data.get("severity", "critical"),
            findings=findings,
            summary=data.get("summary", ""),
        )
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        log.warning("Failed to parse LLM verdict JSON: %s", exc)
        return _FALLBACK_VERDICT


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
        return SecurityVerdict(
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

    # Step 2: LLM deep analysis
    from agent_dispatch import dispatch
    chunks = list(dispatch("security_reviewer", payload_str))
    raw_output = "".join(chunks)

    verdict = _parse_llm_verdict(raw_output)
    verdict.task_id = task_id
    verdict.stage   = stage

    log.info(
        "Security verdict task_id=%s verdict=%s severity=%s findings=%d",
        task_id, verdict.verdict, verdict.severity, len(verdict.findings),
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
            pass


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
