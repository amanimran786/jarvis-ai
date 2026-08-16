"""
tests/test_gemini_coverage.py — GEMINI-2 coverage-gap tests.

Ten focused tests for public functions with no existing test coverage,
identified by the audit in GEMINI_TEST_GAPS.md. All tests are hermetic:
no network, no Ollama, no cloud APIs — filesystem work is isolated to
tmp_path and module state is patched via monkeypatch.
"""

from __future__ import annotations

import json

import pytest

from harness import audit
from harness import circuit_breaker as cb
from harness import request_queue as rq
from harness.agent_coordinator import (
    build_parser,
    clear_cooldown,
    set_cooldown,
)
from harness.task_contract import ContractError, TaskBudget, is_sha256_digest, normalize_task_id


# ── harness/task_contract.py ──────────────────────────────────────────────────


def test_normalize_task_id_accepts_valid_identifier():
    # Arrange
    raw = "  gemini-lane.test_01  "

    # Act
    result = normalize_task_id(raw)

    # Assert — whitespace stripped, allowed charset preserved
    assert result == "gemini-lane.test_01"


def test_normalize_task_id_rejects_invalid_values():
    # Arrange — empty, embedded space, path traversal, over-length
    bad_values = [None, "", "has space", "../etc/passwd", "x" * 129]

    # Act / Assert
    for value in bad_values:
        with pytest.raises(ContractError):
            normalize_task_id(value)


def test_is_sha256_digest_accepts_only_canonical_lowercase_hex():
    # Arrange
    good = "a" * 64
    bad = ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None]

    # Act / Assert
    assert is_sha256_digest(good) is True
    assert is_sha256_digest(f"  {good}  ") is True  # surrounding whitespace tolerated
    for value in bad:
        assert is_sha256_digest(value) is False


def test_task_budget_from_value_defaults_and_overrides():
    # Arrange / Act
    defaults = TaskBudget.from_value(None)  # non-mapping falls back to defaults
    custom = TaskBudget.from_value(
        {"max_attempts": 5, "wall_time_seconds": 60, "tool_calls": 7}
    )

    # Assert
    assert (defaults.max_attempts, defaults.wall_time_seconds, defaults.tool_calls) == (
        3,
        1800,
        40,
    )
    assert (custom.max_attempts, custom.wall_time_seconds, custom.tool_calls) == (5, 60, 7)


# ── harness/agent_coordinator.py ──────────────────────────────────────────────


def test_clear_cooldown_restores_agent_availability(tmp_path):
    # Arrange — empty queue, agent placed in cooldown
    queue_path = tmp_path / "WORK_QUEUE.json"
    state_path = tmp_path / "agent_coordination.json"
    queue_path.write_text("[]", encoding="utf-8")
    set_cooldown(
        "claude",
        seconds=600,
        reason="rate limit",
        queue_path=queue_path,
        state_path=state_path,
    )

    # Act
    result = clear_cooldown("claude", queue_path=queue_path, state_path=state_path)

    # Assert — return value and persisted record both show availability
    assert result == {"status": "available", "agent": "claude"}
    record = json.loads(state_path.read_text(encoding="utf-8"))["agents"]["claude"]
    assert record["status"] == "available"
    assert "cooldown_until" not in record
    assert "cooldown_reason" not in record


def test_build_parser_parses_claim_command():
    # Arrange
    parser = build_parser()

    # Act
    args = parser.parse_args(["claim", "--agent", "claude", "--json"])

    # Assert
    assert args.command == "claim"
    assert args.agent == "claude"
    assert args.json is True


# ── harness/audit.py ──────────────────────────────────────────────────────────


def test_list_snapshots_orders_newest_first_and_tolerates_missing_meta(tmp_path, monkeypatch):
    # Arrange — two snapshot dirs; only the older one has metadata
    snaps = tmp_path / "snapshots"
    older = snaps / "2026-01-01T00-00-00"
    newer = snaps / "2026-02-01T00-00-00"
    for d in (older, newer):
        d.mkdir(parents=True)
    (older / "snapshot_meta.json").write_text(
        json.dumps({"session_id": "s1", "status": "completed"}), encoding="utf-8"
    )
    monkeypatch.setattr(audit, "_SNAPSHOTS_DIR", snaps)

    # Act
    result = audit.list_snapshots()

    # Assert
    assert [entry["name"] for entry in result] == [newer.name, older.name]
    assert result[0]["status"] == "unknown"  # missing meta degrades gracefully
    assert result[1]["session_id"] == "s1"
    assert result[1]["status"] == "completed"


def test_restore_snapshot_round_trip_and_missing_dir(tmp_path, monkeypatch):
    # Arrange — snapshot with memory.json and nested memory/ files
    base = tmp_path / "base"
    base.mkdir()
    snap = tmp_path / "snapshots" / "2026-03-01T00-00-00"
    (snap / "memory").mkdir(parents=True)
    (snap / "memory.json").write_text('{"k": 1}', encoding="utf-8")
    (snap / "memory" / "facts.json").write_text('{"f": 2}', encoding="utf-8")
    monkeypatch.setattr(audit, "_base_dir", lambda: base)
    monkeypatch.setattr(audit, "audit_log", lambda *a, **k: None)

    # Act
    ok, message = audit.restore_snapshot(snap)
    missing_ok, missing_message = audit.restore_snapshot(tmp_path / "nope")

    # Assert
    assert ok is True and snap.name in message
    assert json.loads((base / "memory.json").read_text(encoding="utf-8")) == {"k": 1}
    assert json.loads((base / "memory" / "facts.json").read_text(encoding="utf-8")) == {"f": 2}
    assert missing_ok is False and "not found" in missing_message


# ── harness/request_queue.py ──────────────────────────────────────────────────


def test_should_queue_respects_enable_flag_and_max_depth(monkeypatch):
    # Arrange
    monkeypatch.setattr(rq, "QUEUE_MAX_DEPTH", 2)
    monkeypatch.setattr(rq, "_pending", [])

    # Act / Assert — disabled queue never parks requests
    monkeypatch.setattr(rq, "QUEUE_ENABLED", False)
    assert rq.should_queue() is False

    # Act / Assert — enabled and under depth parks; at depth refuses
    monkeypatch.setattr(rq, "QUEUE_ENABLED", True)
    assert rq.should_queue() is True
    monkeypatch.setattr(rq, "_pending", [object(), object()])
    assert rq.should_queue() is False


# ── harness/circuit_breaker.py ────────────────────────────────────────────────


def test_write_status_snapshot_records_health_and_preserves_keys(tmp_path, monkeypatch):
    # Arrange — isolated state, one recorded failure, pre-existing status keys
    status_path = tmp_path / "ORCHESTRATOR_STATUS.json"
    monkeypatch.setenv("JARVIS_CIRCUIT_BREAKER_PATH", str(tmp_path / "circuit_breaker.json"))
    monkeypatch.setenv("JARVIS_ORCHESTRATOR_STATUS_PATH", str(status_path))
    status_path.write_text(json.dumps({"queue_depth": 3}), encoding="utf-8")
    cb.reset()
    cb.record_failure("openai")

    # Act
    cb.write_status_snapshot()

    # Assert — provider health mirrored, unrelated keys untouched
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["queue_depth"] == 3
    assert status["provider_health"]["openai"]["state"] == cb.CLOSED
    assert status["provider_health"]["openai"]["failures"] == 1
    cb.reset()
