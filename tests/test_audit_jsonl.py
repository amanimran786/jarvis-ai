"""
Regression tests for harness/audit.py.

Covers:
1. query_received and route_decision event types are written by router.py
2. run_id threading: set_run_id() causes run_id to appear in audit records
3. Clearing run_id prevents leakage across tasks
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness.audit as audit_mod
from harness.audit import audit_log, set_run_id, get_run_id


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_run_id():
    """Ensure run_id is cleared before/after each test."""
    set_run_id("")
    yield
    set_run_id("")


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    """Redirect audit output to a temp file."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(audit_mod, "_LOGS_DIR", log_dir)
    monkeypatch.setattr(audit_mod, "_ROTATION_CHECKED", False)
    return log_dir / "audit.jsonl"


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


# ── run_id threading ───────────────────────────────────────────────────────────

class TestRunIdThreading:
    def test_no_run_id_by_default(self, audit_path):
        audit_log("test_event", value="x")
        records = _read_records(audit_path)
        assert len(records) == 1
        assert "run_id" not in records[0]

    def test_set_run_id_appears_in_record(self, audit_path):
        set_run_id("run_abc123")
        audit_log("test_event", value="x")
        records = _read_records(audit_path)
        assert records[0]["run_id"] == "run_abc123"

    def test_cleared_run_id_not_in_record(self, audit_path):
        set_run_id("run_abc123")
        set_run_id("")  # clear
        audit_log("test_event", value="x")
        records = _read_records(audit_path)
        assert "run_id" not in records[0]

    def test_get_run_id_reflects_current(self):
        assert get_run_id() == ""
        set_run_id("run_xyz")
        assert get_run_id() == "run_xyz"
        set_run_id("")
        assert get_run_id() == ""

    def test_run_id_is_thread_local(self, audit_path):
        """run_id set in one thread must not appear in records from another thread."""
        results = {}

        def thread_a():
            set_run_id("run_from_a")
            import time; time.sleep(0.05)   # let thread_b start
            audit_log("event_a")

        def thread_b():
            # No set_run_id call — should have empty run_id
            import time; time.sleep(0.01)
            audit_log("event_b")

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start(); tb.start()
        ta.join(); tb.join()

        records = _read_records(audit_path)
        assert len(records) == 2
        event_a = next(r for r in records if r["event_type"] == "event_a")
        event_b = next(r for r in records if r["event_type"] == "event_b")
        assert event_a.get("run_id") == "run_from_a"
        assert "run_id" not in event_b

    def test_multiple_events_same_run_id(self, audit_path):
        set_run_id("run_multi")
        audit_log("step_start", step=1)
        audit_log("step_end", step=1, ok=True)
        records = _read_records(audit_path)
        assert all(r["run_id"] == "run_multi" for r in records)

    def test_run_id_survives_exception_in_handler(self, audit_path):
        """run_id context must not be corrupted if an audit write fails."""
        set_run_id("run_resilient")
        # Simulate a partial write failure then a successful one
        with patch.object(audit_mod, "_LOGS_DIR", None):
            audit_mod._LOGS_DIR = None
            # Force a path error — audit_log must not raise
            try:
                audit_log("test_fail")
            except Exception:
                pytest.fail("audit_log raised unexpectedly")
        # run_id context must still be intact
        assert get_run_id() == "run_resilient"


# ── query_received and route_decision events ──────────────────────────────────

class TestAuditEventTypes:
    def test_query_received_event_written(self, audit_path):
        """audit_log("query_received") produces a valid record."""
        audit_log("query_received", query="what time is it")
        records = _read_records(audit_path)
        assert len(records) == 1
        r = records[0]
        assert r["event_type"] == "query_received"
        assert r["payload"]["query"] == "what time is it"
        assert "session_id" in r
        assert "ts" in r

    def test_route_decision_event_written(self, audit_path):
        """audit_log("route_decision") produces a valid record."""
        audit_log("route_decision", tool="search", confidence=0.9)
        records = _read_records(audit_path)
        assert len(records) == 1
        r = records[0]
        assert r["event_type"] == "route_decision"
        assert r["payload"]["tool"] == "search"
        assert r["payload"]["confidence"] == 0.9

    def test_query_received_with_run_id(self, audit_path):
        """query_received event carries run_id when one is active."""
        set_run_id("run_task_abc")
        audit_log("query_received", query="hello")
        records = _read_records(audit_path)
        assert records[0]["run_id"] == "run_task_abc"

    def test_route_decision_with_run_id(self, audit_path):
        """route_decision event carries run_id when one is active."""
        set_run_id("run_task_def")
        audit_log("route_decision", tool="chat", confidence=None)
        records = _read_records(audit_path)
        assert records[0]["run_id"] == "run_task_def"

    def test_router_calls_query_received(self, audit_path):
        """Verify router.py imports audit_log and uses it for query_received."""
        # This is a structural check — we verify the call site exists rather
        # than importing router.py (which has heavy deps).
        router_src = Path(__file__).resolve().parents[1] / "router.py"
        content = router_src.read_text()
        assert 'audit_log("query_received"' in content, (
            "router.py must call audit_log(\"query_received\", ...) "
            "(commit b4a0fa9 wired this — check it was not reverted)"
        )
        assert 'audit_log("route_decision"' in content, (
            "router.py must call audit_log(\"route_decision\", ...) "
            "(commit b4a0fa9 wired this — check it was not reverted)"
        )

    def test_audit_log_path_is_audit_jsonl(self):
        """audit.jsonl is in the logs/ directory at the repo root."""
        path = audit_mod._audit_log_path()
        assert path.name == "audit.jsonl"
        assert path.parent.name == "logs"
