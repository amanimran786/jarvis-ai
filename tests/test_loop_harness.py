"""
tests/test_loop_harness.py — Tests for the autonomous orchestration loop harness.

Covers:
  • harness/prompt_generator.py  — LLM path + fallback
  • harness/session_tracker.py   — claim / complete / stalled / list_active
  • LAUNCH_QUEUE.json             — read/write roundtrip
  • orchestrator_loop.py          — dry_run produces correct LAUNCH_QUEUE entries
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Ensure repo root is on path ───────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from harness.prompt_generator import (
    _build_meta_prompt,
    _generate_fallback,
    generate_session_prompt,
)
from harness.session_tracker import SessionTracker
from harness.task_contract import TaskSpec


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sample_task(**overrides) -> dict:
    base = {
        "id": "TASK-001",
        "title": "Add rate limiting to web_fetch",
        "description": "web_fetch has no retry logic on 429 responses.",
        "files_hint": ["harness/web_search.py", "tests/test_web_search.py"],
        "acceptance_criteria": ["retries 3× on 429", "all tests pass"],
        "domain": "harness",
        "assigned_ai": "claude",
        "status": "queued",
    }
    base.update(overrides)
    return base


def _sample_repo_context(**overrides) -> dict:
    base = {
        "recent_commits": [
            "abc1234 [CLAUDE] feat(harness): adaptive router",
            "def5678 [CODEX] fix: tray panel crash",
        ],
        "test_count": 312,
        "active_files": ["harness/web_search.py", "harness/adaptive_router.py"],
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
#  PromptGenerator — fallback (no LLM)
# ─────────────────────────────────────────────────────────────────────────────

class TestPromptGeneratorFallback(unittest.TestCase):

    def test_fallback_contains_task_id(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("TASK-001", prompt)

    def test_fallback_contains_title(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("Add rate limiting to web_fetch", prompt)

    def test_fallback_contains_acceptance_criteria(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("retries 3× on 429", prompt)

    def test_fallback_contains_files_hint(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("harness/web_search.py", prompt)

    def test_fallback_contains_recent_commits(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("abc1234", prompt)

    def test_fallback_contains_role_block(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("<role>", prompt)

    def test_fallback_contains_commit_format(self):
        prompt = _generate_fallback(_sample_task(), _sample_repo_context())
        self.assertIn("[CLAUDE]", prompt)

    def test_fallback_string_files_hint(self):
        task = _sample_task(files_hint="harness/web_search.py")
        prompt = _generate_fallback(task, _sample_repo_context())
        self.assertIn("harness/web_search.py", prompt)

    def test_fallback_empty_criteria(self):
        task = _sample_task(acceptance_criteria=[])
        prompt = _generate_fallback(task, _sample_repo_context())
        self.assertIn("Produce the requested artifact for loop inspection", prompt)

    def test_fallback_no_commits(self):
        ctx = _sample_repo_context(recent_commits=[])
        prompt = _generate_fallback(_sample_task(), ctx)
        self.assertIn("none", prompt.lower())


# ─────────────────────────────────────────────────────────────────────────────
#  PromptGenerator — meta-prompt builder
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildMetaPrompt(unittest.TestCase):

    def test_meta_prompt_includes_task_id(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("TASK-001", mp)

    def test_meta_prompt_includes_description(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("no retry logic", mp)

    def test_meta_prompt_includes_file_hints(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("harness/web_search.py", mp)

    def test_meta_prompt_includes_acceptance(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("retries 3× on 429", mp)

    def test_meta_prompt_includes_commit_count(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("312", mp)

    def test_meta_prompt_includes_recent_commits(self):
        mp = _build_meta_prompt(_sample_task(), _sample_repo_context())
        self.assertIn("abc1234", mp)


# ─────────────────────────────────────────────────────────────────────────────
#  PromptGenerator — LLM path (mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestPromptGeneratorLLM(unittest.TestCase):

    def test_llm_response_returned_verbatim(self):
        expected = "<role>You are an engineer.</role>\n<instructions>Do X.</instructions>"
        mock_ask = MagicMock(return_value=expected)
        with patch.dict("sys.modules", {
            "brains": MagicMock(),
            "brains.brain_ollama": MagicMock(ask_local=mock_ask),
            "config": MagicMock(LOCAL_REASONING="qwen3:30b-a3b"),
        }):
            # Re-import within patch scope
            import importlib
            import harness.prompt_generator as pg
            importlib.reload(pg)
            result = pg._generate_via_llm(_sample_task(), _sample_repo_context())
        self.assertEqual(result, expected)

    def test_falls_back_when_ollama_raises(self):
        """generate_session_prompt should never raise — fallback activates."""
        with patch("harness.prompt_generator._generate_via_llm",
                   side_effect=ConnectionRefusedError("ollama down")):
            result = generate_session_prompt(
                _sample_task(), _sample_repo_context(), use_llm=True
            )
        self.assertIn("TASK-001", result)
        self.assertIn("Add rate limiting", result)

    def test_falls_back_when_llm_returns_empty(self):
        with patch("harness.prompt_generator._generate_via_llm",
                   side_effect=ValueError("LLM returned empty response")):
            result = generate_session_prompt(
                _sample_task(), _sample_repo_context(), use_llm=True
            )
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 50)

    def test_default_renderer_does_not_call_prompt_writing_model(self):
        with patch(
            "harness.prompt_generator._generate_via_llm",
            side_effect=AssertionError("loop must render its own contract"),
        ):
            result = generate_session_prompt(_sample_task(), _sample_repo_context())

        self.assertIn("<task_contract>", result)
        self.assertIn("Contract SHA-256", result)
        self.assertIn("The loop, not this response", result)


# ─────────────────────────────────────────────────────────────────────────────
#  SessionTracker
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionTracker(unittest.TestCase):

    def _tracker(self, tmp: Path) -> SessionTracker:
        return SessionTracker(path=tmp / "ACTIVE_SESSIONS.json")

    # -- claim --

    def test_claim_creates_entry(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "jarvis-board")
            active = t.list_active()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["session_id"], "jarvis-board")
            self.assertEqual(active[0]["task_id"], "TASK-001")

    def test_claim_twice_updates_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "jarvis-board")
            t.claim("TASK-002", "jarvis-board")
            self.assertEqual(t.active_count(), 1)
            self.assertEqual(t.list_active()[0]["task_id"], "TASK-002")

    def test_claim_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "session-a")
            t.claim("TASK-002", "session-b")
            self.assertEqual(t.active_count(), 2)

    # -- complete --

    def test_complete_marks_done(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "jarvis-board")
            t.complete("jarvis-board", "Shipped rate limiting with 3x retry")
            self.assertEqual(t.active_count(), 0)
            completed = t.list_completed()
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["result_summary"], "Shipped rate limiting with 3x retry")

    def test_complete_unknown_session_noop(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "real-session")
            t.complete("nonexistent-session", "done")   # should not raise
            self.assertEqual(t.active_count(), 1)       # real-session still active

    def test_claim_and_complete_preserve_attempt_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim(
                "TASK-001",
                "jarvis-board",
                attempt_id="attempt_123",
                contract_sha256="abc123",
            )
            evidence = {
                "observer": "loop",
                "commands": [{"command": "pytest -q", "exit_code": 0}],
            }
            t.complete("jarvis-board", "implemented", evidence=evidence)

            completed = t.list_completed()[0]
            self.assertEqual(completed["attempt_id"], "attempt_123")
            self.assertEqual(completed["contract_sha256"], "abc123")
            self.assertEqual(completed["completion_evidence"], evidence)

    # -- active_count --

    def test_active_count_zero_initial(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            self.assertEqual(t.active_count(), 0)

    def test_active_count_decrements_on_complete(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "s1")
            t.claim("TASK-002", "s2")
            self.assertEqual(t.active_count(), 2)
            t.complete("s1", "done")
            self.assertEqual(t.active_count(), 1)

    # -- get_stalled --

    def test_get_stalled_empty_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "jarvis-board")
            # Just claimed → should NOT be stalled with 30m timeout
            stalled = t.get_stalled(timeout_minutes=30)
            self.assertEqual(stalled, [])

    def test_get_stalled_detects_old_session(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "old-session")
            # Manually backdate last_updated
            data = t._load()
            old_time = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=60)
            ).isoformat()
            data["sessions"][0]["last_updated"] = old_time
            t._save(data)

            stalled = t.get_stalled(timeout_minutes=30)
            self.assertEqual(len(stalled), 1)
            self.assertEqual(stalled[0]["session_id"], "old-session")

    def test_get_stalled_excludes_completed(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "s1")
            t.complete("s1", "done")
            stalled = t.get_stalled(timeout_minutes=0)   # 0 min → everything is stale
            self.assertEqual(stalled, [])   # completed, not active

    # -- purge_completed --

    def test_purge_completed_removes_entries(self):
        with tempfile.TemporaryDirectory() as d:
            t = self._tracker(Path(d))
            t.claim("TASK-001", "s1")
            t.complete("s1", "done")
            removed = t.purge_completed()
            self.assertEqual(removed, 1)
            self.assertEqual(t.list_completed(), [])

    # -- persistence --

    def test_tracker_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ACTIVE_SESSIONS.json"
            t1 = SessionTracker(path=path)
            t1.claim("TASK-001", "jarvis-board")

            t2 = SessionTracker(path=path)
            self.assertEqual(t2.active_count(), 1)

    def test_tracker_handles_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ACTIVE_SESSIONS.json"
            path.write_text("not json!!!", encoding="utf-8")
            t = SessionTracker(path=path)
            self.assertEqual(t.active_count(), 0)   # graceful recovery


# ─────────────────────────────────────────────────────────────────────────────
#  LAUNCH_QUEUE.json roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestLaunchQueueRoundtrip(unittest.TestCase):

    def _run_loop_isolated(self, tmp: Path, *, seed_task: dict | None = None, **loop_kwargs) -> dict:
        """Run one loop iteration with all paths redirected to tmp."""
        import orchestrator_loop as ol
        orig_wq = ol.WORK_QUEUE_PATH
        orig_lq = ol.LAUNCH_QUEUE_PATH
        orig_ml = ol.MASTER_LOG_PATH
        orig_attempts = ol.ATTEMPT_LOG_PATH

        ol.WORK_QUEUE_PATH   = tmp / "WORK_QUEUE.json"
        ol.LAUNCH_QUEUE_PATH = tmp / "LAUNCH_QUEUE.json"
        ol.MASTER_LOG_PATH   = tmp / "MASTER_LOG.md"
        ol.ATTEMPT_LOG_PATH  = tmp / "attempts.jsonl"

        # Seed a queued task
        task = seed_task or _sample_task(status="queued", priority=1)
        ol.WORK_QUEUE_PATH.write_text(json.dumps([task]), encoding="utf-8")

        # Patch SessionTracker to use tmp dir
        orig_tracker = ol.SessionTracker

        class _TmpTracker(SessionTracker):
            def __init__(self):
                super().__init__(path=tmp / "ACTIVE_SESSIONS.json")

        ol.SessionTracker = _TmpTracker

        # Patch prompt generator to return a canned prompt
        with patch("harness.prompt_generator._generate_via_llm",
                   return_value="<role>Test session</role>"):
            result = ol.run_loop(**loop_kwargs)

        ol.WORK_QUEUE_PATH   = orig_wq
        ol.LAUNCH_QUEUE_PATH = orig_lq
        ol.MASTER_LOG_PATH   = orig_ml
        ol.ATTEMPT_LOG_PATH  = orig_attempts
        ol.SessionTracker    = orig_tracker

        return result

    def _run_completed_isolated(
        self,
        tmp: Path,
        *,
        evidence: dict | None,
        contract_sha256: str | None = None,
    ) -> tuple[dict, list[dict], list[dict]]:
        import orchestrator_loop as ol

        originals = (
            ol.WORK_QUEUE_PATH,
            ol.LAUNCH_QUEUE_PATH,
            ol.MASTER_LOG_PATH,
            ol.ATTEMPT_LOG_PATH,
            ol.SessionTracker,
        )
        ol.WORK_QUEUE_PATH = tmp / "WORK_QUEUE.json"
        ol.LAUNCH_QUEUE_PATH = tmp / "LAUNCH_QUEUE.json"
        ol.MASTER_LOG_PATH = tmp / "MASTER_LOG.md"
        ol.ATTEMPT_LOG_PATH = tmp / "attempts.jsonl"

        task = _sample_task(
            status="in_progress",
            verification_commands=["python -m pytest tests/test_web_search.py -q"],
        )
        spec = TaskSpec.from_queue_task(task)
        ol.WORK_QUEUE_PATH.write_text(json.dumps([task]), encoding="utf-8")

        class _TmpTracker(SessionTracker):
            def __init__(self):
                super().__init__(path=tmp / "ACTIVE_SESSIONS.json")

        tracker = _TmpTracker()
        tracker.claim(
            spec.task_id,
            "session-a",
            attempt_id="attempt_123",
            contract_sha256=contract_sha256 or spec.contract_hash,
        )
        tracker.complete("session-a", "agent says done", evidence=evidence)
        ol.SessionTracker = _TmpTracker

        try:
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                result = ol.run_loop(max_concurrent=0, dry_run=False)
            queue = json.loads(ol.WORK_QUEUE_PATH.read_text())
            attempts = [
                json.loads(line)
                for line in ol.ATTEMPT_LOG_PATH.read_text().splitlines()
            ]
            return result, queue, attempts
        finally:
            (
                ol.WORK_QUEUE_PATH,
                ol.LAUNCH_QUEUE_PATH,
                ol.MASTER_LOG_PATH,
                ol.ATTEMPT_LOG_PATH,
                ol.SessionTracker,
            ) = originals

    def test_dry_run_does_not_write_launch_queue(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=True, max_concurrent=3)
            lq_path = tmp / "LAUNCH_QUEUE.json"
            if lq_path.exists():
                lq = json.loads(lq_path.read_text())
                self.assertEqual(lq, [])
            self.assertEqual(result["dry_run"], True)

    def test_dry_run_reports_launched_count(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=True, max_concurrent=3)
            # In dry_run mode the loop still counts the "would launch" action
            self.assertGreaterEqual(result["launched"], 1)

    def test_non_dry_run_writes_launch_queue(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            lq_path = tmp / "LAUNCH_QUEUE.json"
            self.assertTrue(lq_path.exists())
            lq = json.loads(lq_path.read_text())
            self.assertEqual(len(lq), 1)
            self.assertEqual(lq[0]["task_id"], "TASK-001")
            self.assertEqual(lq[0]["status"], "pending")

    def test_launch_record_contains_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            lq = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())
            self.assertIn("prompt", lq[0])
            self.assertTrue(len(lq[0]["prompt"]) > 10)

    def test_launch_record_contains_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            lq = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())
            self.assertIn("session_id", lq[0])
            self.assertTrue(lq[0]["session_id"].startswith("jarvis-"))

    def test_launch_record_contains_contract_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)

            launch = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())[0]
            attempts = [json.loads(line) for line in (tmp / "attempts.jsonl").read_text().splitlines()]

            self.assertEqual(launch["task_spec"]["id"], "TASK-001")
            self.assertEqual(launch["contract_sha256"], attempts[0]["contract_sha256"])
            self.assertEqual(launch["attempt_id"], attempts[0]["attempt_id"])
            self.assertEqual(attempts[0]["phase"], "dispatch")

    def test_legacy_queue_task_launches_through_compatibility_contract(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            legacy = {
                "task": "Run focused regression tests",
                "notes": "Capture the failing cases",
                "status": "queued",
                "priority": 1,
                "session_name": "legacy-lane",
            }
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(
                    tmp,
                    seed_task=legacy,
                    dry_run=False,
                    max_concurrent=1,
                )

            launch = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())[0]
            queue = json.loads((tmp / "WORK_QUEUE.json").read_text())

            self.assertTrue(launch["task_id"].startswith("LEGACY-"))
            self.assertTrue(launch["task_spec"]["legacy_adapter"])
            self.assertEqual(queue[0]["status"], "in_progress")

    def test_no_duplicate_pending_entries(self):
        """Running the loop twice should not create duplicate pending records."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
                # Task is now in_progress → second run should not re-launch it
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            lq = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())
            pending = [r for r in lq if r.get("status") == "pending"]
            self.assertEqual(len(pending), 1)

    def test_launch_queue_json_is_valid(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            # Parsing should not raise
            data = json.loads((tmp / "LAUNCH_QUEUE.json").read_text())
            self.assertIsInstance(data, list)

    def test_master_log_appended(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            log_text = (tmp / "MASTER_LOG.md").read_text()
            self.assertIn("[orchestrator]", log_text)
            self.assertIn("loop start", log_text)

    def test_max_concurrent_respected(self):
        """With max_concurrent=0 nothing should be launched."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=0)
            self.assertEqual(result["launched"], 0)

    def test_active_count_does_not_double_count_new_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=1)

            self.assertEqual(result["launched"], 1)
            self.assertEqual(result["active_now"], 1)

    def test_contract_render_failure_blocks_instead_of_launching_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch(
                "harness.prompt_generator.generate_session_prompt",
                side_effect=RuntimeError("renderer failed"),
            ):
                result = self._run_loop_isolated(
                    tmp, dry_run=False, max_concurrent=1
                )

            queue = json.loads((tmp / "WORK_QUEUE.json").read_text())
            self.assertEqual(result["launched"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertEqual(queue[0]["status"], "blocked")
            self.assertFalse((tmp / "LAUNCH_QUEUE.json").exists())

    def test_checkpoint_failure_blocks_before_launch(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch(
                "orchestrator_loop.AttemptStore.append",
                side_effect=OSError("disk full"),
            ):
                result = self._run_loop_isolated(
                    tmp, dry_run=False, max_concurrent=1
                )

            queue = json.loads((tmp / "WORK_QUEUE.json").read_text())
            self.assertEqual(result["launched"], 0)
            self.assertEqual(result["blocked"], 1)
            self.assertEqual(queue[0]["status"], "blocked")
            self.assertFalse((tmp / "LAUNCH_QUEUE.json").exists())

    def test_agent_summary_without_loop_evidence_is_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            result, queue, attempts = self._run_completed_isolated(
                Path(d), evidence=None
            )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["unverified"], 1)
        self.assertEqual(queue[0]["status"], "unverified")
        self.assertEqual(attempts[-1]["phase"], "completion_gate")
        self.assertEqual(attempts[-1]["status"], "unverified")

    def test_loop_evidence_promotes_verified_task_to_done(self):
        evidence = {
            "observer": "loop",
            "changed_files": ["harness/web_search.py"],
            "commands": [
                {
                    "command": "python -m pytest tests/test_web_search.py -q",
                    "exit_code": 0,
                }
            ],
            "policy_findings": [],
        }
        with tempfile.TemporaryDirectory() as d:
            result, queue, attempts = self._run_completed_isolated(
                Path(d), evidence=evidence
            )

        self.assertEqual(result["harvested"], 1)
        self.assertEqual(queue[0]["status"], "done")
        self.assertEqual(attempts[-1]["status"], "verified")

    def test_failed_loop_verification_blocks_completion(self):
        evidence = {
            "observer": "loop",
            "changed_files": ["harness/web_search.py"],
            "commands": [
                {
                    "command": "python -m pytest tests/test_web_search.py -q",
                    "exit_code": 1,
                }
            ],
            "policy_findings": [],
        }
        with tempfile.TemporaryDirectory() as d:
            result, queue, attempts = self._run_completed_isolated(
                Path(d), evidence=evidence
            )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(queue[0]["status"], "blocked")
        self.assertEqual(queue[0]["verification_failure_class"], "test_failure")
        self.assertEqual(attempts[-1]["failure_class"], "test_failure")

    def test_contract_change_after_dispatch_blocks_completion(self):
        evidence = {
            "observer": "loop",
            "changed_files": ["harness/web_search.py"],
            "commands": [],
            "policy_findings": [],
        }
        with tempfile.TemporaryDirectory() as d:
            result, queue, attempts = self._run_completed_isolated(
                Path(d),
                evidence=evidence,
                contract_sha256="stale-contract-hash",
            )

        self.assertEqual(result["rejected"], 1)
        self.assertEqual(queue[0]["verification_failure_class"], "contract_mismatch")
        self.assertEqual(attempts[-1]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
