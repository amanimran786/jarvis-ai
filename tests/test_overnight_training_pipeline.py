"""Focused tests for the overnight training pipeline fixes."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_runtime import local_finetune_scheduler


def test_run_eval_falls_back_to_benchmark_when_goldens_skipped():
    """Skipped live goldens should not produce a blind 0/0 promotion gate."""
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


def test_run_training_adds_examples_count():
    """Training result should include source pack row count for dashboard history."""
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

