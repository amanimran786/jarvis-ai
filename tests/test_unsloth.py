"""
Unit tests for local_runtime/local_unsloth.py — Unsloth fine-tuning backend.

Tests cover:
  - is_apple_silicon() returns correct bool for current platform
  - is_available() returns False when unsloth not importable
  - is_available() returns False on Apple Silicon even if unsloth importable
  - train_sft() raises NotImplementedError on Apple Silicon
  - train_sft(dry_run=True) returns dry_run status
  - supported_models() returns a non-empty list
  - get_trainer() factory returns UnslothTrainer instance
  - train_dpo() raises NotImplementedError on Apple Silicon
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock
import platform

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from local_runtime import local_unsloth


class IsAppleSiliconTests(unittest.TestCase):
    def test_is_apple_silicon_returns_bool(self):
        """is_apple_silicon must return a boolean."""
        result = local_unsloth.is_apple_silicon()
        self.assertIsInstance(result, bool)

    def test_is_apple_silicon_arm64_returns_true(self):
        """is_apple_silicon must return True when platform.machine() == 'arm64'."""
        with patch("platform.machine", return_value="arm64"):
            result = local_unsloth.is_apple_silicon()
        self.assertTrue(result)

    def test_is_apple_silicon_x86_64_returns_false(self):
        """is_apple_silicon must return False when platform.machine() != 'arm64'."""
        with patch("platform.machine", return_value="x86_64"):
            result = local_unsloth.is_apple_silicon()
        self.assertFalse(result)


class IsAvailableTests(unittest.TestCase):
    def test_is_available_returns_false_on_apple_silicon(self):
        """is_available must return False on Apple Silicon even if unsloth importable."""
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=True):
            result = local_unsloth.is_available()
        self.assertFalse(result)

    def test_is_available_returns_false_when_unsloth_not_importable(self):
        """is_available must return False when unsloth is not installed."""
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.UNSLOTH_AVAILABLE", False):
            result = local_unsloth.is_available()
        self.assertFalse(result)

    def test_is_available_returns_true_when_unsloth_available_and_not_apple_silicon(self):
        """is_available must return True when unsloth is available and not on Apple Silicon."""
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.UNSLOTH_AVAILABLE", True):
            result = local_unsloth.is_available()
        self.assertTrue(result)


class SupportedModelsTests(unittest.TestCase):
    def test_supported_models_returns_list(self):
        """supported_models must return a list."""
        result = local_unsloth.supported_models()
        self.assertIsInstance(result, list)

    def test_supported_models_returns_non_empty_list(self):
        """supported_models must return at least one model."""
        result = local_unsloth.supported_models()
        self.assertGreater(len(result), 0)

    def test_supported_models_includes_gemma_4_4b(self):
        """supported_models must include unsloth/gemma-4-4b-bnb-4bit."""
        result = local_unsloth.supported_models()
        self.assertIn("unsloth/gemma-4-4b-bnb-4bit", result)

    def test_supported_models_includes_qwen3_8b(self):
        """supported_models must include unsloth/qwen3-8b-bnb-4bit."""
        result = local_unsloth.supported_models()
        self.assertIn("unsloth/qwen3-8b-bnb-4bit", result)

    def test_supported_models_all_strings(self):
        """supported_models must return only strings."""
        result = local_unsloth.supported_models()
        self.assertTrue(all(isinstance(m, str) for m in result))


class UnslothTrainerInitTests(unittest.TestCase):
    def test_trainer_init_sets_model_name(self):
        """UnslothTrainer.__init__ must store model_name."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        self.assertEqual(trainer.model_name, "unsloth/gemma-4-4b-bnb-4bit")

    def test_trainer_init_sets_output_dir(self):
        """UnslothTrainer.__init__ must store output_dir."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        self.assertEqual(trainer.output_dir, "/tmp/output")

    def test_trainer_init_sets_dry_run(self):
        """UnslothTrainer.__init__ must store dry_run flag."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        self.assertTrue(trainer.dry_run)


class TrainSftTests(unittest.TestCase):
    def test_train_sft_returns_error_on_apple_silicon(self):
        """train_sft must return error dict on Apple Silicon."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=True):
            result = trainer.train_sft("/tmp/dataset.jsonl", epochs=3)
        self.assertEqual(result["status"], "error")
        self.assertIn("Apple Silicon", result["error"])
        self.assertIn("local_mlx_training", result["error"])

    def test_train_sft_returns_error_when_unsloth_unavailable(self):
        """train_sft must return error when unsloth not available."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=False):
            result = trainer.train_sft("/tmp/dataset.jsonl", epochs=3)
        self.assertEqual(result["status"], "error")
        self.assertIn("not installed", result["error"])

    def test_train_sft_returns_dry_run_status(self):
        """train_sft with dry_run=True must return dry_run status."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=True):
            result = trainer.train_sft("/tmp/dataset.jsonl", epochs=3)
        self.assertEqual(result["status"], "dry_run")
        self.assertIn("command", result)

    def test_train_sft_dry_run_includes_command(self):
        """train_sft dry_run must include the command string."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=True):
            result = trainer.train_sft("/tmp/dataset.jsonl", epochs=3, batch_size=2)
        self.assertIn("unsloth", result["command"].lower())
        self.assertIn("sft", result["command"].lower())

    def test_train_sft_returns_dict_with_required_keys(self):
        """train_sft must return a dict with required keys."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=True):
            result = trainer.train_sft("/tmp/dataset.jsonl")
        self.assertIn("status", result)
        self.assertIn("adapter_path", result)
        self.assertIn("epochs", result)
        self.assertIn("error", result)
        self.assertIn("command", result)


class TrainDpoTests(unittest.TestCase):
    def test_train_dpo_returns_error_on_apple_silicon(self):
        """train_dpo must return error dict on Apple Silicon."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=True):
            result = trainer.train_dpo("/tmp/dataset.jsonl", epochs=1)
        self.assertEqual(result["status"], "error")
        self.assertIn("Apple Silicon", result["error"])

    def test_train_dpo_returns_error_when_unsloth_unavailable(self):
        """train_dpo must return error when unsloth not available."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=False):
            result = trainer.train_dpo("/tmp/dataset.jsonl", epochs=1)
        self.assertEqual(result["status"], "error")

    def test_train_dpo_returns_dry_run_status(self):
        """train_dpo with dry_run=True must return dry_run status."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=True):
            result = trainer.train_dpo("/tmp/dataset.jsonl", epochs=1)
        self.assertEqual(result["status"], "dry_run")

    def test_train_dpo_returns_dict_with_required_keys(self):
        """train_dpo must return a dict with required keys."""
        trainer = local_unsloth.UnslothTrainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        with patch("local_runtime.local_unsloth.is_apple_silicon", return_value=False), \
             patch("local_runtime.local_unsloth.is_available", return_value=True):
            result = trainer.train_dpo("/tmp/dataset.jsonl")
        self.assertIn("status", result)
        self.assertIn("adapter_path", result)
        self.assertIn("epochs", result)
        self.assertIn("error", result)
        self.assertIn("command", result)


class GetTrainerTests(unittest.TestCase):
    def test_get_trainer_returns_unsloth_trainer(self):
        """get_trainer must return an UnslothTrainer instance."""
        trainer = local_unsloth.get_trainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
        )
        self.assertIsInstance(trainer, local_unsloth.UnslothTrainer)

    def test_get_trainer_sets_model_name(self):
        """get_trainer must pass model_name to trainer."""
        trainer = local_unsloth.get_trainer(
            model_name="unsloth/qwen3-8b-bnb-4bit",
            output_dir="/tmp/output",
        )
        self.assertEqual(trainer.model_name, "unsloth/qwen3-8b-bnb-4bit")

    def test_get_trainer_sets_output_dir(self):
        """get_trainer must pass output_dir to trainer."""
        trainer = local_unsloth.get_trainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/custom_output",
        )
        self.assertEqual(trainer.output_dir, "/tmp/custom_output")

    def test_get_trainer_sets_dry_run(self):
        """get_trainer must pass dry_run flag to trainer."""
        trainer = local_unsloth.get_trainer(
            model_name="unsloth/gemma-4-4b-bnb-4bit",
            output_dir="/tmp/output",
            dry_run=True,
        )
        self.assertTrue(trainer.dry_run)


if __name__ == "__main__":
    unittest.main()
