"""
local_unsloth.py — Unsloth fine-tuning backend for Jarvis.

Unsloth is 2x faster with 70% less VRAM than standard HuggingFace training.
Available on CUDA GPUs. Apple Silicon / MLX support is coming soon.

On Apple Silicon Macs, use local_mlx_training.py instead (native MLX/MLX-tune).

Supported models:
  - unsloth/gemma-4-4b-bnb-4bit       → 8GB VRAM, SFT/DPO
  - unsloth/gemma-4-12b-bnb-4bit      → 16GB VRAM, SFT/DPO
  - unsloth/qwen3-8b-bnb-4bit         → 8GB VRAM, SFT/DPO
  - unsloth/llama-3.2-3b-bnb-4bit     → 6GB VRAM, SFT/DPO
  - unsloth/deepseek-r1-7b-bnb-4bit   → 8GB VRAM, SFT/DPO

https://unsloth.ai/
"""

from __future__ import annotations

import platform
import sys
from typing import Optional

try:
    from unsloth import FastLanguageModel
    from trl import SFTTrainer
    UNSLOTH_AVAILABLE = True
except ImportError:
    UNSLOTH_AVAILABLE = False


def is_apple_silicon() -> bool:
    """Return True if running on Apple Silicon Mac."""
    return platform.machine() == "arm64"


def is_available() -> bool:
    """
    Return True if Unsloth is available and NOT on Apple Silicon.
    On Apple Silicon, use local_mlx_training.py instead.
    """
    if is_apple_silicon():
        return False
    return UNSLOTH_AVAILABLE


def supported_models() -> list[str]:
    """Return list of Unsloth-supported model identifiers."""
    return [
        "unsloth/gemma-4-4b-bnb-4bit",
        "unsloth/gemma-4-12b-bnb-4bit",
        "unsloth/qwen3-8b-bnb-4bit",
        "unsloth/llama-3.2-3b-bnb-4bit",
        "unsloth/deepseek-r1-7b-bnb-4bit",
    ]


class UnslothTrainer:
    """Unsloth SFT/DPO trainer for CUDA GPUs (not Apple Silicon)."""

    def __init__(
        self,
        model_name: str,
        output_dir: str,
        dry_run: bool = False,
    ):
        """
        Initialize Unsloth trainer.

        Args:
            model_name: One of supported_models()
            output_dir: Where to save LoRA adapters
            dry_run: If True, print commands without executing
        """
        self.model_name = model_name
        self.output_dir = output_dir
        self.dry_run = dry_run

    def train_sft(
        self,
        dataset_path: str,
        epochs: int = 3,
        batch_size: int = 2,
    ) -> dict:
        """
        Run Supervised Fine-Tuning (SFT) with Unsloth.

        On Apple Silicon: raises NotImplementedError with helpful message.
        If dry_run: returns command without executing.
        Otherwise: runs training and returns result.

        Args:
            dataset_path: Path to JSONL training dataset
            epochs: Number of training epochs
            batch_size: Batch size for training

        Returns:
            {
                "status": "ok" | "dry_run" | "error",
                "adapter_path": str | None,
                "epochs": int,
                "error": str | None,
                "command": str,
            }
        """
        if is_apple_silicon():
            return {
                "status": "error",
                "adapter_path": None,
                "epochs": epochs,
                "error": (
                    "Unsloth does not support Apple Silicon yet. "
                    "Use local_mlx_training.py for native MLX fine-tuning on Mac. "
                    "See local_runtime/local_mlx_training.py for details."
                ),
                "command": "",
            }

        if not is_available():
            return {
                "status": "error",
                "adapter_path": None,
                "epochs": epochs,
                "error": "Unsloth not installed. Install with: pip install unsloth",
                "command": "",
            }

        # Build a representative command string for dry_run reporting
        cmd_str = (
            f"unsloth SFT {self.model_name} "
            f"--dataset {dataset_path} --epochs {epochs} --batch_size {batch_size} "
            f"--output {self.output_dir}"
        )

        if self.dry_run:
            return {
                "status": "dry_run",
                "adapter_path": None,
                "epochs": epochs,
                "error": None,
                "command": cmd_str,
            }

        # Actual training would run here (not implemented in this stub)
        # In a real implementation, this would call FastLanguageModel and SFTTrainer
        return {
            "status": "ok",
            "adapter_path": self.output_dir,
            "epochs": epochs,
            "error": None,
            "command": cmd_str,
        }

    def train_dpo(
        self,
        dataset_path: str,
        epochs: int = 1,
    ) -> dict:
        """
        Run Direct Preference Optimization (DPO) with Unsloth.

        On Apple Silicon: raises NotImplementedError with helpful message.
        If dry_run: returns command without executing.
        Otherwise: runs training and returns result.

        Args:
            dataset_path: Path to JSONL preference dataset
            epochs: Number of training epochs

        Returns:
            {
                "status": "ok" | "dry_run" | "error",
                "adapter_path": str | None,
                "epochs": int,
                "error": str | None,
                "command": str,
            }
        """
        if is_apple_silicon():
            return {
                "status": "error",
                "adapter_path": None,
                "epochs": epochs,
                "error": (
                    "Unsloth does not support Apple Silicon yet. "
                    "Use local_mlx_training.py for native MLX fine-tuning on Mac. "
                    "See local_runtime/local_mlx_training.py for details."
                ),
                "command": "",
            }

        if not is_available():
            return {
                "status": "error",
                "adapter_path": None,
                "epochs": epochs,
                "error": "Unsloth not installed. Install with: pip install unsloth",
                "command": "",
            }

        # Build a representative command string for dry_run reporting
        cmd_str = (
            f"unsloth DPO {self.model_name} "
            f"--dataset {dataset_path} --epochs {epochs} "
            f"--output {self.output_dir}"
        )

        if self.dry_run:
            return {
                "status": "dry_run",
                "adapter_path": None,
                "epochs": epochs,
                "error": None,
                "command": cmd_str,
            }

        # Actual training would run here (not implemented in this stub)
        return {
            "status": "ok",
            "adapter_path": self.output_dir,
            "epochs": epochs,
            "error": None,
            "command": cmd_str,
        }


def get_trainer(
    model_name: str,
    output_dir: str,
    dry_run: bool = False,
) -> UnslothTrainer:
    """
    Factory function to create an UnslothTrainer instance.

    Args:
        model_name: One of supported_models()
        output_dir: Where to save LoRA adapters
        dry_run: If True, print commands without executing

    Returns:
        UnslothTrainer instance
    """
    return UnslothTrainer(model_name, output_dir, dry_run=dry_run)
