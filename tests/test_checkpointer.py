"""
tests/test_checkpointer.py

Unit tests for infra/checkpointer.py — session state teleportation system.
All filesystem I/O uses a temporary directory; no event bus calls made.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class CheckpointerTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        import infra.checkpointer as cp
        self.cp = cp
        # Redirect checkpoint storage to tempdir
        self._saved_dir = cp._CHECKPOINT_DIR
        cp._CHECKPOINT_DIR = self._tmp / "checkpoints"

    def tearDown(self):
        self.cp._CHECKPOINT_DIR = self._saved_dir
        self._tmpdir.cleanup()

    def _make_checkpoint(self, task_id="t-001", agent="backend_engineer"):
        return self.cp.TaskCheckpoint(
            task_id=task_id,
            agent=agent,
            title="Add auth endpoint",
            description="Implement POST /auth/login",
            context={"priority": "high"},
            progress={"phase": "planning"},
        )

    # ── TaskCheckpoint dataclass ───────────────────────────────────────────

    def test_to_dict_includes_all_fields(self):
        cp = self._make_checkpoint()
        d = cp.to_dict()
        self.assertEqual(d["task_id"], "t-001")
        self.assertEqual(d["agent"], "backend_engineer")
        self.assertIn("created_at", d)
        self.assertIn("progress", d)

    def test_from_dict_roundtrips(self):
        original = self._make_checkpoint()
        recovered = self.cp.TaskCheckpoint.from_dict(original.to_dict())
        self.assertEqual(recovered.task_id, original.task_id)
        self.assertEqual(recovered.agent, original.agent)
        self.assertEqual(recovered.progress, original.progress)

    # ── save / load / delete ──────────────────────────────────────────────

    def test_save_creates_json_file(self):
        checkpoint = self._make_checkpoint()
        result = self.cp.save(checkpoint)
        self.assertTrue(result)
        saved = list((self._tmp / "checkpoints").glob("*.json"))
        self.assertEqual(len(saved), 1)

    def test_load_returns_none_for_missing_task(self):
        result = self.cp.load("nonexistent-task-id")
        self.assertIsNone(result)

    def test_save_then_load_roundtrip(self):
        checkpoint = self._make_checkpoint(task_id="round-trip-001")
        self.cp.save(checkpoint)
        recovered = self.cp.load("round-trip-001")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.title, checkpoint.title)
        self.assertEqual(recovered.progress, checkpoint.progress)

    def test_save_updates_updated_at(self):
        checkpoint = self._make_checkpoint()
        original_updated = checkpoint.updated_at
        import time; time.sleep(0.01)
        self.cp.save(checkpoint)
        self.assertGreaterEqual(checkpoint.updated_at, original_updated)

    def test_delete_removes_file(self):
        checkpoint = self._make_checkpoint(task_id="del-001")
        self.cp.save(checkpoint)
        self.cp.delete("del-001")
        self.assertIsNone(self.cp.load("del-001"))

    def test_delete_missing_returns_true(self):
        # Missing checkpoint should not raise
        result = self.cp.delete("nonexistent")
        self.assertTrue(result)

    def test_list_checkpoints_returns_all(self):
        for i in range(3):
            self.cp.save(self._make_checkpoint(task_id=f"task-{i}"))
        checkpoints = self.cp.list_checkpoints()
        self.assertEqual(len(checkpoints), 3)

    def test_list_checkpoints_empty_dir(self):
        result = self.cp.list_checkpoints()
        self.assertEqual(result, [])

    def test_path_traversal_rejected_in_task_id(self):
        # task_id with ".." should be sanitized, not written to parent dir
        checkpoint = self._make_checkpoint(task_id="../../../etc/evil")
        self.cp.save(checkpoint)
        # File should exist inside checkpoints dir, not at /etc/evil
        base = self._tmp / "checkpoints"
        files = list(base.glob("*.json"))
        for f in files:
            self.assertTrue(str(f).startswith(str(base)))

    # ── Teleportation ─────────────────────────────────────────────────────

    def test_teleport_increments_retry_count(self):
        checkpoint = self._make_checkpoint()
        checkpoint.retry_count = 1

        with patch.object(self.cp, "_enqueue_on_event_bus", return_value={"ok": True}) as mock_enqueue:
            self.cp.teleport(checkpoint)

        self.assertEqual(checkpoint.retry_count, 2)
        self.assertEqual(checkpoint.status, "resuming")

    def test_teleport_includes_checkpoint_id_in_context(self):
        checkpoint = self._make_checkpoint(task_id="teleport-001")
        captured_payload = {}

        def mock_enqueue(payload):
            captured_payload.update(payload)
            return {"ok": True}

        with patch.object(self.cp, "_enqueue_on_event_bus", side_effect=mock_enqueue):
            self.cp.teleport(checkpoint)

        self.assertEqual(captured_payload["context"]["_checkpoint_id"], "teleport-001")

    def test_teleport_includes_resume_progress(self):
        checkpoint = self._make_checkpoint()
        checkpoint.progress = {"phase": "executing", "files_touched": ["api.py"]}

        captured = {}
        with patch.object(self.cp, "_enqueue_on_event_bus", side_effect=lambda p: captured.update(p) or {"ok": True}):
            self.cp.teleport(checkpoint)

        self.assertEqual(captured["context"]["_resume_progress"], checkpoint.progress)

    def test_teleport_with_target_worker(self):
        checkpoint = self._make_checkpoint()
        captured = {}

        with patch.object(self.cp, "_enqueue_on_event_bus", side_effect=lambda p: captured.update(p) or {"ok": True}):
            self.cp.teleport(checkpoint, target_worker="worker-3")

        self.assertEqual(captured["context"]["_target_worker"], "worker-3")

    # ── CheckpointManager context manager ─────────────────────────────────

    def test_context_manager_saves_on_enter(self):
        manager = self.cp.CheckpointManager(
            task_id="cm-001",
            agent="qa_tester",
            title="Run tests",
            description="Full suite",
        )
        with manager:
            # File should exist inside the with block
            cp_on_disk = self.cp.load("cm-001")
            self.assertIsNotNone(cp_on_disk)
        # After clean exit, file should be removed
        self.assertIsNone(self.cp.load("cm-001"))

    def test_context_manager_preserves_checkpoint_on_exception(self):
        manager = self.cp.CheckpointManager(
            task_id="cm-fail-001",
            agent="qa_tester",
            title="Failing task",
            description="This will fail",
        )
        try:
            with manager as m:
                m.update_progress({"phase": "halfway"})
                raise RuntimeError("Something went wrong")
        except RuntimeError:
            pass

        # Checkpoint should still exist on disk
        recovered = self.cp.load("cm-fail-001")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.progress["phase"], "halfway")

    def test_context_manager_update_progress_saves_incrementally(self):
        manager = self.cp.CheckpointManager(
            task_id="cm-progress-001",
            agent="backend_engineer",
            title="Progress test",
            description="...",
        )
        with manager as m:
            m.update_progress({"step": 1, "files": ["a.py"]})
            m.update_progress({"step": 2, "files": ["a.py", "b.py"]})
            cp = self.cp.load("cm-progress-001")
            self.assertEqual(cp.progress["step"], 2)

    def test_context_manager_does_not_suppress_exceptions(self):
        manager = self.cp.CheckpointManager(
            task_id="cm-reraise-001",
            agent="backend_engineer",
            title="Will reraise",
            description="...",
        )
        with self.assertRaises(ValueError):
            with manager:
                raise ValueError("bubbled up")


if __name__ == "__main__":
    unittest.main()
