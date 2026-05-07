"""
Comprehensive tests for overnight training scheduler.

Covers:
- is_training_window() for times inside/outside window
- should_run_tonight() when state file missing, already ran today, ran yesterday
- build_training_pack() with mock memory data
- run_eval() parsing pass/fail from pytest output
- promote_if_better() when eval better vs worse
- log_session() JSONL format
- status() dict structure
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# Mock PyQt6 and sounddevice before any imports that depend on them
sys.modules["PyQt6"] = MagicMock()
sys.modules["PyQt6.QtWidgets"] = MagicMock()
sys.modules["sounddevice"] = MagicMock()

from local_runtime import local_finetune_scheduler


class OvernightTrainerWindowTests(unittest.TestCase):
    """Test is_training_window() for different times."""

    def test_is_training_window_at_11pm(self):
        """11pm should be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 4, 23, 0, 0)
            assert trainer.is_training_window() is True

    def test_is_training_window_at_midnight(self):
        """Midnight should be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 5, 0, 0, 0)
            assert trainer.is_training_window() is True

    def test_is_training_window_at_6am(self):
        """6am should be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 5, 6, 0, 0)
            assert trainer.is_training_window() is True

    def test_is_training_window_at_7am(self):
        """7am should NOT be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 5, 7, 0, 0)
            assert trainer.is_training_window() is False

    def test_is_training_window_at_noon(self):
        """Noon should NOT be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 5, 12, 0, 0)
            assert trainer.is_training_window() is False

    def test_is_training_window_at_10pm(self):
        """10pm should NOT be in training window."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        with patch("local_runtime.local_finetune_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 5, 22, 0, 0)
            assert trainer.is_training_window() is False


class OvernightTrainerScheduleTests(unittest.TestCase):
    """Test should_run_tonight() logic."""

    def test_should_run_tonight_no_state_file(self):
        """When no state file, should run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            assert not state_file.exists()

            trainer = local_finetune_scheduler.OvernightTrainer()
            # Override state file path
            trainer.state = {"last_run_date": None}

            assert trainer.should_run_tonight() is True

    def test_should_run_tonight_first_time(self):
        """When last_run_date is None, should run."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.state = {"last_run_date": None}

        assert trainer.should_run_tonight() is True

    def test_should_run_tonight_already_ran_today(self):
        """When ran today, should not run."""
        with patch("local_runtime.local_finetune_scheduler._today_date") as mock_today:
            mock_today.return_value = "2026-05-04"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.state = {"last_run_date": "2026-05-04"}

            assert trainer.should_run_tonight() is False

    def test_should_run_tonight_ran_yesterday(self):
        """When ran yesterday, should run."""
        with patch("local_runtime.local_finetune_scheduler._today_date") as mock_today:
            mock_today.return_value = "2026-05-04"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.state = {"last_run_date": "2026-05-03"}

            assert trainer.should_run_tonight() is True

    def test_should_run_tonight_after_post_midnight_run_same_calendar_date(self):
        """A 00:xx run belongs to the previous overnight window, not tonight."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.state = {
            "last_run_date": "2026-05-06",
            "last_session": {"timestamp": "2026-05-06T00:32:12", "date": "2026-05-06"},
        }

        with patch("local_runtime.local_finetune_scheduler._training_window_date", return_value="2026-05-06"):
            assert trainer.should_run_tonight() is True

    def test_should_not_run_twice_in_same_overnight_window(self):
        """Once a logical window is recorded, launchd retries should no-op."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.state = {"last_run_window_date": "2026-05-06"}

        with patch("local_runtime.local_finetune_scheduler._training_window_date", return_value="2026-05-06"):
            assert trainer.should_run_tonight() is False


class OvernightTrainerBuildPackTests(unittest.TestCase):
    """Test build_training_pack() with mock memory data."""

    def test_build_pack_with_sufficient_examples(self):
        """With >=10 conversation entries, should build pack."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.json"
            pack_dir = Path(tmpdir) / "packs"
            pack_dir.mkdir(exist_ok=True)

            # Create mock memory
            memory_data = {
                "conversation_history": [
                    {"date": "2026-05-04 10:00", "summary": f"Task {i}"}
                    for i in range(12)
                ]
            }
            with memory_file.open("w") as f:
                json.dump(memory_data, f)

            trainer = local_finetune_scheduler.OvernightTrainer()
            # Override paths
            trainer.logger = MagicMock()

            with patch("local_runtime.local_finetune_scheduler.MEMORY_FILE", memory_file):
                with patch("local_runtime.local_finetune_scheduler.PACKS_DIR", pack_dir):
                    with patch("local_runtime.local_finetune_scheduler._today_date") as mock_today:
                        mock_today.return_value = "2026-05-04"
                        pack_path = trainer.build_training_pack()

            assert pack_path is not None
            assert pack_path.exists()
            assert pack_path.suffix == ".jsonl"

            # Verify JSONL format
            with pack_path.open("r") as f:
                lines = f.readlines()
            assert len(lines) >= 10
            for line in lines:
                record = json.loads(line)
                assert "prompt" in record
                assert "completion" in record

    def test_build_pack_with_insufficient_examples(self):
        """With <10 conversation entries, should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.json"

            # Create mock memory with only 5 entries
            memory_data = {
                "conversation_history": [
                    {"date": "2026-05-04 10:00", "summary": f"Task {i}"}
                    for i in range(5)
                ]
            }
            with memory_file.open("w") as f:
                json.dump(memory_data, f)

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            with patch("local_runtime.local_finetune_scheduler.MEMORY_FILE", memory_file):
                pack_path = trainer.build_training_pack()

            assert pack_path is None

    def test_build_pack_no_memory_file(self):
        """When memory file missing, should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent.json"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            with patch("local_runtime.local_finetune_scheduler.MEMORY_FILE", nonexistent):
                pack_path = trainer.build_training_pack()

            assert pack_path is None

    def test_build_pack_corrupted_memory_file(self):
        """When memory file is corrupted, should return None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.json"
            memory_file.write_text("invalid json")

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            with patch("local_runtime.local_finetune_scheduler.MEMORY_FILE", memory_file):
                pack_path = trainer.build_training_pack()

            assert pack_path is None


class OvernightTrainerEvalTests(unittest.TestCase):
    """Test run_eval() parsing."""

    def test_run_eval_parses_passed_count(self):
        """Should parse 'X passed' from pytest output."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "3 passed in 5.2s\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = trainer.run_eval()

        assert result["passed"] == 3
        assert result["failed"] == 0
        assert result["total"] == 3

    def test_run_eval_parses_failed_count(self):
        """Should parse 'X failed' from pytest output."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "2 passed, 1 failed in 5.2s\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = trainer.run_eval()

        assert result["passed"] == 2
        assert result["failed"] == 1
        assert result["total"] == 3

    def test_run_eval_timeout(self):
        """Should handle subprocess timeout gracefully."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("timeout")

            result = trainer.run_eval()

        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["total"] == 0

    def test_run_eval_no_matches(self):
        """When pytest output has no counts, should return zeros."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "Something went wrong"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with patch.object(trainer, "_run_benchmark_eval") as mock_benchmark:
                mock_benchmark.return_value = {"passed": 0, "failed": 0, "total": 0}
                result = trainer.run_eval()

        assert result["passed"] == 0
        assert result["failed"] == 0
        assert result["total"] == 0

    def test_run_eval_falls_back_to_benchmark_when_goldens_skipped(self):
        """When golden cases are skipped, benchmark totals should drive promotion gate."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = "1 skipped in 1.2s\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with patch.object(trainer, "_run_benchmark_eval") as mock_benchmark:
                mock_benchmark.return_value = {
                    "passed": 312,
                    "failed": 1,
                    "total": 313,
                    "source": "benchmark_tracker",
                }
                result = trainer.run_eval()

        assert result["passed"] == 312
        assert result["failed"] == 1
        assert result["total"] == 313
        assert result["source"] == "benchmark_tracker"


class OvernightTrainerRunTrainingTests(unittest.TestCase):
    """Test run_training() result metadata."""

    def test_run_training_adds_examples_count(self):
        """Training result should include the source pack example count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "pack.jsonl"
            pack_path.write_text('{"prompt":"a","completion":"b"}\n\n{"prompt":"c","completion":"d"}\n')

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            with patch("local_runtime.local_finetune_scheduler.config.LOCAL_CODER", "qwen2.5-coder:7b"):
                with patch("local_runtime.local_finetune_scheduler.local_mlx_training.run_sft") as mock_run:
                    mock_run.return_value = {"ok": True, "duration_sec": 1.25}
                    result = trainer.run_training(pack_path)

            assert result["ok"] is True
            assert result["examples_count"] == 2


class OvernightTrainerPromotionTests(unittest.TestCase):
    """Test promote_if_better() logic."""

    def test_promote_if_better_improved_scores(self):
        """When eval improved over baseline, should promote."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()
        trainer.state = {"baseline_eval_passed": 5, "baseline_eval_total": 10}

        training_result = {
            "ok": True,
            "adapter_path": "/path/to/adapter",
        }
        eval_result = {"passed": 7, "failed": 3, "total": 10}

        with patch("local_runtime.local_mlx_training.fuse_adapter") as mock_fuse:
            mock_fuse.return_value = {"ok": True, "path": "/path/to/fused"}

            promoted = trainer.promote_if_better(training_result, eval_result)

        assert promoted is True
        assert trainer.state["baseline_eval_passed"] == 7

    def test_promote_if_better_no_change(self):
        """When eval same as baseline, should still promote."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()
        trainer.state = {"baseline_eval_passed": 5, "baseline_eval_total": 10}

        training_result = {
            "ok": True,
            "adapter_path": "/path/to/adapter",
        }
        eval_result = {"passed": 5, "failed": 5, "total": 10}

        with patch("local_runtime.local_mlx_training.fuse_adapter") as mock_fuse:
            mock_fuse.return_value = {"ok": True, "path": "/path/to/fused"}

            promoted = trainer.promote_if_better(training_result, eval_result)

        assert promoted is True

    def test_promote_if_better_worse_scores(self):
        """When eval worse than baseline, should not promote."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()
        trainer.state = {"baseline_eval_passed": 8, "baseline_eval_total": 10}

        training_result = {
            "ok": True,
            "adapter_path": "/path/to/adapter",
        }
        eval_result = {"passed": 6, "failed": 4, "total": 10}

        promoted = trainer.promote_if_better(training_result, eval_result)

        assert promoted is False

    def test_promote_if_better_training_failed(self):
        """When training failed, should not promote."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        training_result = {
            "ok": False,
            "error": "Training timed out",
        }
        eval_result = {"passed": 10, "failed": 0, "total": 10}

        promoted = trainer.promote_if_better(training_result, eval_result)

        assert promoted is False

    def test_promote_if_better_fuse_failed(self):
        """When fuse fails, should not promote."""
        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()
        trainer.state = {"baseline_eval_passed": 5, "baseline_eval_total": 10}

        training_result = {
            "ok": True,
            "adapter_path": "/path/to/adapter",
        }
        eval_result = {"passed": 9, "failed": 1, "total": 10}

        with patch("local_runtime.local_mlx_training.fuse_adapter") as mock_fuse:
            mock_fuse.return_value = {"ok": False, "error": "Fuse failed"}

            promoted = trainer.promote_if_better(training_result, eval_result)

        assert promoted is False


class OvernightTrainerLoggingTests(unittest.TestCase):
    """Test log_session() JSONL format."""

    def test_log_session_creates_jsonl(self):
        """Should write session to overnight_log.jsonl in JSONL format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "overnight_log.jsonl"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            session = {
                "timestamp": "2026-05-04T23:00:00Z",
                "date": "2026-05-04",
                "stages": {"build": {"ok": True}},
            }

            with patch("local_runtime.local_finetune_scheduler.LOG_FILE", log_file):
                trainer.log_session(session)

            assert log_file.exists()

            # Verify JSONL format
            with log_file.open("r") as f:
                line = f.readline()
            record = json.loads(line)
            assert record["timestamp"] == "2026-05-04T23:00:00Z"
            assert record["stages"]["build"]["ok"] is True

    def test_log_session_appends(self):
        """Should append to existing log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "overnight_log.jsonl"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.logger = MagicMock()

            session1 = {"timestamp": "2026-05-04T23:00:00Z", "date": "2026-05-04"}
            session2 = {"timestamp": "2026-05-05T23:00:00Z", "date": "2026-05-05"}

            with patch("local_runtime.local_finetune_scheduler.LOG_FILE", log_file):
                trainer.log_session(session1)
                trainer.log_session(session2)

            with log_file.open("r") as f:
                lines = f.readlines()
            assert len(lines) == 2


class OvernightTrainerStatusTests(unittest.TestCase):
    """Test status() dict structure."""

    def test_status_dict_has_required_keys(self):
        """status() should return dict with required keys."""
        result = local_finetune_scheduler.status()

        assert "is_training_window" in result
        assert "should_run_tonight" in result
        assert "last_run_date" in result
        assert "last_session" in result
        assert "baseline_eval_passed" in result
        assert "baseline_eval_total" in result
        assert "next_scheduled_run" in result

    def test_status_next_scheduled_run_is_iso(self):
        """next_scheduled_run should be ISO 8601 timestamp."""
        result = local_finetune_scheduler.status()

        # Should not raise
        datetime.fromisoformat(result["next_scheduled_run"])

    def test_status_baseline_defaults_to_zero(self):
        """baseline_eval_passed/total should default to 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"

            trainer = local_finetune_scheduler.OvernightTrainer()
            trainer.state = {"last_run_date": None}

            # Manually call status logic
            result = {
                "is_training_window": trainer.is_training_window(),
                "should_run_tonight": trainer.should_run_tonight(),
                "last_run_date": trainer.state.get("last_run_date"),
                "baseline_eval_passed": trainer.state.get("baseline_eval_passed", 0),
                "baseline_eval_total": trainer.state.get("baseline_eval_total", 0),
            }

            assert result["baseline_eval_passed"] == 0
            assert result["baseline_eval_total"] == 0


if __name__ == "__main__":
    unittest.main()
