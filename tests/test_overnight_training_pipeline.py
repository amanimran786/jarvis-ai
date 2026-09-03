"""Focused tests for the overnight training pipeline fixes."""

from __future__ import annotations

import tempfile
import json
import os
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


def test_run_eval_prefers_full_benchmark_over_partial_golden_counts():
    """Full category benchmarks are the source of truth for promotion gates."""
    trainer = local_finetune_scheduler.OvernightTrainer()
    trainer.logger = MagicMock()

    with patch("subprocess.run", side_effect=AssertionError("goldens should be fallback only")):
        with patch.object(trainer, "_run_benchmark_eval") as mock_benchmark:
            mock_benchmark.return_value = {
                "passed": 597,
                "failed": 4,
                "total": 601,
                "source": "benchmark_tracker",
            }
            result = trainer.run_eval()

    assert result["passed"] == 597
    assert result["failed"] == 4
    assert result["total"] == 601
    assert result["source"] == "benchmark_tracker"


def test_run_training_adds_examples_count():
    """Training result should include source pack row count for dashboard history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pack_path = Path(tmpdir) / "pack.jsonl"
        pack_path.write_text('{"prompt":"a","completion":"b"}\n\n{"prompt":"c","completion":"d"}\n')

        trainer = local_finetune_scheduler.OvernightTrainer()
        trainer.logger = MagicMock()

        with patch("local_runtime.local_finetune_scheduler.config.MLX_TRAINING_MODEL", "qwen3:8b"):
            with patch("local_runtime.local_finetune_scheduler.local_mlx_training.run_sft") as mock_run:
                mock_run.return_value = {"ok": True, "duration_sec": 1.25}
                result = trainer.run_training(pack_path)

    assert result["ok"] is True
    assert result["examples_count"] == 2
    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == "qwen3:8b"


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
             patch("local_runtime.local_finetune_scheduler.LOG_FILE", log_path), \
             patch("local_runtime.local_finetune_scheduler.TRAINING_ROOT", root):
            trainer = local_finetune_scheduler.OvernightTrainer()

    assert trainer.state["last_run_date"] == "2026-05-04"
    assert trainer.state["baseline_eval_passed"] == 312
    assert trainer.state["baseline_eval_total"] == 313
    assert trainer.state["last_session"]["examples_count"] == 30
    assert trainer.state["last_session"]["duration_seconds"] == 17.5


def test_state_repairs_last_session_eval_from_latest_benchmark():
    """Status should not keep showing stale partial eval totals after full benchmark exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        state_path = root / "overnight_state.json"
        log_path = root / "overnight_log.jsonl"
        benchmark_path = root / "benchmarks.jsonl"
        state_path.write_text(
            json.dumps(
                {
                    "baseline_eval_passed": 312,
                    "baseline_eval_total": 313,
                    "last_session": {
                        "timestamp": "2026-05-05T06:35:16Z",
                        "stages": {"eval": {"passed": 312, "failed": 1, "total": 313}},
                        "eval_passed": 312,
                        "eval_total": 313,
                    },
                }
            )
        )
        log_path.write_text("")
        benchmark_path.write_text(
            json.dumps(
                {
                    "total_passed": 597,
                    "total_tests": 601,
                    "overall": 0.9933,
                    "categories": {"voice": {"passed": 34, "total": 34}},
                }
            )
            + "\n"
        )

        with patch("local_runtime.local_finetune_scheduler.STATE_FILE", state_path), \
             patch("local_runtime.local_finetune_scheduler.LOG_FILE", log_path), \
             patch("local_runtime.local_finetune_scheduler.TRAINING_ROOT", root):
            trainer = local_finetune_scheduler.OvernightTrainer()

    assert trainer.state["baseline_eval_passed"] == 597
    assert trainer.state["baseline_eval_total"] == 601
    assert trainer.state["last_session"]["eval_passed"] == 597
    assert trainer.state["last_session"]["eval_total"] == 601
    assert trainer.state["last_session"]["stages"]["eval"]["categories"]["voice"]["passed"] == 34


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

    calls = [c.args[0] for c in mock_run.call_args_list]
    add_args = next(c for c in calls if c[:2] == ["git", "add"])
    commit_args = next(c for c in calls if c[:2] == ["git", "commit"])
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


def test_auto_commit_artifacts_skips_gitignored_paths():
    """A gitignored artifact must not abort the commit of the remaining ones.

    Regression: training/dashboard.html was gitignored on 2026-06-10, so
    `git add` exited 1 and the nightly training commit silently no-opped for
    months behind a non-fatal warning.
    """
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

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "check-ignore"]:
                return MagicMock(returncode=0, stdout="training/dashboard.html\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("local_runtime.local_finetune_scheduler.REPO_ROOT", root), \
             patch("local_runtime.local_finetune_scheduler._today_date", return_value="2026-05-05"), \
             patch("subprocess.run", side_effect=fake_run) as mock_run:
            trainer._auto_commit_artifacts(promoted=False)

    calls = [c.args[0] for c in mock_run.call_args_list]
    add_args = next(c for c in calls if c[:2] == ["git", "add"])
    commit_args = next(c for c in calls if c[:2] == ["git", "commit"])

    assert "training/dashboard.html" not in add_args
    assert "training/dashboard.html" not in commit_args
    # the three non-ignored artifacts still get committed
    for kept in (
        "training/overnight_log.jsonl",
        "training/benchmarks.jsonl",
        "training/overnight_state.json",
    ):
        assert kept in add_args
        assert kept in commit_args


def test_quiet_overnight_mode_suppresses_training_notification():
    """The launchd overnight job must not play notification sounds or steal attention."""
    trainer = local_finetune_scheduler.OvernightTrainer.__new__(
        local_finetune_scheduler.OvernightTrainer
    )
    trainer.logger = MagicMock()

    with patch.dict(os.environ, {"JARVIS_OVERNIGHT_QUIET": "1"}, clear=False), \
         patch("subprocess.run") as mock_run:
        trainer._notify_training_complete(promoted=True)

    mock_run.assert_not_called()
    trainer.logger.info.assert_called_with(
        "macOS notification suppressed: quiet overnight mode"
    )


def test_non_quiet_notification_can_disable_sound_only():
    """Manual runs can still notify without using an audible notification sound."""
    trainer = local_finetune_scheduler.OvernightTrainer.__new__(
        local_finetune_scheduler.OvernightTrainer
    )
    trainer.logger = MagicMock()

    with patch.dict(
        os.environ,
        {"JARVIS_OVERNIGHT_QUIET": "0", "JARVIS_NO_NOTIFICATION_SOUND": "1"},
        clear=False,
    ), patch("subprocess.run") as mock_run:
        trainer._notify_training_complete(promoted=False)

    args = mock_run.call_args.args[0]
    assert args[:2] == ["osascript", "-e"]
    assert "Jarvis Training Complete" in args[2]
    assert "sound name" not in args[2]


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


def test_teacher_examples_skip_known_bad_capability_claims(tmp_path):
    """The overnight pack must not train on overbroad capability bragging."""
    teacher_dir = tmp_path / "teacher_examples"
    teacher_dir.mkdir()
    (teacher_dir / "bad.jsonl").write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "Now give a more in-depth introduction."},
                    {
                        "role": "assistant",
                        "content": "I have direct access to Aman's Mac and admin/sudo privileges.",
                    },
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

    assert examples == []


def test_verbatim_examples_skip_known_bad_message_loops(tmp_path):
    """Bad live messaging loops should not become training examples."""
    memory_dir = tmp_path / "memory" / "conversations"
    memory_dir.mkdir(parents=True)
    verbatim = memory_dir / "verbatim.jsonl"
    verbatim.write_text(
        json.dumps(
            {
                "user": "send it",
                "assistant": "Draft ready for it: \"Hi Dad\". Say confirm send to send it.",
            }
        )
        + "\n"
        + json.dumps(
            {
                "user": "what can you do",
                "assistant": "I can check the runtime, route tasks, and tell you what is ready.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trainer = local_finetune_scheduler.OvernightTrainer.__new__(
        local_finetune_scheduler.OvernightTrainer
    )
    trainer.logger = MagicMock()

    with patch("local_runtime.local_finetune_scheduler.REPO_ROOT", tmp_path), \
         patch("local_runtime.local_finetune_scheduler.config.SYSTEM_PROMPT", "canonical Jarvis prompt"):
        examples = trainer._collect_verbatim_examples(limit=10)

    assert len(examples) == 1
    assert examples[0]["messages"][1]["content"] == "what can you do"
