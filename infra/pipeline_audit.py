"""
Pipeline truthfulness audit for Jarvis — independent anti-fabrication layer.

The agent pipeline already defends against fabricated results at runtime
(task_runtime._detect_fabricated_execution injects a RUNTIME WARNING, and an
LLM verifier scores each output). This module is the *independent* second line:
it does NOT trust the LLM verifier. It re-checks the persisted record with
deterministic invariants and maintains a tamper-evident ledger, so that
"the results are real, not hallucinated" is provable after the fact rather
than taken on the verifier's word.

Distinct from infra/security_audit.py:
  - security_audit answers "was this action authorized?"
  - pipeline_audit answers "is this result real, or faked?"

Inputs (READ-ONLY — this module never mutates pipeline state):
  - ~/.jarvis/verifier_verdicts.jsonl   (override: JARVIS_VERDICTS_PATH)
  - ~/.jarvis/projects.db               (override: JARVIS_PROJECTS_DB) [optional]

Output (this module's own append-only artifact):
  - ~/.jarvis/pipeline_audit.jsonl      (override: JARVIS_PIPELINE_AUDIT_PATH)

Tamper-evidence comes from two mechanisms:
  1. Each audit run appends one record whose `this_hash` chains the previous
     record's hash (sha256(prev_hash + canonical(record))). Editing any past
     ledger record breaks the chain on the next run -> LEDGER_TAMPERED.
  2. Each run stores a prefix hash of the verdict log it has already audited.
     If a previously-seen verdict line is later edited or deleted, the prefix
     hash stops matching -> VERDICT_LOG_MUTATED. This makes the *upstream*
     evidence tamper-evident even though this module cannot write to it.

CLI:
    python -m infra.pipeline_audit --once        # audit, print report, append ledger
    python -m infra.pipeline_audit --watch        # loop, re-audit on verdict-log change
    python -m infra.pipeline_audit --once --json  # machine-readable report
Exit code: 0 = clean, 1 = warnings only, 2 = at least one critical.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Pass threshold MUST mirror task_runtime._auto_verify (pass = score >= 0.65).
# If task_runtime changes it, this constant is the single knob to update; the
# SCORE_PASS_INCOHERENT invariant exists precisely to catch silent drift here.
PASS_THRESHOLD = 0.65
MAX_RETRIES = 2  # mirrors task_runtime retry budget

# Agents whose tasks imply real work (commands, tests, file reads). A pass with
# zero tool calls and no inherited evidence from one of these is suspicious.
# Mirrors task_runtime._VERIFIABLE_AGENTS; kept local to stay decoupled from
# that module's churn. _verifiable_agents() tries the live import first.
_DEFAULT_VERIFIABLE = frozenset({
    "security-reviewer", "qa-tester", "backend-engineer",
    "researcher", "output-quality-checker", "automation-engineer",
})

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"
_SEVERITY_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}


def _home_jarvis() -> Path:
    return Path(os.path.expanduser("~")) / ".jarvis"


def verdicts_path() -> Path:
    return Path(os.getenv("JARVIS_VERDICTS_PATH", str(_home_jarvis() / "verifier_verdicts.jsonl")))


def ledger_path() -> Path:
    return Path(os.getenv("JARVIS_PIPELINE_AUDIT_PATH", str(_home_jarvis() / "pipeline_audit.jsonl")))


def projects_db_path() -> Path:
    return Path(os.getenv("JARVIS_PROJECTS_DB", str(_home_jarvis() / "projects.db")))


def triage_path() -> Path:
    return Path(os.getenv("JARVIS_PIPELINE_AUDIT_TRIAGE_PATH", str(_home_jarvis() / "pipeline_audit_triage.jsonl")))


def _verifiable_agents() -> frozenset[str]:
    try:
        import task_runtime  # type: ignore
        agents = getattr(task_runtime, "_VERIFIABLE_AGENTS", None)
        if agents:
            return frozenset(agents)
    except Exception:
        pass
    return _DEFAULT_VERIFIABLE


# ─── Canonical hashing ────────────────────────────────────────────────────────
# Tamper-evidence is provided by the shared infra._hashchain helper so this and
# security_audit (S5) use one verified mechanism. Thin aliases keep call sites
# and the on-disk ledger format identical to before the extraction.
from infra import _hashchain as _hc
from infra._hashchain import canonical as _canonical, sha256_hex as _sha256


def prefix_sha(lines: list[str], n: int) -> str:
    """Hash of the first n raw verdict lines — detects edits to history."""
    return _sha256("\n".join(lines[:n]))


# ─── Findings ─────────────────────────────────────────────────────────────────

class Finding:
    __slots__ = ("code", "severity", "task_id", "agent_id", "detail")

    def __init__(self, code: str, severity: str, task_id: str, agent_id: str, detail: str):
        self.code = code
        self.severity = severity
        self.task_id = task_id
        self.agent_id = agent_id
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "detail": self.detail,
            "fingerprint": finding_fingerprint(self),
        }


def finding_fingerprint(finding: "Finding | dict") -> str:
    """Stable id for triaging known historical warnings without mutating logs."""
    if isinstance(finding, Finding):
        parts = (finding.code, finding.severity, finding.task_id, finding.agent_id, finding.detail)
    else:
        parts = (
            str(finding.get("code", "")),
            str(finding.get("severity", "")),
            str(finding.get("task_id", "")),
            str(finding.get("agent_id", "")),
            str(finding.get("detail", "")),
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def read_triage(path: Path | None = None) -> dict[str, dict]:
    path = path or triage_path()
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        fp = str(item.get("fingerprint", ""))
        if fp:
            out[fp] = item
    return out


def acknowledge_findings(findings: list[dict], *, reason: str, actor: str = "operator",
                         path: Path | None = None) -> list[dict]:
    """Append triage acknowledgements, hash-chained for tamper-evidence.

    Each new ack chains onto the prior record's `this_hash` (GENESIS if the file
    is empty or holds only legacy un-chained records), so editing a past
    acknowledgement — who/why/when a warning was silenced — is detectable via
    verify_triage_integrity(). Does not edit verdict or audit ledgers. Only
    WARNING findings are acknowledgeable; criticals are never suppressible."""
    path = path or triage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _hc.read_chain(path)
    records: list[dict] = []
    for finding in findings:
        if finding.get("severity") != WARNING:
            continue
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "fingerprint": finding_fingerprint(finding),
            "code": finding.get("code", ""),
            "task_id": finding.get("task_id", ""),
            "agent_id": finding.get("agent_id", ""),
            "reason": reason,
            "actor": actor,
        }
        stored = _hc.append_record(path, rec, entries)  # adds prev_hash/this_hash
        entries.append(stored)
        records.append(stored)
    return records


def verify_triage_integrity(path: Path | None = None) -> list[Finding]:
    """Verify the hash-chained suffix of the triage ledger. Records written
    before this hardening have no `this_hash` and are treated as an unprotected
    legacy baseline (skipped, not failed); every chained record after them must
    verify. A broken chain means an acknowledgement was edited after the fact —
    a silent-suppression attempt — surfaced as CRITICAL so it can't itself be
    triaged away."""
    path = path or triage_path()
    entries = _hc.read_chain(path)
    chained = [e for e in entries if isinstance(e, dict) and _hc.HASH_FIELD in e]
    if not chained:
        return []  # all legacy or empty — nothing chained to verify yet
    ok, reason, idx = _hc.verify_chain(chained)
    if ok:
        return []
    return [Finding(
        "TRIAGE_LEDGER_TAMPERED", CRITICAL, f"triage:{idx}", "-",
        f"acknowledgement chain broken ({reason}) — a triage record was edited after the fact",
    )]


def _apply_triage(findings: list[Finding]) -> tuple[list[Finding], list[dict]]:
    triage = read_triage()
    if not triage:
        return findings, []
    active: list[Finding] = []
    acknowledged: list[dict] = []
    for finding in findings:
        fp = finding_fingerprint(finding)
        if finding.severity == WARNING and fp in triage:
            item = finding.to_dict()
            item["acknowledged"] = True
            item["triage"] = triage[fp]
            acknowledged.append(item)
        else:
            active.append(finding)
    return active, acknowledged


# ─── Verdict loading ──────────────────────────────────────────────────────────

def load_verdict_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    return [ln for ln in raw.splitlines() if ln.strip()]


def parse_verdicts(lines: list[str]) -> tuple[list[dict], list[Finding]]:
    """Parse JSONL verdict lines; malformed lines become MALFORMED_RECORD findings."""
    records: list[dict] = []
    findings: list[Finding] = []
    for i, ln in enumerate(lines):
        try:
            obj = json.loads(ln)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
            obj["_line"] = i
            records.append(obj)
        except Exception as exc:
            findings.append(Finding(
                "MALFORMED_RECORD", WARNING, f"line:{i}", "?",
                f"unparseable verdict line: {exc}",
            ))
    return records, findings


# ─── Invariants ───────────────────────────────────────────────────────────────

def audit_records(records: list[dict]) -> list[Finding]:
    """Apply deterministic truthfulness invariants. Verifier-distrust: we never
    rely on the LLM's `pass`/`reason`, only on hard signals (score, tool_calls,
    and — when present — the runtime's deterministic fabrication_flag)."""
    findings: list[Finding] = []
    verifiable = _verifiable_agents()
    last_ts: str | None = None
    retry_seen: dict[str, int] = {}

    for r in records:
        tid = str(r.get("task_id", "?"))
        aid = str(r.get("agent_id", "?"))
        score = r.get("score")
        passed = r.get("pass")
        tool_calls = r.get("tool_calls")
        retry = r.get("retry_count", 0)
        # Enrichment fields (present only once F adopts the proposed patch):
        fab_flag = r.get("fabrication_flag")            # str: matched exec claim, or ""
        inherited = bool(r.get("had_inherited_evidence", False))
        transcript_sha = r.get("transcript_sha256")     # str or None

        # Schema sanity — missing hard signals make every other check unsafe.
        if score is None or passed is None or tool_calls is None:
            findings.append(Finding(
                "MISSING_FIELDS", WARNING, tid, aid,
                f"record missing score/pass/tool_calls (score={score}, pass={passed}, tool_calls={tool_calls})",
            ))
            continue

        # I1 — score/pass coherence. The runtime computes pass = score >= 0.65
        # and is explicit that the model's own pass flag is never trusted. If a
        # logged pass disagrees with its logged score, either the log was
        # tampered or the threshold drifted. Either way: do not trust this pass.
        try:
            expected_pass = float(score) >= PASS_THRESHOLD
            if bool(passed) != expected_pass:
                findings.append(Finding(
                    "SCORE_PASS_INCOHERENT", CRITICAL, tid, aid,
                    f"pass={passed} but score={score} (threshold {PASS_THRESHOLD} => expected pass={expected_pass})",
                ))
        except (TypeError, ValueError):
            findings.append(Finding(
                "MISSING_FIELDS", WARNING, tid, aid, f"non-numeric score={score!r}",
            ))

        # I2 — fabrication/pass conflict (fires only with the enrichment patch).
        # The runtime's deterministic detector matched an execution claim, the
        # agent made zero real tool calls, no inherited evidence — yet the
        # verifier passed it. That is a fabricated result that slipped the gate.
        if fab_flag and bool(passed) and int(tool_calls) == 0 and not inherited:
            findings.append(Finding(
                "FABRICATION_PASS_CONFLICT", CRITICAL, tid, aid,
                f"PASSED a response that claims execution ({fab_flag!r}) with zero tool calls and no inherited evidence",
            ))

        # I3 — silent pass: a verifiable agent passed with zero tool calls and
        # no carried-over evidence. Detectable with TODAY's schema. Not proof of
        # fabrication, but exactly the shape fabrication takes — worth a human look.
        if (bool(passed) and int(tool_calls) == 0 and not inherited
                and aid in verifiable):
            findings.append(Finding(
                "SILENT_PASS_NO_EVIDENCE", WARNING, tid, aid,
                "verifiable agent passed with zero tool calls and no inherited evidence",
            ))

        # I4 — claimed-evidence without captured transcript (enrichment only):
        # tool_calls > 0 but the runtime stored no transcript hash means the
        # "evidence" the verifier scored against was never persisted.
        if transcript_sha is not None and int(tool_calls) > 0 and not transcript_sha:
            findings.append(Finding(
                "EVIDENCE_NOT_CAPTURED", WARNING, tid, aid,
                f"{tool_calls} tool call(s) but empty transcript hash — evidence not persisted",
            ))

        # I5 — retry exhaustion still failing: the agent could not ground its
        # answer across the full retry budget. Signals a stuck/fabricating agent.
        if int(retry) >= MAX_RETRIES and not bool(passed):
            findings.append(Finding(
                "RETRY_EXHAUSTION_FAIL", WARNING, tid, aid,
                f"failed at retry_count={retry} (budget {MAX_RETRIES}) — could not ground its answer",
            ))

        # I6 — the fabrication defense working as designed (informational, so
        # operators can see the system actively caught a fake, not just silence).
        if int(tool_calls) == 0 and not bool(passed) and not inherited:
            findings.append(Finding(
                "ZERO_CALL_FAIL", INFO, tid, aid,
                f"correctly failed: zero tool calls (score={score})",
            ))

        # I7 — monotonic timestamps: out-of-order ts implies reordering/tamper.
        ts = r.get("ts")
        if isinstance(ts, str):
            if last_ts is not None and ts < last_ts:
                findings.append(Finding(
                    "NONMONOTONIC_TS", WARNING, tid, aid,
                    f"timestamp {ts} precedes prior record's {last_ts} — log reordered?",
                ))
            last_ts = ts if (last_ts is None or ts >= last_ts) else last_ts

        retry_seen[tid] = max(retry_seen.get(tid, 0), int(retry))

    return findings


# ─── Cross-source reconciliation (projects.db) ────────────────────────────────

def reconcile_projects_db(db_path: Path, verdict_task_ids: set[str]) -> list[Finding]:
    """Audit the *rest* of the pipeline, not just the verifier's own log.

    A verdict only exists for verifiable agents with auto-verify on. Tasks that
    were dispatched and projects marked complete WITHOUT a recorded verdict are
    the blind spot: a result shipped with no truthfulness check. We surface
    those as warnings (absence of a verdict is an audit gap, not proof of a fake).

    Read-only; never raises into the audit (a missing/locked DB just yields no
    findings)."""
    findings: list[Finding] = []
    if not db_path.exists():
        return findings
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except Exception:
        return findings
    verifiable = _verifiable_agents()
    try:
        # Map project -> dispatched (task_id, agent_id) and whether it completed.
        dispatched: dict[str, list[tuple[str, str]]] = {}
        completed: set[str] = set()
        for row in con.execute("SELECT project_id, type, payload FROM project_events"):
            pid = row["project_id"]
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                payload = {}
            if row["type"] == "task_dispatched":
                dispatched.setdefault(pid, []).append(
                    (str(payload.get("task_id", "")), str(payload.get("agent_id", "")))
                )
            elif row["type"] == "project_complete":
                completed.add(pid)

        try:
            projects = {r["id"]: r for r in con.execute(
                "SELECT id, status, goal, "
                "(synthesis_result IS NOT NULL AND synthesis_result != '') AS has_syn "
                "FROM projects")}
        except Exception:
            projects = {}

        for pid, tasks in dispatched.items():
            proj = projects.get(pid)
            status = (proj["status"] if proj else "?")
            proj_done = pid in completed or status in ("complete", "completed", "done", "succeeded")
            verified_any = False
            verifiable_task_count = 0
            for task_id, agent_id in tasks:
                if agent_id in verifiable:
                    verifiable_task_count += 1
                if task_id and task_id in verdict_task_ids:
                    verified_any = True
                    continue
                # Only a verifiable agent's silent completion is concerning;
                # non-verifiable agents legitimately have no verdict path.
                if agent_id in verifiable and proj_done:
                    findings.append(Finding(
                        "UNVERIFIED_COMPLETION", WARNING, task_id or "?", agent_id or "?",
                        f"verifiable task dispatched in project {pid} (status={status}) "
                        f"but no verdict was recorded — completion never truth-checked",
                    ))
            # A project that shipped a synthesized result with zero verified
            # underlying tasks is an unaudited deliverable.
            if proj and proj["has_syn"] and proj_done and not verified_any and verifiable_task_count:
                findings.append(Finding(
                    "UNAUDITED_SYNTHESIS", WARNING, pid, "manager",
                    f"project completed with a synthesis_result but none of its "
                    f"{verifiable_task_count} verifiable dispatched task(s) have a recorded verdict",
                ))
    except Exception:
        # Schema drift or a partial DB must not break the audit.
        return findings
    finally:
        try:
            con.close()
        except Exception:
            pass
    return findings


# ─── Ledger (tamper-evident hash chain) ───────────────────────────────────────

def read_ledger(path: Path) -> list[dict]:
    return _hc.read_chain(path)


def verify_ledger_chain(entries: list[dict]) -> list[Finding]:
    """Recompute the hash chain; any mismatch means a past ledger record was
    edited after the fact. Wraps the shared verifier into audit Findings."""
    ok, reason, idx = _hc.verify_chain(entries)
    if ok:
        return []
    code = ("LEDGER_CORRUPT" if reason and "unparseable" in reason
            else "LEDGER_CHAIN_BREAK" if reason and "prev_hash" in reason
            else "LEDGER_TAMPERED")
    return [Finding(code, CRITICAL, f"ledger:{idx}", "-", reason or "chain broken")]


def append_ledger(path: Path, record: dict, entries: list[dict]) -> dict:
    """Append a run record, chaining it to the last verified entry's hash."""
    return _hc.append_record(path, record, entries)


# ─── Orchestration ────────────────────────────────────────────────────────────

def run_once(append: bool = True) -> dict:
    """Audit the verdict log once. Returns a structured report and (by default)
    appends a tamper-evident record to the ledger."""
    vpath = verdicts_path()
    lpath = ledger_path()

    lines = load_verdict_lines(vpath)
    records, parse_findings = parse_verdicts(lines)
    findings = parse_findings + audit_records(records)

    # Reconcile against the rest of the pipeline: dispatched/completed work in
    # projects.db that never produced a verdict is the unaudited blind spot.
    verdict_task_ids = {str(r.get("task_id")) for r in records if r.get("task_id")}
    findings += reconcile_projects_db(projects_db_path(), verdict_task_ids)

    ledger_entries = read_ledger(lpath)
    findings += verify_ledger_chain(ledger_entries)

    # Upstream tamper check: compare the prefix hash of the verdict log against
    # what the last run recorded for that same prefix length. A mismatch means a
    # previously-audited verdict line was edited or deleted.
    last_run = next((e for e in reversed(ledger_entries) if "verdicts_count" in e), None)
    if last_run is not None:
        prev_n = int(last_run.get("verdicts_count", 0))
        prev_prefix = last_run.get("verdicts_prefix_sha", "")
        if prev_n <= len(lines):
            now_prefix = prefix_sha(lines, prev_n)
            if prev_prefix and now_prefix != prev_prefix:
                findings.append(Finding(
                    "VERDICT_LOG_MUTATED", CRITICAL, "verdict-log", "-",
                    f"first {prev_n} verdict lines changed since last audit — history was edited/deleted",
                ))
        else:
            findings.append(Finding(
                "VERDICT_LOG_TRUNCATED", CRITICAL, "verdict-log", "-",
                f"verdict log shrank from {prev_n} to {len(lines)} lines — records deleted",
            ))

    # The triage ledger can suppress warnings, so its own integrity is audited:
    # a tampered acknowledgement chain is CRITICAL (and thus itself un-triageable).
    findings += verify_triage_integrity()

    findings, acknowledged = _apply_triage(findings)

    sev_counts = {CRITICAL: 0, WARNING: 0, INFO: 0}
    for f in findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1

    passes = sum(1 for r in records if r.get("pass") is True)
    fails = sum(1 for r in records if r.get("pass") is False)

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": _sha256(str(time.time_ns()))[:12],
        "verdicts_count": len(lines),
        "verdicts_prefix_sha": prefix_sha(lines, len(lines)),
        "records_parsed": len(records),
        "pass_count": passes,
        "fail_count": fails,
        "severity_counts": sev_counts,
        "findings": [f.to_dict() for f in findings],
        "acknowledged_findings": acknowledged,
        "acknowledged_count": len(acknowledged),
        "verdict": ("CRITICAL" if sev_counts[CRITICAL]
                    else "WARN" if sev_counts[WARNING] else "CLEAN"),
    }

    if append:
        # The ledger record stores summary + tamper-anchors, not the full
        # finding list, to stay compact; findings live in the returned report.
        ledger_record = {
            "ts": report["ts"],
            "run_id": report["run_id"],
            "verdicts_count": report["verdicts_count"],
            "verdicts_prefix_sha": report["verdicts_prefix_sha"],
            "records_parsed": report["records_parsed"],
            "pass_count": passes,
            "fail_count": fails,
            "severity_counts": sev_counts,
            "verdict": report["verdict"],
        }
        appended = append_ledger(lpath, ledger_record, ledger_entries)
        report["ledger_hash"] = appended["this_hash"]

    return report


def exit_code(report: dict) -> int:
    sc = report["severity_counts"]
    if sc.get(CRITICAL):
        return 2
    if sc.get(WARNING):
        return 1
    return 0


def format_report(report: dict) -> str:
    sc = report["severity_counts"]
    lines = [
        f"Pipeline truthfulness audit — {report['verdict']}  ({report['ts']})",
        f"  verdicts: {report['verdicts_count']}  parsed: {report['records_parsed']}  "
        f"pass: {report['pass_count']}  fail: {report['fail_count']}",
        f"  findings: {sc.get(CRITICAL,0)} critical, {sc.get(WARNING,0)} warning, {sc.get(INFO,0)} info",
    ]
    if report.get("ledger_hash"):
        lines.append(f"  ledger: {report['ledger_hash'][:16]}…  ({ledger_path()})")
    if report.get("acknowledged_count"):
        lines.append(f"  acknowledged historical warnings: {report['acknowledged_count']}  ({triage_path()})")
    sev_order = {CRITICAL: 0, WARNING: 1, INFO: 2}
    for f in sorted(report["findings"], key=lambda x: sev_order.get(x["severity"], 9)):
        mark = "✗" if f["severity"] == CRITICAL else ("⚠" if f["severity"] == WARNING else "·")
        lines.append(f"  {mark} [{f['severity']}] {f['code']}  {f['agent_id']}/{f['task_id']}: {f['detail']}")
    return "\n".join(lines)


def watch(interval: float = 30.0) -> None:
    """Re-audit whenever the verdict log changes; heartbeat audit every interval.
    Long idle interval by design (rate-limit / cache friendly)."""
    vpath = verdicts_path()
    last_sig = None
    print(f"[pipeline_audit] watching {vpath} every {interval:.0f}s (Ctrl-C to stop)", flush=True)
    while True:
        try:
            sig = (vpath.stat().st_mtime, vpath.stat().st_size) if vpath.exists() else (0, 0)
        except OSError:
            sig = (0, 0)
        if sig != last_sig:
            report = run_once(append=True)
            print(format_report(report), flush=True)
            if report["verdict"] == "CRITICAL":
                print("[pipeline_audit] CRITICAL findings — see ledger; escalate.", flush=True)
            last_sig = sig
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Jarvis pipeline truthfulness audit")
    ap.add_argument("--once", action="store_true", help="audit once and exit (default)")
    ap.add_argument("--watch", action="store_true", help="loop, re-audit on verdict-log change")
    ap.add_argument("--interval", type=float, default=30.0, help="watch poll interval seconds")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    ap.add_argument("--no-append", action="store_true", help="do not write to the ledger")
    ap.add_argument("--ack-current-warnings", action="store_true",
                    help="acknowledge current warning findings as historical triage")
    ap.add_argument("--ack-reason", default="historical baseline acknowledged by operator",
                    help="reason stored with --ack-current-warnings")
    args = ap.parse_args(argv)

    if args.watch:
        watch(args.interval)
        return 0

    report = run_once(append=not args.no_append)
    if args.ack_current_warnings:
        acknowledged = acknowledge_findings(report.get("findings", []), reason=args.ack_reason)
        report["acknowledged_now"] = acknowledged
        report = run_once(append=not args.no_append)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
