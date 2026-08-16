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
import shlex
import subprocess
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
from harness.commit_review_gate import CommitGateResult
from harness.completion_verifier import (
    CompletionAssessment,
    compact_completion_evidence,
)
from harness.session_tracker import SessionTracker, SessionTrackerError
from harness.task_contract import (
    TaskContract,
    TaskSpec,
    TaskType,
    evaluate_completion,
)


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
        "orchestrated_by": "codex",
        "orchestration_state": "assigned",
        "worker_type": "claude",
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


def _trusted_assessment(evidence: dict):
    def verify(spec: TaskSpec, *_args, **_kwargs) -> CompletionAssessment:
        return CompletionAssessment(
            base_commit="a" * 40,
            completion_commit="b" * 40,
            evidence=evidence,
            gate=CommitGateResult(
                passed=True,
                base_commit="a" * 40,
                completion_commit="b" * 40,
            ),
            verdict=evaluate_completion(spec, evidence),
        )

    return verify


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
            with self.assertRaises(SessionTrackerError):
                t.active_count()


# ─────────────────────────────────────────────────────────────────────────────
#  LAUNCH_QUEUE.json roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestLaunchQueueRoundtrip(unittest.TestCase):

    def test_run_loop_flushes_log_buffer_after_unhandled_exception(self):
        import orchestrator_loop as ol

        flushed = []

        def fail_loop(**_kwargs):
            ol._LOG_BUFFER = ["buffered failure context\n"]
            raise RuntimeError("simulated loop failure")

        with tempfile.TemporaryDirectory() as d, \
             patch.object(ol, "WORK_QUEUE_PATH", Path(d) / "WORK_QUEUE.json"), \
             patch.object(ol, "_run_loop", side_effect=fail_loop), \
             patch.object(ol, "_write_master_lines", side_effect=flushed.append):
            with self.assertRaisesRegex(RuntimeError, "simulated loop failure"):
                ol.run_loop()

        self.assertIsNone(ol._LOG_BUFFER)
        self.assertEqual(flushed, [["buffered failure context\n"]])

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
        contract_id = str(task.get("contract_id") or task.get("id") or "").strip()
        task_spec_sha256 = TaskSpec.from_queue_task(
            task
        ).normalized_task_spec_hash
        contract = (
            TaskContract(
                task_id=contract_id,
                task_type=TaskType.ANALYSIS,
                description="Hermetic loop launch contract",
                task_spec_sha256=task_spec_sha256,
            )
            if contract_id
            else None
        )

        # Patch SessionTracker to use tmp dir
        orig_tracker = ol.SessionTracker

        class _TmpTracker(SessionTracker):
            def __init__(self):
                super().__init__(path=tmp / "ACTIVE_SESSIONS.json")

        ol.SessionTracker = _TmpTracker

        # Patch prompt generator to return a canned prompt
        with patch("harness.prompt_generator._generate_via_llm",
                   return_value="<role>Test session</role>"), \
             patch("orchestrator_loop.contract_for_task", return_value=contract), \
             patch("orchestrator_loop.check_contract_capabilities", return_value={}), \
             patch("orchestrator_loop._ensure_dashboard_running"):
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
        attempt_number: int = 1,
        task_override: dict | None = None,
        repo_path: Path | None = None,
        base_ref: str = "",
        completion_commit: str = "",
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

        task = dict(task_override) if task_override else _sample_task(
            status="in_progress",
            verification_commands=["python -m pytest tests/test_web_search.py -q"],
        )
        task["assigned_to"] = "session-a"
        task["orchestration_state"] = "leased"
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
            attempt_number=attempt_number,
            repo_path=str(repo_path) if repo_path else "",
            base_ref=base_ref,
        )
        tracker.complete(
            "session-a",
            "agent says done",
            evidence=evidence,
            completion_commit=completion_commit or (
                "b" * 40 if repo_path else ""
            ),
        )
        ol.SessionTracker = _TmpTracker

        try:
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]), \
                 patch("orchestrator_loop._ensure_dashboard_running"):
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

    def test_dry_run_never_reports_a_launch(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=True, max_concurrent=3)
            self.assertEqual(result["launched"], 0)

    def test_codex_assigned_task_is_never_launched_by_legacy_loop(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=3)
            queue = json.loads((tmp / "WORK_QUEUE.json").read_text())

            self.assertEqual(result["launched"], 0)
            self.assertEqual(queue[0]["status"], "queued")
            self.assertEqual(queue[0]["orchestrated_by"], "codex")
            self.assertEqual(queue[0]["orchestration_state"], "assigned")
            self.assertFalse((tmp / "LAUNCH_QUEUE.json").exists())
            self.assertFalse((tmp / "ACTIVE_SESSIONS.json").exists())
            self.assertFalse((tmp / "attempts.jsonl").exists())

    def test_unassigned_queued_work_is_never_launched(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            unassigned = _sample_task(status="queued", priority=1)
            unassigned.pop("orchestrated_by")
            unassigned.pop("orchestration_state")
            unassigned.pop("worker_type")
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                result = self._run_loop_isolated(
                    tmp,
                    seed_task=unassigned,
                    dry_run=False,
                    max_concurrent=1,
                )

            queue = json.loads((tmp / "WORK_QUEUE.json").read_text())

            self.assertEqual(result["launched"], 0)
            self.assertEqual(queue[0]["status"], "queued")
            self.assertFalse((tmp / "LAUNCH_QUEUE.json").exists())

    def test_repeated_legacy_polls_never_create_pending_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch("orchestrator_loop._suggest_follow_ups", return_value=[]):
                first = self._run_loop_isolated(
                    tmp, dry_run=False, max_concurrent=3
                )
                second = self._run_loop_isolated(
                    tmp, dry_run=False, max_concurrent=3
                )

            self.assertEqual(first["launched"], 0)
            self.assertEqual(second["launched"], 0)
            self.assertFalse((tmp / "LAUNCH_QUEUE.json").exists())

    def test_max_concurrent_respected(self):
        """With max_concurrent=0 nothing should be launched."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=0)
            self.assertEqual(result["launched"], 0)

    def test_codex_assignment_does_not_create_a_legacy_active_session(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            result = self._run_loop_isolated(tmp, dry_run=False, max_concurrent=1)

            self.assertEqual(result["launched"], 0)
            self.assertEqual(result["active_now"], 0)

    def test_stalled_legacy_session_is_quarantined_as_unverified(self):
        import orchestrator_loop as ol

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            queue_path = tmp / "WORK_QUEUE.json"
            tracker = SessionTracker(path=tmp / "ACTIVE_SESSIONS.json")
            tracker.claim("TASK-001", "legacy-session")
            sessions = tracker._load()
            sessions["sessions"][0]["last_updated"] = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=60)
            ).isoformat()
            tracker._save(sessions)
            queue_path.write_text(json.dumps([
                {
                    "id": "TASK-001",
                    "title": "Legacy work",
                    "description": "A pre-coordinator task",
                    "assigned_ai": "claude",
                    "status": "in_progress",
                    "assigned_to": "legacy-session",
                }
            ]), encoding="utf-8")

            with patch.object(ol, "WORK_QUEUE_PATH", queue_path), \
                 patch.object(ol, "MASTER_LOG_PATH", tmp / "MASTER_LOG.md"):
                expired = ol._expire_stalled_sessions(
                    tracker, timeout_minutes=30
                )

            task = json.loads(queue_path.read_text())[0]
            self.assertEqual(expired, 1)
            self.assertEqual(task["status"], "unverified")
            self.assertIsNone(task["assigned_to"])
            self.assertEqual(
                task["verification_failure_class"], "legacy_stalled_session"
            )
            self.assertNotEqual(task["status"], "queued")

    def test_agent_summary_without_loop_evidence_is_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            result, queue, attempts = self._run_completed_isolated(
                Path(d), evidence=None
            )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["unverified"], 1)
        self.assertEqual(queue[0]["status"], "unverified")
        self.assertEqual(attempts[-2]["phase"], "completion_gate")
        self.assertEqual(attempts[-2]["status"], "unverified")
        self.assertEqual(attempts[-1]["phase"], "retry_policy")
        self.assertEqual(attempts[-1]["status"], "route_to_verifier")

    def test_verified_worker_output_awaits_codex_review_instead_of_done(self):
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
            with patch(
                "orchestrator_loop.verify_completion",
                side_effect=_trusted_assessment(evidence),
            ):
                result, queue, attempts = self._run_completed_isolated(
                    Path(d),
                    evidence=evidence,
                    repo_path=Path(d),
                    base_ref="a" * 40,
                )

        self.assertEqual(result["harvested"], 1)
        self.assertEqual(queue[0]["status"], "awaiting_codex_review")
        self.assertEqual(
            queue[0]["orchestration_state"], "awaiting_codex_review"
        )
        self.assertEqual(queue[0]["candidate_result_summary"], "agent says done")
        self.assertNotIn("completed_at", queue[0])
        self.assertEqual(attempts[-1]["status"], "verified")

    def test_local_followups_stay_unassigned_proposals(self):
        import orchestrator_loop as ol

        model_output = json.dumps([
            {
                "id": "TASK-002",
                "title": "Harden retry telemetry",
                "description": "Add focused retry telemetry coverage.",
                "files_hint": ["harness/web_search.py"],
                "acceptance_criteria": ["retry telemetry is covered"],
                "domain": "harness",
                "priority": 2,
            }
        ])
        mock_ask = MagicMock(return_value=model_output)

        with tempfile.TemporaryDirectory() as d, \
             patch.object(ol, "WORK_QUEUE_PATH", Path(d) / "WORK_QUEUE.json"), \
             patch.dict("sys.modules", {
                 "brains": MagicMock(),
                 "brains.brain_ollama": MagicMock(ask_local=mock_ask),
                 "config": MagicMock(LOCAL_REASONING="qwen3:30b-a3b"),
             }):
            proposals = ol._suggest_follow_ups(_sample_task(status="done"))

        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["proposed_by"], "local")
        self.assertTrue(proposal["requires_codex_assignment"])
        self.assertIsNone(proposal["assigned_ai"])
        self.assertIsNone(proposal["assigned_to"])
        self.assertNotIn("worker_type", proposal)

    def test_legacy_mutators_reject_coordination_v2_tasks(self):
        import orchestrator_loop as ol

        with tempfile.TemporaryDirectory() as d:
            original = ol.WORK_QUEUE_PATH
            ol.WORK_QUEUE_PATH = Path(d) / "WORK_QUEUE.json"
            task = _sample_task(
                status="queued",
                coordination_version=2,
                orchestration_state="assigned",
            )
            ol.WORK_QUEUE_PATH.write_text(json.dumps([task]), encoding="utf-8")
            try:
                with self.assertRaisesRegex(RuntimeError, "agent_coordinator"):
                    ol._mark_task_in_progress("TASK-001", "legacy-session")
            finally:
                ol.WORK_QUEUE_PATH = original

    def test_harvest_rejects_uncommitted_worktree_completion(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            worktree = tmp / "worktree"
            worktree.mkdir()

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", *args],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=30,
                )
                return result.stdout.strip()

            git("init", "-b", "main")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Loop Test")
            (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-m", "base")
            base_ref = git("rev-parse", "HEAD")
            (worktree / "tracked.txt").write_text("done\n", encoding="utf-8")

            command = (
                f"{shlex.quote(sys.executable)} -m compileall -q tracked.txt"
            )
            task = _sample_task(
                status="in_progress",
                files_hint=["tracked.txt"],
                verification_commands=[command],
            )
            result, queue, attempts = self._run_completed_isolated(
                tmp,
                evidence=None,
                task_override=task,
                repo_path=worktree,
                base_ref=base_ref,
                completion_commit=git("rev-parse", "HEAD"),
            )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["retried"], 1)
        self.assertEqual(queue[0]["status"], "queued")
        completion = attempts[-2]
        self.assertEqual(completion["phase"], "completion_gate")
        self.assertEqual(completion["status"], "unverified")
        self.assertEqual(
            completion["failure_class"],
            "infrastructure_failure",
        )
        self.assertEqual(completion["evidence"]["changed_files"], [])

    def test_launch_provenance_replaces_agent_supplied_loop_evidence(self):
        claimed_evidence = {
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
        observed_evidence = {
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
            with patch(
                "orchestrator_loop.verify_completion",
                side_effect=_trusted_assessment(observed_evidence),
            ):
                result, queue, attempts = self._run_completed_isolated(
                    Path(d),
                    evidence=claimed_evidence,
                    repo_path=Path(d),
                    base_ref="a" * 40,
                )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["retried"], 1)
        self.assertEqual(queue[0]["status"], "queued")
        self.assertEqual(
            attempts[-2]["evidence"],
            compact_completion_evidence(observed_evidence),
        )

    def test_failed_loop_verification_queues_retry_within_budget(self):
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
            with patch(
                "orchestrator_loop.verify_completion",
                side_effect=_trusted_assessment(evidence),
            ):
                result, queue, attempts = self._run_completed_isolated(
                    Path(d),
                    evidence=evidence,
                    repo_path=Path(d),
                    base_ref="a" * 40,
                )

        self.assertEqual(result["harvested"], 0)
        self.assertEqual(result["retried"], 1)
        self.assertEqual(queue[0]["status"], "queued")
        self.assertEqual(queue[0]["retry_failure_class"], "test_failure")
        self.assertEqual(queue[0]["attempt_number"], 2)
        self.assertEqual(attempts[-1]["phase"], "retry_policy")
        self.assertEqual(attempts[-1]["status"], "retry")
        self.assertEqual(attempts[-1]["failure_class"], "test_failure")

    def test_failed_loop_verification_escalates_when_attempt_budget_exhausted(self):
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
            with patch(
                "orchestrator_loop.verify_completion",
                side_effect=_trusted_assessment(evidence),
            ):
                result, queue, attempts = self._run_completed_isolated(
                    Path(d),
                    evidence=evidence,
                    attempt_number=3,
                    repo_path=Path(d),
                    base_ref="a" * 40,
                )

        self.assertEqual(result["rejected"], 1)
        self.assertEqual(queue[0]["status"], "blocked")
        self.assertEqual(queue[0]["next_action"], "escalate")
        self.assertEqual(attempts[-2]["attempt_number"], 3)
        self.assertEqual(attempts[-1]["attempt_number"], 3)

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
        self.assertEqual(attempts[-2]["status"], "rejected")
        self.assertEqual(attempts[-1]["status"], "escalate")


if __name__ == "__main__":
    unittest.main()
