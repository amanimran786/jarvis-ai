from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import task_persistence
import task_runtime


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _task_record(task_id: str, *, status: str = "queued") -> dict:
    return {
        "id": task_id,
        "kind": "task",
        "source": "persistence_test",
        "status": status,
        "prompt": "persisted prompt",
        "effective_prompt": "persisted prompt",
        "assigned_agent_id": "chat-router",
        "created_at": "2026-04-10T00:00:00+00:00",
        "assigned_at": "2026-04-10T00:01:00+00:00" if status != "queued" else "",
        "started_at": "2026-04-10T00:02:00+00:00" if status in {"running", "streaming"} else "",
        "finished_at": "",
        "updated_at": "2026-04-10T00:02:30+00:00",
        "result": "",
        "model": "",
        "error": "",
        "interaction_id": "",
        "usage": {},
        "cancel_requested": False,
        "terse_mode": "",
        "workspace": {"ok": True, "enabled": False, "created": False, "reason": "disabled"},
        "meta": {"seeded": True},
    }


def _clear_runtime_memory_only() -> None:
    with task_runtime._LOCK:
        task_runtime._AGENTS.clear()
        task_runtime._TASKS.clear()
        task_runtime._TASK_EVENTS.clear()
        task_runtime._TASK_THREADS.clear()
        task_runtime._BOOTSTRAPPED = False


class PersistentJarvisWebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(api.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def tearDown(self) -> None:
        api._API_TOKEN = ""

    def test_trigger_webhook_requires_signature_when_secret_is_configured(self) -> None:
        payload = {"prompt": "handle this webhook trigger"}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(os.environ, {"JARVIS_WEBHOOK_SECRET": "test-secret"}, clear=False):
            response = self.client.post(
                "/webhooks/trigger",
                content=body,
                headers={"content-type": "application/json"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "signature_missing")

    def test_trigger_webhook_fails_closed_when_secret_is_not_configured(self) -> None:
        payload = {"prompt": "handle this webhook trigger"}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(
            os.environ,
            {"JARVIS_WEBHOOK_SECRET": "", "JARVIS_ALLOW_UNSIGNED_WEBHOOKS": "0"},
            clear=False,
        ):
            response = self.client.post(
                "/webhooks/trigger",
                content=body,
                headers={"content-type": "application/json"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "webhook_secret_missing")

    def test_webhook_endpoints_create_tasks_and_return_ids(self) -> None:
        generic_payload = {
            "prompt": "handle this generic webhook",
            "kind": "task",
            "meta": {"source_name": "generic"},
        }
        github_payload = {
            "action": "opened",
            "repository": {"full_name": "openai/jarvis-ai"},
            "pull_request": {"number": 17, "title": "Tighten runtime boot"},
        }
        generic_body = json.dumps(generic_payload).encode("utf-8")
        github_body = json.dumps(github_payload).encode("utf-8")

        submitted: list[dict] = []

        def fake_submit_task(prompt: str, **kwargs):
            task_id = f"task_test_{len(submitted) + 1}"
            task = {
                "id": task_id,
                "status": "queued",
                "prompt": prompt,
                "kind": kwargs.get("kind", "task"),
                "source": kwargs.get("source", "api"),
                "meta": kwargs.get("meta", {}),
            }
            submitted.append(task)
            return task

        with patch.dict(os.environ, {"JARVIS_WEBHOOK_SECRET": "test-secret"}, clear=False), \
             patch("api.task_runtime.submit_task", side_effect=fake_submit_task):
            generic_response = self.client.post(
                "/webhooks/trigger",
                content=generic_body,
                headers={
                    "content-type": "application/json",
                    "x-jarvis-signature": _sign(generic_body, "test-secret"),
                },
            )
            github_response = self.client.post(
                "/webhooks/github",
                content=github_body,
                headers={
                    "content-type": "application/json",
                    "x-github-event": "pull_request",
                    "x-hub-signature-256": _sign(github_body, "test-secret"),
                },
            )

        self.assertEqual(generic_response.status_code, 200)
        self.assertEqual(github_response.status_code, 200)
        self.assertEqual(generic_response.json()["task_id"], "task_test_1")
        self.assertEqual(github_response.json()["task_id"], "task_test_2")
        self.assertEqual(generic_response.json()["task"]["source"], "webhook")
        self.assertEqual(github_response.json()["task"]["source"], "github_webhook")
        self.assertIn("generic webhook", submitted[0]["prompt"])
        self.assertIn("GitHub webhook event", submitted[1]["prompt"])


class PersistentJarvisRuntimePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._env = patch.dict(
            os.environ,
            {"JARVIS_TASK_DB_PATH": str(Path(self._tmpdir.name) / "jarvis_tasks.sqlite3")},
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        task_persistence.reset_for_tests()
        task_runtime.reset_for_tests()

    def tearDown(self) -> None:
        task_persistence.reset_for_tests()
        _clear_runtime_memory_only()

    def test_bootstrap_rehydrates_persisted_tasks_and_events(self) -> None:
        task_id = "task_persisted_001"
        task = _task_record(task_id, status="succeeded")
        task["finished_at"] = "2026-04-10T00:03:00+00:00"
        task["updated_at"] = task["finished_at"]
        task["result"] = "done"
        task["model"] = "UnitTestModel"
        task_persistence.upsert_task(task)
        task_persistence.append_event(
            {"task_id": task_id, "type": "status", "ts": "2026-04-10T00:00:01+00:00", "status": "queued"},
            0,
        )
        task_persistence.append_event(
            {"task_id": task_id, "type": "meta", "ts": "2026-04-10T00:03:00+00:00", "status": "succeeded"},
            1,
        )

        _clear_runtime_memory_only()
        task_runtime.bootstrap()

        restored = task_runtime.get_task(task_id)
        events = task_runtime.get_task_events(task_id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["result"], "done")
        self.assertEqual([event["type"] for event in events], ["status", "meta"])

    def test_non_terminal_tasks_fail_on_restart_with_restart_marker(self) -> None:
        task_id = "task_running_001"
        task_persistence.upsert_task(_task_record(task_id, status="running"))
        task_persistence.append_event(
            {"task_id": task_id, "type": "status", "ts": "2026-04-10T00:00:01+00:00", "status": "running"},
            0,
        )

        _clear_runtime_memory_only()
        task_runtime.bootstrap()

        restored = task_runtime.get_task(task_id)
        events = task_runtime.get_task_events(task_id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["error"], "daemon_restart")
        self.assertTrue(restored["finished_at"])
        restart_events = [
            event
            for event in events
            if event.get("type") == "error" and event.get("reason") == "daemon_restart"
        ]
        self.assertEqual(len(restart_events), 1)
