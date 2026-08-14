"""
Unit tests for local_mlx_training module.

These tests run WITHOUT mlx installed (sandbox). mlx is mocked.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Stub mlx modules before importing local_mlx_training
sys.modules["mlx"] = MagicMock()
sys.modules["mlx.core"] = MagicMock()
sys.modules["mlx_lm"] = MagicMock()
sys.modules["mlx_lm.lora"] = MagicMock()
sys.modules["mlx_lm.fuse"] = MagicMock()


def test_status_returns_dict():
    """status() returns dict with required keys."""
    from local_runtime import local_mlx_training

    result = local_mlx_training.status()

    assert isinstance(result, dict)
    assert "available" in result
    assert "platform" in result
    assert "mlx_version" in result
    assert "reason" in result


def test_status_unavailable_on_non_mac():
    """When not on Apple Silicon, is_available() returns False."""
    from local_runtime import local_mlx_training

    with patch("local_runtime.local_mlx_training._is_apple_silicon", return_value=False):
        # Reset cached availability
        local_mlx_training._MLX_AVAILABLE = None
        local_mlx_training._MLX_IMPORT_ERROR = None

        available = local_mlx_training.is_available()
        assert available is False

        status = local_mlx_training.status()
        assert status["available"] is False


def test_list_supported_models_returns_known_tags():
    """list_supported_models() returns expected model tags."""
    from local_runtime import local_mlx_training

    models = local_mlx_training.list_supported_models()

    assert isinstance(models, list)
    assert len(models) > 0
    assert "qwen2.5-coder:7b" in models
    assert "qwen3:8b" in models
    assert "gemma4:e4b" in models
    assert "deepseek-r1:14b" in models


def test_model_presets_have_required_fields():
    """All model presets have required metadata."""
    from local_runtime import local_mlx_training

    for tag, preset in local_mlx_training.MLX_MODEL_PRESETS.items():
        assert "hf_id" in preset
        assert "mlx_id" in preset
        assert "lora_layers" in preset
        assert "seq_len" in preset
        assert "disk_est" in preset
        assert "train_time_est" in preset


def test_run_sft_fails_gracefully_when_unavailable():
    """When mlx not available, run_sft returns ok=False with error."""
    from local_runtime import local_mlx_training

    with patch("local_runtime.local_mlx_training.is_available", return_value=False):
        with patch(
            "local_runtime.local_mlx_training._check_mlx_available",
            return_value=(False, "mlx not installed"),
        ):
            result = local_mlx_training.run_sft("qwen2.5-coder:7b")

            assert result["ok"] is False
            assert result["error"] is not None
            assert "mlx-tune not available" in result["error"]


def test_run_sft_unsupported_model_returns_error():
    """Unknown ollama tag returns error."""
    from local_runtime import local_mlx_training

    with patch("local_runtime.local_mlx_training.is_available", return_value=True):
        result = local_mlx_training.run_sft("unknown-model:99b")

        assert result["ok"] is False
        assert result["error"] is not None
        assert "Unsupported model" in result["error"]


def test_run_sft_missing_data_returns_error():
    """Missing training data returns error."""
    from local_runtime import local_mlx_training

    with patch("local_runtime.local_mlx_training.is_available", return_value=True):
        result = local_mlx_training.run_sft(
            "qwen2.5-coder:7b",
            train_jsonl="/nonexistent/path.jsonl",
        )

        assert result["ok"] is False
        assert "not found" in result["error"].lower()


def test_run_sft_dry_run_returns_command_string():
    """dry_run=True returns command without executing."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create minimal training data
        train_file = tmpdir / "train.jsonl"
        train_file.write_text(
            json.dumps({
                "messages": [
                    {"role": "user", "content": "test"},
                    {"role": "assistant", "content": "response"},
                ]
            }) + "\n"
        )

        with patch("local_runtime.local_mlx_training.is_available", return_value=True):
            result = local_mlx_training.run_sft(
                "qwen2.5-coder:7b",
                train_jsonl=train_file,
                dry_run=True,
            )

            assert result["ok"] is True
            assert result["dry_run"] is True
            assert result["command"] != ""
            assert "mlx_lm.lora" in result["command"]
            assert result["adapter_path"] is None  # dry_run doesn't create


def test_run_sft_command_supports_completion_only_seed_and_resume():
    """Guarded runs mask prompts and can resume deterministic checkpoints."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        train_file = root / "train.jsonl"
        train_file.write_text(
            json.dumps({
                "messages": [
                    {"role": "user", "content": "test"},
                    {"role": "assistant", "content": "response"},
                ]
            }) + "\n"
        )
        resume = root / "adapters.safetensors"
        resume.write_text("fixture")
        with patch("local_runtime.local_mlx_training.is_available", return_value=True):
            result = local_mlx_training.run_sft(
                "qwen3:8b",
                train_jsonl=train_file,
                seed=123,
                completion_only=True,
                resume_adapter_file=resume,
                dry_run=True,
            )
        assert result["ok"] is True
        assert "--mask-prompt" in result["command"]
        assert "--seed 123" in result["command"]
        assert f"--resume-adapter-file {resume.resolve()}" in result["command"]


def test_convert_to_mlx_format_transforms_jsonl():
    """_convert_to_mlx_format converts Jarvis JSONL to mlx format."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create input JSONL with Jarvis format
        input_file = tmpdir / "input.jsonl"
        test_data = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
            "meta": {"source": "test"},
        }
        input_file.write_text(json.dumps(test_data) + "\n")

        output_dir = tmpdir / "output"
        ok, path = local_mlx_training._convert_to_mlx_format(input_file, output_dir)

        assert ok is True
        assert output_dir.exists()

        split_files = [
            output_dir / "train.jsonl",
            output_dir / "valid.jsonl",
            output_dir / "test.jsonl",
        ]
        for split_file in split_files:
            assert split_file.exists()
            assert split_file.read_text().strip()

        # Read output and verify format
        lines = split_files[0].read_text().strip().split("\n")
        assert lines

        output_record = json.loads(lines[0])
        assert "messages" in output_record
        assert output_record["messages"] == test_data["messages"]
        assert "meta" not in output_record  # meta should be stripped


def test_convert_to_mlx_format_supports_prompt_completion_packs():
    """Overnight prompt/completion packs should produce non-empty MLX splits."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        input_file = tmpdir / "overnight.jsonl"
        records = [
            {"prompt": f"User: task {i}\n\nJarvis:", "completion": f" response {i}"}
            for i in range(30)
        ]
        input_file.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        output_dir = tmpdir / "output"
        ok, path = local_mlx_training._convert_to_mlx_format(input_file, output_dir)

        assert ok is True
        assert path == str(output_dir)

        split_records = {}
        for split_name in ("train", "valid", "test"):
            split_path = output_dir / f"{split_name}.jsonl"
            lines = split_path.read_text(encoding="utf-8").strip().splitlines()
            assert lines
            split_records[split_name] = [json.loads(line) for line in lines]
            assert len(lines) >= local_mlx_training.config.MLX_BATCH_SIZE
            for loaded in split_records[split_name]:
                assert [message["role"] for message in loaded["messages"]] == ["user", "assistant"]
                assert loaded["messages"][0]["content"].startswith("User: task")
                assert loaded["messages"][1]["content"].startswith("response")

        assert len(split_records["valid"]) == len(split_records["test"])

        split_prompts = {
            split_name: {
                loaded["messages"][0]["content"]
                for loaded in loaded_records
            }
            for split_name, loaded_records in split_records.items()
        }
        expected_prompts = {record["prompt"].strip() for record in records}
        assert set().union(*split_prompts.values()) == expected_prompts
        assert split_prompts["train"].isdisjoint(split_prompts["valid"])
        assert split_prompts["train"].isdisjoint(split_prompts["test"])
        assert split_prompts["valid"].isdisjoint(split_prompts["test"])


def test_convert_to_mlx_format_rejects_empty_pack():
    """Invalid packs should fail before mlx_lm raises an opaque IndexError."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        input_file = tmpdir / "empty.jsonl"
        input_file.write_text('{"meta": {"source": "bad"}}\nnot-json\n', encoding="utf-8")

        ok, error = local_mlx_training._convert_to_mlx_format(input_file, tmpdir / "out")

        assert ok is False
        assert "No usable training examples" in error


def test_fuse_adapter_requires_availability():
    """fuse_adapter returns error when mlx not available."""
    from local_runtime import local_mlx_training

    with patch("local_runtime.local_mlx_training.is_available", return_value=False):
        with patch(
            "local_runtime.local_mlx_training._check_mlx_available",
            return_value=(False, "mlx not installed"),
        ):
            result = local_mlx_training.fuse_adapter(
                "/nonexistent/adapter",
                "Qwen/Qwen2.5-Coder-7B-Instruct",
                "/output",
            )

            assert result["ok"] is False
            assert "not available" in result["error"]


def test_generate_modelfile_creates_file():
    """generate_modelfile creates a Modelfile."""
    from local_runtime import local_mlx_training

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create a dummy adapter directory
        adapter_dir = tmpdir / "adapter"
        adapter_dir.mkdir()

        output_dir = tmpdir / "modelfile_out"

        result = local_mlx_training.generate_modelfile(
            adapter_dir,
            "qwen2.5-coder:7b",
            output_dir,
        )

        assert result["ok"] is True
        assert output_dir.exists()

        modelfile = output_dir / "Modelfile"
        assert modelfile.exists()

        content = modelfile.read_text()
        assert "qwen2.5-coder:7b" in content
        assert "Jarvis" in content or "SYSTEM" in content


def test_config_constants_exist():
    """MLX config constants are defined in config."""
    import config

    assert hasattr(config, "MLX_TRAINING_ENABLED")
    assert hasattr(config, "MLX_NUM_ITERS")
    assert hasattr(config, "MLX_LEARNING_RATE")
    assert hasattr(config, "MLX_LORA_RANK")
    assert hasattr(config, "MLX_BATCH_SIZE")

    # Verify defaults
    assert isinstance(config.MLX_NUM_ITERS, int)
    assert config.MLX_NUM_ITERS > 0
    assert isinstance(config.MLX_LEARNING_RATE, float)
    assert config.MLX_LEARNING_RATE > 0
    assert isinstance(config.MLX_LORA_RANK, int)
    assert config.MLX_LORA_RANK > 0
    assert isinstance(config.MLX_BATCH_SIZE, int)
    assert config.MLX_BATCH_SIZE > 0
