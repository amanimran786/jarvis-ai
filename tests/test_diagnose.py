"""
tests/test_diagnose.py — Unit tests for harness/diagnose.py

Covers all 8 checks, DiagnoseReport.overall logic, and diagnose_text() formatting.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Pre-stub `ollama` so brain_ollama.py can be imported in envs without it
import sys
from unittest.mock import MagicMock as _MM
if "ollama" not in sys.modules:
    sys.modules["ollama"] = _MM()

from harness.diagnose import (
    CheckResult,
    DiagnoseReport,
    OK, WARN, FAIL,
    _check_ollama,
    _check_memory,
    _check_audit_log,
    _check_google_auth,
    _check_budget,
    _check_self_eval,
    _check_adaptive_router,
    _check_tests,
    run_diagnose,
    diagnose_text,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _tmpbase() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    (base / "logs").mkdir(parents=True)
    (base / "memory" / "working").mkdir(parents=True)
    (base / "memory" / "episodic").mkdir(parents=True)
    (base / "memory" / "semantic").mkdir(parents=True)
    (base / "kb" / "core").mkdir(parents=True)
    return td, base


def _patch_base(base: Path):
    return patch("harness.diagnose._base_dir", return_value=base)


def _recent_ts(hours_ago: float = 0.1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _old_ts(hours_ago: float = 2.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ── Test 1: DiagnoseReport.overall aggregation ─────────────────────────────────

class TestDiagnoseReportOverall(unittest.TestCase):
    """overall() returns the worst status across all checks."""

    def test_overall_ok_when_all_ok(self):
        r = DiagnoseReport(results=[
            CheckResult("A", OK, "good"),
            CheckResult("B", OK, "good"),
        ])
        self.assertEqual(r.overall, OK)

    def test_overall_warn_when_any_warn(self):
        r = DiagnoseReport(results=[
            CheckResult("A", OK, "good"),
            CheckResult("B", WARN, "degraded"),
        ])
        self.assertEqual(r.overall, WARN)

    def test_overall_fail_beats_warn(self):
        r = DiagnoseReport(results=[
            CheckResult("A", WARN, "degraded"),
            CheckResult("B", FAIL, "broken"),
        ])
        self.assertEqual(r.overall, FAIL)

    def test_by_name_returns_correct_result(self):
        cr = CheckResult("Ollama", OK, "2 models")
        r = DiagnoseReport(results=[cr])
        self.assertIs(r.by_name("Ollama"), cr)

    def test_by_name_none_when_missing(self):
        r = DiagnoseReport(results=[])
        self.assertIsNone(r.by_name("NonExistent"))


# ── Test 2: _check_ollama ──────────────────────────────────────────────────────

class TestCheckOllama(unittest.TestCase):
    """Ollama: reachable + models listed."""

    def test_ok_with_models(self):
        with patch("brains.brain_ollama.list_local_models",
                   return_value=["qwen3:30b", "phi4:latest"]):
            r = _check_ollama()
        self.assertEqual(r.status, OK)
        self.assertIn("2", r.detail)

    def test_warn_no_models(self):
        with patch("brains.brain_ollama.list_local_models", return_value=[]):
            r = _check_ollama()
        self.assertEqual(r.status, WARN)
        self.assertIn("no models", r.detail)

    def test_fail_unreachable(self):
        with patch("brains.brain_ollama.list_local_models",
                   side_effect=ConnectionError("refused")):
            r = _check_ollama()
        self.assertEqual(r.status, FAIL)
        self.assertTrue(r.fix, "fix instruction should be non-empty")

    def test_more_than_four_models_truncated(self):
        models = [f"m{i}:latest" for i in range(6)]
        with patch("brains.brain_ollama.list_local_models", return_value=models):
            r = _check_ollama()
        self.assertEqual(r.status, OK)
        self.assertIn("+2 more", r.detail)


# ── Test 3: _check_memory ──────────────────────────────────────────────────────

class TestCheckMemory(unittest.TestCase):
    """Memory: working/, episodic/, kb/ non-empty."""

    def setUp(self):
        self._td, self._base = _tmpbase()
        # Seed non-empty dirs
        (self._base / "memory" / "working" / "session.json").write_text("{}")
        (self._base / "memory" / "episodic" / "ep1.json").write_text("{}")
        (self._base / "kb" / "core" / "identity.md").write_text("# id")

    def tearDown(self):
        self._td.cleanup()

    def test_ok_when_all_present_and_non_empty(self):
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, OK)

    def test_warn_when_working_dir_missing(self):
        import shutil
        shutil.rmtree(self._base / "memory" / "working")
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("working", r.detail)

    def test_warn_when_episodic_dir_empty(self):
        ep_dir = self._base / "memory" / "episodic"
        for f in ep_dir.iterdir():
            f.unlink()
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("episodic", r.detail)

    def test_warn_when_kb_missing(self):
        import shutil
        shutil.rmtree(self._base / "kb")
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("kb/", r.detail)


# ── Test 4: _check_audit_log ───────────────────────────────────────────────────

class TestCheckAuditLog(unittest.TestCase):
    """Audit log: exists and last entry < 1 hour ago."""

    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def _write_audit(self, ts: str) -> Path:
        path = self._base / "logs" / "audit.jsonl"
        path.write_text(json.dumps({"ts": ts, "event": "test"}) + "\n")
        return path

    def test_ok_when_recent_entry(self):
        self._write_audit(_recent_ts(0.1))
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertEqual(r.status, OK)

    def test_warn_when_last_entry_stale(self):
        self._write_audit(_old_ts(2.0))
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertEqual(r.status, WARN)
        self.assertIn("stale", r.detail)

    def test_warn_when_file_missing(self):
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertEqual(r.status, WARN)
        self.assertIn("not found", r.detail)

    def test_entry_count_in_detail(self):
        path = self._base / "logs" / "audit.jsonl"
        entries = [json.dumps({"ts": _recent_ts(0.1), "event": f"e{i}"}) for i in range(5)]
        path.write_text("\n".join(entries) + "\n")
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertIn("5", r.detail)


# ── Test 5: _check_budget ──────────────────────────────────────────────────────

class TestCheckBudget(unittest.TestCase):
    """Budget: calls status_text() and checks hard limits."""

    def test_ok_when_no_hard_limits(self):
        ok_state = {"hard": False, "soft": False, "used_1h": 0, "used_session": 0,
                    "used_week": 0, "limit_soft": 80000, "limit_hard": 100000,
                    "provider": "anthropic", "tier": "paid"}
        with patch("harness.budget.status_text", return_value="budget ok"), \
             patch("harness.budget.check", return_value=ok_state):
            r = _check_budget()
        self.assertEqual(r.status, OK)
        self.assertIn("within limits", r.detail)

    def test_warn_when_hard_limit_hit(self):
        hard_state = {"hard": True, "soft": False, "used_1h": 150000, "used_session": 0,
                      "used_week": 0, "limit_soft": 80000, "limit_hard": 100000,
                      "provider": "anthropic", "tier": "paid"}
        with patch("harness.budget.status_text", return_value="hard limit"), \
             patch("harness.budget.check", return_value=hard_state):
            r = _check_budget()
        self.assertEqual(r.status, WARN)
        self.assertIn("HARD LIMIT", r.detail)

    def test_extra_contains_budget_text(self):
        ok_state = {"hard": False, "soft": False, "used_1h": 0, "used_session": 0,
                    "used_week": 0, "limit_soft": 0, "limit_hard": 0,
                    "provider": "ollama_cloud", "tier": "cloud_free"}
        with patch("harness.budget.status_text", return_value="detailed budget report"), \
             patch("harness.budget.check", return_value=ok_state):
            r = _check_budget()
        self.assertIn("detailed budget report", r.extra)


# ── Test 6: _check_self_eval ───────────────────────────────────────────────────

class TestCheckSelfEval(unittest.TestCase):
    """Self-eval: count + avg quality of last 20."""

    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def _write_evals(self, scores: list[float]) -> None:
        path = self._base / "logs" / "self_eval.jsonl"
        entries = [
            {"ts": _recent_ts(i * 0.01), "response_quality": q}
            for i, q in enumerate(scores)
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def test_ok_when_good_scores(self):
        self._write_evals([0.8] * 5)
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, OK)
        self.assertIn("0.80", r.detail)

    def test_warn_when_low_avg_quality(self):
        self._write_evals([0.4] * 10)
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, WARN)

    def test_warn_when_missing(self):
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, WARN)
        self.assertIn("not found", r.detail)

    def test_only_last_20_used_for_avg(self):
        # 30 entries: first 10 bad, last 20 good
        scores = [0.3] * 10 + [0.9] * 20
        self._write_evals(scores)
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, OK)
        self.assertIn("0.90", r.detail)

    def test_total_count_in_detail(self):
        self._write_evals([0.7] * 7)
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertIn("7", r.detail)


# ── Test 7: _check_adaptive_router ────────────────────────────────────────────

class TestCheckAdaptiveRouter(unittest.TestCase):
    """Router: OK when no demotions, WARN when routes demoted."""

    def test_ok_when_no_demotions(self):
        with patch("harness.adaptive_router.route_quality_report",
                   return_value="All routes healthy"), \
             patch("harness.adaptive_router.get_demoted_routes",
                   return_value=frozenset()):
            r = _check_adaptive_router()
        self.assertEqual(r.status, OK)
        self.assertIn("within quality threshold", r.detail)

    def test_warn_when_routes_demoted(self):
        with patch("harness.adaptive_router.route_quality_report",
                   return_value="Status route demoted"), \
             patch("harness.adaptive_router.get_demoted_routes",
                   return_value=frozenset({"Status", "Vault"})):
            r = _check_adaptive_router()
        self.assertEqual(r.status, WARN)
        self.assertIn("2", r.detail)

    def test_extra_contains_route_report(self):
        with patch("harness.adaptive_router.route_quality_report",
                   return_value="route quality detail"), \
             patch("harness.adaptive_router.get_demoted_routes",
                   return_value=frozenset()):
            r = _check_adaptive_router()
        self.assertIn("route quality detail", r.extra)


# ── Test 8: _check_tests ──────────────────────────────────────────────────────

class TestCheckTests(unittest.TestCase):
    """Test suite: parse pytest output for pass/fail counts."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = ""
        result.returncode = returncode
        return result

    def test_ok_when_all_passed(self):
        with patch("subprocess.run",
                   return_value=self._mock_run("42 passed in 3.14s")):
            r = _check_tests()
        self.assertEqual(r.status, OK)
        self.assertIn("42", r.detail)
        self.assertIn("0 failed", r.detail)

    def test_warn_when_failures(self):
        with patch("subprocess.run",
                   return_value=self._mock_run("38 passed, 4 failed in 5.01s", 1)):
            r = _check_tests()
        self.assertEqual(r.status, WARN)
        self.assertIn("4 failed", r.detail)

    def test_warn_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pytest", 60)):
            r = _check_tests()
        self.assertEqual(r.status, WARN)
        self.assertIn("timed out", r.detail)


# ── Test 9: run_diagnose ───────────────────────────────────────────────────────

class TestRunDiagnose(unittest.TestCase):
    """run_diagnose() runs all 8 checks and populates DiagnoseReport."""

    def _patch_all(self, base: Path):
        """Return all context-manager patches for a clean run."""
        ok = CheckResult("x", OK, "ok")
        return [
            patch("harness.diagnose._check_ollama",        return_value=ok),
            patch("harness.diagnose._check_memory",        return_value=ok),
            patch("harness.diagnose._check_audit_log",     return_value=ok),
            patch("harness.diagnose._check_google_auth",   return_value=ok),
            patch("harness.diagnose._check_budget",        return_value=ok),
            patch("harness.diagnose._check_self_eval",     return_value=ok),
            patch("harness.diagnose._check_adaptive_router", return_value=ok),
            patch("harness.diagnose._check_tests",         return_value=ok),
        ]

    def test_returns_eight_results(self):
        td, base = _tmpbase()
        try:
            patches = self._patch_all(base)
            with patches[0], patches[1], patches[2], patches[3], \
                 patches[4], patches[5], patches[6], patches[7]:
                report = run_diagnose()
            self.assertEqual(len(report.results), 8)
        finally:
            td.cleanup()

    def test_crashed_check_becomes_fail_not_exception(self):
        with patch("harness.diagnose._check_ollama",
                   side_effect=RuntimeError("boom")), \
             patch("harness.diagnose._check_memory",
                   return_value=CheckResult("Memory", OK, "ok")), \
             patch("harness.diagnose._check_audit_log",
                   return_value=CheckResult("Audit", OK, "ok")), \
             patch("harness.diagnose._check_google_auth",
                   return_value=CheckResult("Google", OK, "ok")), \
             patch("harness.diagnose._check_budget",
                   return_value=CheckResult("Budget", OK, "ok")), \
             patch("harness.diagnose._check_self_eval",
                   return_value=CheckResult("Self-eval", OK, "ok")), \
             patch("harness.diagnose._check_adaptive_router",
                   return_value=CheckResult("Router", OK, "ok")), \
             patch("harness.diagnose._check_tests",
                   return_value=CheckResult("Tests", OK, "ok")):
            report = run_diagnose()
        # All 8 results still present; Ollama is FAIL
        self.assertEqual(len(report.results), 8)
        ollama = report.by_name("Ollama")
        self.assertEqual(ollama.status, FAIL)

    def test_generated_at_contains_utc(self):
        ok = CheckResult("x", OK, "ok")
        patches = [patch(f"harness.diagnose._{c}", return_value=ok)
                   for c in ("check_ollama", "check_memory", "check_audit_log",
                              "check_google_auth", "check_budget", "check_self_eval",
                              "check_adaptive_router", "check_tests")]
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4], patches[5], patches[6], patches[7]:
            report = run_diagnose()
        self.assertIn("UTC", report.generated_at)


# ── Test 10: diagnose_text ─────────────────────────────────────────────────────

class TestDiagnoseText(unittest.TestCase):
    """diagnose_text() formatting: icons, fix block, extra sections, overall."""

    def _all_ok_report(self) -> DiagnoseReport:
        names = ["Ollama", "Memory", "Audit", "Google", "Budget",
                 "Self-eval", "Router", "Tests"]
        return DiagnoseReport(
            results=[CheckResult(n, OK, "good") for n in names],
            generated_at="2026-06-28 12:00 UTC",
        )

    def test_contains_all_subsystem_names(self):
        with patch("harness.diagnose.run_diagnose", return_value=self._all_ok_report()):
            text = diagnose_text()
        for name in ("Ollama", "Memory", "Audit", "Google", "Budget",
                     "Self-eval", "Router", "Tests"):
            self.assertIn(name, text)

    def test_overall_line_present(self):
        with patch("harness.diagnose.run_diagnose", return_value=self._all_ok_report()):
            text = diagnose_text()
        self.assertIn("Overall:", text)
        self.assertIn("✅", text)

    def test_fix_instructions_shown_when_broken(self):
        results = [
            CheckResult("Ollama", FAIL, "Down", fix="Start Ollama first"),
        ] + [CheckResult(n, OK, "ok") for n in
             ("Memory", "Audit", "Google", "Budget", "Self-eval", "Router", "Tests")]
        report = DiagnoseReport(results=results, generated_at="2026-06-28 12:00 UTC")
        with patch("harness.diagnose.run_diagnose", return_value=report):
            text = diagnose_text()
        self.assertIn("Fix instructions:", text)
        self.assertIn("Start Ollama first", text)

    def test_no_fix_block_when_all_ok(self):
        with patch("harness.diagnose.run_diagnose", return_value=self._all_ok_report()):
            text = diagnose_text()
        self.assertNotIn("Fix instructions:", text)

    def test_extra_block_included_by_default(self):
        results = [CheckResult("Budget", OK, "ok", extra="budget detail line")]
        results += [CheckResult(n, OK, "ok") for n in
                    ("Ollama", "Memory", "Audit", "Google", "Self-eval", "Router", "Tests")]
        report = DiagnoseReport(results=results, generated_at="2026-06-28 12:00 UTC")
        with patch("harness.diagnose.run_diagnose", return_value=report):
            text = diagnose_text(include_extras=True)
        self.assertIn("budget detail line", text)

    def test_extra_block_omitted_when_disabled(self):
        results = [CheckResult("Budget", OK, "ok", extra="budget detail line")]
        results += [CheckResult(n, OK, "ok") for n in
                    ("Ollama", "Memory", "Audit", "Google", "Self-eval", "Router", "Tests")]
        report = DiagnoseReport(results=results, generated_at="2026-06-28 12:00 UTC")
        with patch("harness.diagnose.run_diagnose", return_value=report):
            text = diagnose_text(include_extras=False)
        self.assertNotIn("budget detail line", text)


if __name__ == "__main__":
    unittest.main()
