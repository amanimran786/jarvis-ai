"""Lesson accumulation on approval + verifier verdict persistence."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import task_runtime


def _approve(agent_id: str):
    import api
    with patch("infra.rbac.registry.caller_from_request", return_value=MagicMock()):
        return asyncio.run(api.approve_lesson(agent_id, MagicMock()))


def _write_pending(lessons_dir: Path, agent_id: str, lesson: str) -> None:
    (lessons_dir / f"{agent_id}.pending.md").write_text(
        f"# PENDING_APPROVAL\n# Agent: {agent_id}\n# Proposed: now\n{lesson}\n",
        encoding="utf-8",
    )


class LessonAccumulationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patcher = patch.object(task_runtime, "_LESSONS_DIR", self.dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_second_approval_keeps_first_lesson(self):
        _write_pending(self.dir, "qa-tester", "Always run the failing test first.")
        _approve("qa-tester")
        _write_pending(self.dir, "qa-tester", "Quote exact error messages.")
        _approve("qa-tester")
        active = (self.dir / "qa-tester.md").read_text(encoding="utf-8")
        self.assertIn("Always run the failing test first.", active)
        self.assertIn("Quote exact error messages.", active)

    def test_duplicate_lesson_not_appended_twice(self):
        _write_pending(self.dir, "qa-tester", "Always run the failing test first.")
        _approve("qa-tester")
        _write_pending(self.dir, "qa-tester", "Always run the failing test first.")
        res = _approve("qa-tester")
        active = (self.dir / "qa-tester.md").read_text(encoding="utf-8")
        self.assertEqual(active.count("Always run the failing test first."), 1)
        self.assertTrue(res.get("duplicate"))
        self.assertFalse((self.dir / "qa-tester.pending.md").exists())

    def test_loader_keeps_newest_lessons_when_over_budget(self):
        old = "- [2026-01-01] old lesson " + "x" * 5000
        new = "- [2026-06-10] newest lesson"
        (self.dir / "qa-tester.md").write_text(old + "\n\n" + new + "\n", encoding="utf-8")
        loaded = task_runtime._load_agent_lesson("qa-tester")
        self.assertIn("newest lesson", loaded)


class VerdictPersistenceTests(unittest.TestCase):
    def test_record_verdict_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.jsonl"
            with patch.object(task_runtime, "_VERDICTS_PATH", path):
                task_runtime._record_verdict(
                    "task_1", "qa-tester",
                    {"pass": True, "score": 0.9, "reason": "solid"},
                    tool_calls=2, retry_count=0,
                )
                task_runtime._record_verdict(
                    "task_2", "qa-tester",
                    {"pass": False, "score": 0.3, "reason": "fabricated"},
                    tool_calls=0, retry_count=1,
                )
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            first, second = json.loads(lines[0]), json.loads(lines[1])
            self.assertEqual(first["task_id"], "task_1")
            self.assertEqual(first["score"], 0.9)
            self.assertEqual(second["pass"], False)
            self.assertEqual(second["tool_calls"], 0)
            self.assertEqual(second["retry_count"], 1)

    def test_record_verdict_never_raises(self):
        with patch.object(task_runtime, "_VERDICTS_PATH", Path("/dev/null/impossible/x.jsonl")):
            task_runtime._record_verdict("t", "a", {}, 0, 0)  # must not raise


if __name__ == "__main__":
    unittest.main()
