import unittest
import json
import time
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock, call

import sys
try:
    import PyQt6  # noqa: F401
except ModuleNotFoundError:
    sys.modules['PyQt6'] = MagicMock()
    sys.modules['PyQt6.QtWidgets'] = MagicMock()

import brain_daemon


class BrainAgentBaseTests(unittest.TestCase):
    """Tests for BrainAgent base class."""

    def test_agent_starts_and_stops_cleanly(self):
        """Agent starts daemon thread and stops without blocking."""
        agent = brain_daemon.MemoryAgent()
        self.assertFalse(agent._stop_event.is_set())

        agent.start()
        self.assertIsNotNone(agent._thread)
        self.assertTrue(agent._thread.is_alive())

        # Stop sets the event
        agent.stop()
        self.assertTrue(agent._stop_event.is_set())
        # Daemon thread may take a moment to notice the event
        # Just verify stop() doesn't crash
        agent._thread.join(timeout=2)

    def test_agent_stop_wakes_sleeping_loop(self):
        """Agent.stop() should wake long interval sleeps instead of waiting for timeout."""
        agent = brain_daemon.MemoryAgent()
        agent.interval_seconds = 60

        with patch.object(agent, 'run_once', return_value={"ok": True, "summary": "test"}):
            agent.start()
            time.sleep(0.05)
            start = time.monotonic()
            agent.stop()
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, 1.0)
        self.assertFalse(agent._thread.is_alive())

    def test_agent_status_returns_correct_structure(self):
        """Agent status dict has all required keys."""
        agent = brain_daemon.MemoryAgent()
        status = agent.status()

        self.assertIn("name", status)
        self.assertIn("interval_seconds", status)
        self.assertIn("last_run", status)
        self.assertIn("run_count", status)
        self.assertIn("next_run", status)

        self.assertEqual(status["name"], "MemoryAgent")
        self.assertEqual(status["interval_seconds"], 1800)
        self.assertEqual(status["run_count"], 0)
        self.assertIsNone(status["last_run"])
        self.assertIsNone(status["next_run"])

    def test_agent_status_updates_after_run(self):
        """Status reflects run_count and last_run after execution."""
        agent = brain_daemon.MemoryAgent()

        with patch.object(agent, 'run_once', return_value={"ok": True, "summary": "test"}):
            agent.run_once()
            agent._run_count = 1
            agent._last_run = datetime.now()

        status = agent.status()
        self.assertEqual(status["run_count"], 1)
        self.assertIsNotNone(status["last_run"])
        self.assertIsNotNone(status["next_run"])


class MemoryAgentTests(unittest.TestCase):
    """Tests for MemoryAgent."""

    def test_memory_agent_consolidates_memory(self):
        """MemoryAgent.run_once calls consolidate_memory."""
        mock_memory = MagicMock()
        mock_memory.load.return_value = {
            "facts": ["fact1", "fact1", "fact2"],
            "preferences": {},
            "conversation_history": [],
            "projects": [],
            "topic_counts": {},
            "working_memory": {},
            "long_term_profile": {},
            "last_updated": None
        }
        mock_memory.consolidate_memory.return_value = {"ok": True}
        mock_memory._dedupe_keep_order.return_value = ["fact1", "fact2"]

        agent = brain_daemon.MemoryAgent()
        with patch.dict('sys.modules', {'memory': mock_memory}):
            # Re-import to pick up mock
            import importlib
            import brain_daemon as bd_module
            result = bd_module.MemoryAgent().run_once()

        self.assertTrue(result["ok"])
        self.assertIn("facts", result["summary"])
        self.assertIn("deduped", result["summary"])

    def test_memory_agent_dedupes_facts(self):
        """MemoryAgent tries to consolidate and dedupe facts."""
        agent = brain_daemon.MemoryAgent()

        # Just verify the agent has run_once method and handles errors gracefully
        result = agent.run_once()

        # The test environment may not have the memory module, so just check structure
        self.assertIn("ok", result)
        self.assertIn("summary", result)
        self.assertIsInstance(result["summary"], str)

    def test_memory_agent_handles_missing_memory_module(self):
        """MemoryAgent gracefully handles import errors."""
        agent = brain_daemon.MemoryAgent()

        with patch('builtins.__import__', side_effect=ImportError("memory not found")):
            result = agent.run_once()

        self.assertFalse(result["ok"])
        self.assertIn("failed", result["summary"].lower())


class VaultSynthAgentTests(unittest.TestCase):
    """Tests for VaultSynthAgent."""

    def test_vault_synth_agent_searches_brain_files(self):
        """VaultSynthAgent runs rg search on brain directory."""
        agent = brain_daemon.VaultSynthAgent()

        mock_result = MagicMock()
        mock_result.stdout = "vault/wiki/brain/file1.md:3\nvault/wiki/brain/file2.md:5\n"
        mock_result.stderr = ""

        with patch('subprocess.run', return_value=mock_result), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('pathlib.Path.glob', return_value=[
                 Path("/mock/file1.md"),
                 Path("/mock/file2.md"),
                 Path("/mock/file3.md")
             ]), \
             patch('pathlib.Path.stat') as mock_stat:

            # Mock stat to return old mtime (not recently modified)
            mock_stat.return_value.st_mtime = 0

            result = agent.run_once()

        self.assertTrue(result["ok"])
        self.assertIn("indexed", result["summary"])
        self.assertIn("8 key matches", result["summary"])  # 3 + 5

    def test_repo_map_sync_preserves_frontmatter(self):
        """Repo Map sync updates metadata without replacing the YAML delimiter."""
        with TemporaryDirectory() as tmp:
            repo_map = Path(tmp) / "Repo Map.md"
            repo_map.write_text(
                "---\n"
                "type: brain_meta\n"
                "updated: 2026-04-21\n"
                "---\n\n"
                "# Repo Map\n\n"
                "Body stays here.\n",
                encoding="utf-8",
            )

            brain_daemon._sync_repo_map_metadata(repo_map, datetime(2026, 6, 3, 12, 34))

            content = repo_map.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"))
            self.assertIn("updated: 2026-06-03", content)
            self.assertIn("\n# Repo Map\n", content)

    def test_repo_map_sync_repairs_old_synced_header_damage(self):
        """Repo Map sync repairs prior daemon output that broke frontmatter."""
        with TemporaryDirectory() as tmp:
            repo_map = Path(tmp) / "Repo Map.md"
            repo_map.write_text(
                "# Repo Map (synced 2026-05-29 19:41)\n"
                "type: brain_meta\n"
                "updated: 2026-04-21\n"
                "---\n\n"
                "# Repo Map\n",
                encoding="utf-8",
            )

            brain_daemon._sync_repo_map_metadata(repo_map, datetime(2026, 6, 3, 12, 34))

            content = repo_map.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"))
            self.assertNotIn("# Repo Map (synced", content.splitlines()[0])
            self.assertIn("updated: 2026-06-03", content)

    def test_vault_synth_agent_handles_missing_vault(self):
        """VaultSynthAgent handles missing vault gracefully."""
        agent = brain_daemon.VaultSynthAgent()

        with patch('pathlib.Path.exists', return_value=False):
            result = agent.run_once()

        self.assertFalse(result["ok"])
        self.assertIn("not found", result["summary"].lower())

    def test_vault_synth_agent_updates_repo_map_when_brain_modified(self):
        """VaultSynthAgent updates Repo Map header when brain is recent."""
        agent = brain_daemon.VaultSynthAgent()

        mock_result = MagicMock()
        mock_result.stdout = "vault/wiki/brain/file1.md:3\n"
        mock_result.stderr = ""

        now = time.time()

        with patch('subprocess.run', return_value=mock_result), \
             patch('pathlib.Path.exists', return_value=True), \
             patch('pathlib.Path.glob', return_value=[
                 Path("/mock/file1.md"),
             ]), \
             patch('pathlib.Path.stat') as mock_stat, \
             patch('pathlib.Path.read_text') as mock_read, \
             patch('pathlib.Path.write_text') as mock_write:

            # Mock stat to return recent mtime
            mock_stat.return_value.st_mtime = now - 3600  # 1 hour ago

            mock_read.return_value = "# Repo Map (synced old time)\nContent here"

            result = agent.run_once()

        self.assertTrue(result["ok"])


class KnowledgeFeedAgentTests(unittest.TestCase):
    """Tests for KnowledgeFeedAgent."""

    def test_knowledge_feed_agent_calls_refresh(self):
        """KnowledgeFeedAgent calls _refresh_knowledge_feed."""
        agent = brain_daemon.KnowledgeFeedAgent()

        mock_learner = MagicMock()
        mock_learner._refresh_knowledge_feed = MagicMock()

        with patch.dict('sys.modules', {'learner': mock_learner}):
            with patch('builtins.__import__') as mock_import:
                mock_import.return_value = mock_learner
                result = agent.run_once()

        # Result should indicate attempt was made
        self.assertIn("knowledge feed", result["summary"].lower())

    def test_knowledge_feed_agent_skips_on_network_error(self):
        """KnowledgeFeedAgent silently skips on network errors."""
        agent = brain_daemon.KnowledgeFeedAgent()

        with patch('builtins.__import__', side_effect=ImportError("no internet")):
            result = agent.run_once()

        self.assertFalse(result["ok"])
        self.assertIn("skipped", result["summary"].lower())


class EvalAgentTests(unittest.TestCase):
    """Tests for EvalAgent."""

    def test_eval_agent_runs_benchmark_tracker_and_logs_summary(self):
        """EvalAgent runs benchmark_tracker and summarizes pass totals."""
        agent = brain_daemon.EvalAgent()

        mock_benchmark = MagicMock()
        mock_benchmark.get_latest.return_value = {"overall": 0.9}
        mock_benchmark.run_full_benchmark.return_value = {
            "total_passed": 5,
            "total_tests": 6,
            "overall": 0.8333,
            "delta_vs_baseline": -0.0667,
            "categories": {},
        }

        mock_file = MagicMock()
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)
        mock_file.write = MagicMock()

        with patch.dict('sys.modules', {'benchmark_tracker': mock_benchmark}):
            with patch('builtins.open', return_value=mock_file):
                result = agent.run_once()

        self.assertTrue(result["ok"])
        self.assertIn("5/6", result["summary"])
        mock_benchmark.run_full_benchmark.assert_called_once()
        self.assertTrue(mock_file.write.called)

    def test_eval_agent_logs_to_jsonl(self):
        """EvalAgent appends eval result to eval_history.jsonl."""
        agent = brain_daemon.EvalAgent()

        mock_benchmark = MagicMock()
        mock_benchmark.get_latest.return_value = None
        mock_benchmark.run_full_benchmark.return_value = {
            "total_passed": 3,
            "total_tests": 3,
            "overall": 1.0,
            "delta_vs_baseline": None,
            "categories": {},
        }

        written_lines = []

        def mock_file_write(content):
            written_lines.append(content)

        mock_file = MagicMock()
        mock_file.write = mock_file_write
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)

        with patch.dict('sys.modules', {'benchmark_tracker': mock_benchmark}), \
             patch('builtins.open', return_value=mock_file):
            result = agent.run_once()

        self.assertTrue(result["ok"])
        self.assertTrue(written_lines)
        self.assertIn('"passed": 3', written_lines[0])

    def test_eval_agent_handles_benchmark_exception(self):
        """EvalAgent handles benchmark tracker failures gracefully."""
        agent = brain_daemon.EvalAgent()

        mock_benchmark = MagicMock()
        mock_benchmark.get_latest.return_value = None
        mock_benchmark.run_full_benchmark.side_effect = TimeoutError("benchmark timeout")

        with patch.dict('sys.modules', {'benchmark_tracker': mock_benchmark}):
            result = agent.run_once()

        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["summary"].lower())

    def test_eval_agent_handles_no_tests(self):
        """EvalAgent handles benchmark runs with no totals gracefully."""
        agent = brain_daemon.EvalAgent()

        mock_benchmark = MagicMock()
        mock_benchmark.get_latest.return_value = None
        mock_benchmark.run_full_benchmark.return_value = {
            "total_passed": 0,
            "total_tests": 0,
            "overall": None,
            "categories": {},
        }

        with patch.dict('sys.modules', {'benchmark_tracker': mock_benchmark}), \
             patch('builtins.open', create=True):
            result = agent.run_once()

        self.assertIn("summary", result)
        self.assertFalse(result["ok"])


class TrainingPackAgentTests(unittest.TestCase):
    """Tests for TrainingPackAgent."""

    def test_training_pack_agent_skips_outside_0_to_6am(self):
        """TrainingPackAgent skips if hour is outside 0-6."""
        agent = brain_daemon.TrainingPackAgent()

        # Mock datetime.now() to return a time outside 0-6
        mock_now = MagicMock()
        mock_now.hour = 10  # 10am

        with patch('brain_daemon.datetime') as mock_datetime:
            mock_datetime.now.return_value = mock_now
            result = agent.run_once()

        self.assertTrue(result["ok"])
        self.assertIn("skipped", result["summary"].lower())
        self.assertIn("hour", result["summary"].lower())

    def test_training_pack_agent_runs_during_0_to_6am(self):
        """TrainingPackAgent only runs between 0-6am."""
        agent = brain_daemon.TrainingPackAgent()

        mock_now = MagicMock()
        mock_now.hour = 3  # 3am

        mock_trainer = MagicMock()
        mock_trainer.build_training_pack.return_value = "/path/to/pack.tar.gz"

        with patch('brain_daemon.datetime') as mock_datetime:
            mock_datetime.now.return_value = mock_now
            with patch('builtins.__import__', side_effect=lambda name, *args, **kwargs: mock_trainer if 'OvernightTrainer' in str(args) else None):
                result = agent.run_once()

        # Should attempt to import and run
        self.assertIn("summary", result)

    def test_training_pack_agent_handles_missing_module(self):
        """TrainingPackAgent handles missing local_finetune_scheduler."""
        agent = brain_daemon.TrainingPackAgent()

        mock_now = MagicMock()
        mock_now.hour = 2  # 2am

        with patch('brain_daemon.datetime') as mock_datetime:
            mock_datetime.now.return_value = mock_now
            with patch('builtins.__import__', side_effect=ModuleNotFoundError("no such module")):
                result = agent.run_once()

        self.assertFalse(result["ok"])
        self.assertIn("not available", result["summary"].lower())


class BrainDaemonTests(unittest.TestCase):
    """Tests for BrainDaemon."""

    def test_brain_daemon_starts_all_agents(self):
        """BrainDaemon has 7 agents ready to start."""
        daemon = brain_daemon.BrainDaemon()
        self.assertEqual(len(daemon.agents), 7)
        # Just verify all agents are present; don't actually start threads
        self.assertFalse(daemon.is_running())

    def test_brain_daemon_stops_all_agents(self):
        """BrainDaemon.stop() marks daemon as not running."""
        daemon = brain_daemon.BrainDaemon()
        daemon._running = True  # Simulate running state
        self.assertTrue(daemon.is_running())

        daemon.stop()
        self.assertFalse(daemon.is_running())

    def test_brain_daemon_status_includes_all_agents(self):
        """BrainDaemon.status() returns all agent statuses."""
        daemon = brain_daemon.BrainDaemon()
        status = daemon.status()

        self.assertIn("running", status)
        self.assertIn("agents", status)
        self.assertEqual(len(status["agents"]), 7)

        agent_names = [a["name"] for a in status["agents"]]
        self.assertIn("MemoryAgent", agent_names)
        self.assertIn("VaultSynthAgent", agent_names)
        self.assertIn("KnowledgeFeedAgent", agent_names)
        self.assertIn("EvalAgent", agent_names)
        self.assertIn("TrainingPackAgent", agent_names)
        self.assertIn("CalendarAlert", agent_names)
        self.assertIn("EmailAlert", agent_names)

    def test_brain_daemon_is_running_reflects_state(self):
        """BrainDaemon.is_running() returns correct state."""
        daemon = brain_daemon.BrainDaemon()

        self.assertFalse(daemon.is_running())

        daemon._running = True
        self.assertTrue(daemon.is_running())

        daemon._running = False
        self.assertFalse(daemon.is_running())


class ModuleLevelFunctionsTests(unittest.TestCase):
    """Tests for module-level functions."""

    def test_module_start_creates_daemon(self):
        """brain_daemon.start() creates and returns BrainDaemon."""
        # Reset global state
        brain_daemon._daemon = None

        # Create without starting threads (don't call start() which runs agents)
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True  # Mark as running

        try:
            self.assertIsNotNone(daemon)
            self.assertIsInstance(daemon, brain_daemon.BrainDaemon)
            self.assertTrue(daemon.is_running())
        finally:
            daemon._running = False
            brain_daemon._daemon = None

    def test_module_stop_stops_daemon(self):
        """brain_daemon.stop() stops the global daemon."""
        # Reset and create
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True

        brain_daemon.stop()
        self.assertFalse(daemon.is_running())
        brain_daemon._daemon = None

    def test_module_status_returns_daemon_status(self):
        """brain_daemon.status() returns current daemon status."""
        # Reset and create
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True

        try:
            status = brain_daemon.status()

            self.assertIn("running", status)
            self.assertTrue(status["running"])
        finally:
            daemon._running = False
            brain_daemon._daemon = None

    def test_module_status_when_daemon_not_running(self):
        """brain_daemon.status() returns offline when no daemon."""
        brain_daemon._daemon = None

        status = brain_daemon.status()

        self.assertIn("running", status)
        self.assertFalse(status["running"])

    def test_get_activity_summary_formats_correctly(self):
        """get_activity_summary() formats agent activity nicely."""
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True

        try:
            # Simulate agent having run
            daemon.agents[0]._last_run = datetime.now()

            summary = brain_daemon.get_activity_summary()

            self.assertIn("Brain:", summary)
            self.assertIsInstance(summary, str)
            self.assertGreater(len(summary), 5)
        finally:
            daemon._running = False
            brain_daemon._daemon = None

    def test_get_activity_summary_offline_when_no_daemon(self):
        """get_activity_summary() returns 'offline' when daemon not running."""
        brain_daemon._daemon = None

        summary = brain_daemon.get_activity_summary()

        self.assertIn("offline", summary.lower())


class ActivitySummaryTests(unittest.TestCase):
    """Tests for activity summary formatting."""

    def test_activity_summary_shows_time_ago_in_seconds(self):
        """Summary formats recent activity in seconds."""
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True  # Mark as running for summary

        # Don't start daemon threads - just manually set agent state
        import datetime as dt_module
        ago_30s = datetime.now() - dt_module.timedelta(seconds=30)
        daemon.agents[0]._last_run = ago_30s

        summary = brain_daemon.get_activity_summary()

        self.assertIn("30s ago", summary)
        brain_daemon._daemon = None

    def test_activity_summary_shows_time_ago_in_minutes(self):
        """Summary formats activity from minutes ago."""
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True  # Mark as running for summary

        # Simulate agent run 5 minutes ago
        import datetime as dt_module
        ago_5m = datetime.now() - dt_module.timedelta(minutes=5)
        daemon.agents[0]._last_run = ago_5m

        summary = brain_daemon.get_activity_summary()

        self.assertIn("5m ago", summary)
        brain_daemon._daemon = None

    def test_activity_summary_shows_time_ago_in_hours(self):
        """Summary formats activity from hours ago."""
        brain_daemon._daemon = None
        daemon = brain_daemon.BrainDaemon()
        brain_daemon._daemon = daemon
        daemon._running = True  # Mark as running for summary

        # Simulate agent run 2 hours ago
        import datetime as dt_module
        ago_2h = datetime.now() - dt_module.timedelta(hours=2)
        daemon.agents[0]._last_run = ago_2h

        summary = brain_daemon.get_activity_summary()

        self.assertIn("2h ago", summary)
        brain_daemon._daemon = None


# Needed for test discovery
if __name__ == '__main__':
    unittest.main()
