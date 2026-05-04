"""
Local MLX SFT training for Apple Silicon Macs.

mlx-tune provides an Unsloth-compatible API for local LoRA SFT on Apple Silicon.
All training happens offline with zero cloud cost.

Supports: Qwen 2.5 Coder 7B, Qwen3 8B, Gemma4, DeepSeek R1 14B via 4-bit quantized
HF mirrors from mlx-community.

Requires: mlx-tune, mlx-lm (pip install mlx-tune, only on Mac with Apple Silicon)
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config


REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINING_ROOT = REPO_ROOT / "training"
EXPORTS_DIR = TRAINING_ROOT / "exports"
PACKS_DIR = TRAINING_ROOT / "packs"
MLX_ADAPTERS_DIR = TRAINING_ROOT / "mlx_adapters"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_mlx_dirs() -> None:
    MLX_ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)


def _is_apple_silicon() -> bool:
    """Detect Apple Silicon Mac."""
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memtype"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "apple" in result.stdout.lower()
    except Exception:
        return False


_MLX_AVAILABLE: Optional[bool] = None
_MLX_IMPORT_ERROR: Optional[str] = None


def _check_mlx_available() -> tuple[bool, str]:
    """Check if mlx-tune is available and importable."""
    global _MLX_AVAILABLE, _MLX_IMPORT_ERROR

    if _MLX_AVAILABLE is not None:
        return _MLX_AVAILABLE, _MLX_IMPORT_ERROR or ""

    if not _is_apple_silicon():
        _MLX_AVAILABLE = False
        _MLX_IMPORT_ERROR = "Not running on Apple Silicon Mac"
        return False, _MLX_IMPORT_ERROR

    try:
        import mlx
        import mlx.core
        import mlx_lm
        import mlx_lm.lora
        _MLX_AVAILABLE = True
        _MLX_IMPORT_ERROR = None
        return True, ""
    except ImportError as e:
        _MLX_AVAILABLE = False
        _MLX_IMPORT_ERROR = str(e)
        return False, _MLX_IMPORT_ERROR


MLX_MODEL_PRESETS = {
    "qwen2.5-coder:7b": {
        "hf_id": "Qwen/Qwen2.5-Coder-7B-Instruct",
        "mlx_id": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        "lora_layers": 16,
        "seq_len": 4096,
        "disk_est": "about 5GB",
        "train_time_est": "20-30 min per 500 rows on M2 Pro+",
    },
    "qwen3:8b": {
        "hf_id": "Qwen/Qwen3-8B",
        "mlx_id": "mlx-community/Qwen3-8B-4bit",
        "lora_layers": 16,
        "seq_len": 8192,
        "disk_est": "about 5GB",
        "train_time_est": "25-35 min per 500 rows on M2 Pro+",
    },
    "gemma4:e4b": {
        "hf_id": "google/gemma-4-e4b",
        "mlx_id": "mlx-community/gemma-4-e4b-4bit",
        "lora_layers": 8,
        "seq_len": 4096,
        "disk_est": "about 3GB",
        "train_time_est": "15-20 min per 500 rows on M2 Pro+",
    },
    "deepseek-r1:14b": {
        "hf_id": "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
        "mlx_id": "mlx-community/DeepSeek-R1-Distill-Qwen-14B-4bit",
        "lora_layers": 16,
        "seq_len": 8192,
        "disk_est": "about 9GB",
        "train_time_est": "45-60 min per 500 rows on M2 Pro+",
    },
}


def is_available() -> bool:
    """Return True if mlx-tune is installed and Apple Silicon is detected."""
    available, _ = _check_mlx_available()
    return available


def status() -> dict:
    """Return status dict with availability and platform info."""
    available, error = _check_mlx_available()

    mlx_version = None
    if available:
        try:
            import mlx
            mlx_version = getattr(mlx, "__version__", "unknown")
        except Exception:
            pass

    return {
        "available": available,
        "platform": platform.system(),
        "mlx_version": mlx_version,
        "reason": error or "mlx-tune ready" if available else error or "unavailable",
    }


def list_supported_models() -> list[str]:
    """Return list of Ollama tags we have presets for."""
    return list(MLX_MODEL_PRESETS.keys())


def _convert_to_mlx_format(input_path: Path, output_dir: Path) -> tuple[bool, str]:
    """
    Convert Jarvis JSONL pack format to mlx-tune messages format.

    Jarvis format: {"messages": [...], "meta": {...}}
    MLX format: {"messages": [...]} (no metadata needed)

    Actually, they're already compatible! mlx-tune expects messages format,
    and Jarvis packs already use it. Just copy the file with only messages.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "train.jsonl"

    try:
        with input_path.open("r", encoding="utf-8") as infile:
            with output_path.open("w", encoding="utf-8") as outfile:
                for line in infile:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                        # mlx-tune expects only messages field
                        if "messages" in record:
                            mlx_record = {"messages": record["messages"]}
                            outfile.write(json.dumps(mlx_record, ensure_ascii=False) + "\n")
                    except json.JSONDecodeError:
                        continue

        return True, str(output_path)
    except Exception as e:
        return False, str(e)


def _latest_pack_path() -> Optional[Path]:
    """Find the latest training pack JSONL."""
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    packs = sorted(PACKS_DIR.glob("*.jsonl"))
    return packs[-1] if packs else None


def run_sft(
    ollama_tag: str,
    train_jsonl: str | Path | None = None,
    *,
    val_jsonl: str | Path | None = None,
    output_dir: str | Path | None = None,
    num_iters: int | None = None,
    learning_rate: float | None = None,
    lora_rank: int | None = None,
    batch_size: int | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Run LoRA SFT fine-tuning via mlx-tune.

    Args:
        ollama_tag: Model identifier, e.g. "qwen2.5-coder:7b"
        train_jsonl: Path to training JSONL (JARVIS pack format). If None, uses latest pack.
        val_jsonl: Path to validation JSONL. Optional.
        output_dir: Where to save adapter. Defaults to TRAINING_ROOT/mlx_adapters/<model_slug>/<timestamp>/
        num_iters: Training iterations. Defaults to config.MLX_NUM_ITERS
        learning_rate: LR. Defaults to config.MLX_LEARNING_RATE
        lora_rank: LoRA rank. Defaults to config.MLX_LORA_RANK
        batch_size: Batch size. Defaults to config.MLX_BATCH_SIZE
        dry_run: If True, return the command without executing.

    Returns:
        {
            "ok": bool,
            "adapter_path": str | None,
            "model_tag": str,
            "iters": int,
            "duration_sec": float | None,
            "error": str | None,
            "command": str,
            "dry_run": bool,
        }
    """
    _ensure_mlx_dirs()

    # Check availability
    if not is_available():
        _, error = _check_mlx_available()
        return {
            "ok": False,
            "adapter_path": None,
            "model_tag": ollama_tag,
            "iters": num_iters or config.MLX_NUM_ITERS,
            "duration_sec": None,
            "error": f"mlx-tune not available: {error}",
            "command": "",
            "dry_run": False,
        }

    # Validate model
    if ollama_tag not in MLX_MODEL_PRESETS:
        return {
            "ok": False,
            "adapter_path": None,
            "model_tag": ollama_tag,
            "iters": num_iters or config.MLX_NUM_ITERS,
            "duration_sec": None,
            "error": f"Unsupported model: {ollama_tag}. Supported: {', '.join(list_supported_models())}",
            "command": "",
            "dry_run": False,
        }

    preset = MLX_MODEL_PRESETS[ollama_tag]
    mlx_id = preset["mlx_id"]

    # Resolve training data
    if train_jsonl is None:
        train_jsonl = _latest_pack_path()
        if train_jsonl is None:
            return {
                "ok": False,
                "adapter_path": None,
                "model_tag": ollama_tag,
                "iters": num_iters or config.MLX_NUM_ITERS,
                "duration_sec": None,
                "error": "No training data provided and no packs found in training/packs/",
                "command": "",
                "dry_run": False,
            }
    else:
        train_jsonl = Path(train_jsonl)

    if not train_jsonl.exists():
        return {
            "ok": False,
            "adapter_path": None,
            "model_tag": ollama_tag,
            "iters": num_iters or config.MLX_NUM_ITERS,
            "duration_sec": None,
            "error": f"Training JSONL not found: {train_jsonl}",
            "command": "",
            "dry_run": False,
        }

    # Defaults from config
    num_iters = num_iters or config.MLX_NUM_ITERS
    learning_rate = learning_rate or config.MLX_LEARNING_RATE
    lora_rank = lora_rank or config.MLX_LORA_RANK
    batch_size = batch_size or config.MLX_BATCH_SIZE

    # Output directory
    if output_dir is None:
        model_slug = ollama_tag.replace(":", "-").replace(".", "-")
        timestamp = _timestamp()
        output_dir = MLX_ADAPTERS_DIR / model_slug / timestamp
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Convert training data to mlx format (in temp directory)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        convert_ok, result = _convert_to_mlx_format(train_jsonl, data_dir)

        if not convert_ok:
            return {
                "ok": False,
                "adapter_path": None,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": None,
                "error": f"Failed to convert training data: {result}",
                "command": "",
                "dry_run": False,
            }

        # Build mlx_lm.lora command
        cmd = [
            sys.executable,
            "-m",
            "mlx_lm.lora",
            "--model", mlx_id,
            "--train",
            "--data", str(data_dir),
            "--num-iterations", str(num_iters),
            "--learning-rate", str(learning_rate),
            "--rank", str(lora_rank),
            "--batch-size", str(batch_size),
            "--adapter-path", str(output_dir),
        ]

        if val_jsonl:
            val_path = Path(val_jsonl)
            if val_path.exists():
                # Copy val data to data_dir/valid.jsonl
                with val_path.open("r", encoding="utf-8") as infile:
                    with (data_dir / "valid.jsonl").open("w", encoding="utf-8") as outfile:
                        for line in infile:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                                if "messages" in record:
                                    mlx_record = {"messages": record["messages"]}
                                    outfile.write(json.dumps(mlx_record, ensure_ascii=False) + "\n")
                            except json.JSONDecodeError:
                                continue

        cmd_str = " ".join(cmd)

        if dry_run:
            return {
                "ok": True,
                "adapter_path": None,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": None,
                "error": None,
                "command": cmd_str,
                "dry_run": True,
            }

        # Run training
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour max
            )
            duration_sec = time.time() - start_time

            if result.returncode != 0:
                return {
                    "ok": False,
                    "adapter_path": None,
                    "model_tag": ollama_tag,
                    "iters": num_iters,
                    "duration_sec": duration_sec,
                    "error": f"mlx-tune failed: {result.stderr}",
                    "command": cmd_str,
                    "dry_run": False,
                }

            return {
                "ok": True,
                "adapter_path": str(output_dir),
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": duration_sec,
                "error": None,
                "command": cmd_str,
                "dry_run": False,
            }

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "adapter_path": None,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": 3600,
                "error": "Training timed out (> 1 hour)",
                "command": cmd_str,
                "dry_run": False,
            }
        except Exception as e:
            return {
                "ok": False,
                "adapter_path": None,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": None,
                "error": str(e),
                "command": cmd_str,
                "dry_run": False,
            }


def fuse_adapter(
    adapter_path: str | Path,
    base_model_hf_id: str,
    output_dir: str | Path,
) -> dict:
    """
    Fuse a trained LoRA adapter into the base model weights.

    Args:
        adapter_path: Path to the trained LoRA adapter directory
        base_model_hf_id: HF model ID (e.g. "Qwen/Qwen2.5-Coder-7B-Instruct")
        output_dir: Where to save fused weights

    Returns:
        {"ok": bool, "path": str, "error": str|None}
    """
    if not is_available():
        _, error = _check_mlx_available()
        return {
            "ok": False,
            "path": None,
            "error": f"mlx-tune not available: {error}",
        }

    adapter_path = Path(adapter_path)
    output_dir = Path(output_dir)

    if not adapter_path.exists():
        return {
            "ok": False,
            "path": None,
            "error": f"Adapter path not found: {adapter_path}",
        }

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [
            sys.executable,
            "-m",
            "mlx_lm.fuse",
            "--model", base_model_hf_id,
            "--adapter-path", str(adapter_path),
            "--save-path", str(output_dir),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            return {
                "ok": False,
                "path": None,
                "error": f"Fuse failed: {result.stderr}",
            }

        return {
            "ok": True,
            "path": str(output_dir),
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "path": None,
            "error": "Fusion timed out (> 10 min)",
        }
    except Exception as e:
        return {
            "ok": False,
            "path": None,
            "error": str(e),
        }


def generate_modelfile(
    adapter_path: str | Path,
    ollama_tag: str,
    output_dir: str | Path | None = None,
) -> dict:
    """
    Write an Ollama Modelfile pointing at the fused adapter weights.

    Args:
        adapter_path: Path to trained LoRA adapter or fused weights
        ollama_tag: Model tag (e.g. "qwen2.5-coder:7b")
        output_dir: Where to save Modelfile. Defaults to adapter_path parent.

    Returns:
        {"ok": bool, "modelfile_path": str, "error": str|None}
    """
    adapter_path = Path(adapter_path)

    if not adapter_path.exists():
        return {
            "ok": False,
            "modelfile_path": None,
            "error": f"Adapter path not found: {adapter_path}",
        }

    if output_dir is None:
        output_dir = adapter_path.parent
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    modelfile_path = output_dir / "Modelfile"

    try:
        # Ollama Modelfile format for local adapter
        modelfile_content = f"""FROM {ollama_tag}
PARAMETER stop [INST]
PARAMETER stop [/INST]

SYSTEM You are Jarvis - a local AI assistant trained on curated interactions.
"""

        with modelfile_path.open("w", encoding="utf-8") as f:
            f.write(modelfile_content)

        return {
            "ok": True,
            "modelfile_path": str(modelfile_path),
            "error": None,
        }

    except Exception as e:
        return {
            "ok": False,
            "modelfile_path": None,
            "error": str(e),
        }
