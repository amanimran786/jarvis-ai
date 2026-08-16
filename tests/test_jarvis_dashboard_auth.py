from fastapi.testclient import TestClient
import json
import stat
import threading

import pytest

import jarvis_dashboard


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(jarvis_dashboard, "_DASHBOARD_TOKEN", "test-dashboard-token")
    return TestClient(jarvis_dashboard.app)


def test_dashboard_rejects_unauthenticated_reads(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/api/status")

    assert response.status_code == 401
    assert response.json() == {"error": "dashboard_auth_required"}


def test_dashboard_accepts_bearer_auth(monkeypatch):
    client = _client(monkeypatch)

    response = client.get(
        "/api/status",
        headers={"Authorization": "Bearer test-dashboard-token"},
    )

    assert response.status_code == 200
    assert "tasks" in response.json()


def test_dashboard_fragment_bootstrap_sets_strict_http_only_cookie(monkeypatch):
    client = _client(monkeypatch)

    bootstrap = client.get("/session-bootstrap")
    response = client.post(
        "/session-bootstrap",
        json={"token": "test-dashboard-token"},
    )

    assert bootstrap.status_code == 200
    assert "test-dashboard-token" not in bootstrap.text
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "jarvis_dashboard_token=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/").status_code == 200


def test_dashboard_rejects_wrong_bootstrap_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/session-bootstrap", json={"token": "wrong"})

    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_dashboard_rejects_non_loopback_bind(monkeypatch):
    monkeypatch.setenv("JARVIS_DASHBOARD_HOST", "0.0.0.0")

    try:
        jarvis_dashboard._validated_dashboard_host()
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("remote dashboard bind was accepted")


def test_dashboard_uses_dedicated_persisted_token(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setenv("JARVIS_API_TOKEN", "general-api-token-must-not-be-reused")
    monkeypatch.setattr(jarvis_dashboard.runtime_state, "app_data_dir", lambda: tmp_path)

    token = jarvis_dashboard._load_dashboard_token()
    token_path = tmp_path / ".jarvis_dashboard_token"

    assert token != "general-api-token-must-not-be-reused"
    assert token_path.read_text(encoding="utf-8") == token
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_dashboard_bootstrap_token_stays_in_url_fragment(monkeypatch):
    monkeypatch.setattr(jarvis_dashboard, "_DASHBOARD_TOKEN", "fragment-secret")

    url = jarvis_dashboard.dashboard_bootstrap_url("127.0.0.1")

    request_url, fragment = url.split("#", 1)
    assert "fragment-secret" not in request_url
    assert fragment == "fragment-secret"


def test_dashboard_rejects_unauthenticated_approval(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/approve/sensitive-task", follow_redirects=False)

    assert response.status_code == 401


def test_dashboard_cookie_mutation_requires_same_origin(monkeypatch):
    client = _client(monkeypatch)
    client.post("/session-bootstrap", json={"token": "test-dashboard-token"})

    response = client.post("/run-loop", follow_redirects=False)

    assert response.status_code == 403
    assert response.json() == {"error": "dashboard_origin_required"}


def test_dashboard_escapes_queue_and_log_content(monkeypatch, tmp_path):
    malicious = "<img src=x onerror=alert(1)>"
    (tmp_path / "WORK_QUEUE.json").write_text(
        json.dumps(
            [
                {
                    "session_name": malicious,
                    "task": malicious,
                    "notes": malicious,
                    "status": malicious,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "MASTER_LOG.md").write_text(malicious, encoding="utf-8")
    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    client = _client(monkeypatch)

    response = client.get(
        "/",
        headers={"Authorization": "Bearer test-dashboard-token"},
    )

    assert response.status_code == 200
    assert malicious not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


def test_dashboard_prevents_overlapping_orchestrator_loops(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_loop(**_kwargs):
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr("orchestrator_loop.run_loop", blocked_loop)
    client = _client(monkeypatch)
    headers = {"Authorization": "Bearer test-dashboard-token"}

    first = client.post("/run-loop", headers=headers, follow_redirects=False)
    assert started.wait(timeout=1)
    second = client.post("/run-loop", headers=headers, follow_redirects=False)
    release.set()

    assert first.status_code == 303
    assert second.status_code == 409
    assert second.json() == {"error": "orchestrator_loop_already_running"}


def test_dashboard_requeue_is_rejected_for_codex_control(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "WORK_QUEUE.json").write_text(
        json.dumps(
            [
                {
                    "id": "TASK-001",
                    "status": "blocked",
                    "assigned_to": "claude",
                    "assigned_at": "2026-08-15T00:00:00+00:00",
                    "blocked_reason": "dependency unavailable",
                    "blocked_at": "2026-08-15T00:01:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    client = _client(monkeypatch)

    response = client.post(
        "/requeue/0",
        headers={"Authorization": "Bearer test-dashboard-token"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "codex_assignment_required",
        "task_index": 0,
    }
    task = json.loads(
        (tmp_path / "WORK_QUEUE.json").read_text(encoding="utf-8")
    )[0]
    assert task["status"] == "blocked"
    assert task["assigned_to"] == "claude"
    assert task["blocked_reason"] == "dependency unavailable"


@pytest.mark.parametrize("active_status", ["active", "in_progress", "running"])
def test_dashboard_does_not_requeue_active_task(
    monkeypatch,
    tmp_path,
    active_status,
):
    queue_path = tmp_path / "WORK_QUEUE.json"
    queue_path.write_text(
        json.dumps(
            [
                {
                    "id": "TASK-001",
                    "status": active_status,
                    "assigned_to": "claude",
                    "lease_id": "lease-001",
                }
            ]
        ),
        encoding="utf-8",
    )
    original = queue_path.read_bytes()
    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    client = _client(monkeypatch)

    response = client.post(
        "/requeue/0",
        headers={"Authorization": "Bearer test-dashboard-token"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "codex_assignment_required",
        "task_index": 0,
    }
    assert queue_path.read_bytes() == original


def test_dashboard_rolls_back_new_approval_when_requeue_fails(monkeypatch, tmp_path):
    record = {
        "task_id": "TASK-001",
        "task_contract_sha256": "a" * 64,
        "task_spec_sha256": "b" * 64,
        "approved_at": "2026-07-12T00:00:00+00:00",
        "approved_by": "dashboard",
    }
    consumed = []
    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    monkeypatch.setattr(
        "harness.approval_workflow.record_approval",
        lambda *_args, **_kwargs: (record, True),
    )
    monkeypatch.setattr(
        "harness.approval_workflow.requeue_approved_task",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "harness.approval_workflow.consume_approval",
        lambda *_args, **_kwargs: consumed.append(True),
    )
    client = _client(monkeypatch)

    response = client.post(
        "/approve/TASK-001",
        headers={"Authorization": "Bearer test-dashboard-token"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert response.json() == {"error": "approved_task_could_not_be_requeued"}
    assert consumed == [True]


def test_dashboard_expire_session_does_not_mutate_work_queue(monkeypatch, tmp_path):
    queue_path = tmp_path / "WORK_QUEUE.json"
    queue_path.write_text(
        json.dumps(
            [{"id": "TASK-001", "status": "in_progress", "assigned_to": "session-1"}]
        ),
        encoding="utf-8",
    )
    original = queue_path.read_bytes()
    saved = []

    class FakeTracker:
        def _load(self):
            return {"sessions": [{"session_id": "session-1", "status": "active"}]}

        def _save(self, data):
            saved.append(data)

    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    monkeypatch.setattr("harness.session_tracker.SessionTracker", FakeTracker)
    client = _client(monkeypatch)

    response = client.post(
        "/expire-session/session-1",
        headers={"Authorization": "Bearer test-dashboard-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert queue_path.read_bytes() == original
    assert saved[0]["sessions"][0]["status"] == "stalled"


def test_dashboard_expire_reports_mutation_failure(monkeypatch, tmp_path):
    class FailingTracker:
        def _load(self):
            raise OSError("simulated state failure")

    monkeypatch.setattr(jarvis_dashboard, "BASE", tmp_path)
    monkeypatch.setattr("harness.session_tracker.SessionTracker", FailingTracker)
    client = _client(monkeypatch)

    response = client.post(
        "/expire-session/session-1",
        headers={"Authorization": "Bearer test-dashboard-token"},
        follow_redirects=False,
    )

    assert response.status_code == 500
    assert response.json() == {"error": "expire_session_failed"}
