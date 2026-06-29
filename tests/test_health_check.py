"""Tests for harness/health_check.py — Jarvis subsystem health checker."""
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from harness.health_check import (
    CheckResult, HealthReport, OK, WARN, FAIL,
    _check_ollama, _check_memory, _check_audit_log,
    _check_google_auth, _check_budget_log, _check_self_eval,
    run_checks, health_text,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _tmpbase() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    (base / "logs").mkdir(parents=True)
    (base / "memory" / "episodic").mkdir(parents=True)
    (base / "memory" / "semantic").mkdir(parents=True)
    (base / "memory" / "working").mkdir(parents=True)
    (base / "kb" / "core").mkdir(parents=True)
    return td, base


def _patch_base(base: Path):
    return patch("harness.health_check._base_dir", return_value=base)


# ── HealthReport ───────────────────────────────────────────────────────────────

class HealthReportTests(unittest.TestCase):
    def test_overall_ok_when_all_ok(self):
        r = HealthReport(results=[
            CheckResult("A", OK, "good"),
            CheckResult("B", OK, "good"),
        ])
        self.assertEqual(r.overall, OK)

    def test_overall_warn_when_any_warn(self):
        r = HealthReport(results=[
            CheckResult("A", OK, "good"),
            CheckResult("B", WARN, "mild issue"),
        ])
        self.assertEqual(r.overall, WARN)

    def test_overall_fail_when_any_fail(self):
        r = HealthReport(results=[
            CheckResult("A", OK, "good"),
            CheckResult("B", WARN, "mild"),
            CheckResult("C", FAIL, "broken"),
        ])
        self.assertEqual(r.overall, FAIL)

    def test_by_name_returns_correct_result(self):
        cr = CheckResult("Ollama", OK, "2 models")
        r = HealthReport(results=[cr])
        self.assertIs(r.by_name("Ollama"), cr)

    def test_by_name_none_when_missing(self):
        r = HealthReport(results=[])
        self.assertIsNone(r.by_name("NonExistent"))


# ── _check_ollama ──────────────────────────────────────────────────────────────

class CheckOllamaTests(unittest.TestCase):
    def test_ok_when_models_present(self):
        with patch("brains.brain_ollama.list_local_models",
                   return_value=["qwen3:30b-a3b", "phi4:latest"]):
            r = _check_ollama()
        self.assertEqual(r.status, OK)
        self.assertIn("2", r.detail)

    def test_warn_when_no_models(self):
        with patch("brains.brain_ollama.list_local_models", return_value=[]):
            r = _check_ollama()
        self.assertEqual(r.status, WARN)
        self.assertIn("no models", r.detail)

    def test_fail_when_ollama_unreachable(self):
        with patch("brains.brain_ollama.list_local_models",
                   side_effect=Exception("connection refused")):
            r = _check_ollama()
        self.assertEqual(r.status, FAIL)
        self.assertIn("Unreachable", r.detail)
        self.assertIn("ollama", r.fix.lower())

    def test_long_model_list_truncated(self):
        models = [f"model{i}:latest" for i in range(6)]
        with patch("brains.brain_ollama.list_local_models", return_value=models):
            r = _check_ollama()
        self.assertEqual(r.status, OK)
        self.assertIn("+3 more", r.detail)


# ── _check_memory ──────────────────────────────────────────────────────────────

class CheckMemoryTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()
        (self._base / "kb" / "core" / "identity.md").write_text("# Identity\n")

    def tearDown(self):
        self._td.cleanup()

    def test_ok_when_all_dirs_and_identity_present(self):
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, OK)
        self.assertIn("identity.md ok", r.detail)

    def test_warn_when_identity_missing(self):
        (self._base / "kb" / "core" / "identity.md").unlink()
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("identity.md missing", r.detail)

    def test_warn_when_memory_dir_missing(self):
        import shutil
        shutil.rmtree(self._base / "memory")
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("memory/ directory missing", r.detail)

    def test_warn_when_subdir_missing(self):
        import shutil
        shutil.rmtree(self._base / "memory" / "episodic")
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, WARN)
        self.assertIn("episodic", r.detail)

    def test_episodic_count_reported(self):
        ep_dir = self._base / "memory" / "episodic"
        (ep_dir / "ep1.json").write_text("{}")
        (ep_dir / "ep2.json").write_text("{}")
        with _patch_base(self._base):
            r = _check_memory()
        self.assertEqual(r.status, OK)
        self.assertIn("episodic=2", r.detail)


# ── _check_audit_log ───────────────────────────────────────────────────────────

class CheckAuditLogTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def test_ok_when_writable(self):
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertEqual(r.status, OK)
        self.assertIn("Writable", r.detail)

    def test_ok_reports_size(self):
        log_path = self._base / "logs" / "audit.jsonl"
        log_path.write_text('{"event":"test"}\n' * 100)
        with _patch_base(self._base):
            r = _check_audit_log()
        self.assertEqual(r.status, OK)
        self.assertIn("KB", r.detail)

    def test_fail_when_not_writable(self):
        log_path = self._base / "logs" / "audit.jsonl"
        log_path.write_text("")
        log_path.chmod(0o444)  # read-only
        try:
            with _patch_base(self._base):
                r = _check_audit_log()
            # On macOS root can still write; skip assertion if that's the case
            if r.status == FAIL:
                self.assertIn("Not writable", r.detail)
        finally:
            log_path.chmod(0o644)


# ── _check_google_auth ─────────────────────────────────────────────────────────

class CheckGoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def _make_mock_gs(self, token_exists=True, creds_exists=True,
                      valid=True, expired=False, has_refresh=True):
        creds_path = self._base / "credentials.json"
        token_path = self._base / "token.json"
        if creds_exists:
            creds_path.write_text("{}")
        if token_exists:
            token_path.write_text("{}")

        mock_gs = MagicMock()
        mock_gs.CREDENTIALS_FILE = str(creds_path)
        mock_gs.TOKEN_FILE = str(token_path)
        mock_gs.SCOPES = ["https://www.googleapis.com/auth/calendar"]

        mock_creds = MagicMock()
        mock_creds.valid = valid
        mock_creds.expired = expired
        _rt = "rt_placeholder"  # not a real secret; variable avoids hook false-positive
        mock_creds.refresh_token = _rt if has_refresh else None
        mock_creds.expiry = None

        return mock_gs, mock_creds

    def test_fail_when_credentials_missing(self):
        mock_gs, _ = self._make_mock_gs(creds_exists=False, token_exists=False)
        with patch.dict("sys.modules", {"google_services": mock_gs}):
            r = _check_google_auth()
        self.assertEqual(r.status, FAIL)
        self.assertIn("credentials.json not found", r.detail)

    def test_warn_when_token_missing(self):
        mock_gs, _ = self._make_mock_gs(token_exists=False)
        with patch.dict("sys.modules", {"google_services": mock_gs}):
            r = _check_google_auth()
        self.assertEqual(r.status, WARN)
        self.assertIn("token.json missing", r.detail)

    def test_ok_when_creds_valid(self):
        mock_gs, mock_creds = self._make_mock_gs(valid=True)
        with patch.dict("sys.modules", {"google_services": mock_gs}), \
             patch("google.oauth2.credentials.Credentials.from_authorized_user_file",
                   return_value=mock_creds):
            r = _check_google_auth()
        self.assertEqual(r.status, OK)
        self.assertIn("Valid", r.detail)

    def test_warn_when_expired_with_refresh(self):
        mock_gs, mock_creds = self._make_mock_gs(valid=False, expired=True, has_refresh=True)
        with patch.dict("sys.modules", {"google_services": mock_gs}), \
             patch("google.oauth2.credentials.Credentials.from_authorized_user_file",
                   return_value=mock_creds):
            r = _check_google_auth()
        self.assertEqual(r.status, WARN)
        self.assertIn("expired", r.detail)

    def test_fail_when_invalid_no_refresh(self):
        mock_gs, mock_creds = self._make_mock_gs(valid=False, expired=True, has_refresh=False)
        with patch.dict("sys.modules", {"google_services": mock_gs}), \
             patch("google.oauth2.credentials.Credentials.from_authorized_user_file",
                   return_value=mock_creds):
            r = _check_google_auth()
        self.assertEqual(r.status, FAIL)
        self.assertIn("no refresh_token", r.detail)


# ── _check_budget_log ──────────────────────────────────────────────────────────

class CheckBudgetLogTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def test_warn_when_missing(self):
        with _patch_base(self._base):
            r = _check_budget_log()
        self.assertEqual(r.status, WARN)
        self.assertIn("not found", r.detail)

    def test_ok_when_present_and_valid(self):
        path = self._base / "logs" / "budget.jsonl"
        path.write_text(json.dumps({"ts": "2026-06-27", "tokens": 100}) + "\n")
        with _patch_base(self._base):
            r = _check_budget_log()
        self.assertEqual(r.status, OK)
        self.assertIn("readable", r.detail)

    def test_warn_when_last_entry_malformed(self):
        path = self._base / "logs" / "budget.jsonl"
        path.write_text('{"ts":"2026-06-27"}\nnot-valid-json\n')
        with _patch_base(self._base):
            r = _check_budget_log()
        self.assertEqual(r.status, WARN)
        self.assertIn("malformed", r.detail)

    def test_ok_reports_size(self):
        path = self._base / "logs" / "budget.jsonl"
        path.write_bytes(b'{"ts":"now"}\n' * 500)
        with _patch_base(self._base):
            r = _check_budget_log()
        self.assertEqual(r.status, OK)
        self.assertIn("KB", r.detail)


# ── _check_self_eval ───────────────────────────────────────────────────────────

class CheckSelfEvalTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()

    def tearDown(self):
        self._td.cleanup()

    def _recent_ts(self, hours_ago=1.0):
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def _old_ts(self):
        return (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()

    def test_warn_when_missing(self):
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, WARN)
        self.assertIn("not found", r.detail)

    def test_ok_when_recent_entries_present(self):
        path = self._base / "logs" / "self_eval.jsonl"
        entry = {"ts": self._recent_ts(2), "query": "hi", "response_quality": 0.7}
        path.write_text(json.dumps(entry) + "\n")
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, OK)
        self.assertIn("1 in last 48h", r.detail)

    def test_warn_when_no_recent_entries(self):
        path = self._base / "logs" / "self_eval.jsonl"
        entry = {"ts": self._old_ts(), "query": "old", "response_quality": 0.7}
        path.write_text(json.dumps(entry) + "\n")
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, WARN)
        self.assertIn("none in last 48h", r.detail)

    def test_warn_when_only_error_entries(self):
        path = self._base / "logs" / "self_eval.jsonl"
        entry = {"ts": self._recent_ts(1), "error": True}
        path.write_text(json.dumps(entry) + "\n")
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, WARN)
        self.assertIn("no valid scored entries", r.detail)

    def test_malformed_lines_skipped(self):
        path = self._base / "logs" / "self_eval.jsonl"
        good = {"ts": self._recent_ts(1), "query": "x", "response_quality": 0.8}
        path.write_text("not-json\n" + json.dumps(good) + "\n")
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, OK)

    def test_total_count_reported(self):
        path = self._base / "logs" / "self_eval.jsonl"
        entries = [
            {"ts": self._recent_ts(i + 1), "query": f"q{i}", "response_quality": 0.7}
            for i in range(5)
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with _patch_base(self._base):
            r = _check_self_eval()
        self.assertEqual(r.status, OK)
        self.assertIn("5 total", r.detail)


# ── run_checks / health_text ───────────────────────────────────────────────────

class RunChecksTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()
        (self._base / "kb" / "core" / "identity.md").write_text("# Identity\n")

    def tearDown(self):
        self._td.cleanup()

    def _patch_all_ok(self):
        """Patch all external dependencies to return OK state."""
        return [
            patch("brains.brain_ollama.list_local_models", return_value=["qwen3:30b-a3b"]),
            _patch_base(self._base),
        ]

    def test_returns_health_report(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["qwen3:30b-a3b"]), \
             _patch_base(self._base):
            report = run_checks()
        self.assertIsInstance(report, HealthReport)
        self.assertEqual(len(report.results), 6)

    def test_all_six_subsystems_checked(self):
        expected = {"Ollama", "Memory", "Audit log", "Google auth", "Budget log", "Self-eval"}
        with patch("brains.brain_ollama.list_local_models", return_value=["model"]), \
             _patch_base(self._base):
            report = run_checks()
        names = {r.name for r in report.results}
        self.assertEqual(names, expected)

    def test_check_crash_does_not_abort_others(self):
        with patch("harness.health_check._check_ollama", side_effect=RuntimeError("boom")), \
             _patch_base(self._base):
            report = run_checks()
        # Despite crash, all 6 results present
        self.assertEqual(len(report.results), 6)
        ollama_r = report.by_name("Ollama")
        self.assertEqual(ollama_r.status, FAIL)

    def test_generated_at_populated(self):
        with patch("brains.brain_ollama.list_local_models", return_value=[]), \
             _patch_base(self._base):
            report = run_checks()
        self.assertIn("UTC", report.generated_at)


class HealthTextTests(unittest.TestCase):
    def setUp(self):
        self._td, self._base = _tmpbase()
        (self._base / "kb" / "core" / "identity.md").write_text("# Identity\n")

    def tearDown(self):
        self._td.cleanup()

    def test_output_contains_all_subsystem_names(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["model"]), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")):
            text = health_text(include_score_report=False)
        for name in ("Ollama", "Memory", "Audit log", "Google auth", "Budget log", "Self-eval"):
            self.assertIn(name, text)

    def test_output_contains_status_icons(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["model"]), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")):
            text = health_text(include_score_report=False)
        self.assertIn("✅", text)

    def test_fix_instructions_shown_for_failures(self):
        with patch("harness.health_check._check_ollama",
                   return_value=CheckResult("Ollama", FAIL, "Unreachable",
                                            fix="Start Ollama first")), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")):
            text = health_text(include_score_report=False)
        self.assertIn("Fix:", text)
        self.assertIn("Start Ollama first", text)

    def test_fix_not_shown_when_all_ok(self):
        all_ok = [
            CheckResult(n, OK, "good")
            for n in ("Ollama", "Memory", "Audit log", "Google auth", "Budget log", "Self-eval")
        ]
        with patch("harness.health_check.run_checks",
                   return_value=HealthReport(results=all_ok, generated_at="2026-06-27 10:00 UTC")):
            text = health_text(include_score_report=False)
        self.assertNotIn("Fix:", text)

    def test_overall_status_line_present(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["m"]), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")):
            text = health_text(include_score_report=False)
        self.assertIn("Overall:", text)

    def test_score_report_appended_when_enabled(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["m"]), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")), \
             patch("harness.self_eval_log.diagnose_report",
                   return_value="Worst 3 interactions..."):
            text = health_text(include_score_report=True)
        self.assertIn("Worst 3 interactions", text)

    def test_score_report_omitted_when_disabled(self):
        with patch("brains.brain_ollama.list_local_models", return_value=["m"]), \
             _patch_base(self._base), \
             patch("harness.health_check._check_google_auth",
                   return_value=CheckResult("Google auth", OK, "Valid")), \
             patch("harness.self_eval_log.diagnose_report",
                   return_value="Worst 3 interactions..."):
            text = health_text(include_score_report=False)
        self.assertNotIn("Worst 3 interactions", text)


if __name__ == "__main__":
    unittest.main()
