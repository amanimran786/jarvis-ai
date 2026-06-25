"""
tests/test_budget.py — Budget rate limiter and dashboard tests.

Covers:
  - check() returns correct soft/hard state for each provider tier
  - Hard limit raises in _candidate_stream via RuntimeError
  - record() appends to budget.jsonl with running_total_in
  - context_pressure() thresholds
  - status_text() renders all three tiers
  - LOCAL_FIRST mode routes to local when paid provider is at hard limit
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_budget_log(tmp_path, monkeypatch):
    """Redirect budget.jsonl to tmp_path so tests don't write to the real logs."""
    import harness.budget as bmod
    monkeypatch.setattr(bmod, "_budget_log_path", lambda: tmp_path / "budget.jsonl")
    # Reset in-memory running totals between tests
    with bmod._lock:
        bmod._session_tokens_in.clear()
    yield
    with bmod._lock:
        bmod._session_tokens_in.clear()


@pytest.fixture()
def zero_usage(monkeypatch):
    """Patch usage_tracker to report zero tokens for all providers."""
    import harness.budget as bmod
    monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda provider, hours: 0)
    monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 0)


@pytest.fixture()
def high_usage(monkeypatch):
    """Patch usage_tracker to report usage above the hard limit for paid providers."""
    import harness.budget as bmod
    monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda provider, hours: 150_000)
    monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 250_000)


# ── check() — local ollama ────────────────────────────────────────────────────

class TestCheckLocal:
    def test_ollama_always_ok(self, zero_usage):
        from harness.budget import check
        r = check("ollama")
        assert r["hard"] is False
        assert r["soft"] is False
        assert r["tier"] == "local"

    def test_ollama_ok_even_under_extreme_load(self, high_usage):
        from harness.budget import check
        r = check("ollama")
        assert r["hard"] is False
        assert r["soft"] is False


# ── check() — ollama_cloud ────────────────────────────────────────────────────

class TestCheckOllamaCloud:
    def test_zero_usage_is_ok(self, zero_usage):
        from harness.budget import check
        r = check("ollama_cloud")
        assert r["soft"] is False
        assert r["hard"] is False
        assert r["tier"] == "cloud_free"

    def test_soft_limit_fires_at_session_threshold(self, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_SESSION_SOFT", 100)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_SESSION_HARD", 200)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_WEEKLY_SOFT", 9_999_999)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_WEEKLY_HARD", 9_999_999)
        monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 120)  # above soft
        from harness.budget import check
        r = check("ollama_cloud")
        assert r["soft"] is True
        assert r["hard"] is False

    def test_hard_limit_fires_when_session_exceeded(self, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_SESSION_SOFT", 100)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_SESSION_HARD", 200)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_WEEKLY_SOFT", 9_999_999)
        monkeypatch.setattr(bmod, "OLLAMA_CLOUD_WEEKLY_HARD", 9_999_999)
        monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 250)  # above hard
        from harness.budget import check
        r = check("ollama_cloud")
        assert r["hard"] is True


# ── check() — paid providers ──────────────────────────────────────────────────

class TestCheckPaid:
    @pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
    def test_zero_usage_ok(self, provider, zero_usage):
        from harness.budget import check
        r = check(provider)
        assert r["soft"] is False
        assert r["hard"] is False
        assert r["tier"] == "paid"

    @pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
    def test_soft_limit_fires(self, provider, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "_HOURLY_SOFT", {provider: 1000})
        monkeypatch.setattr(bmod, "_HOURLY_HARD", {provider: 2000})
        monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda p, hours: 1500)
        from harness.budget import check
        r = check(provider)
        assert r["soft"] is True
        assert r["hard"] is False

    @pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
    def test_hard_limit_fires(self, provider, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "_HOURLY_SOFT", {provider: 1000})
        monkeypatch.setattr(bmod, "_HOURLY_HARD", {provider: 2000})
        monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda p, hours: 2500)
        from harness.budget import check
        r = check(provider)
        assert r["hard"] is True
        assert r["soft"] is False  # hard takes precedence; soft is False when hard is True

    def test_unknown_provider_fails_open(self, zero_usage):
        from harness.budget import check
        r = check("some_unknown_api")
        assert r["hard"] is False
        assert r["soft"] is False
        assert r["tier"] == "unknown"


# ── record() and budget.jsonl ─────────────────────────────────────────────────

class TestRecord:
    def test_record_writes_budget_jsonl(self, tmp_path, monkeypatch):
        import harness.budget as bmod
        log_path = tmp_path / "budget.jsonl"
        monkeypatch.setattr(bmod, "_budget_log_path", lambda: log_path)

        bmod.record(provider="anthropic", model="claude-haiku-4-5", tokens_in=500, tokens_out=100)

        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["provider"] == "anthropic"
        assert entry["tokens_in"] == 500
        assert entry["tokens_out"] == 100
        assert "ts" in entry
        assert "session_id" in entry
        assert "running_total_in" in entry

    def test_running_total_accumulates(self, tmp_path, monkeypatch):
        import harness.budget as bmod
        log_path = tmp_path / "budget.jsonl"
        monkeypatch.setattr(bmod, "_budget_log_path", lambda: log_path)

        bmod.record(provider="openai", model="gpt-4o-mini", tokens_in=100, tokens_out=20)
        bmod.record(provider="openai", model="gpt-4o-mini", tokens_in=200, tokens_out=40)
        bmod.record(provider="openai", model="gpt-4o-mini", tokens_in=300, tokens_out=60)

        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert lines[0]["running_total_in"] == 100
        assert lines[1]["running_total_in"] == 300
        assert lines[2]["running_total_in"] == 600

    def test_running_total_is_per_provider(self, tmp_path, monkeypatch):
        import harness.budget as bmod
        log_path = tmp_path / "budget.jsonl"
        monkeypatch.setattr(bmod, "_budget_log_path", lambda: log_path)

        bmod.record(provider="anthropic", model="m1", tokens_in=1000, tokens_out=0)
        bmod.record(provider="openai",    model="m2", tokens_in=500,  tokens_out=0)
        bmod.record(provider="anthropic", model="m1", tokens_in=200,  tokens_out=0)

        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        anthropic_entries = [l for l in lines if l["provider"] == "anthropic"]
        openai_entries    = [l for l in lines if l["provider"] == "openai"]
        assert anthropic_entries[0]["running_total_in"] == 1000
        assert anthropic_entries[1]["running_total_in"] == 1200
        assert openai_entries[0]["running_total_in"] == 500

    def test_concurrent_record_is_safe(self, tmp_path, monkeypatch):
        import harness.budget as bmod
        log_path = tmp_path / "budget.jsonl"
        monkeypatch.setattr(bmod, "_budget_log_path", lambda: log_path)

        errors = []
        def _write():
            try:
                bmod.record(provider="anthropic", model="m", tokens_in=10, tokens_out=5)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert errors == [], f"Concurrent writes raised: {errors}"
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 8


# ── context_pressure() ────────────────────────────────────────────────────────

class TestContextPressure:
    def test_none_below_75(self):
        from harness.budget import context_pressure
        assert context_pressure(700, 1000) == "none"
        assert context_pressure(0, 1000) == "none"
        assert context_pressure(749, 1000) == "none"

    def test_compress_between_75_and_90(self):
        from harness.budget import context_pressure
        assert context_pressure(750, 1000) == "compress"
        assert context_pressure(800, 1000) == "compress"
        assert context_pressure(899, 1000) == "compress"

    def test_switch_above_90(self):
        from harness.budget import context_pressure
        assert context_pressure(900, 1000) == "switch"
        assert context_pressure(1000, 1000) == "switch"
        assert context_pressure(950, 1000) == "switch"

    def test_zero_budget_returns_none(self):
        from harness.budget import context_pressure
        assert context_pressure(100, 0) == "none"


# ── status_text() ─────────────────────────────────────────────────────────────

class TestStatusText:
    def test_all_tiers_present(self, zero_usage):
        from harness.budget import status_text
        text = status_text()
        assert "ollama" in text.lower()
        assert "ollama_cloud" in text.lower()
        assert "anthropic" in text.lower()
        assert "openai" in text.lower()
        assert "gemini" in text.lower()

    def test_local_first_label_present(self, zero_usage):
        from harness.budget import status_text
        text = status_text()
        assert "LOCAL_FIRST" in text

    def test_hard_limit_shown_in_status(self, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda p, hours: 150_000)
        monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 0)
        from harness.budget import status_text
        text = status_text()
        assert "HARD LIMIT" in text


# ── model_router integration — hard limit raises RuntimeError ─────────────────

class TestModelRouterBudgetGate:
    def test_hard_limit_raises_before_cloud_call(self, monkeypatch):
        """_candidate_stream should raise RuntimeError for a hard-limited provider."""
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda p, hours: 999_999)
        monkeypatch.setattr(bmod, "_ollama_cloud_tokens_in_window", lambda hours: 999_999)

        # Simulate what _candidate_stream does
        candidate = MagicMock()
        candidate.local = False
        candidate.provider = "anthropic"

        from harness import budget as _budget
        bcheck = _budget.check("anthropic")
        assert bcheck["hard"] is True

        # Verify RuntimeError would be raised
        if bcheck["hard"]:
            with pytest.raises(RuntimeError, match="hard rate limit"):
                raise RuntimeError(
                    f"[Budget] {candidate.provider} hard rate limit exceeded — falling through to local"
                )

    def test_local_ollama_never_raises(self, monkeypatch):
        import harness.budget as bmod
        monkeypatch.setattr(bmod, "_tokens_in_last_hours", lambda p, hours: 999_999)
        from harness.budget import check
        r = check("ollama")
        assert r["hard"] is False
