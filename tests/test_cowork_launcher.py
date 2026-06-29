"""
tests/test_cowork_launcher.py — Tests for the Cowork launcher bridge and loop monitor.

Covers (15 tests total):
  • harness/cowork_launcher.py   — process_launch_queue (8 tests)
  • harness/loop_monitor.py      — status_text (6 tests)
  • router.py /status wiring     — _is_status_command / _status_command_reply (1 test)
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ── Ensure repo root is on path ───────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from harness.cowork_launcher import process_launch_queue
from harness.loop_monitor import (
    _find_stalled,
    _launch_queue_counts,
    _read_log_tail,
    _work_queue_counts,
    status_text,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _pending_entry(task_id: str = "TASK-001", **overrides) -> dict:
    attempt_id = overrides.pop("attempt_id", f"attempt-{task_id.lower()}")
    task_spec = {
        "id": task_id,
        "title": f"Implement {task_id}",
        "goal": f"Implement {task_id}",
        "description": f"Implement {task_id}",
        "allowed_files": ["harness/cowork_launcher.py"],
        "forbidden_files": [],
        "acceptance_criteria": ["The launcher is correct"],
        "verification_commands": ["pytest -q"],
        "constraints": {"local_first": True},
        "budget": {"max_attempts": 3},
        "domain": "harness",
        "assigned_ai": "claude",
        "legacy_adapter": False,
    }
    base = {
        "task_id":     task_id,
        "session_id":  f"jarvis-harness-claude-{task_id.lower().replace('-', '')}",
        "attempt_id":  attempt_id,
        "contract_sha256": "a" * 64,
        "task_spec":   task_spec,
        "repo_path":   "/tmp/jarvis-ai",
        "base_ref":    "abc123",
        "prompt":      f"<role>Implement {task_id}.</role>",
        "queued_at":   "2026-06-28T00:00:00+00:00",
        "status":      "pending",
        "domain":      "harness",
        "assigned_ai": "claude",
    }
    base.update(overrides)
    return base


def _artifact_path(root: str | Path, entry: dict) -> Path:
    return Path(root) / "PENDING_SESSIONS" / f"{entry['attempt_id']}.json"


def _active_session(session_id: str = "s1", task_id: str = "TASK-001",
                    minutes_ago: int = 5) -> dict:
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes_ago)
    ).isoformat()
    return {
        "session_id":   session_id,
        "task_id":      task_id,
        "claimed_at":   ts,
        "last_updated": ts,
        "status":       "active",
        "result_summary": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  1. process_launch_queue — CoworkLauncher
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessLaunchQueue(unittest.TestCase):

    # ── test 1 ────────────────────────────────────────────────────────────────
    def test_missing_file_returns_empty(self):
        """process_launch_queue returns [] when the file does not exist."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            result = process_launch_queue(queue_path)
        self.assertEqual(result, [])

    # ── test 2 ────────────────────────────────────────────────────────────────
    def test_empty_queue_returns_empty(self):
        """No pending entries → empty list, no files written."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue_path.write_text(json.dumps([]), encoding="utf-8")
            result = process_launch_queue(queue_path)
        self.assertEqual(result, [])

    # ── test 3 ────────────────────────────────────────────────────────────────
    def test_pending_entry_is_processed(self):
        """A single pending entry is returned in the processed list."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue_path.write_text(json.dumps([_pending_entry()]), encoding="utf-8")
            result = process_launch_queue(queue_path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["task_id"], "TASK-001")

    # ── test 4 ────────────────────────────────────────────────────────────────
    def test_entry_status_set_to_handoff_ready(self):
        """Materialized entries are ready for pickup, not claimed as running."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue_path.write_text(json.dumps([_pending_entry()]), encoding="utf-8")
            result = process_launch_queue(queue_path)

        self.assertEqual(result[0]["status"], "handoff_ready")

    # ── test 5 ────────────────────────────────────────────────────────────────
    def test_entry_gets_handoff_ready_timestamp(self):
        """Processed entries record when the handoff became durable."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue_path.write_text(json.dumps([_pending_entry()]), encoding="utf-8")
            result = process_launch_queue(queue_path)

        ready_at = result[0].get("handoff_ready_at", "")
        self.assertTrue(ready_at.startswith("202"), f"ready timestamp looks wrong: {ready_at!r}")

    # ── test 6 ────────────────────────────────────────────────────────────────
    def test_prompt_file_written_to_pending_sessions(self):
        """An attempt-specific envelope is created for each fired entry."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-007")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")
            process_launch_queue(queue_path)
            prompt_file = _artifact_path(d, entry)
            self.assertTrue(prompt_file.exists(), f"Expected {prompt_file} to exist")

    # ── test 7 ────────────────────────────────────────────────────────────────
    def test_prompt_file_contains_prompt_text(self):
        """The written envelope includes the original prompt."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-008", prompt="UNIQUE_PROMPT_CONTENT_XYZ")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")
            process_launch_queue(queue_path)
            envelope = json.loads(_artifact_path(d, entry).read_text())
        self.assertEqual(envelope["prompt"], "UNIQUE_PROMPT_CONTENT_XYZ")

    # ── test 8 ────────────────────────────────────────────────────────────────
    def test_non_pending_entries_are_skipped(self):
        """Already-fired or done entries should not be re-processed."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue = [
                _pending_entry("TASK-001", status="fired"),
                _pending_entry("TASK-002", status="done"),
                _pending_entry("TASK-003", status="pending"),
            ]
            queue_path.write_text(json.dumps(queue), encoding="utf-8")
            result = process_launch_queue(queue_path)

        # Only TASK-003 was pending
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["task_id"], "TASK-003")

    # ── test 9 — queue file persisted after processing ────────────────────────
    def test_queue_file_updated_after_processing(self):
        """The queue JSON file is atomically updated with the fired status."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            queue_path.write_text(json.dumps([_pending_entry()]), encoding="utf-8")
            process_launch_queue(queue_path)
            on_disk = json.loads(queue_path.read_text())

        self.assertEqual(on_disk[0]["status"], "handoff_ready")
        self.assertIn("handoff_ready_at", on_disk[0])

    def test_envelope_contains_exact_launch_contract(self):
        """Pickup artifacts carry all attempt and contract data without reconstruction."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-010")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")

            process_launch_queue(queue_path)

            envelope = json.loads(_artifact_path(d, entry).read_text())

        for field in (
            "attempt_id",
            "session_id",
            "contract_sha256",
            "task_spec",
            "prompt",
            "repo_path",
            "base_ref",
        ):
            self.assertEqual(envelope[field], entry[field])

    def test_artifact_write_failure_is_recorded_and_not_fired(self):
        """A failed pickup write leaves the launch retryable instead of fired."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-011")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")

            with patch(
                "harness.cowork_launcher._write_prompt_file",
                side_effect=OSError("disk full"),
            ):
                result = process_launch_queue(queue_path)

            on_disk = json.loads(queue_path.read_text())[0]

        self.assertEqual(result, [])
        self.assertEqual(on_disk["status"], "launch_error")
        self.assertNotIn("fired_at", on_disk)
        self.assertIn("disk full", on_disk["launch_error"])

    def test_launch_error_retries_same_attempt_idempotently(self):
        """A failed attempt becomes fired only after its artifact exists."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-012")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")

            with patch(
                "harness.cowork_launcher._write_prompt_file",
                side_effect=OSError("temporary failure"),
            ):
                process_launch_queue(queue_path)
            result = process_launch_queue(queue_path)

            on_disk = json.loads(queue_path.read_text())[0]
            envelope = json.loads(_artifact_path(d, entry).read_text())

        self.assertEqual(len(result), 1)
        self.assertEqual(on_disk["status"], "handoff_ready")
        self.assertNotIn("launch_error", on_disk)
        self.assertEqual(envelope["attempt_id"], entry["attempt_id"])

    def test_retry_attempts_for_same_task_get_distinct_envelopes(self):
        """A later attempt cannot overwrite an earlier attempt's pickup artifact."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            first = _pending_entry(
                "TASK-013",
                attempt_id="attempt-first",
                session_id="session-first",
                prompt="first prompt",
            )
            second = _pending_entry(
                "TASK-013",
                attempt_id="attempt-second",
                session_id="session-second",
                prompt="second prompt",
            )
            queue_path.write_text(json.dumps([first, second]), encoding="utf-8")

            result = process_launch_queue(queue_path)

            first_envelope = json.loads(_artifact_path(d, first).read_text())
            second_envelope = json.loads(_artifact_path(d, second).read_text())

        self.assertEqual(len(result), 2)
        self.assertEqual(first_envelope["prompt"], "first prompt")
        self.assertEqual(second_envelope["prompt"], "second prompt")
        self.assertNotEqual(_artifact_path(d, first), _artifact_path(d, second))

    def test_queue_save_failure_propagates_and_retry_reuses_artifact(self):
        """Queue persistence failure stays pending and does not rewrite the artifact."""
        with tempfile.TemporaryDirectory() as d:
            queue_path = Path(d) / "LAUNCH_QUEUE.json"
            entry = _pending_entry("TASK-014")
            queue_path.write_text(json.dumps([entry]), encoding="utf-8")

            with patch(
                "harness.cowork_launcher._save_queue",
                side_effect=OSError("queue unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "queue unavailable"):
                    process_launch_queue(queue_path)

            artifact = _artifact_path(d, entry)
            before = artifact.read_bytes()
            self.assertEqual(json.loads(queue_path.read_text())[0]["status"], "pending")

            process_launch_queue(queue_path)

            self.assertEqual(artifact.read_bytes(), before)
            self.assertEqual(
                json.loads(queue_path.read_text())[0]["status"], "handoff_ready"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  2. loop_monitor.status_text
# ─────────────────────────────────────────────────────────────────────────────

class TestLoopMonitorStatusText(unittest.TestCase):

    def _write_work_queue(self, path: Path, tasks: list[dict]) -> None:
        path.write_text(json.dumps(tasks), encoding="utf-8")

    def _write_active_sessions(self, path: Path, sessions: list[dict]) -> None:
        path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")

    def _write_master_log(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── test 10 ───────────────────────────────────────────────────────────────
    def test_status_text_returns_string(self):
        """status_text() always returns a non-empty string."""
        with tempfile.TemporaryDirectory() as d:
            result = status_text(work_queue_path=Path(d) / "missing.json")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 10)

    # ── test 11 ───────────────────────────────────────────────────────────────
    def test_status_text_shows_task_counts(self):
        """Task counts by status appear in the output."""
        with tempfile.TemporaryDirectory() as d:
            wq = Path(d) / "WORK_QUEUE.json"
            self._write_work_queue(wq, [
                {"status": "queued"},
                {"status": "queued"},
                {"status": "in_progress"},
                {"status": "done"},
            ])
            result = status_text(
                work_queue_path=wq,
                active_sessions_path=Path(d) / "missing_as.json",
                master_log_path=Path(d) / "missing_ml.json",
                launch_queue_path=Path(d) / "missing_lq.json",
            )
        self.assertIn("queued", result)
        self.assertIn("in_progress", result)
        self.assertIn("done", result)

    # ── test 12 ───────────────────────────────────────────────────────────────
    def test_status_text_shows_active_sessions(self):
        """Active session IDs appear in the output."""
        with tempfile.TemporaryDirectory() as d:
            as_path = Path(d) / "ACTIVE_SESSIONS.json"
            self._write_active_sessions(as_path, [_active_session("my-special-session")])
            result = status_text(
                work_queue_path=Path(d) / "missing_wq.json",
                active_sessions_path=as_path,
                master_log_path=Path(d) / "missing_ml.json",
                launch_queue_path=Path(d) / "missing_lq.json",
            )
        self.assertIn("my-special-session", result)

    # ── test 13 ───────────────────────────────────────────────────────────────
    def test_status_text_shows_last_5_log_entries(self):
        """The last (up to 5) log lines appear in the output."""
        with tempfile.TemporaryDirectory() as d:
            ml_path = Path(d) / "MASTER_LOG.md"
            log_lines = [f"[2026-06-28 00:0{i} UTC] event {i}" for i in range(8)]
            self._write_master_log(ml_path, log_lines)
            result = status_text(
                work_queue_path=Path(d) / "missing_wq.json",
                active_sessions_path=Path(d) / "missing_as.json",
                master_log_path=ml_path,
                launch_queue_path=Path(d) / "missing_lq.json",
            )
        # Last 5 entries should appear; first 3 should not
        self.assertIn("event 7", result)
        self.assertIn("event 5", result)
        self.assertNotIn("event 0", result)
        self.assertNotIn("event 1", result)
        self.assertNotIn("event 2", result)

    # ── test 14 ───────────────────────────────────────────────────────────────
    def test_status_text_flags_stalled_session(self):
        """A session with last_updated >30 min ago appears as stalled."""
        with tempfile.TemporaryDirectory() as d:
            as_path = Path(d) / "ACTIVE_SESSIONS.json"
            stale_ts = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=60)
            ).isoformat()
            stale_session = {
                "session_id":   "stalled-worker",
                "task_id":      "TASK-999",
                "claimed_at":   stale_ts,
                "last_updated": stale_ts,
                "status":       "active",
                "result_summary": None,
            }
            self._write_active_sessions(as_path, [stale_session])
            result = status_text(
                work_queue_path=Path(d) / "missing_wq.json",
                active_sessions_path=as_path,
                master_log_path=Path(d) / "missing_ml.json",
                launch_queue_path=Path(d) / "missing_lq.json",
                stall_minutes=30,
            )
        self.assertIn("stalled-worker", result.lower())
        self.assertIn("STALLED", result)

    # ── test 15 ───────────────────────────────────────────────────────────────
    def test_status_text_handles_all_missing_files_gracefully(self):
        """/status must not raise even when every data file is absent."""
        with tempfile.TemporaryDirectory() as d:
            result = status_text(
                work_queue_path=Path(d) / "no_wq.json",
                active_sessions_path=Path(d) / "no_as.json",
                master_log_path=Path(d) / "no_ml.md",
                launch_queue_path=Path(d) / "no_lq.json",
            )
        self.assertIsInstance(result, str)
        self.assertIn("Jarvis Loop Status", result)


# ─────────────────────────────────────────────────────────────────────────────
#  3. router.py /status wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestStatusRouterWiring(unittest.TestCase):
    """
    Verify that the /status command is wired into router.py without importing
    the full router (which pulls in ~50 heavyweight modules).

    Strategy: parse _is_status_command out of router.py with ast + compile,
    run it in a clean namespace.
    """

    def _extract_is_status_fn(self) -> object:
        """
        Return a callable version of _is_status_command extracted directly
        from the router.py source, executed in isolation.
        """
        import ast
        import re as _re
        router_src = (_REPO / "router.py").read_text(encoding="utf-8")
        # Find the function definition block
        m = _re.search(
            r"(def _is_status_command\(lower.*?)(?=\ndef |\nclass |\Z)",
            router_src,
            _re.DOTALL,
        )
        if not m:
            return None
        fn_src = m.group(1)
        # Compile and exec in isolation (only needs builtins + re)
        ns: dict = {"re": __import__("re"), "Any": None}
        exec(compile(fn_src, "<router_snippet>", "exec"), ns)  # noqa: S102
        return ns.get("_is_status_command")

    def test_status_command_detected(self):
        """/status and 'loop status' trigger _is_status_command in router.py."""
        is_status = self._extract_is_status_fn()
        self.assertIsNotNone(is_status, "_is_status_command not found in router.py")
        self.assertTrue(is_status("/status"))
        self.assertTrue(is_status("loop status"))
        self.assertTrue(is_status("show orchestrator"))
        # Must NOT fire on unrelated status queries
        self.assertFalse(is_status("git status"))
        self.assertFalse(is_status("what's my meeting status"))


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers unit-tested directly
# ─────────────────────────────────────────────────────────────────────────────

class TestLoopMonitorHelpers(unittest.TestCase):

    def test_find_stalled_excludes_fresh_sessions(self):
        sessions = [_active_session("fresh", minutes_ago=5)]
        stalled = _find_stalled(sessions, timeout_minutes=30)
        self.assertEqual(stalled, [])

    def test_find_stalled_includes_old_sessions(self):
        sessions = [_active_session("old", minutes_ago=60)]
        stalled = _find_stalled(sessions, timeout_minutes=30)
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["session_id"], "old")

    def test_read_log_tail_n5(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "MASTER_LOG.md"
            path.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")
            tail = _read_log_tail(path, n=5)
        self.assertEqual(tail, ["line5", "line6", "line7", "line8", "line9"])

    def test_work_queue_counts_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            result = _work_queue_counts(Path(d) / "nope.json")
        self.assertIsNone(result)

    def test_launch_queue_counts_pending_and_handoff_ready(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "LAUNCH_QUEUE.json"
            path.write_text(json.dumps([
                {"status": "pending"},
                {"status": "pending"},
                {"status": "handoff_ready"},
                {"status": "fired"},
            ]), encoding="utf-8")
            pending, ready = _launch_queue_counts(path)
        self.assertEqual(pending, 2)
        self.assertEqual(ready, 2)


if __name__ == "__main__":
    unittest.main()
