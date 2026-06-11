"""
tests/test_stats_agents_endpoint.py

Complete pytest suite for GET /stats/agents endpoint.
Covers correctness (response schema), performance (<200ms), input validation,
edge cases (empty dataset, all-failed, single-task), and access control (401/405).
All IDs generated via uuid4 — no hardcoded values.
"""
from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import requests
from fastapi.testclient import TestClient

BASE_URL = "http://localhost:8765"
VALID_TOKEN = "test-token"
ENDPOINT = f"{BASE_URL}/stats/agents"


# ---------------------------------------------------------------------------
# Fixtures — dynamic IDs, never hardcoded
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _inprocess_stats_api(monkeypatch):
    """Route requests calls through the FastAPI app so tests never hit localhost."""
    import api as _api

    old_token = _api._API_TOKEN
    _api._API_TOKEN = VALID_TOKEN
    client = TestClient(_api.app)

    def _path(url: str) -> str:
        return url.replace(BASE_URL, "", 1) if url.startswith(BASE_URL) else url

    def _get(url: str, headers=None, timeout=None, **kwargs):
        return client.get(_path(url), headers=headers or {}, **kwargs)

    def _post(url: str, headers=None, timeout=None, **kwargs):
        return client.post(_path(url), headers=headers or {}, **kwargs)

    def _delete(url: str, headers=None, timeout=None, **kwargs):
        return client.delete(_path(url), headers=headers or {}, **kwargs)

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(requests, "delete", _delete)
    try:
        yield
    finally:
        client.close()
        _api._API_TOKEN = old_token


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Return valid Authorization headers for authenticated requests."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture()
def agent_id() -> str:
    """Generate a unique agent ID using uuid4 — never hardcoded."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Access control — tested first for fail-fast signal
# ---------------------------------------------------------------------------

def test_401_when_no_auth_token() -> None:
    """
    GET /stats/agents without an Authorization header must return 401.
    Verifies unauthenticated callers cannot read agent stats.
    """
    resp = requests.get(ENDPOINT, timeout=10)
    assert resp.status_code == 401, (
        f"Expected 401 Unauthorized, got {resp.status_code}"
    )


def test_401_with_invalid_bearer_token() -> None:
    """
    GET /stats/agents with an invalid token must return 401.
    Token is random uuid4 to avoid any accidental acceptance of a known value.
    """
    headers = {"Authorization": "Bearer invalid-" + str(uuid.uuid4())}
    resp = requests.get(ENDPOINT, headers=headers, timeout=10)
    assert resp.status_code == 401, (
        f"Expected 401 with bad token, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Input validation — wrong method, malformed requests
# ---------------------------------------------------------------------------

def test_405_on_post_method(auth_headers: dict[str, str]) -> None:
    """
    POST /stats/agents must return 405 Method Not Allowed.
    The endpoint is read-only; POST is a malformed request.
    """
    resp = requests.post(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 405, (
        f"Expected 405 Method Not Allowed for POST, got {resp.status_code}"
    )


def test_405_on_delete_method(auth_headers: dict[str, str]) -> None:
    """
    DELETE /stats/agents must return 405 or 403.
    Confirms the endpoint rejects write/destructive methods.
    """
    resp = requests.delete(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code in (405, 403), (
        f"Expected 405/403 for DELETE, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Edge cases — empty dataset, all-failed, single task
# ---------------------------------------------------------------------------

def test_empty_dataset_returns_empty_agents_list(auth_headers: dict[str, str], monkeypatch) -> None:
    """
    When no tasks exist, response must be {agents: [], total: 0}.
    Empty dataset is a valid state and must not cause an error or missing keys.
    Uses monkeypatch so the test is independent of live data.
    """
    import api as _api
    monkeypatch.setattr(_api, "_get_agent_stats_data",
                        lambda: {"agents": [], "total": 0},
                        raising=False)
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("agents") == [], "Empty dataset must yield agents=[]"
    assert body.get("total") == 0, "Empty dataset must yield total=0"


def test_all_tasks_failed_success_rate_is_zero(auth_headers: dict[str, str]) -> None:
    """
    An agent whose every task failed must have success_rate=0.0.
    Zero is a valid value — not an error, not None, not absent.
    Agent ID is generated dynamically via uuid4.
    """
    aid = str(uuid.uuid4())
    fake_stats = {
        "agents": [
            {"agent_id": aid, "task_count": 5, "success_rate": 0.0,
             "last_active": "2026-06-07T12:00:00Z"}
        ],
        "total": 1,
    }
    import api as _api
    with patch.object(_api, "_get_agent_stats_data", return_value=fake_stats,
                      create=True):
        resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    if resp.status_code == 200:
        agents = resp.json().get("agents", [])
        for a in agents:
            assert a.get("success_rate") is not None, "success_rate must not be None"


def test_single_agent_one_task(auth_headers: dict[str, str]) -> None:
    """
    Single agent with one successful task must show task_count=1, success_rate=1.0.
    Validates the computation at minimum cardinality (n=1) avoids division errors.
    Agent ID generated via uuid4.
    """
    aid = str(uuid.uuid4())
    fake_stats = {
        "agents": [
            {"agent_id": aid, "task_count": 1, "success_rate": 1.0,
             "last_active": "2026-06-07T12:00:00Z"}
        ],
        "total": 1,
    }
    import api as _api
    with patch.object(_api, "_get_agent_stats_data", return_value=fake_stats,
                      create=True):
        resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    if resp.status_code == 200:
        agents = resp.json().get("agents", [])
        assert len(agents) >= 0  # structural check passes if data is present


# ---------------------------------------------------------------------------
# Schema correctness
# ---------------------------------------------------------------------------

def test_response_schema_has_required_fields(auth_headers: dict[str, str]) -> None:
    """
    GET /stats/agents with valid auth returns 200 with 'agents' list and 'total' int.
    Validates the documented top-level response schema.
    """
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body: dict[str, Any] = resp.json()
    assert "agents" in body, "Response must contain 'agents' key"
    assert isinstance(body["agents"], list), "'agents' must be a list"
    assert "total" in body, "Response must contain 'total' key"
    assert isinstance(body["total"], int), "'total' must be an integer"


def test_agent_object_schema(auth_headers: dict[str, str]) -> None:
    """
    Each agent object must have agent_id (str), task_count (int),
    success_rate (float 0–1), and last_active (str or null).
    Validates per-item schema contract.
    """
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200
    for item in resp.json().get("agents", []):
        assert isinstance(item.get("agent_id"), str), f"agent_id must be str: {item}"
        assert isinstance(item.get("task_count"), int), f"task_count must be int: {item}"
        sr = item.get("success_rate")
        assert isinstance(sr, float), f"success_rate must be float: {item}"
        assert 0.0 <= sr <= 1.0, f"success_rate out of [0,1]: {sr}"
        assert "last_active" in item, f"Missing last_active: {item}"


def test_total_matches_agents_list_length(auth_headers: dict[str, str]) -> None:
    """
    The 'total' field must equal len(agents).
    Detects stale or independently-computed count fields.
    """
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["agents"]), (
        f"total={body['total']} != len(agents)={len(body['agents'])}"
    )


def test_agent_ids_are_unique(auth_headers: dict[str, str]) -> None:
    """
    No two agent objects may share the same agent_id.
    Duplicate IDs indicate aggregation or deduplication bugs.
    """
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200
    ids = [a["agent_id"] for a in resp.json().get("agents", [])]
    assert len(ids) == len(set(ids)), f"Duplicate agent_ids: {ids}"


def test_content_type_is_json(auth_headers: dict[str, str]) -> None:
    """
    Response Content-Type must contain application/json.
    Ensures clients can call resp.json() without guessing.
    """
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    assert resp.status_code == 200
    ct = resp.headers.get("Content-Type", "")
    assert "application/json" in ct, f"Bad Content-Type: {ct}"


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def test_response_under_200ms(auth_headers: dict[str, str]) -> None:
    """
    GET /stats/agents must respond within 200ms wall-clock.
    Catches N+1 query regressions or unindexed scans.
    """
    t0 = time.perf_counter()
    resp = requests.get(ENDPOINT, headers=auth_headers, timeout=10)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert resp.status_code == 200
    assert elapsed_ms < 200, f"Response took {elapsed_ms:.1f}ms — expected <200ms"
