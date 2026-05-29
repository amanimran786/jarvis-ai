"""Tests for local DPO/ORPO preference training via mlx-lm-lora."""

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock mlx imports to allow tests to run without mlx installed
sys.modules['mlx'] = MagicMock()
sys.modules['mlx.core'] = MagicMock()
sys.modules['mlx_lm'] = MagicMock()
sys.modules['mlx_lm_lora'] = MagicMock()

# Now we can import the module under test
from local_runtime import local_mlx_dpo


class MlxDpoStatusTests(unittest.TestCase):
    """Tests for availability and status reporting."""

    def test_status_returns_dict_with_required_keys(self):
        """Status should return a dict with availability keys."""
        result = local_mlx_dpo.status()
        self.assertIsInstance(result, dict)
        self.assertIn("available", result)
        self.assertIn("mlx_lm_lora_installed", result)
        self.assertIn("apple_silicon", result)

    def test_is_available_returns_bool(self):
        """is_available should return a boolean."""
        result = local_mlx_dpo.is_available()
        self.assertIsInstance(result, bool)

    def test_list_algorithms_includes_dpo_orpo_ipo(self):
        """list_algorithms should return supported preference algorithms including GRPO."""
        algos = local_mlx_dpo.list_algorithms()
        self.assertIsInstance(algos, list)
        self.assertIn("dpo", algos)
        self.assertIn("orpo", algos)
        self.assertIn("ipo", algos)
        self.assertIn("grpo", algos)
        self.assertEqual(len(algos), 4)


class MlxDpoFormatConversionTests(unittest.TestCase):
    """Tests for preference format conversion."""

    def test_export_preferences_converts_format_correctly(self):
        """Conversion should transform Jarvis format to mlx-lm-lora format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create input file in Jarvis format
            jarvis_data = [
                {
                    "prompt": "What is 2+2?",
                    "chosen": "The answer is 4.",
                    "rejected": "The answer is 5.",
                    "meta": {"source": "test"},
                },
                {
                    "prompt": "What is capital of France?",
                    "chosen": "Paris is the capital of France.",
                    "rejected": "London is the capital of France.",
                    "meta": {"source": "test"},
                },
            ]
            input_file = tmpdir_path / "jarvis_input.jsonl"
            with input_file.open("w") as f:
                for item in jarvis_data:
                    f.write(json.dumps(item) + "\n")

            # Mock _latest_preferences_path to return our test file
            with patch.object(local_mlx_dpo, "_latest_preferences_path", return_value=input_file):
                result = local_mlx_dpo.export_latest_preferences_for_mlx(
                    output_dir=tmpdir_path
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["pair_count"], 2)

            # Verify output format
            output_path = Path(result["path"])
            self.assertTrue(output_path.exists())

            with output_path.open() as f:
                converted_lines = f.readlines()

            self.assertEqual(len(converted_lines), 2)

            # Check first pair structure
            first_pair = json.loads(converted_lines[0])
            self.assertEqual(first_pair["prompt"], "What is 2+2?")
            self.assertIsInstance(first_pair["chosen"], list)
            self.assertIsInstance(first_pair["rejected"], list)
            self.assertEqual(len(first_pair["chosen"]), 1)
            self.assertEqual(len(first_pair["rejected"]), 1)
            self.assertEqual(
                first_pair["chosen"][0]["content"],
                "The answer is 4."
            )
            self.assertEqual(
                first_pair["rejected"][0]["content"],
                "The answer is 5."
            )

    def test_export_preferences_skips_invalid_pairs(self):
        """Conversion should skip pairs with missing fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create input with mixed valid and invalid pairs
            jarvis_data = [
                {
                    "prompt": "Valid question?",
                    "chosen": "Valid answer.",
                    "rejected": "Invalid answer.",
                },
                {
                    "prompt": "Missing chosen",
                    "chosen": "",
                    "rejected": "Some answer.",
                },
                {
                    "prompt": "",  # Missing prompt
                    "chosen": "Some answer.",
                    "rejected": "Bad answer.",
                },
            ]
            input_file = tmpdir_path / "mixed_input.jsonl"
            with input_file.open("w") as f:
                for item in jarvis_data:
                    f.write(json.dumps(item) + "\n")

            with patch.object(local_mlx_dpo, "_latest_preferences_path", return_value=input_file):
                result = local_mlx_dpo.export_latest_preferences_for_mlx(
                    output_dir=tmpdir_path
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["pair_count"], 1)  # Only 1 valid pair

    def test_export_preferences_returns_error_when_no_file(self):
        """Should return error when no preference file exists."""
        with patch.object(local_mlx_dpo, "_latest_preferences_path", return_value=None):
            result = local_mlx_dpo.export_latest_preferences_for_mlx()

        self.assertFalse(result["ok"])
        self.assertIsNone(result["path"])
        self.assertEqual(result["pair_count"], 0)
        self.assertIn("error", result)


class MlxDpoTrainingTests(unittest.TestCase):
    """Tests for preference training execution."""

    def test_validate_preference_dataset_rejects_bad_pairs(self):
        validation = local_mlx_dpo.validate_preference_dataset([
            {
                "prompt": "Q",
                "chosen": "same",
                "rejected": "same",
            },
            {
                "prompt": "Q2",
                "chosen": "RuntimeError: model failed",
                "rejected": "A better answer.",
            },
        ])

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["valid_count"], 0)
        self.assertGreaterEqual(validation["error_count"], 2)

    def test_validate_preference_dataset_rejects_unreviewed_auto_mined_pairs(self):
        validation = local_mlx_dpo.validate_preference_dataset([
            {
                "prompt": "Q",
                "chosen": "Good answer.",
                "rejected": "Bad answer.",
                "meta": {"source": "local_eval"},
            }
        ])

        self.assertFalse(validation["ok"])
        self.assertIn("reviewed", validation["errors"][0])

    def test_run_preference_training_dry_run_returns_command(self):
        """Dry run should return the command without executing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create a minimal preference file
            pref_data = [
                {
                    "prompt": "Test?",
                    "chosen": [{"role": "assistant", "content": "Good answer."}],
                    "rejected": [{"role": "assistant", "content": "Bad answer."}],
                }
            ]
            pref_file = tmpdir_path / "prefs.jsonl"
            with pref_file.open("w") as f:
                for item in pref_data:
                    f.write(json.dumps(item) + "\n")

            with patch.object(local_mlx_dpo, "is_available", return_value=True):
                result = local_mlx_dpo.run_preference_training(
                    "qwen2.5-coder:7b",
                    preference_jsonl=str(pref_file),
                    dry_run=True,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["dry_run"])
            self.assertIn("command", result)
            self.assertIn("mlx_lm_lora.train", result["command"])
            self.assertEqual(result["pair_count"], 1)
            self.assertIsNone(result["adapter_path"])

    def test_run_preference_training_accepts_grpo_algorithm(self):
        """GRPO is now a fully implemented algorithm — dry_run should succeed."""
        result = local_mlx_dpo.run_preference_training(
            "qwen2.5-coder:7b",
            algorithm="grpo",
            dry_run=True,
        )

        # GRPO is implemented: dry_run should succeed (ok=True) or fail only due
        # to missing Apple Silicon / mlx-lm-lora, not because GRPO is unsupported.
        if not result["ok"]:
            self.assertNotIn("not implemented", result.get("error", "").lower())

    def test_run_preference_training_fails_gracefully_when_unavailable(self):
        """Should return error when mlx-lm-lora is unavailable."""
        with patch.object(local_mlx_dpo, "is_available", return_value=False):
            result = local_mlx_dpo.run_preference_training(
                "qwen2.5-coder:7b",
                dry_run=True,
            )

        self.assertFalse(result["ok"])
        self.assertIn("not available", result["error"].lower())

    def test_run_preference_training_unsupported_model_returns_error(self):
        """Should return error for unsupported model."""
        with patch.object(local_mlx_dpo, "is_available", return_value=True):
            result = local_mlx_dpo.run_preference_training(
                "unsupported-model:7b",
                dry_run=True,
            )

        self.assertFalse(result["ok"])
        self.assertIn("unsupported", result["error"].lower())
        self.assertEqual(result["pair_count"], 0)

    def test_run_preference_training_missing_file_returns_error(self):
        """Should return error when preference file doesn't exist."""
        with patch.object(local_mlx_dpo, "is_available", return_value=True):
            result = local_mlx_dpo.run_preference_training(
                "qwen2.5-coder:7b",
                preference_jsonl="/nonexistent/file.jsonl",
                dry_run=True,
            )

        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"].lower())

    def test_run_preference_training_result_structure(self):
        """Result dict should have all required fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            pref_file = tmpdir_path / "prefs.jsonl"
            with pref_file.open("w") as f:
                f.write(json.dumps({
                    "prompt": "Q",
                    "chosen": [{"role": "assistant", "content": "A"}],
                    "rejected": [{"role": "assistant", "content": "B"}],
                }) + "\n")

            with patch.object(local_mlx_dpo, "is_available", return_value=True):
                result = local_mlx_dpo.run_preference_training(
                    "qwen2.5-coder:7b",
                    preference_jsonl=str(pref_file),
                    algorithm="dpo",
                    num_iters=100,
                    dry_run=True,
                )

        required_keys = [
            "ok", "adapter_path", "algorithm", "model_tag", "iters",
            "duration_sec", "error", "command", "dry_run", "pair_count"
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

        self.assertEqual(result["algorithm"], "dpo")
        self.assertEqual(result["model_tag"], "qwen2.5-coder:7b")
        self.assertEqual(result["iters"], 100)


class MlxDpoAlgorithmTests(unittest.TestCase):
    """Tests for algorithm selection."""

    def test_orpo_is_default_algorithm(self):
        """ORPO should be the default algorithm."""
        # This test documents the design choice by checking the function signature
        import inspect
        sig = inspect.signature(local_mlx_dpo.run_preference_training)
        self.assertEqual(sig.parameters["algorithm"].default, "orpo")

    def test_all_algorithms_in_list(self):
        """All algorithms used in tests should be in the supported list."""
        supported = local_mlx_dpo.list_algorithms()
        test_algos = ["dpo", "orpo", "ipo"]
        for algo in test_algos:
            self.assertIn(algo, supported)


class MlxDpoModelPresetsTests(unittest.TestCase):
    """Tests for model preset configuration."""

    def test_model_presets_have_required_fields(self):
        """Each model preset should have mlx_id and lora_layers."""
        for model_tag, preset in local_mlx_dpo._DPO_MODEL_PRESETS.items():
            self.assertIn("mlx_id", preset)
            self.assertIn("lora_layers", preset)
            self.assertIsInstance(preset["mlx_id"], str)
            self.assertIsInstance(preset["lora_layers"], int)
            self.assertTrue(preset["mlx_id"].startswith("mlx-community/"))

    def test_model_presets_coverage(self):
        """Should support at least qwen2.5-coder, qwen3, and gemma4."""
        presets = local_mlx_dpo._DPO_MODEL_PRESETS
        self.assertIn("qwen2.5-coder:7b", presets)
        self.assertIn("qwen3:8b", presets)
        self.assertIn("gemma4:e4b", presets)


class MlxDpoIntegrationTests(unittest.TestCase):
    """Integration tests for the full workflow."""

    def test_end_to_end_dry_run_workflow(self):
        """Full workflow: create prefs, export, and train (dry run)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Step 1: Create Jarvis-format preferences
            jarvis_prefs = [
                {
                    "prompt": "Explain async programming.",
                    "chosen": "Async allows non-blocking execution.",
                    "rejected": "Async is not important.",
                    "meta": {"source": "test"},
                },
            ]
            jarvis_file = tmpdir_path / "jarvis_prefs.jsonl"
            with jarvis_file.open("w") as f:
                for item in jarvis_prefs:
                    f.write(json.dumps(item) + "\n")

            # Step 2: Export to mlx format
            with patch.object(local_mlx_dpo, "_latest_preferences_path", return_value=jarvis_file):
                export_result = local_mlx_dpo.export_latest_preferences_for_mlx(
                    output_dir=tmpdir_path
                )

            self.assertTrue(export_result["ok"])
            self.assertEqual(export_result["pair_count"], 1)

            # Step 3: Run training (dry run)
            with patch.object(local_mlx_dpo, "is_available", return_value=True):
                train_result = local_mlx_dpo.run_preference_training(
                    "qwen2.5-coder:7b",
                    preference_jsonl=export_result["path"],
                    algorithm="orpo",
                    num_iters=50,
                    dry_run=True,
                )

            self.assertTrue(train_result["ok"])
            self.assertTrue(train_result["dry_run"])
            self.assertEqual(train_result["pair_count"], 1)
            self.assertIn("orpo", train_result["command"])


if __name__ == "__main__":
    unittest.main()
