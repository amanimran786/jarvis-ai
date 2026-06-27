from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


TRAY_PATH = Path(__file__).resolve().parents[1] / "ui" / "tray.py"
SPEC = importlib.util.spec_from_file_location("jarvis_tray", TRAY_PATH)
assert SPEC and SPEC.loader
tray = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tray
SPEC.loader.exec_module(tray)


def test_snapshot_reports_running_and_latest_task():
    snapshot = tray.snapshot_from_payload({
        "sessions": [
            {
                "name": "older",
                "status": "idle",
                "last_active": "2026-06-26T10:00:00Z",
                "current_task": "Old task",
            },
            {
                "name": "worker",
                "status": "active",
                "last_active": "2026-06-26T11:00:00Z",
                "current_task": "Run tests",
            },
        ]
    })

    assert snapshot.state == "running"
    assert snapshot.task == "Run tests"
    assert snapshot.detail == "worker: Run tests"


def test_snapshot_prioritizes_error_state():
    snapshot = tray.snapshot_from_payload({
        "sessions": [
            {"status": "active"},
            {"status": "failed", "current_task": "Build app"},
        ]
    })

    assert snapshot.state == "error"


def test_snapshot_reports_idle_without_active_sessions():
    snapshot = tray.snapshot_from_payload({"sessions": [{"status": "idle"}]})

    assert snapshot.state == "idle"


def test_load_snapshot_reports_invalid_json_as_error(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text("not json", encoding="utf-8")

    snapshot = tray.load_snapshot(status_path)

    assert snapshot.state == "error"
    assert snapshot.task == "Status unavailable"
