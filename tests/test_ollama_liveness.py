"""Fail-fast Ollama liveness cache in brains.brain_ollama."""

from unittest.mock import patch

import pytest

from brains import brain_ollama as bo


@pytest.fixture(autouse=True)
def _liveness_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_OLLAMA_LIVENESS_DISABLED", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    bo._ollama_liveness.update({"ok": True, "checked_at": 0.0})
    yield
    bo._ollama_liveness.update({"ok": True, "checked_at": 0.0})


def _force_probe(ok: bool):
    """Patch the HTTP layer so the probe returns `ok` without touching the network."""
    if bo.httpx is not None:
        response = type("R", (), {"status_code": 200 if ok else 503})()
        if ok:
            return patch.object(bo.httpx, "get", return_value=response)
        return patch.object(bo.httpx, "get", side_effect=ConnectionError("refused"))
    raise RuntimeError("httpx unavailable in test environment")


def test_dead_ollama_marks_unavailable_and_caches():
    with _force_probe(False) as mock_get:
        assert bo._check_ollama_liveness() is False
        assert bo._check_ollama_liveness() is False  # cached — no second probe
        assert mock_get.call_count == 1


def test_cache_expires_after_ttl():
    fake_now = [1000.0]
    with patch.object(bo.time, "time", side_effect=lambda: fake_now[0]), _force_probe(False) as mock_get:
        bo._check_ollama_liveness()
        fake_now[0] += bo.LIVENESS_TTL + 1
        bo._check_ollama_liveness()
        assert mock_get.call_count == 2


def test_recovery_logs_info(caplog):
    import logging

    with _force_probe(False):
        bo._check_ollama_liveness()
    bo._ollama_liveness["checked_at"] = 0.0  # expire cache
    with _force_probe(True), caplog.at_level(logging.INFO, logger=bo.log.name):
        assert bo._check_ollama_liveness() is True
    assert any("Ollama recovered" in r.message for r in caplog.records)


def test_ask_local_stream_raises_immediately_when_dead():
    with _force_probe(False):
        with pytest.raises(bo.OllamaUnavailableError):
            list(bo.ask_local_stream("hello", raise_on_error=True))


def test_ask_local_stream_yields_friendly_error_without_raise():
    with _force_probe(False):
        chunks = list(bo.ask_local_stream("hello", raise_on_error=False))
    assert chunks and "isn't responding" in chunks[0]


def test_ask_local_structured_returns_empty_without_raise():
    with _force_probe(False):
        assert bo.ask_local_structured("q", {"type": "object"}, raise_on_error=False) == ""


def test_disabled_env_skips_probe(monkeypatch):
    monkeypatch.setenv("JARVIS_OLLAMA_LIVENESS_DISABLED", "1")
    with _force_probe(False) as mock_get:
        assert bo._check_ollama_liveness() is True
        assert mock_get.call_count == 0


def test_unavailable_error_is_connection_error():
    assert issubclass(bo.OllamaUnavailableError, ConnectionError)
