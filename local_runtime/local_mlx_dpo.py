"""
Local DPO/ORPO preference training via mlx-lm-lora on Apple Silicon.

Supports running preference optimization algorithms (DPO, ORPO, IPO) locally
on macOS without cloud dependencies, using preference pairs exported from
Jarvis's local training pipeline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import runtime_state

_MLX_LM_LORA_AVAILABLE = False
try:
    import mlx.core as mx
    import mlx_lm_lora
    _MLX_LM_LORA_AVAILABLE = True
except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINING_ROOT = runtime_state.writable_data_path("training", seed_from=REPO_ROOT / "training")
PREFERENCES_DIR = TRAINING_ROOT / "preferences"
MLX_ADAPTERS_DIR = TRAINING_ROOT / "mlx_dpo_adapters"


_DPO_MODEL_PRESETS = {
    "qwen2.5-coder:7b": {
        "mlx_id": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        "lora_layers": 16,
    },
    "qwen3:8b": {
        "mlx_id": "mlx-community/Qwen3-8B-4bit",
        "lora_layers": 16,
    },
    "gemma4:e4b": {
        "mlx_id": "mlx-community/gemma-4-e4b-4bit",
        "lora_layers": 8,
    },
}


def _is_apple_silicon() -> bool:
    """Check if running on Apple Silicon Mac."""
    import platform
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _timestamp() -> str:
    """Return ISO timestamp for artifact naming."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_dirs() -> None:
    """Create training directories if needed."""
    for path in (PREFERENCES_DIR, MLX_ADAPTERS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def is_available() -> bool:
    """Return True if mlx-lm-lora is installed and Apple Silicon detected."""
    return _MLX_LM_LORA_AVAILABLE and _is_apple_silicon()


def status() -> dict:
    """Return availability status with version info."""
    result = {
        "available": is_available(),
        "mlx_lm_lora_installed": _MLX_LM_LORA_AVAILABLE,
        "apple_silicon": _is_apple_silicon(),
    }
    if _MLX_LM_LORA_AVAILABLE:
        try:
            result["mlx_lm_lora_version"] = getattr(mlx_lm_lora, "__version__", "unknown")
        except Exception:
            result["mlx_lm_lora_version"] = "unknown"
    return result


def list_algorithms() -> list[str]:
    """Return supported preference training algorithms."""
    return ["dpo", "orpo", "ipo", "grpo"]


def _latest_preferences_path() -> Path | None:
    """Find the most recent preference JSONL file."""
    _ensure_dirs()
    preferences = sorted(PREFERENCES_DIR.glob("*.jsonl"))
    return preferences[-1] if preferences else None


def _build_grpo_prompt_file(output_path: Path) -> int:
    """
    Extract user prompts from the most recent SFT training pack for GRPO.

    GRPO trains by sampling multiple completions per prompt and scoring them
    with reward functions — it needs prompts only, not chosen/rejected pairs.
    We source prompts from the overnight SFT pack (real Jarvis interactions).

    Returns number of prompts written, or 0 if no pack found.
    """
    packs_dir = TRAINING_ROOT / "packs"
    packs = sorted(packs_dir.glob("overnight_*.jsonl")) if packs_dir.exists() else []
    if not packs:
        return 0

    latest_pack = packs[-1]
    prompts: list[dict] = []
    seen: set[str] = set()

    for line in latest_pack.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        messages = row.get("messages", [])
        user_msg = next(
            (m["content"] for m in messages if m.get("role") == "user"), None
        )
        if user_msg and user_msg not in seen:
            seen.add(user_msg)
            # GRPO data format: single "prompt" key per row
            prompts.append({"prompt": user_msg})

    if not prompts:
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for row in prompts:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(prompts)


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file and return list of dicts."""
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_jsonl(path: Path, examples: list[dict]) -> None:
    """Write list of dicts to JSONL file."""
    with path.open("w", encoding="utf-8") as f:
        for example in examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")


def _assistant_text(value) -> str:
    """Normalize Jarvis or mlx preference answer fields into plain text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("content") or ""))
        return "\n".join(parts).strip()
    return ""


def validate_preference_dataset(pairs: list[dict]) -> dict:
    """Validate preference pairs before local preference optimization."""
    errors: list[str] = []
    valid = 0
    bad_chosen_markers = (
        "traceback",
        "runtimeerror",
        "exception:",
        "training error",
        "i hit an upstream model error",
    )
    auto_sources = {"auto", "autopilot", "mined", "failure_mining", "local_eval", "overnight"}

    for idx, pair in enumerate(pairs, start=1):
        prompt = str(pair.get("prompt") or "").strip()
        chosen = _assistant_text(pair.get("chosen"))
        rejected = _assistant_text(pair.get("rejected"))
        meta = pair.get("meta") if isinstance(pair.get("meta"), dict) else {}
        source = str(meta.get("source") or pair.get("source") or "").strip().lower()
        reviewed = bool(pair.get("reviewed") or pair.get("trusted") or meta.get("reviewed") or meta.get("trusted"))

        if not prompt or not chosen or not rejected:
            errors.append(f"pair {idx}: missing prompt/chosen/rejected")
            continue
        if chosen == rejected:
            errors.append(f"pair {idx}: chosen and rejected are identical")
            continue
        if any(marker in chosen.lower() for marker in bad_chosen_markers):
            errors.append(f"pair {idx}: chosen answer looks like a runtime failure")
            continue
        if source in auto_sources and not reviewed:
            errors.append(f"pair {idx}: auto-mined pair is not marked reviewed/trusted")
            continue
        valid += 1

    return {
        "ok": valid > 0 and not errors,
        "valid_count": valid,
        "error_count": len(errors),
        "errors": errors,
    }


def export_latest_preferences_for_mlx(output_dir: str | Path | None = None) -> dict:
    """
    Find the latest Jarvis preference JSONL and convert to mlx-lm-lora format.

    Jarvis format: {"prompt": str, "chosen": str, "rejected": str}
    mlx-lm-lora DPO format:
        {"prompt": str,
         "chosen": [{"role":"assistant","content":str}],
         "rejected": [{"role":"assistant","content":str}]}

    Returns {"ok": bool, "path": str, "pair_count": int, "error": str|None}
    """
    _ensure_dirs()

    input_path = _latest_preferences_path()
    if not input_path:
        return {
            "ok": False,
            "path": None,
            "pair_count": 0,
            "error": "No preference JSONL found in training/preferences/",
        }

    pairs = _read_jsonl(input_path)
    if not pairs:
        return {
            "ok": False,
            "path": None,
            "pair_count": 0,
            "error": f"Preference file is empty: {input_path}",
        }

    converted = []
    for pair in pairs:
        prompt = (pair.get("prompt") or "").strip()
        chosen = (pair.get("chosen") or "").strip()
        rejected = (pair.get("rejected") or "").strip()

        if not prompt or not chosen or not rejected:
            continue

        converted.append({
            "prompt": prompt,
            "chosen": [{"role": "assistant", "content": chosen}],
            "rejected": [{"role": "assistant", "content": rejected}],
        })

    if not converted:
        return {
            "ok": False,
            "path": None,
            "pair_count": 0,
            "error": "No valid preference pairs after conversion",
        }

    if output_dir:
        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)
        output_path = output_dir_path / f"mlx_dpo_converted_{_timestamp()}.jsonl"
    else:
        output_path = PREFERENCES_DIR / f"mlx_dpo_converted_{_timestamp()}.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_path, converted)

    return {
        "ok": True,
        "path": str(output_path),
        "pair_count": len(converted),
        "error": None,
    }


def run_preference_training(
    ollama_tag: str,
    preference_jsonl: str | Path | None = None,
    *,
    algorithm: str = "orpo",
    output_dir: str | Path | None = None,
    num_iters: int = 50,
    learning_rate: float = 5e-6,
    lora_rank: int = 8,
    beta: float = 0.1,
    # GRPO-specific parameters (ignored for DPO/ORPO/IPO)
    grpo_group_size: int = 4,
    grpo_reward_functions: str = "accuracy_reward,format_reward",
    grpo_reward_weights: str = "[0.7, 0.3]",
    grpo_max_completion_length: int = 256,
    grpo_temperature: float = 0.8,
    dry_run: bool = False,
) -> dict:
    """
    Run DPO/ORPO/IPO/GRPO preference fine-tuning via mlx-lm-lora.

    Args:
        ollama_tag: Model tag like "qwen2.5-coder:7b"
        preference_jsonl: Path to preference JSONL (auto-finds latest if None).
            For GRPO: path to prompt-only JSONL; auto-extracted from SFT pack if None.
        algorithm: "dpo", "orpo", "ipo", or "grpo" (default: "orpo")
        output_dir: Where to save adapter (default: TRAINING_ROOT/mlx_dpo_adapters/...)
        num_iters: Training iterations (default: 50)
        learning_rate: Learning rate (default: 5e-6)
        lora_rank: LoRA rank (default: 8)
        beta: KL penalty for DPO/IPO (ignored for GRPO)
        grpo_group_size: Completions sampled per prompt for GRPO (default: 4)
        grpo_reward_functions: Comma-separated reward fn names for GRPO
        grpo_reward_weights: JSON array of weights matching reward functions
        grpo_max_completion_length: Max tokens per GRPO completion (default: 256)
        grpo_temperature: Sampling temperature for GRPO completions (default: 0.8)
        dry_run: Return command without executing (default: False)

    Returns dict with:
        - ok: bool
        - adapter_path: str | None
        - algorithm: str
        - model_tag: str
        - iters: int
        - duration_sec: float | None
        - error: str | None
        - command: str
        - dry_run: bool
        - pair_count: int
    """
    _ensure_dirs()
    algorithm = algorithm.strip().lower()

    if algorithm not in list_algorithms():
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": (
                "Unsupported preference algorithm: grpo is not implemented for Jarvis's local MLX lane yet."
                if algorithm == "grpo"
                else f"Unsupported preference algorithm: {algorithm}. Supported: {list_algorithms()}"
            ),
            "command": "",
            "dry_run": dry_run,
            "pair_count": 0,
        }

    # Check availability
    if not is_available():
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": "mlx-lm-lora not available or not on Apple Silicon",
            "command": "",
            "dry_run": dry_run,
            "pair_count": 0,
        }

    # Look up model preset
    if ollama_tag not in _DPO_MODEL_PRESETS:
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": f"Unsupported model: {ollama_tag}. Supported: {list(_DPO_MODEL_PRESETS.keys())}",
            "command": "",
            "dry_run": dry_run,
            "pair_count": 0,
        }

    preset = _DPO_MODEL_PRESETS[ollama_tag]
    model_id = preset["mlx_id"]

    # ── GRPO branch — uses prompt-only data and reward functions ─────────────
    if algorithm == "grpo":
        model_slug = ollama_tag.replace(":", "_").replace(".", "_")
        adapter_dir = MLX_ADAPTERS_DIR / "grpo" / model_slug / _timestamp()
        if output_dir:
            adapter_dir = Path(output_dir)
        adapter_dir.mkdir(parents=True, exist_ok=True)

        # Resolve prompt data: explicit file or auto-extract from SFT pack
        if preference_jsonl:
            grpo_data_path = Path(preference_jsonl)
            if not grpo_data_path.exists():
                return {
                    "ok": False, "adapter_path": None, "algorithm": "grpo",
                    "model_tag": ollama_tag, "iters": num_iters,
                    "duration_sec": None,
                    "error": f"Prompt file not found: {preference_jsonl}",
                    "command": "", "dry_run": dry_run, "pair_count": 0,
                }
            prompt_count = sum(1 for _ in grpo_data_path.read_text().splitlines() if _.strip())
        else:
            grpo_data_path = PREFERENCES_DIR / f"grpo_prompts_{_timestamp()}.jsonl"
            prompt_count = _build_grpo_prompt_file(grpo_data_path)
            if prompt_count == 0:
                return {
                    "ok": False, "adapter_path": None, "algorithm": "grpo",
                    "model_tag": ollama_tag, "iters": num_iters,
                    "duration_sec": None,
                    "error": "No SFT pack found to extract GRPO prompts from. "
                             "Run build_training_pack() first or pass preference_jsonl.",
                    "command": "", "dry_run": dry_run, "pair_count": 0,
                }

        cmd = [
            sys.executable, "-m", "mlx_lm_lora.train",
            "--model", model_id,
            "--train-mode", "grpo",
            "--data", str(grpo_data_path),
            "--num-iterations", str(num_iters),
            "--learning-rate", str(learning_rate),
            "--rank", str(lora_rank),
            "--group-size", str(grpo_group_size),
            "--reward-functions", grpo_reward_functions,
            "--reward-weights", grpo_reward_weights,
            "--max-completion-length", str(grpo_max_completion_length),
            "--temperature", str(grpo_temperature),
            "--adapter-path", str(adapter_dir),
        ]
        command_str = " ".join(cmd)

        if dry_run:
            return {
                "ok": True, "adapter_path": None, "algorithm": "grpo",
                "model_tag": ollama_tag, "iters": num_iters,
                "duration_sec": None, "error": None,
                "command": command_str, "dry_run": True,
                "pair_count": prompt_count,
            }

        start_time = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            duration = time.time() - start_time
            if result.returncode != 0:
                return {
                    "ok": False, "adapter_path": None, "algorithm": "grpo",
                    "model_tag": ollama_tag, "iters": num_iters,
                    "duration_sec": duration,
                    "error": f"GRPO training failed: {result.stderr[-500:]}",
                    "command": command_str, "dry_run": False,
                    "pair_count": prompt_count,
                }
            return {
                "ok": True, "adapter_path": str(adapter_dir), "algorithm": "grpo",
                "model_tag": ollama_tag, "iters": num_iters,
                "duration_sec": duration, "error": None,
                "command": command_str, "dry_run": False,
                "pair_count": prompt_count,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False, "adapter_path": None, "algorithm": "grpo",
                "model_tag": ollama_tag, "iters": num_iters,
                "duration_sec": 3600.0,
                "error": "GRPO training timed out after 1 hour.",
                "command": command_str, "dry_run": False,
                "pair_count": prompt_count,
            }
        except Exception as exc:
            return {
                "ok": False, "adapter_path": None, "algorithm": "grpo",
                "model_tag": ollama_tag, "iters": num_iters,
                "duration_sec": None, "error": str(exc),
                "command": command_str, "dry_run": False,
                "pair_count": prompt_count,
            }
    # ── end GRPO branch ───────────────────────────────────────────────────────

    # Find or convert preference data
    if preference_jsonl:
        data_path = Path(preference_jsonl)
        if not data_path.exists():
            return {
                "ok": False,
                "adapter_path": None,
                "algorithm": algorithm,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": None,
                "error": f"Preference file not found: {preference_jsonl}",
                "command": "",
                "dry_run": dry_run,
                "pair_count": 0,
            }
        pairs = _read_jsonl(data_path)
    else:
        export_result = export_latest_preferences_for_mlx()
        if not export_result["ok"]:
            return {
                "ok": False,
                "adapter_path": None,
                "algorithm": algorithm,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": None,
                "error": export_result.get("error", "Failed to export preferences"),
                "command": "",
                "dry_run": dry_run,
                "pair_count": 0,
            }
        data_path = Path(export_result["path"])
        pairs = _read_jsonl(data_path)

    if not pairs:
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": f"No preference pairs found in {data_path}",
            "command": "",
            "dry_run": dry_run,
            "pair_count": 0,
        }

    validation = validate_preference_dataset(pairs)
    if not validation["ok"]:
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": "Preference dataset failed validation: " + "; ".join(validation["errors"][:3]),
            "command": "",
            "dry_run": dry_run,
            "pair_count": len(pairs),
        }

    # Prepare output directory
    model_slug = ollama_tag.replace(":", "_").replace(".", "_")
    adapter_dir = MLX_ADAPTERS_DIR / algorithm / model_slug / _timestamp()
    adapter_dir.mkdir(parents=True, exist_ok=True)

    if output_dir:
        adapter_dir = Path(output_dir)
        adapter_dir.mkdir(parents=True, exist_ok=True)

    # Build the training command
    cmd = [
        sys.executable,
        "-m",
        "mlx_lm_lora.train",
        "--model",
        model_id,
        "--algorithm",
        algorithm,
        "--data",
        str(data_path),
        "--num-iterations",
        str(num_iters),
        "--learning-rate",
        str(learning_rate),
        "--rank",
        str(lora_rank),
        "--beta",
        str(beta),
        "--adapter-path",
        str(adapter_dir),
    ]

    command_str = " ".join(cmd)

    if dry_run:
        return {
            "ok": True,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": None,
            "command": command_str,
            "dry_run": True,
            "pair_count": len(pairs),
        }

    # Run the training
    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
        )
        duration = time.time() - start_time

        if result.returncode != 0:
            return {
                "ok": False,
                "adapter_path": None,
                "algorithm": algorithm,
                "model_tag": ollama_tag,
                "iters": num_iters,
                "duration_sec": duration,
                "error": f"Training failed: {result.stderr}",
                "command": command_str,
                "dry_run": False,
                "pair_count": len(pairs),
            }

        return {
            "ok": True,
            "adapter_path": str(adapter_dir),
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": duration,
            "error": None,
            "command": command_str,
            "dry_run": False,
            "pair_count": len(pairs),
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": "Training timed out after 1 hour",
            "command": command_str,
            "dry_run": False,
            "pair_count": len(pairs),
        }
    except Exception as e:
        return {
            "ok": False,
            "adapter_path": None,
            "algorithm": algorithm,
            "model_tag": ollama_tag,
            "iters": num_iters,
            "duration_sec": None,
            "error": f"Training error: {str(e)}",
            "command": command_str,
            "dry_run": False,
            "pair_count": len(pairs),
        }
