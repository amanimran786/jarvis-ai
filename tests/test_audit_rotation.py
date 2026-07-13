"""
Regression tests for audit_errors.log rotation and error-count alerting.

Covers:
1. rotate_audit_errors_log() rotates the file when it exceeds max_bytes
2. rotate_audit_errors_log() is a no-op when the file is under the threshold
3. get_audit_error_count() counts lines correctly
4. get_audit_error_count() ignores entries older than since_hours
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness.audit as audit_mod
from harness.audit import get_audit_error_count, rotate_audit_errors_log


@pytest.fixture
def errors_log(tmp_path, monkeypatch):
    """Redirect audit_errors.log to a temp dir."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(audit_mod, "_LOGS_DIR", log_dir)
    return log_dir / "audit_errors.log"


def _write_line(path: Path, ts: datetime, msg: str = "boom") -> None:
    with open(path, "a") as f:
        f.write(f"{ts.isoformat()} audit_log('evt') failed: RuntimeError: {msg}\n")


class TestRotation:
    def test_rotation_triggers_when_file_too_large(self, errors_log):
        errors_log.write_text("x" * 1000 + "\n")
        rotate_audit_errors_log(max_bytes=500, backup_count=3)
        backup = errors_log.with_name(errors_log.name + ".1")
        assert backup.exists()
        assert not errors_log.exists()
        assert backup.read_text() == "x" * 1000 + "\n"

    def test_no_rotation_when_under_threshold(self, errors_log):
        errors_log.write_text("small\n")
        rotate_audit_errors_log(max_bytes=500_000, backup_count=3)
        backup = errors_log.with_name(errors_log.name + ".1")
        assert errors_log.exists()
        assert not backup.exists()

    def test_no_rotation_when_file_missing(self, errors_log):
        rotate_audit_errors_log(max_bytes=10, backup_count=3)
        assert not errors_log.exists()

    def test_rotation_shifts_existing_backups(self, errors_log):
        errors_log.with_name(errors_log.name + ".1").write_text("old-1\n")
        errors_log.with_name(errors_log.name + ".2").write_text("old-2\n")
        errors_log.write_text("x" * 1000 + "\n")
        rotate_audit_errors_log(max_bytes=500, backup_count=3)
        assert errors_log.with_name(errors_log.name + ".1").read_text() == "x" * 1000 + "\n"
        assert errors_log.with_name(errors_log.name + ".2").read_text() == "old-1\n"
        assert errors_log.with_name(errors_log.name + ".3").read_text() == "old-2\n"

    def test_rotation_discards_beyond_backup_count(self, errors_log):
        errors_log.with_name(errors_log.name + ".1").write_text("old-1\n")
        errors_log.with_name(errors_log.name + ".2").write_text("old-2\n")
        errors_log.with_name(errors_log.name + ".3").write_text("old-3-should-be-dropped\n")
        errors_log.write_text("x" * 1000 + "\n")
        rotate_audit_errors_log(max_bytes=500, backup_count=3)
        assert errors_log.with_name(errors_log.name + ".3").read_text() == "old-2\n"


class TestErrorCount:
    def test_count_returns_zero_when_missing(self, errors_log):
        assert get_audit_error_count() == 0

    def test_count_returns_correct_value(self, errors_log):
        now = datetime.now(timezone.utc)
        _write_line(errors_log, now)
        _write_line(errors_log, now - timedelta(minutes=5))
        _write_line(errors_log, now - timedelta(hours=1))
        assert get_audit_error_count(since_hours=24) == 3

    def test_count_ignores_old_entries(self, errors_log):
        now = datetime.now(timezone.utc)
        _write_line(errors_log, now)
        _write_line(errors_log, now - timedelta(hours=25))
        _write_line(errors_log, now - timedelta(days=3))
        assert get_audit_error_count(since_hours=24) == 1

    def test_count_respects_custom_window(self, errors_log):
        now = datetime.now(timezone.utc)
        _write_line(errors_log, now - timedelta(hours=2))
        _write_line(errors_log, now - timedelta(hours=10))
        assert get_audit_error_count(since_hours=1) == 0
        assert get_audit_error_count(since_hours=6) == 1
        assert get_audit_error_count(since_hours=24) == 2

    def test_count_skips_malformed_lines(self, errors_log):
        now = datetime.now(timezone.utc)
        _write_line(errors_log, now)
        with open(errors_log, "a") as f:
            f.write("not a valid timestamped line\n")
        assert get_audit_error_count(since_hours=24) == 1
