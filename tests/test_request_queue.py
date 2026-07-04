"""Request queue: park requests on total provider exhaustion, drain on recovery."""

import json
import threading
import time

import pytest

from harness import request_queue as rq

# Keep the background drain loop responsive for the whole test module —
# the production 5s poll makes threaded assertions needlessly slow.
rq.DRAIN_INTERVAL_SECONDS = 0.05


@pytest.fixture(autouse=True)
def _isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORCHESTRATOR_STATUS_PATH", str(tmp_path / "ORCHESTRATOR_STATUS.json")
    )
    monkeypatch.setattr(rq, "QUEUE_ENABLED", True)
    monkeypatch.setattr(rq, "QUEUE_MAX_DEPTH", 10)
    monkeypatch.setattr(rq, "QUEUE_MAX_WAIT_SECONDS", 5)
    rq.reset()
    yield tmp_path
    rq.reset()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_enqueue_drains_when_provider_becomes_available(monkeypatch):
    available = [False]
    monkeypatch.setattr(rq, "_any_provider_available", lambda: available[0])
    # Provider "recovers" shortly after the request is parked.
    threading.Timer(0.15, lambda: available.__setitem__(0, True)).start()

    result = rq.enqueue(lambda x: x * 2, 21)

    assert result == 42
    assert rq.queue_depth() == 0


def test_enqueue_propagates_function_exception(monkeypatch):
    monkeypatch.setattr(rq, "_any_provider_available", lambda: True)

    def _boom():
        raise ValueError("provider still broken")

    with pytest.raises(ValueError, match="provider still broken"):
        rq.enqueue(_boom)


def test_queue_full_error_at_max_depth(monkeypatch):
    monkeypatch.setattr(rq, "_any_provider_available", lambda: False)
    monkeypatch.setattr(rq, "QUEUE_MAX_DEPTH", 1)
    monkeypatch.setattr(rq, "QUEUE_MAX_WAIT_SECONDS", 1)

    errors = []

    def _blocked_caller():
        try:
            rq.enqueue(lambda: "never runs")
        except Exception as exc:  # noqa: BLE001 — captured for join
            errors.append(exc)

    t = threading.Thread(target=_blocked_caller)
    t.start()
    assert _wait_for(lambda: rq.queue_depth() == 1)

    with pytest.raises(rq.QueueFullError):
        rq.enqueue(lambda: "no room")

    t.join(timeout=5)
    assert not t.is_alive()


def test_timeout_error_after_max_wait(monkeypatch):
    monkeypatch.setattr(rq, "_any_provider_available", lambda: False)
    monkeypatch.setattr(rq, "QUEUE_MAX_WAIT_SECONDS", 0.3)

    start = time.monotonic()
    with pytest.raises(rq.QueueTimeoutError):
        rq.enqueue(lambda: "never runs")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3
    # Timed-out request is removed from the queue, not left to drain later.
    assert rq.queue_depth() == 0


def test_queue_depth_written_to_status_file(_isolated_queue, monkeypatch):
    monkeypatch.setattr(rq, "_any_provider_available", lambda: False)
    monkeypatch.setattr(rq, "QUEUE_MAX_WAIT_SECONDS", 2)
    status_path = _isolated_queue / "ORCHESTRATOR_STATUS.json"
    status_path.write_text(json.dumps({"sessions": [{"name": "x"}]}))

    def _blocked_caller():
        try:
            rq.enqueue(lambda: "parked")
        except Exception:  # noqa: BLE001 — expected timeout on teardown path
            pass

    def _snapshot_shows_depth_one():
        try:
            return json.loads(status_path.read_text()).get("queue_depth") == 1
        except Exception:  # noqa: BLE001 — mid-write reads
            return False

    t = threading.Thread(target=_blocked_caller)
    t.start()
    # enqueue appends first, then mirrors the snapshot — wait on the file itself.
    assert _wait_for(_snapshot_shows_depth_one)

    data = json.loads(status_path.read_text())
    assert data["queue_depth"] == 1
    assert data["queue_oldest_wait_seconds"] >= 0
    assert data["sessions"] == [{"name": "x"}]  # existing keys preserved

    rq.reset()
    t.join(timeout=5)
    data = json.loads(status_path.read_text())
    assert data["queue_depth"] == 0
