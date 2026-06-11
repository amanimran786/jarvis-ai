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
        os.environ["JARVIS_VERDICTS_PATH"] = str(self.vpath)
        os.environ["JARVIS_PIPELINE_AUDIT_PATH"] = str(self.lpath)

    def tearDown(self):
        for k in ("JARVIS_VERDICTS_PATH", "JARVIS_PIPELINE_AUDIT_PATH"):
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

    def test_retry_exhaustion_warns(self):
        self._write(_verdict(
            task_id="r", tool_calls=1, score=0.3, **{"pass": False}, retry_count=2,
        ))
        report = pa.run_once(append=False)
        self.assertIn("RETRY_EXHAUSTION_FAIL", self._codes(report))

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


if __name__ == "__main__":
    unittest.main()
