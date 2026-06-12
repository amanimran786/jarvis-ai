"""
Regression: task approval/denial emit security audit entries (S2 wiring,
applied by the runtime lane per the C handoff in ~/CLAUDE_SESSIONS.md).

approve_task → APPROVAL_GRANTED, deny_task → APPROVAL_DENIED, each carrying
the task_id so the dashboard can join audit entries to tasks.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import task_runtime


_FAKE_WORKSPACE = {
    "ok": False, "enabled": False, "created": False, "reason": "",
    "repo_root": "", "worktree_path": "", "branch": "",
}


class TaskRuntimeAuditEmitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        self._env = patch.dict(
            os.environ, {"JARVIS_SECURITY_AUDIT_PATH": str(self.audit_path)}
        )
        self._env.start()
        task_runtime.reset_for_tests()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _entries(self, action: str) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [
            e for e in (json.loads(line) for line in
                        self.audit_path.read_text().splitlines() if line.strip())
            if e.get("action") == action
        ]

    def _submit_held_task(self) -> str:
        task = task_runtime.submit_task("deploy the current Jarvis build", kind="task")
        self.assertEqual(task["status"], "waiting_approval")
        return task["id"]

    def test_approve_emits_approval_granted_with_task_id(self):
        with patch("task_runtime.smart_stream",
                   return_value=(iter(["done"]), "UnitTestModel")), \
             patch("task_runtime.worktree_manager.prepare_isolated_workspace",
                   return_value=_FAKE_WORKSPACE):
            task_id = self._submit_held_task()
            task_runtime.approve_task(task_id)
            task_runtime.wait_for_task(task_id, timeout=5.0)

        entries = self._entries("approval_granted")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task_id"], task_id)
        self.assertTrue(entries[0]["summary"])

    def test_deny_emits_approval_denied_with_task_id(self):
        with patch("task_runtime.smart_stream") as stream_mock, \
             patch("task_runtime.worktree_manager.prepare_isolated_workspace",
                   return_value=_FAKE_WORKSPACE):
            task_id = self._submit_held_task()
            task_runtime.deny_task(task_id)

        stream_mock.assert_not_called()
        entries = self._entries("approval_denied")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task_id"], task_id)


if __name__ == "__main__":
    unittest.main()
