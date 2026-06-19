"""Tests for the independent pipeline truthfulness auditor.

These prove the auditor actually catches fabrication / tampering — i.e. it is
NOT a rubber stamp that silently passes everything. That guarantee matters most:
an audit that always says "clean" is itself a hallucination.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infra import pipeline_audit as pa


def _verdict(**kw) -> str:
    base = {
        "ts": "2026-06-11T00:00:00+00:00",
        "task_id": "t1", "agent_id": "qa-tester",
        "score": 1.0, "pass": True, "reason": "ok",
        "tool_calls": 1, "retry_count": 0,
    }
    base.update(kw)
    return json.dumps(base)


class AuditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.vpath = d / "verdicts.jsonl"
        self.lpath = d / "ledger.jsonl"
        self.tpath = d / "triage.jsonl"
        os.environ["JARVIS_VERDICTS_PATH"] = str(self.vpath)
        os.environ["JARVIS_PIPELINE_AUDIT_PATH"] = str(self.lpath)
        os.environ["JARVIS_PIPELINE_AUDIT_TRIAGE_PATH"] = str(self.tpath)
        # Isolate from the real projects.db so reconciliation stays silent here.
        os.environ["JARVIS_PROJECTS_DB"] = str(d / "no_such.db")

    def tearDown(self):
        for k in ("JARVIS_VERDICTS_PATH", "JARVIS_PIPELINE_AUDIT_PATH",
                  "JARVIS_PIPELINE_AUDIT_TRIAGE_PATH", "JARVIS_PROJECTS_DB"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _write(self, *lines: str):
        self.vpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _codes(self, report) -> set:
        return {f["code"] for f in report["findings"]}

    # ── invariants ────────────────────────────────────────────────────────────

    def test_clean_log_no_criticals(self):
        self._write(
            _verdict(task_id="a", tool_calls=2, score=1.0, **{"pass": True}),
            _verdict(task_id="b", tool_calls=0, score=0.0, **{"pass": False}),
        )
        report = pa.run_once(append=False)
        self.assertEqual(report["severity_counts"][pa.CRITICAL], 0)
        self.assertEqual(report["verdict"], "CLEAN")  # only INFO findings

    def test_fabrication_pass_conflict_is_critical(self):
        # Enrichment fields present: detector matched an exec claim, zero calls,
        # no inherited evidence — yet pass=True. This must be CRITICAL.
        self._write(_verdict(
            task_id="fab", agent_id="backend-engineer", tool_calls=0,
            score=0.9, **{"pass": True},
            fabrication_flag="I ran pytest", had_inherited_evidence=False,
        ))
        report = pa.run_once(append=False)
        self.assertIn("FABRICATION_PASS_CONFLICT", self._codes(report))
        self.assertEqual(pa.exit_code(report), 2)

    def test_inherited_evidence_suppresses_fabrication_flag(self):
        # Same shape but the runtime carried real evidence from a prior attempt:
        # a retry that cites inherited outputs is grounded, not fabricated.
        self._write(_verdict(
            task_id="fab2", agent_id="backend-engineer", tool_calls=0,
            score=0.9, **{"pass": True},
            fabrication_flag="I ran pytest", had_inherited_evidence=True,
        ))
        report = pa.run_once(append=False)
        self.assertNotIn("FABRICATION_PASS_CONFLICT", self._codes(report))

    def test_score_pass_incoherent_is_critical(self):
        # pass=True but score below threshold => log tamper or threshold drift.
        self._write(_verdict(task_id="x", score=0.2, **{"pass": True}, tool_calls=1))
        report = pa.run_once(append=False)
        self.assertIn("SCORE_PASS_INCOHERENT", self._codes(report))
        self.assertEqual(report["severity_counts"][pa.CRITICAL], 1)

    def test_silent_pass_no_evidence_warns(self):
        self._write(_verdict(
            task_id="s", agent_id="security-reviewer", tool_calls=0,
            score=1.0, **{"pass": True},
        ))
        report = pa.run_once(append=False)
        self.assertIn("SILENT_PASS_NO_EVIDENCE", self._codes(report))

    def test_zero_call_fail_does_not_claim_zero_calls_caused_failure(self):
        self._write(_verdict(
            task_id="z", agent_id="security-reviewer", tool_calls=0,
            score=0.0, **{"pass": False},
        ))

        report = pa.run_once(append=False)

        finding = next(f for f in report["findings"] if f["code"] == "ZERO_CALL_FAIL")
        self.assertIn("failure cause is not established", finding["detail"])
        self.assertNotIn("correctly failed", finding["detail"])

    def test_zero_call_fail_reports_deterministic_fabrication_evidence(self):
        self._write(_verdict(
            task_id="z-fab", agent_id="backend-engineer", tool_calls=0,
            score=0.0, **{"pass": False}, fabrication_flag="I ran pytest",
        ))

        report = pa.run_once(append=False)

        finding = next(f for f in report["findings"] if f["code"] == "ZERO_CALL_FAIL")
        self.assertIn("deterministic fabrication detection", finding["detail"])
        self.assertIn("I ran pytest", finding["detail"])

    def test_retry_exhaustion_warns(self):
        self._write(_verdict(
            task_id="r", tool_calls=1, score=0.3, **{"pass": False}, retry_count=2,
        ))
        report = pa.run_once(append=False)
        self.assertIn("RETRY_EXHAUSTION_FAIL", self._codes(report))

    def test_acknowledged_warning_no_longer_blocks_clean_current_audit(self):
        self._write(_verdict(
            task_id="r", tool_calls=1, score=0.3, **{"pass": False}, retry_count=2,
        ))
        first = pa.run_once(append=False)
        self.assertEqual(first["verdict"], "WARN")
        pa.acknowledge_findings(first["findings"], reason="historical baseline")

        second = pa.run_once(append=False)

        self.assertEqual(second["verdict"], "CLEAN")
        self.assertEqual(second["severity_counts"][pa.WARNING], 0)
        self.assertEqual(second["acknowledged_count"], 1)
        self.assertEqual(second["acknowledged_findings"][0]["code"], "RETRY_EXHAUSTION_FAIL")

    def test_malformed_record_warns(self):
        self.vpath.write_text("{not json}\n" + _verdict() + "\n", encoding="utf-8")
        report = pa.run_once(append=False)
        self.assertIn("MALFORMED_RECORD", self._codes(report))

    def test_missing_fields_warns(self):
        self.vpath.write_text(json.dumps({"task_id": "m", "agent_id": "qa-tester"}) + "\n",
                              encoding="utf-8")
        report = pa.run_once(append=False)
        self.assertIn("MISSING_FIELDS", self._codes(report))

    # ── tamper-evidence ───────────────────────────────────────────────────────

    def test_ledger_chain_detects_tamper(self):
        # Two clean runs build a 2-link chain.
        self._write(_verdict(task_id="a", tool_calls=1))
        pa.run_once(append=True)
        self._write(_verdict(task_id="a", tool_calls=1), _verdict(task_id="b", tool_calls=1))
        pa.run_once(append=True)
        # Tamper: edit the first ledger record's summary after the fact.
        entries = [json.loads(l) for l in self.lpath.read_text().splitlines() if l.strip()]
        self.assertEqual(len(entries), 2)
        entries[0]["pass_count"] = 999  # forge
        self.lpath.write_text("\n".join(pa._canonical(e) for e in entries) + "\n",
                              encoding="utf-8")
        findings = pa.verify_ledger_chain(pa.read_ledger(self.lpath))
        self.assertTrue(any(f.code == "LEDGER_TAMPERED" for f in findings))

    def test_verdict_log_mutation_detected(self):
        # Run once over a 2-line log; then edit an already-audited line.
        self._write(_verdict(task_id="a", tool_calls=1), _verdict(task_id="b", tool_calls=1))
        pa.run_once(append=True)
        # Silently rewrite a historical verdict (e.g. flip a fail to a pass).
        self._write(_verdict(task_id="a", tool_calls=1, score=0.0, **{"pass": False}),
                    _verdict(task_id="b", tool_calls=1))
        report = pa.run_once(append=True)
        self.assertIn("VERDICT_LOG_MUTATED", self._codes(report))
        self.assertEqual(pa.exit_code(report), 2)

    def test_verdict_log_truncation_detected(self):
        self._write(_verdict(task_id="a", tool_calls=1), _verdict(task_id="b", tool_calls=1))
        pa.run_once(append=True)
        self._write(_verdict(task_id="a", tool_calls=1))  # deleted a record
        report = pa.run_once(append=True)
        self.assertIn("VERDICT_LOG_TRUNCATED", self._codes(report))

    def test_clean_reaudit_is_stable(self):
        # Append-only growth must NOT trip the mutation detector.
        self._write(_verdict(task_id="a", tool_calls=1))
        pa.run_once(append=True)
        self._write(_verdict(task_id="a", tool_calls=1), _verdict(task_id="b", tool_calls=2))
        report = pa.run_once(append=True)
        self.assertNotIn("VERDICT_LOG_MUTATED", self._codes(report))
        self.assertNotIn("VERDICT_LOG_TRUNCATED", self._codes(report))
        self.assertEqual(report["severity_counts"][pa.CRITICAL], 0)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.vpath = d / "verdicts.jsonl"
        self.lpath = d / "ledger.jsonl"
        self.dbpath = d / "projects.db"
        self.tpath = d / "triage.jsonl"
        os.environ["JARVIS_VERDICTS_PATH"] = str(self.vpath)
        os.environ["JARVIS_PIPELINE_AUDIT_PATH"] = str(self.lpath)
        os.environ["JARVIS_PROJECTS_DB"] = str(self.dbpath)
        os.environ["JARVIS_PIPELINE_AUDIT_TRIAGE_PATH"] = str(self.tpath)

    def tearDown(self):
        for k in ("JARVIS_VERDICTS_PATH", "JARVIS_PIPELINE_AUDIT_PATH",
                  "JARVIS_PIPELINE_AUDIT_TRIAGE_PATH", "JARVIS_PROJECTS_DB"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _build_db(self, status="complete", has_syn=True, agent="security-reviewer",
                  task_id="task_orphan", complete_event=True):
        import sqlite3
        con = sqlite3.connect(str(self.dbpath))
        con.execute("CREATE TABLE projects (id TEXT, goal TEXT, status TEXT, "
                    "created_at TEXT, synthesis_result TEXT)")
        con.execute("CREATE TABLE project_events (id INTEGER PRIMARY KEY, project_id TEXT, "
                    "type TEXT, ts TEXT, payload TEXT)")
        con.execute("INSERT INTO projects VALUES (?,?,?,?,?)",
                    ("proj_1", "do thing", status, "2026-06-11", "synth" if has_syn else ""))
        con.execute("INSERT INTO project_events (project_id,type,ts,payload) VALUES (?,?,?,?)",
                    ("proj_1", "task_dispatched", "2026-06-11",
                     json.dumps({"task_id": task_id, "agent_id": agent})))
        if complete_event:
            con.execute("INSERT INTO project_events (project_id,type,ts,payload) VALUES (?,?,?,?)",
                        ("proj_1", "project_complete", "2026-06-11", "{}"))
        con.commit(); con.close()

    def _codes(self, report):
        return {f["code"] for f in report["findings"]}

    def test_unverified_completion_flagged(self):
        # Completed project, verifiable agent dispatched, NO matching verdict.
        self.vpath.write_text(_verdict(task_id="other", tool_calls=1) + "\n", encoding="utf-8")
        self._build_db(task_id="task_orphan", agent="security-reviewer")
        report = pa.run_once(append=False)
        self.assertIn("UNVERIFIED_COMPLETION", self._codes(report))
        self.assertIn("UNAUDITED_SYNTHESIS", self._codes(report))

    def test_verified_task_not_flagged(self):
        # Same project, but the dispatched task DOES have a verdict.
        self.vpath.write_text(_verdict(task_id="task_orphan", tool_calls=2) + "\n", encoding="utf-8")
        self._build_db(task_id="task_orphan", agent="security-reviewer")
        report = pa.run_once(append=False)
        self.assertNotIn("UNVERIFIED_COMPLETION", self._codes(report))
        self.assertNotIn("UNAUDITED_SYNTHESIS", self._codes(report))

    def test_non_verifiable_agent_not_flagged(self):
        # A non-verifiable agent legitimately has no verdict path.
        self.vpath.write_text(_verdict(task_id="other", tool_calls=1) + "\n", encoding="utf-8")
        self._build_db(task_id="task_orphan", agent="frontend-designer")
        report = pa.run_once(append=False)
        self.assertNotIn("UNVERIFIED_COMPLETION", self._codes(report))
        self.assertNotIn("UNAUDITED_SYNTHESIS", self._codes(report))

    def test_incomplete_project_not_flagged(self):
        # Still running: absence of a verdict is expected, not a gap.
        self.vpath.write_text(_verdict(task_id="other", tool_calls=1) + "\n", encoding="utf-8")
        self._build_db(task_id="task_orphan", status="running", complete_event=False)
        report = pa.run_once(append=False)
        self.assertNotIn("UNVERIFIED_COMPLETION", self._codes(report))

    def test_missing_db_is_silent(self):
        # No projects.db -> reconciliation contributes nothing, no crash.
        self.vpath.write_text(_verdict(task_id="a", tool_calls=1) + "\n", encoding="utf-8")
        report = pa.run_once(append=False)
        self.assertNotIn("UNVERIFIED_COMPLETION", self._codes(report))


class TriageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.tpath = d / "triage.jsonl"
        os.environ["JARVIS_PIPELINE_AUDIT_TRIAGE_PATH"] = str(self.tpath)

    def tearDown(self):
        os.environ.pop("JARVIS_PIPELINE_AUDIT_TRIAGE_PATH", None)
        self.tmp.cleanup()

    def _warn(self, task_id="w1"):
        return {"code": "RETRY_EXHAUSTION_FAIL", "severity": pa.WARNING,
                "task_id": task_id, "agent_id": "qa-tester", "detail": "d"}

    def test_acks_are_hash_chained(self):
        recs = pa.acknowledge_findings([self._warn("a"), self._warn("b")],
                                       reason="known baseline")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["prev_hash"], pa._hc.GENESIS)
        self.assertEqual(recs[1]["prev_hash"], recs[0]["this_hash"])
        self.assertEqual(pa.verify_triage_integrity(self.tpath), [])

    def test_edited_ack_detected(self):
        pa.acknowledge_findings([self._warn("a"), self._warn("b")], reason="x")
        entries = pa._hc.read_chain(self.tpath)
        entries[0]["reason"] = "forged — pretend a different operator approved"
        self.tpath.write_text("\n".join(pa._hc.canonical(e) for e in entries) + "\n")
        findings = pa.verify_triage_integrity(self.tpath)
        self.assertTrue(any(f.code == "TRIAGE_LEDGER_TAMPERED" for f in findings))
        self.assertEqual(findings[0].severity, pa.CRITICAL)

    def test_legacy_plain_records_tolerated(self):
        # Pre-hardening records have no this_hash — must not false-positive, and
        # a new chained ack appended after them still verifies.
        self.tpath.write_text(json.dumps({"fingerprint": "old", "code": "X",
                                          "severity": "warning", "reason": "legacy"}) + "\n")
        self.assertEqual(pa.verify_triage_integrity(self.tpath), [])
        pa.acknowledge_findings([self._warn("new")], reason="post-hardening")
        self.assertEqual(pa.verify_triage_integrity(self.tpath), [])

    def test_critical_cannot_be_acknowledged(self):
        crit = {"code": "SCORE_PASS_INCOHERENT", "severity": pa.CRITICAL,
                "task_id": "c", "agent_id": "backend-engineer", "detail": "d"}
        recs = pa.acknowledge_findings([crit], reason="attempt to silence")
        self.assertEqual(recs, [])


if __name__ == "__main__":
    unittest.main()
