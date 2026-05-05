"""Focused tests for the overnight training pipeline fixes."""

from __future__ import annotations

import tempfile
import json
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


def test_status_repairs_stale_state_from_successful_log():
    """Status should not trust stale overnight_state.json when the log has a promoted run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state_path = root / "overnight_state.json"
        log_path = root / "overnight_log.jsonl"
        state_path.write_text(json.dumps({"baseline_eval_passed": 5, "baseline_eval_total": 10}))
        log_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-05T06:35:16Z",
                    "date": "2026-05-04",
                    "stages": {
                        "training": {"ok": True, "examples_count": 30, "duration_sec": 17.5},
                        "eval": {"passed": 312, "failed": 1, "total": 313},
                        "promotion": {"promoted": True},
                    },
                    "promoted": True,
                }
            )
            + "\n"
        )

        with patch("local_runtime.local_finetune_scheduler.STATE_FILE", state_path), \
             patch("local_runtime.local_finetune_scheduler.LOG_FILE", log_path):
            trainer = local_finetune_scheduler.OvernightTrainer()

    assert trainer.state["last_run_date"] == "2026-05-04"
    assert trainer.state["baseline_eval_passed"] == 312
    assert trainer.state["baseline_eval_total"] == 313
    assert trainer.state["last_session"]["examples_count"] == 30
    assert trainer.state["last_session"]["duration_seconds"] == 17.5


def test_auto_commit_artifacts_uses_pathspec_to_avoid_staged_work():
    """Scheduled artifact commits must not sweep unrelated staged agent work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for rel_path in (
            "training/overnight_log.jsonl",
            "training/benchmarks.jsonl",
            "training/dashboard.html",
            "training/overnight_state.json",
        ):
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

        trainer = local_finetune_scheduler.OvernightTrainer.__new__(
            local_finetune_scheduler.OvernightTrainer
        )
        trainer.logger = MagicMock()

        with patch("local_runtime.local_finetune_scheduler.REPO_ROOT", root), \
             patch("local_runtime.local_finetune_scheduler._today_date", return_value="2026-05-05"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            trainer._auto_commit_artifacts(promoted=True)

    add_args = mock_run.call_args_list[0].args[0]
    commit_args = mock_run.call_args_list[1].args[0]
    artifact_paths = [
        "training/overnight_log.jsonl",
        "training/benchmarks.jsonl",
        "training/dashboard.html",
        "training/overnight_state.json",
    ]

    assert add_args == ["git", "add", *artifact_paths]
    assert commit_args == [
        "git",
        "commit",
        "-m",
        "chore(training): overnight artifacts 2026-05-05 [promoted]",
        "--",
        *artifact_paths,
    ]


def test_teacher_examples_use_current_system_prompt(tmp_path):
    """Teacher files should not smuggle stale capability claims into training."""
    teacher_dir = tmp_path / "teacher_examples"
    teacher_dir.mkdir()
    (teacher_dir / "example.jsonl").write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "stale prompt with sudo claims"},
                    {"role": "user", "content": "What is 7 * 8?"},
                    {"role": "assistant", "content": "Fifty-six."},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trainer = local_finetune_scheduler.OvernightTrainer.__new__(
        local_finetune_scheduler.OvernightTrainer
    )
    trainer.logger = MagicMock()

    with patch("local_runtime.local_finetune_scheduler.TRAINING_ROOT", tmp_path), \
         patch("local_runtime.local_finetune_scheduler.config.SYSTEM_PROMPT", "canonical Jarvis prompt"):
        examples = trainer._collect_teacher_examples()

    assert len(examples) == 1
    assert examples[0]["messages"][0] == {
        "role": "system",
        "content": "canonical Jarvis prompt",
    }
    assert "sudo claims" not in json.dumps(examples[0])


def test_teacher_examples_get_system_prompt_when_missing(tmp_path):
    """Messages-format examples without a system row still get the canonical prompt."""
    teacher_dir = tmp_path / "teacher_examples"
    teacher_dir.mkdir()
    (teacher_dir / "example.jsonl").write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "What is 2 + 2?"},
                    {"role": "assistant", "content": "Four."},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trainer = local_finetune_scheduler.OvernightTrainer.__new__(
        local_finetune_scheduler.OvernightTrainer
    )
    trainer.logger = MagicMock()

    with patch("local_runtime.local_finetune_scheduler.TRAINING_ROOT", tmp_path), \
         patch("local_runtime.local_finetune_scheduler.config.SYSTEM_PROMPT", "canonical Jarvis prompt"):
        examples = trainer._collect_teacher_examples()

    assert examples[0]["messages"][0]["role"] == "system"
    assert examples[0]["messages"][0]["content"] == "canonical Jarvis prompt"
