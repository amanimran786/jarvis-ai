"""
Local-model improvement pipeline for Jarvis.

This does not magically make small local models equal to frontier hosted models.
It does give Jarvis a practical, low-cost loop:

1. Export strong interaction examples into SFT-style JSONL.
2. Distill failed or weak interactions with a stronger teacher model only on demand.
3. Generate an Ollama Modelfile target for a tuned Jarvis-local model.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

import evals
import skills
from brains.brain_claude import ask_claude
from config import LOCAL_DEFAULT, LOCAL_TUNED, SONNET, SYSTEM_PROMPT


REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINING_ROOT = REPO_ROOT / "training"
EXPORTS_DIR = TRAINING_ROOT / "exports"
DISTILLED_DIR = TRAINING_ROOT / "distilled"
TEACHER_DIR = TRAINING_ROOT / "teacher_examples"
PREFERENCES_DIR = TRAINING_ROOT / "preferences"
MODELFILES_DIR = TRAINING_ROOT / "modelfiles"
PACKS_DIR = TRAINING_ROOT / "packs"
HANDOFFS_DIR = TRAINING_ROOT / "handoffs"
COLAB_DEFAULT_TARGET = "qwen2.5-coder:7b"

MODEL_PRESETS = {
    "llama3.1:8b": {
        "slug": "llama3_1_8b",
        "label": "Llama 3.1 8B Instruct",
        "hf_model": "meta-llama/Llama-3.1-8B-Instruct",
        "unsloth_model": "unsloth/llama-3.1-8b-unsloth-bnb-4bit",
        "unsloth_chat_template": "llama",
        "axolotl_chat_template": "tokenizer_default",
        "sequence_len": 2048,
    },
    "qwen2.5-coder:7b": {
        "slug": "qwen2_5_coder_7b",
        "label": "Qwen 2.5 Coder 7B Instruct",
        "hf_model": "Qwen/Qwen2.5-Coder-7B-Instruct",
        "unsloth_model": "Qwen/Qwen2.5-Coder-7B-Instruct",
        "unsloth_chat_template": "chatml",
        "axolotl_chat_template": "tokenizer_default",
        "sequence_len": 4096,
    },
}

EXPERT_DISTILL_CASES = [
    {
        "id": "tech_kv_cache",
        "category": "technology_science",
        "prompt": "Why do transformer KV caches improve inference speed, and what are the memory tradeoffs as sequence length grows?",
        "expected": "Lead with the mechanism, explain the compute-versus-memory tradeoff, and name the dominant scaling constraint.",
    },
    {
        "id": "science_entropy",
        "category": "technology_science",
        "prompt": "What is the difference between entropy in thermodynamics and entropy in information theory?",
        "expected": "State the shared mathematical structure, then distinguish the physical meaning from the probabilistic meaning without generic filler.",
    },
    {
        "id": "science_crispr",
        "category": "technology_science",
        "prompt": "What are the main ways CRISPR editing creates off-target effects, and how do researchers reduce them?",
        "expected": "Explain the main mechanisms, then map them to the main mitigation strategies with concrete terminology.",
    },
]


def _ensure_dirs() -> None:
    for path in (EXPORTS_DIR, DISTILLED_DIR, TEACHER_DIR, PREFERENCES_DIR, MODELFILES_DIR, PACKS_DIR, HANDOFFS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "sample"


def _linked_failure_map(data: dict) -> dict[str, list[dict]]:
    linked: dict[str, list[dict]] = {}
    for failure in data.get("failures", []):
        interaction_id = failure.get("interaction_id")
        if not interaction_id:
            continue
        linked.setdefault(interaction_id, []).append(failure)
    return linked


def _write_jsonl(path: Path, examples: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for example in examples:
            f.write(json.dumps(example, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _latest_pack_path() -> Path | None:
    _ensure_dirs()
    packs = sorted(PACKS_DIR.glob("*.jsonl"))
    return packs[-1] if packs else None


def _messages_to_instruct(messages: list[dict]) -> dict:
    user_turns = [m.get("content", "").strip() for m in messages if m.get("role") == "user" and m.get("content")]
    assistant_turns = [m.get("content", "").strip() for m in messages if m.get("role") == "assistant" and m.get("content")]

    instruction = user_turns[-1] if user_turns else ""
    output = assistant_turns[-1] if assistant_turns else ""
    earlier_users = user_turns[:-1]
    earlier_assistant = assistant_turns[:-1]

    history_parts = []
    for user_msg, assistant_msg in zip(earlier_users, earlier_assistant):
        history_parts.append(f"User: {user_msg}")
        history_parts.append(f"Assistant: {assistant_msg}")

    return {
        "instruction": instruction,
        "input": "\n".join(history_parts).strip(),
        "output": output,
    }


def _split_examples(examples: list[dict], val_fraction: float = 0.1) -> tuple[list[dict], list[dict]]:
    if not examples:
        return [], []
    val_count = max(1, int(round(len(examples) * val_fraction))) if len(examples) >= 10 else 1 if len(examples) > 4 else 0
    if val_count == 0:
        return examples, []
    return examples[:-val_count], examples[-val_count:]


def _write_handoff_jsonl(path: Path, rows: list[dict]) -> None:
    _write_jsonl(path, rows)


def _notebook(cells: list[dict]) -> dict:
    return {
        "cells": cells,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _markdown_cell(text: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": [line + "\n" for line in text.splitlines()],
    }


def _code_cell(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": [line + "\n" for line in source.splitlines()],
    }


def _build_unsloth_script(data_rel: str, val_rel: str, preset: dict) -> str:
    return "\n".join(
        [
            "from datasets import load_dataset",
            "from transformers import TrainingArguments",
            "from trl import SFTTrainer",
            "from unsloth import FastLanguageModel, is_bfloat16_supported",
            "from unsloth.chat_templates import get_chat_template",
            "",
            f'MODEL_NAME = "{preset["unsloth_model"]}"',
            f'DATA_PATH = "{data_rel}"',
            f'EVAL_PATH = "{val_rel}"',
            f'CHAT_TEMPLATE = "{preset["unsloth_chat_template"]}"',
            f"MAX_SEQ_LENGTH = {preset['sequence_len']}",
            "",
            "model, tokenizer = FastLanguageModel.from_pretrained(",
            "    model_name=MODEL_NAME,",
            "    max_seq_length=MAX_SEQ_LENGTH,",
            "    load_in_4bit=True,",
            "    dtype=None,",
            ")",
            "",
            "tokenizer = get_chat_template(tokenizer, chat_template=CHAT_TEMPLATE)",
            "",
            "model = FastLanguageModel.get_peft_model(",
            "    model,",
            "    r=16,",
            "    target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj'],",
            "    lora_alpha=16,",
            "    lora_dropout=0,",
            "    bias='none',",
            "    use_gradient_checkpointing='unsloth',",
            "    random_state=3407,",
            ")",
            "",
            "train_dataset = load_dataset('json', data_files=DATA_PATH, split='train')",
            "eval_dataset = load_dataset('json', data_files=EVAL_PATH, split='train') if EVAL_PATH else None",
            "",
            "def format_messages(batch):",
            "    texts = [",
            "        tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)",
            "        for messages in batch['messages']",
            "    ]",
            "    return {'text': texts}",
            "",
            "train_dataset = train_dataset.map(format_messages, batched=True)",
            "if eval_dataset is not None:",
            "    eval_dataset = eval_dataset.map(format_messages, batched=True)",
            "",
            "trainer = SFTTrainer(",
            "    model=model,",
            "    tokenizer=tokenizer,",
            "    train_dataset=train_dataset,",
            "    eval_dataset=eval_dataset,",
            "    dataset_text_field='text',",
            "    max_seq_length=MAX_SEQ_LENGTH,",
            "    packing=False,",
            "    args=TrainingArguments(",
            "        output_dir='outputs',",
            "        per_device_train_batch_size=2,",
            "        gradient_accumulation_steps=4,",
            "        warmup_steps=10,",
            "        num_train_epochs=3,",
            "        learning_rate=2e-4,",
            "        logging_steps=1,",
            "        optim='adamw_8bit',",
            "        weight_decay=0.01,",
            "        lr_scheduler_type='linear',",
            "        fp16=not is_bfloat16_supported(),",
            "        bf16=is_bfloat16_supported(),",
            "        report_to='none',",
            "    ),",
            ")",
            "",
            "trainer.train()",
            "model.save_pretrained('outputs/final_adapter')",
            "tokenizer.save_pretrained('outputs/final_adapter')",
            "",
        ]
    )


def _build_axolotl_yaml(train_rel: str, val_rel: str, preset: dict) -> str:
    return "\n".join(
        [
            f'base_model: {preset["hf_model"]}',
            "",
            "adapter: qlora",
            "load_in_4bit: true",
            "strict: false",
            "chat_template: tokenizer_default",
            "",
            "datasets:",
            f"  - path: {train_rel}",
            "    type: chat_template",
            f'    chat_template: {preset["axolotl_chat_template"]}',
            "    field_messages: messages",
            "    message_property_mappings:",
            "      role: role",
            "      content: content",
            "    roles:",
            "      assistant:",
            "        - assistant",
            "      user:",
            "        - user",
            "    roles_to_train: ['assistant']",
            "    train_on_eos: turn",
            "",
            "test_datasets:",
            f"  - path: {val_rel}",
            "    type: chat_template",
            f'    chat_template: {preset["axolotl_chat_template"]}',
            "    field_messages: messages",
            "    message_property_mappings:",
            "      role: role",
            "      content: content",
            "    roles:",
            "      assistant:",
            "        - assistant",
            "      user:",
            "        - user",
            "",
            f"sequence_len: {preset['sequence_len']}",
            "sample_packing: false",
            "pad_to_sequence_len: false",
            "train_on_inputs: false",
            "gradient_accumulation_steps: 4",
            "micro_batch_size: 2",
            "num_epochs: 3",
            "learning_rate: 2e-4",
            "optimizer: adamw_torch_fused",
            "lr_scheduler: cosine",
            "bf16: auto",
            "gradient_checkpointing: true",
            "flash_attention: true",
            "logging_steps: 1",
            "warmup_steps: 10",
            "evals_per_epoch: 1",
            "saves_per_epoch: 1",
            "save_total_limit: 2",
            "output_dir: ./outputs",
            "dataset_prepared_path: ./prepared",
            "",
        ]
    )


def _build_handoff_readme(preset: dict, handoff_dir: Path) -> str:
    return "\n".join(
        [
            f"# Jarvis Fine-Tune Handoff for {preset['label']}",
            "",
            "This folder is the offline fine-tune handoff generated from Jarvis's local training pipeline.",
            "",
            "Contents:",
            "- data/train_messages.jsonl and data/val_messages.jsonl for conversation-style supervised fine-tuning",
            "- data/train_instruct.jsonl and data/val_instruct.jsonl for instruction-style experiments",
            "- configs/axolotl_qlora.yml for Axolotl",
            "- scripts/train_unsloth.py for Unsloth",
            "",
            "Notes:",
            "- Unsloth's local install documentation says local installation is for Linux, WSL, or Windows environments rather than macOS.",
            "- Axolotl's quickstart documentation requires Python 3.11 and a supported GPU environment.",
            "- This handoff is meant to move from your Mac to a Linux GPU box or cloud trainer.",
            "",
            "Recommended flow:",
            "1. Copy this folder to the training machine.",
            "2. Start with the Unsloth script or the Axolotl YAML as a baseline QLoRA run.",
            "3. Train adapters first before attempting any full fine-tune.",
            "4. Evaluate the adapter on Jarvis's recent local failure prompts before promoting it.",
            "",
            f"Generated in: {handoff_dir}",
            "",
        ]
    )


def _build_colab_notebook(target: str, preset: dict, train_file: str, val_file: str) -> dict:
    model_name = preset["unsloth_model"]
    chat_template = preset["unsloth_chat_template"]
    max_seq_length = preset["sequence_len"]
    return _notebook(
        [
            _markdown_cell(
                "\n".join(
                    [
                        "# Jarvis Open LLM Colab Trainer",
                        "",
                        "This notebook fine-tunes an open model on a Jarvis training pack using Google Colab as an interactive training lab.",
                        "",
                        "Use Runtime > Change runtime type > GPU. Free Colab GPU access is best-effort, not guaranteed, and this notebook should not be used as a background service or remote-control workaround.",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "from google.colab import drive",
                        "drive.mount('/content/drive')",
                        "",
                        "# Copy this generated handoff folder into Google Drive, then adjust if you moved it.",
                        "HANDOFF_DIR = '/content/drive/MyDrive/jarvis_colab_handoff'",
                        f"TRAIN_FILE = f'{{HANDOFF_DIR}}/data/{train_file}'",
                        f"VAL_FILE = f'{{HANDOFF_DIR}}/data/{val_file}'",
                        "OUTPUT_DIR = f'{HANDOFF_DIR}/outputs'",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "import os, json, pathlib, torch",
                        "for path in (TRAIN_FILE, VAL_FILE):",
                        "    if not pathlib.Path(path).exists():",
                        "        raise FileNotFoundError(f'Missing {path}. Upload the handoff folder to Google Drive first.')",
                        "print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')",
                        "print('Train file:', TRAIN_FILE)",
                        "print('Validation file:', VAL_FILE)",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "%%capture",
                        "!pip install -U unsloth",
                        "!pip install -U --no-deps trl peft accelerate bitsandbytes datasets",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "from datasets import load_dataset",
                        "from transformers import TrainingArguments",
                        "from trl import SFTTrainer",
                        "from unsloth import FastLanguageModel, is_bfloat16_supported",
                        "from unsloth.chat_templates import get_chat_template",
                        "",
                        f"MODEL_NAME = '{model_name}'",
                        f"CHAT_TEMPLATE = '{chat_template}'",
                        f"MAX_SEQ_LENGTH = {max_seq_length}",
                        "",
                        "model, tokenizer = FastLanguageModel.from_pretrained(",
                        "    model_name=MODEL_NAME,",
                        "    max_seq_length=MAX_SEQ_LENGTH,",
                        "    dtype=None,",
                        "    load_in_4bit=True,",
                        ")",
                        "tokenizer = get_chat_template(tokenizer, chat_template=CHAT_TEMPLATE)",
                        "model = FastLanguageModel.get_peft_model(",
                        "    model,",
                        "    r=16,",
                        "    target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj'],",
                        "    lora_alpha=16,",
                        "    lora_dropout=0,",
                        "    bias='none',",
                        "    use_gradient_checkpointing='unsloth',",
                        "    random_state=3407,",
                        ")",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "train_dataset = load_dataset('json', data_files=TRAIN_FILE, split='train')",
                        "eval_dataset = load_dataset('json', data_files=VAL_FILE, split='train')",
                        "",
                        "def format_messages(batch):",
                        "    texts = [tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False) for messages in batch['messages']]",
                        "    return {'text': texts}",
                        "",
                        "train_dataset = train_dataset.map(format_messages, batched=True)",
                        "eval_dataset = eval_dataset.map(format_messages, batched=True)",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "trainer = SFTTrainer(",
                        "    model=model,",
                        "    tokenizer=tokenizer,",
                        "    train_dataset=train_dataset,",
                        "    eval_dataset=eval_dataset,",
                        "    dataset_text_field='text',",
                        "    max_seq_length=MAX_SEQ_LENGTH,",
                        "    packing=False,",
                        "    args=TrainingArguments(",
                        "        output_dir=OUTPUT_DIR,",
                        "        per_device_train_batch_size=2,",
                        "        gradient_accumulation_steps=4,",
                        "        warmup_steps=10,",
                        "        num_train_epochs=2,",
                        "        learning_rate=2e-4,",
                        "        logging_steps=1,",
                        "        optim='adamw_8bit',",
                        "        weight_decay=0.01,",
                        "        lr_scheduler_type='linear',",
                        "        fp16=not is_bfloat16_supported(),",
                        "        bf16=is_bfloat16_supported(),",
                        "        report_to='none',",
                        "    ),",
                        ")",
                        "trainer.train()",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "FINAL_ADAPTER = f'{OUTPUT_DIR}/final_adapter'",
                        "model.save_pretrained(FINAL_ADAPTER)",
                        "tokenizer.save_pretrained(FINAL_ADAPTER)",
                        "print('Saved adapter:', FINAL_ADAPTER)",
                        "print('Next: download or copy this adapter back to the Mac, run Jarvis evals, then promote only if the gate passes.')",
                    ]
                )
            ),
            _markdown_cell(
                "\n".join(
                    [
                        "## Import Back Into Jarvis",
                        "",
                        "After training, bring the adapter or converted model back to the Mac. Do not promote it just because training completed. Run Jarvis local evals first and keep the current model if the candidate does not beat the baseline.",
                        "",
                        f"Target preset: `{target}`. Base HF model: `{model_name}`.",
                    ]
                )
            ),
        ]
    )


def _build_colab_readme(target: str, preset: dict, handoff_dir: Path) -> str:
    return "\n".join(
        [
            f"# Jarvis Colab Training Handoff for {preset['label']}",
            "",
            "This folder prepares Jarvis's local training pack for interactive Google Colab training.",
            "",
            "Use this as a training lab, not as a 24/7 Jarvis host. Free Colab GPU availability is not guaranteed, usage limits fluctuate, and remote-control/background-service patterns are outside the free-tier intent.",
            "",
            "Flow:",
            "1. Copy this folder to Google Drive as `MyDrive/jarvis_colab_handoff` or edit `HANDOFF_DIR` in the notebook.",
            "2. Open `Jarvis_Open_LLM_Trainer.ipynb` in Colab.",
            "3. Select Runtime > Change runtime type > GPU.",
            "4. Run the notebook cells in order.",
            "5. Copy the trained adapter or converted model back to the Mac.",
            "6. Run Jarvis local evals and promote only if the eval gate clears.",
            "",
            f"Target preset: {target}",
            f"Hugging Face model: {preset['hf_model']}",
            f"Generated in: {handoff_dir}",
            "",
        ]
    )


def _build_colab_preference_notebook(target: str, preset: dict, train_file: str, val_file: str) -> dict:
    model_name = preset["unsloth_model"]
    chat_template = preset["unsloth_chat_template"]
    max_seq_length = preset["sequence_len"]
    return _notebook(
        [
            _markdown_cell(
                "\n".join(
                    [
                        "# Jarvis Preference RL Colab Trainer",
                        "",
                        "This notebook runs RLHF-style preference optimization on Jarvis preference pairs using an open model adapter.",
                        "",
                        "It is a training lab only. Free Colab GPU access is best-effort, not guaranteed, and trained adapters must pass local Jarvis evals before promotion.",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "from google.colab import drive",
                        "drive.mount('/content/drive')",
                        "",
                        "# Copy this generated handoff folder into Google Drive, then adjust if you moved it.",
                        "HANDOFF_DIR = '/content/drive/MyDrive/jarvis_preference_rl_handoff'",
                        f"TRAIN_FILE = f'{{HANDOFF_DIR}}/data/{train_file}'",
                        f"VAL_FILE = f'{{HANDOFF_DIR}}/data/{val_file}'",
                        "OUTPUT_DIR = f'{HANDOFF_DIR}/outputs'",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "import json, pathlib, torch",
                        "for path in (TRAIN_FILE, VAL_FILE):",
                        "    if not pathlib.Path(path).exists():",
                        "        raise FileNotFoundError(f'Missing {path}. Upload the handoff folder to Google Drive first.')",
                        "print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')",
                        "print('Train file:', TRAIN_FILE)",
                        "print('Validation file:', VAL_FILE)",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "%%capture",
                        "!pip install -U unsloth",
                        "!pip install -U --no-deps trl peft accelerate bitsandbytes datasets",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "from datasets import load_dataset",
                        "from trl import DPOTrainer, DPOConfig",
                        "from unsloth import FastLanguageModel, is_bfloat16_supported",
                        "from unsloth.chat_templates import get_chat_template",
                        "",
                        f"MODEL_NAME = '{model_name}'",
                        f"CHAT_TEMPLATE = '{chat_template}'",
                        f"MAX_SEQ_LENGTH = {max_seq_length}",
                        "",
                        "model, tokenizer = FastLanguageModel.from_pretrained(",
                        "    model_name=MODEL_NAME,",
                        "    max_seq_length=MAX_SEQ_LENGTH,",
                        "    dtype=None,",
                        "    load_in_4bit=True,",
                        ")",
                        "tokenizer = get_chat_template(tokenizer, chat_template=CHAT_TEMPLATE)",
                        "model = FastLanguageModel.get_peft_model(",
                        "    model,",
                        "    r=16,",
                        "    target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj'],",
                        "    lora_alpha=16,",
                        "    lora_dropout=0,",
                        "    bias='none',",
                        "    use_gradient_checkpointing='unsloth',",
                        "    random_state=3407,",
                        ")",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "train_dataset = load_dataset('json', data_files=TRAIN_FILE, split='train')",
                        "eval_dataset = load_dataset('json', data_files=VAL_FILE, split='train')",
                        "",
                        "def format_pair(row):",
                        "    prompt_messages = [{'role': 'user', 'content': row['prompt']}]",
                        "    chosen_messages = prompt_messages + [{'role': 'assistant', 'content': row['chosen']}]",
                        "    rejected_messages = prompt_messages + [{'role': 'assistant', 'content': row['rejected']}]",
                        "    return {",
                        "        'prompt': tokenizer.apply_chat_template(prompt_messages, tokenize=False, add_generation_prompt=True),",
                        "        'chosen': tokenizer.apply_chat_template(chosen_messages, tokenize=False, add_generation_prompt=False),",
                        "        'rejected': tokenizer.apply_chat_template(rejected_messages, tokenize=False, add_generation_prompt=False),",
                        "    }",
                        "",
                        "train_dataset = train_dataset.map(format_pair)",
                        "eval_dataset = eval_dataset.map(format_pair)",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "args = DPOConfig(",
                        "    output_dir=OUTPUT_DIR,",
                        "    per_device_train_batch_size=1,",
                        "    gradient_accumulation_steps=8,",
                        "    warmup_steps=5,",
                        "    num_train_epochs=1,",
                        "    learning_rate=5e-6,",
                        "    beta=0.1,",
                        "    logging_steps=1,",
                        "    optim='adamw_8bit',",
                        "    lr_scheduler_type='linear',",
                        "    fp16=not is_bfloat16_supported(),",
                        "    bf16=is_bfloat16_supported(),",
                        "    report_to='none',",
                        ")",
                        "",
                        "try:",
                        "    trainer = DPOTrainer(model=model, ref_model=None, args=args, train_dataset=train_dataset, eval_dataset=eval_dataset, processing_class=tokenizer)",
                        "except TypeError:",
                        "    trainer = DPOTrainer(model=model, ref_model=None, args=args, train_dataset=train_dataset, eval_dataset=eval_dataset, tokenizer=tokenizer)",
                        "trainer.train()",
                    ]
                )
            ),
            _code_cell(
                "\n".join(
                    [
                        "FINAL_ADAPTER = f'{OUTPUT_DIR}/final_preference_adapter'",
                        "model.save_pretrained(FINAL_ADAPTER)",
                        "tokenizer.save_pretrained(FINAL_ADAPTER)",
                        "print('Saved preference adapter:', FINAL_ADAPTER)",
                        "print('Next: bring this adapter back to the Mac, run Jarvis evals, and promote only if the gate passes.')",
                    ]
                )
            ),
        ]
    )


def _build_colab_preference_readme(target: str, preset: dict, handoff_dir: Path) -> str:
    return "\n".join(
        [
            f"# Jarvis Preference RL Handoff for {preset['label']}",
            "",
            "This folder prepares Jarvis preference pairs for RLHF-style adapter training in Google Colab.",
            "",
            "This is not magic frontier parity. It is a controlled preference-learning lane: bad answers become rejected samples, corrected Jarvis answers become chosen samples, and promotion stays eval-gated locally.",
            "",
            "Flow:",
            "1. Copy this folder to Google Drive as `MyDrive/jarvis_preference_rl_handoff` or edit `HANDOFF_DIR` in the notebook.",
            "2. Open `Jarvis_Preference_RL_Trainer.ipynb` in Colab.",
            "3. Select Runtime > Change runtime type > GPU.",
            "4. Run the notebook cells in order.",
            "5. Copy the trained adapter back to the Mac.",
            "6. Run Jarvis local evals and promote only if the candidate beats the baseline.",
            "",
            "Guardrails:",
            "- Do not train on private secrets, prompt leaks, or unreviewed hostile prompt text.",
            "- Do not promote an adapter without local eval evidence.",
            "- Keep Colab as an interactive training lab, not a 24/7 Jarvis host.",
            "",
            f"Target preset: {target}",
            f"Hugging Face model: {preset['hf_model']}",
            f"Generated in: {handoff_dir}",
            "",
        ]
    )


def _candidate_interactions(limit: int = 200, cloud_only: bool = True) -> list[dict]:
    data = evals.load()
    failures_by_interaction = _linked_failure_map(data)
    interactions = []
    for item in reversed(data.get("interactions", [])):
        model = item.get("model", "")
        response = (item.get("response") or "").strip()
        if not response or len(response) < 40:
            continue
        if cloud_only and model == "Local":
            continue
        if item.get("id") in failures_by_interaction:
            continue
        if response.lower().startswith("local model error"):
            continue
        interactions.append(item)
        if len(interactions) >= limit:
            break
    interactions.reverse()
    return interactions


def _interaction_to_example(interaction: dict, source: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": interaction.get("user_input", "")},
            {"role": "assistant", "content": interaction.get("response", "")},
        ],
        "meta": {
            "interaction_id": interaction.get("id"),
            "timestamp": interaction.get("timestamp"),
            "teacher_source": source,
            "model": interaction.get("model"),
            "interaction_source": interaction.get("source", ""),
            "context": interaction.get("context", {}),
        },
    }


def record_teacher_example(
    prompt: str,
    answer: str,
    *,
    source: str = "manual_teacher",
    tags: list[str] | None = None,
    meta: dict | None = None,
) -> dict:
    _ensure_dirs()
    prompt = (prompt or "").strip()
    answer = (answer or "").strip()
    if not prompt:
        return {"ok": False, "error": "Teacher example is missing a prompt."}
    if not answer:
        return {"ok": False, "error": "Teacher example is missing an answer."}

    example = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": answer},
        ],
        "meta": {
            "interaction_id": "",
            "teacher_source": source,
            "teacher_model": "human_curated",
            "tags": tags or [],
            "recorded_at": _timestamp(),
            "meta": meta or {},
        },
    }
    path = TEACHER_DIR / f"jarvis_teacher_{_timestamp()}_{_safe_slug(prompt)[:48]}.jsonl"
    _write_jsonl(path, [example])
    return {
        "ok": True,
        "path": str(path),
        "example_count": 1,
        "source": source,
        "tags": tags or [],
        "prompt": prompt,
    }


def _teacher_examples() -> list[dict]:
    _ensure_dirs()
    examples: list[dict] = []
    for path in sorted(TEACHER_DIR.glob("*.jsonl")):
        examples.extend(_read_jsonl(path))
    return examples


def _assistant_answer_for_prompt(example: dict) -> tuple[str, str]:
    prompt = ""
    answer = ""
    for message in example.get("messages", []):
        if message.get("role") == "user" and message.get("content"):
            prompt = str(message["content"]).strip()
        if message.get("role") == "assistant" and message.get("content"):
            answer = str(message["content"]).strip()
    return prompt, answer


def _trusted_chosen_answers() -> dict[str, dict]:
    answers: dict[str, dict] = {}
    for source_name, rows, priority in (
        ("manual_teacher", _teacher_examples(), 4),
        ("failure_distillation", _read_many_jsonl(DISTILLED_DIR.glob("jarvis_distilled_*.jsonl")), 3),
        ("expert_distillation", _read_many_jsonl(DISTILLED_DIR.glob("jarvis_expert_distilled_*.jsonl")), 2),
    ):
        for row in rows:
            prompt, answer = _assistant_answer_for_prompt(row)
            if not prompt or not answer:
                continue
            existing = answers.get(prompt)
            if existing is None or priority >= existing["priority"]:
                answers[prompt] = {"answer": answer, "source": source_name, "priority": priority}
    return answers


def _read_many_jsonl(paths) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(paths):
        rows.extend(_read_jsonl(path))
    return rows


def export_preference_dataset(limit: int = 120) -> dict:
    _ensure_dirs()
    data = evals.load()
    trusted = _trusted_chosen_answers()
    later_successes: dict[str, dict] = {}
    for interaction in data.get("interactions", []):
        prompt = (interaction.get("user_input") or "").strip()
        response = (interaction.get("response") or "").strip()
        if not prompt or not response:
            continue
        if interaction.get("id") in _linked_failure_map(data):
            continue
        if response.lower().startswith("local model error"):
            continue
        later_successes[prompt] = {"answer": response, "source": "later_successful_interaction", "priority": 1}

    pairs = []
    skipped = 0
    seen: set[tuple[str, str, str]] = set()
    for failure in reversed(data.get("failures", [])):
        prompt = (failure.get("user_input") or "").strip()
        rejected = (failure.get("response") or "").strip()
        if not prompt or not rejected:
            skipped += 1
            continue
        chosen_payload = trusted.get(prompt) or later_successes.get(prompt)
        if not chosen_payload:
            skipped += 1
            continue
        chosen = chosen_payload["answer"].strip()
        if not chosen or chosen == rejected:
            skipped += 1
            continue
        key = (prompt, chosen, rejected)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            {
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "meta": {
                    "failure_id": failure.get("id", ""),
                    "interaction_id": failure.get("interaction_id", ""),
                    "category": failure.get("category", ""),
                    "issue": failure.get("issue", ""),
                    "chosen_source": chosen_payload["source"],
                    "created_at": _timestamp(),
                    "policy": "Use for preference optimization only after local eval review; do not include secrets or prompt leaks.",
                },
            }
        )
        if len(pairs) >= limit:
            break

    pairs.reverse()
    path = PREFERENCES_DIR / f"jarvis_preferences_{_timestamp()}.jsonl"
    _write_jsonl(path, pairs)
    return {
        "ok": True,
        "path": str(path),
        "pair_count": len(pairs),
        "skipped_failures": skipped,
        "source": "eval_failures_plus_trusted_corrections",
    }


def export_sft_dataset(limit: int = 150, cloud_only: bool = True) -> dict:
    _ensure_dirs()
    interactions = _candidate_interactions(limit=limit, cloud_only=cloud_only)
    examples = [_interaction_to_example(item, source="successful_interaction") for item in interactions]
    path = EXPORTS_DIR / f"jarvis_sft_{_timestamp()}.jsonl"
    _write_jsonl(path, examples)

    return {
        "ok": True,
        "path": str(path),
        "example_count": len(examples),
        "cloud_only": cloud_only,
    }


def _build_distill_prompt(interaction: dict, failures: list[dict]) -> str:
    user_input = interaction.get("user_input", "")
    response = interaction.get("response", "")
    failure_lines = []
    for failure in failures[:3]:
        line = failure.get("issue", "")
        if failure.get("expected"):
            line += f" Expected behavior: {failure['expected']}"
        failure_lines.append(line.strip())

    return (
        "Rewrite the assistant answer so it would be a stronger target for a small local model.\n"
        "Keep it accurate, direct, compact, and spoken-output friendly.\n"
        "Do not use markdown, bullets, or headers.\n"
        "Preserve the user's intent, fix the failure, and avoid generic filler.\n\n"
        f"User input:\n{user_input}\n\n"
        f"Original answer:\n{response}\n\n"
        f"Observed failure(s):\n" + "\n".join(failure_lines) + "\n\n"
        "Return only the improved assistant answer."
    )


def _failure_priority(failures: list[dict], interaction: dict | None = None) -> int:
    score = 0
    categories = Counter(failure.get("category", "") for failure in failures)
    if interaction and interaction.get("model") == "Local":
        score += 5
    score += len(failures) * 2
    score += categories.get("memory", 0) * 4
    score += categories.get("routing", 0) * 4
    score += categories.get("knowledge", 0) * 3
    score += categories.get("self_improve", 0) * 3
    score += categories.get("stability", 0)
    return score


def distill_failures(
    limit: int = 12,
    teacher_model: str = SONNET,
    categories: list[str] | None = None,
    prioritize_local: bool = True,
) -> dict:
    _ensure_dirs()
    if limit <= 0:
        path = DISTILLED_DIR / f"jarvis_distilled_{_timestamp()}.jsonl"
        _write_jsonl(path, [])
        return {
            "ok": True,
            "path": str(path),
            "example_count": 0,
            "teacher_model": teacher_model,
            "categories": categories or [],
        }

    data = evals.load()
    interactions_by_id = {item.get("id"): item for item in data.get("interactions", []) if item.get("id")}
    grouped = _linked_failure_map(data)
    standalone_groups: dict[str, list[dict]] = {}

    examples = []
    used = 0
    ranked_groups = []
    for interaction_id, failures in grouped.items():
        interaction = interactions_by_id.get(interaction_id)
        if not interaction:
            continue
        if categories and not any(failure.get("category") in categories for failure in failures):
            continue
        priority = _failure_priority(failures, interaction if prioritize_local else None)
        last_ts = failures[-1].get("timestamp", "")
        ranked_groups.append((interaction_id, failures, interaction, priority, last_ts))

    for failure in data.get("failures", []):
        interaction_id = failure.get("interaction_id", "")
        if interaction_id and interaction_id in interactions_by_id:
            continue
        if categories and failure.get("category") not in categories:
            continue
        prompt = (failure.get("user_input") or "").strip()
        if not prompt:
            continue
        standalone_groups.setdefault(prompt, []).append(failure)

    for prompt, failures in standalone_groups.items():
        interaction = {
            "id": "",
            "user_input": prompt,
            "response": next((f.get("response", "") for f in reversed(failures) if f.get("response")), ""),
            "model": next((f.get("model", "") for f in reversed(failures) if f.get("model")), ""),
        }
        priority = _failure_priority(failures, interaction if prioritize_local else None)
        last_ts = failures[-1].get("timestamp", "")
        ranked_groups.append(("", failures, interaction, priority, last_ts))

    ranked_groups.sort(key=lambda item: (item[3], item[4]), reverse=True)

    for interaction_id, failures, interaction, priority, _last_ts in ranked_groups:
        if not interaction:
            continue
        if used >= limit:
            break

        system_extra, _ = skills.build_system_extra(interaction.get("user_input", ""), tool="chat")
        improved = ask_claude(
            _build_distill_prompt(interaction, failures),
            model=teacher_model,
            system_extra=system_extra,
        ).strip()
        if not improved:
            continue

        examples.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": interaction.get("user_input", "")},
                    {"role": "assistant", "content": improved},
                ],
                "meta": {
                    "interaction_id": interaction_id,
                    "teacher_source": "failure_distillation",
                    "teacher_model": teacher_model,
                    "failure_ids": [failure.get("id") for failure in failures],
                    "failure_categories": sorted({failure.get("category", "") for failure in failures}),
                    "priority": priority,
                    "original_model": interaction.get("model", ""),
                },
            }
        )
        used += 1

    path = DISTILLED_DIR / f"jarvis_distilled_{_timestamp()}.jsonl"
    _write_jsonl(path, examples)

    return {
        "ok": True,
        "path": str(path),
        "example_count": len(examples),
        "teacher_model": teacher_model,
        "categories": categories or [],
    }


def _build_expert_distill_prompt(case: dict) -> str:
    return (
        "Write the ideal Jarvis answer for this advanced technology or science question.\n"
        "The target is a strong small local model, so the answer should be compact, precise, and high-signal.\n"
        "Do not use markdown, bullets, or headers.\n"
        "Do not open with filler like 'great question'.\n"
        "Lead with the conclusion or core distinction, then explain the mechanism and the main tradeoff or constraint.\n"
        "Use exact technical terms when they help, but define them naturally if they could be misunderstood.\n\n"
        f"User input:\n{case['prompt']}\n\n"
        f"Expected behavior:\n{case['expected']}\n\n"
        "Return only the improved assistant answer."
    )


def distill_expert_cases(
    limit: int = 3,
    teacher_model: str = SONNET,
    case_ids: list[str] | None = None,
) -> dict:
    _ensure_dirs()
    if limit <= 0:
        path = DISTILLED_DIR / f"jarvis_expert_distilled_{_timestamp()}.jsonl"
        _write_jsonl(path, [])
        return {
            "ok": True,
            "path": str(path),
            "example_count": 0,
            "teacher_model": teacher_model,
            "case_ids": [],
        }

    selected = []
    wanted = set(case_ids or [])
    for case in EXPERT_DISTILL_CASES:
        if wanted and case["id"] not in wanted:
            continue
        selected.append(case)
        if len(selected) >= limit:
            break

    examples = []
    for case in selected:
        system_extra, _ = skills.build_system_extra(case["prompt"], tool="chat")
        improved = ask_claude(
            _build_expert_distill_prompt(case),
            model=teacher_model,
            system_extra=system_extra,
        ).strip()
        if not improved:
            continue
        examples.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": case["prompt"]},
                    {"role": "assistant", "content": improved},
                ],
                "meta": {
                    "interaction_id": "",
                    "teacher_source": "expert_distillation",
                    "teacher_model": teacher_model,
                    "case_id": case["id"],
                    "category": case["category"],
                    "expected": case["expected"],
                },
            }
        )

    path = DISTILLED_DIR / f"jarvis_expert_distilled_{_timestamp()}.jsonl"
    _write_jsonl(path, examples)
    return {
        "ok": True,
        "path": str(path),
        "example_count": len(examples),
        "teacher_model": teacher_model,
        "case_ids": [case["id"] for case in selected],
    }


def _example_key(example: dict) -> str:
    messages = example.get("messages", [])
    user_text = ""
    for message in messages:
        if message.get("role") == "user" and message.get("content"):
            user_text = message["content"]
    if user_text:
        return f"user::{user_text}"
    return json.dumps(messages, sort_keys=True)


def _example_priority(example: dict) -> int:
    meta = example.get("meta", {})
    source = meta.get("teacher_source", "")
    if source == "manual_teacher":
        return 5
    if source == "expert_distillation":
        return 4
    if source == "failure_distillation":
        return 3
    if source == "successful_interaction":
        return 2
    return 1


def build_training_pack(
    export_limit: int = 150,
    distill_limit: int = 8,
    expert_distill_limit: int = 3,
    teacher_model: str = SONNET,
    cloud_only_export: bool = True,
    base_model: str = LOCAL_DEFAULT,
    target_name: str = LOCAL_TUNED,
    categories: list[str] | None = None,
) -> dict:
    _ensure_dirs()
    export_result = export_sft_dataset(limit=export_limit, cloud_only=cloud_only_export)
    distill_result = distill_failures(limit=distill_limit, teacher_model=teacher_model, categories=categories)
    expert_result = distill_expert_cases(limit=expert_distill_limit, teacher_model=teacher_model)
    modelfile_result = build_modelfile(base_model=base_model, target_name=target_name)

    exported_examples = _read_jsonl(Path(export_result["path"]))
    distilled_examples = _read_jsonl(Path(distill_result["path"]))
    expert_examples = _read_jsonl(Path(expert_result["path"]))
    teacher_examples = _teacher_examples()

    deduped = {}
    for example in exported_examples + distilled_examples + expert_examples + teacher_examples:
        key = _example_key(example)
        existing = deduped.get(key)
        if existing is None or _example_priority(example) >= _example_priority(existing):
            deduped[key] = example

    pack_path = PACKS_DIR / f"jarvis_training_pack_{_timestamp()}.jsonl"
    manifest_path = PACKS_DIR / f"jarvis_training_pack_{_timestamp()}.manifest.json"
    merged_examples = list(deduped.values())
    _write_jsonl(pack_path, merged_examples)

    manifest = {
        "created_at": _timestamp(),
        "pack_path": str(pack_path),
        "modelfile_path": modelfile_result["path"],
        "target_name": target_name,
        "base_model": base_model,
        "teacher_model": teacher_model,
        "export_examples": export_result["example_count"],
        "distilled_examples": distill_result["example_count"],
        "expert_distilled_examples": expert_result["example_count"],
        "teacher_examples": len(teacher_examples),
        "merged_examples": len(merged_examples),
        "distilled_categories": distill_result.get("categories", []),
        "expert_case_ids": expert_result.get("case_ids", []),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "pack_path": str(pack_path),
        "manifest_path": str(manifest_path),
        "export": export_result,
        "distill": distill_result,
        "expert_distill": expert_result,
        "modelfile": modelfile_result,
        "example_count": len(merged_examples),
        "teacher_examples": len(teacher_examples),
        "teacher_model": teacher_model,
    }


def build_finetune_handoff(
    pack_path: str | None = None,
    targets: list[str] | None = None,
) -> dict:
    _ensure_dirs()
    selected_targets = targets or ["llama3.1:8b", "qwen2.5-coder:7b"]
    for target in selected_targets:
        if target not in MODEL_PRESETS:
            return {"ok": False, "error": f"Unknown fine-tune target: {target}"}

    resolved_pack = Path(pack_path) if pack_path else _latest_pack_path()
    if not resolved_pack or not resolved_pack.exists():
        return {"ok": False, "error": "No training pack found. Run the local training pack builder first."}

    examples = _read_jsonl(resolved_pack)
    if not examples:
        return {"ok": False, "error": f"Training pack is empty: {resolved_pack}"}

    train_examples, val_examples = _split_examples(examples, val_fraction=0.1)
    if not train_examples:
        return {"ok": False, "error": "Training pack did not contain enough examples to create a train split."}

    created = []
    stamp = _timestamp()

    for target in selected_targets:
        preset = MODEL_PRESETS[target]
        handoff_dir = HANDOFFS_DIR / f"{stamp}_{preset['slug']}"
        data_dir = handoff_dir / "data"
        config_dir = handoff_dir / "configs"
        scripts_dir = handoff_dir / "scripts"
        for path in (data_dir, config_dir, scripts_dir):
            path.mkdir(parents=True, exist_ok=True)

        train_messages_path = data_dir / "train_messages.jsonl"
        val_messages_path = data_dir / "val_messages.jsonl"
        train_instruct_path = data_dir / "train_instruct.jsonl"
        val_instruct_path = data_dir / "val_instruct.jsonl"

        _write_handoff_jsonl(train_messages_path, train_examples)
        _write_handoff_jsonl(val_messages_path, val_examples)
        _write_handoff_jsonl(train_instruct_path, [_messages_to_instruct(example["messages"]) for example in train_examples])
        _write_handoff_jsonl(val_instruct_path, [_messages_to_instruct(example["messages"]) for example in val_examples])

        unsloth_script = scripts_dir / "train_unsloth.py"
        axolotl_config = config_dir / "axolotl_qlora.yml"
        readme = handoff_dir / "README.md"

        unsloth_script.write_text(
            _build_unsloth_script(
                data_rel="../data/train_messages.jsonl",
                val_rel="../data/val_messages.jsonl" if val_examples else "",
                preset=preset,
            ),
            encoding="utf-8",
        )
        axolotl_config.write_text(
            _build_axolotl_yaml(
                train_rel="../data/train_messages.jsonl",
                val_rel="../data/val_messages.jsonl" if val_examples else "../data/train_messages.jsonl",
                preset=preset,
            ),
            encoding="utf-8",
        )
        readme.write_text(_build_handoff_readme(preset, handoff_dir), encoding="utf-8")

        manifest = {
            "target": target,
            "label": preset["label"],
            "source_pack": str(resolved_pack),
            "train_examples": len(train_examples),
            "val_examples": len(val_examples),
            "hf_model": preset["hf_model"],
            "unsloth_model": preset["unsloth_model"],
            "files": {
                "train_messages": str(train_messages_path),
                "val_messages": str(val_messages_path),
                "train_instruct": str(train_instruct_path),
                "val_instruct": str(val_instruct_path),
                "unsloth_script": str(unsloth_script),
                "axolotl_config": str(axolotl_config),
                "readme": str(readme),
            },
        }
        manifest_path = handoff_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        created.append({"target": target, "dir": str(handoff_dir), "manifest": str(manifest_path)})

    return {
        "ok": True,
        "source_pack": str(resolved_pack),
        "targets": created,
        "train_examples": len(train_examples),
        "val_examples": len(val_examples),
    }


def build_colab_handoff(
    pack_path: str | None = None,
    target: str = COLAB_DEFAULT_TARGET,
) -> dict:
    _ensure_dirs()
    target = (target or COLAB_DEFAULT_TARGET).strip()
    if target not in MODEL_PRESETS:
        return {"ok": False, "error": f"Unknown Colab training target: {target}"}

    resolved_pack = Path(pack_path) if pack_path else _latest_pack_path()
    if not resolved_pack or not resolved_pack.exists():
        return {"ok": False, "error": "No training pack found. Run the local training pack builder first."}

    examples = _read_jsonl(resolved_pack)
    if not examples:
        return {"ok": False, "error": f"Training pack is empty: {resolved_pack}"}

    train_examples, val_examples = _split_examples(examples, val_fraction=0.1)
    if not train_examples:
        return {"ok": False, "error": "Training pack did not contain enough examples to create a train split."}
    if not val_examples:
        val_examples = train_examples[-1:]

    preset = MODEL_PRESETS[target]
    stamp = _timestamp()
    handoff_dir = HANDOFFS_DIR / f"{stamp}_colab_{preset['slug']}"
    data_dir = handoff_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    train_file = "train_messages.jsonl"
    val_file = "val_messages.jsonl"
    train_path = data_dir / train_file
    val_path = data_dir / val_file
    notebook_path = handoff_dir / "Jarvis_Open_LLM_Trainer.ipynb"
    readme_path = handoff_dir / "README.md"
    manifest_path = handoff_dir / "manifest.json"

    _write_handoff_jsonl(train_path, train_examples)
    _write_handoff_jsonl(val_path, val_examples)
    notebook_path.write_text(
        json.dumps(_build_colab_notebook(target, preset, train_file, val_file), indent=2),
        encoding="utf-8",
    )
    readme_path.write_text(_build_colab_readme(target, preset, handoff_dir), encoding="utf-8")

    manifest = {
        "target": target,
        "label": preset["label"],
        "source_pack": str(resolved_pack),
        "train_examples": len(train_examples),
        "val_examples": len(val_examples),
        "hf_model": preset["hf_model"],
        "unsloth_model": preset["unsloth_model"],
        "notebook": str(notebook_path),
        "readme": str(readme_path),
        "data": {
            "train_messages": str(train_path),
            "val_messages": str(val_path),
        },
        "policy": {
            "google_service": "Google Colab",
            "free_tier": "best-effort interactive training lab; resources are not guaranteed",
            "not_for": "24/7 hosting, remote-control bypass, or unattended background service",
            "promotion_gate": "Run Jarvis local evals before changing defaults.",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "target": target,
        "source_pack": str(resolved_pack),
        "dir": str(handoff_dir),
        "manifest": str(manifest_path),
        "notebook": str(notebook_path),
        "readme": str(readme_path),
        "train_examples": len(train_examples),
        "val_examples": len(val_examples),
    }


def build_colab_preference_handoff(
    preference_path: str | None = None,
    target: str = COLAB_DEFAULT_TARGET,
) -> dict:
    _ensure_dirs()
    target = (target or COLAB_DEFAULT_TARGET).strip()
    if target not in MODEL_PRESETS:
        return {"ok": False, "error": f"Unknown Colab preference target: {target}"}

    preference_result = None
    resolved_preferences = Path(preference_path) if preference_path else None
    if not resolved_preferences:
        preference_result = export_preference_dataset()
        resolved_preferences = Path(preference_result["path"])
    if not resolved_preferences.exists():
        return {"ok": False, "error": f"Preference dataset not found: {resolved_preferences}"}

    pairs = _read_jsonl(resolved_preferences)
    if not pairs:
        return {
            "ok": False,
            "error": (
                "Preference dataset is empty. Add corrected teacher examples for recent Jarvis failures, "
                "then run the preference export again."
            ),
            "preference_path": str(resolved_preferences),
            "preference_export": preference_result,
        }

    train_pairs, val_pairs = _split_examples(pairs, val_fraction=0.1)
    if not train_pairs:
        return {"ok": False, "error": "Preference dataset did not contain enough pairs to create a train split."}
    if not val_pairs:
        val_pairs = train_pairs[-1:]

    preset = MODEL_PRESETS[target]
    stamp = _timestamp()
    handoff_dir = HANDOFFS_DIR / f"{stamp}_preference_rl_{preset['slug']}"
    data_dir = handoff_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    train_file = "train_preferences.jsonl"
    val_file = "val_preferences.jsonl"
    train_path = data_dir / train_file
    val_path = data_dir / val_file
    notebook_path = handoff_dir / "Jarvis_Preference_RL_Trainer.ipynb"
    readme_path = handoff_dir / "README.md"
    manifest_path = handoff_dir / "manifest.json"

    _write_handoff_jsonl(train_path, train_pairs)
    _write_handoff_jsonl(val_path, val_pairs)
    notebook_path.write_text(
        json.dumps(_build_colab_preference_notebook(target, preset, train_file, val_file), indent=2),
        encoding="utf-8",
    )
    readme_path.write_text(_build_colab_preference_readme(target, preset, handoff_dir), encoding="utf-8")

    manifest = {
        "kind": "preference_rl_handoff",
        "target": target,
        "label": preset["label"],
        "source_preferences": str(resolved_preferences),
        "train_pairs": len(train_pairs),
        "val_pairs": len(val_pairs),
        "hf_model": preset["hf_model"],
        "unsloth_model": preset["unsloth_model"],
        "notebook": str(notebook_path),
        "readme": str(readme_path),
        "data": {
            "train_preferences": str(train_path),
            "val_preferences": str(val_path),
        },
        "policy": {
            "method": "RLHF-style DPO preference optimization",
            "google_service": "Google Colab",
            "free_tier": "best-effort interactive training lab; resources are not guaranteed",
            "not_for": "24/7 hosting, prompt-leak training, secret extraction, or unattended model promotion",
            "promotion_gate": "Import the adapter locally, run Jarvis evals against the baseline, then promote only if the gate clears.",
        },
        "preference_export": preference_result,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "kind": "preference_rl_handoff",
        "target": target,
        "source_preferences": str(resolved_preferences),
        "dir": str(handoff_dir),
        "manifest": str(manifest_path),
        "notebook": str(notebook_path),
        "readme": str(readme_path),
        "train_pairs": len(train_pairs),
        "val_pairs": len(val_pairs),
        "preference_export": preference_result,
    }


def build_modelfile(base_model: str = LOCAL_DEFAULT, target_name: str = LOCAL_TUNED) -> dict:
    _ensure_dirs()
    modelfile_path = MODELFILES_DIR / f"{_safe_slug(target_name)}.Modelfile"
    body = "\n".join(
        [
            f"FROM {base_model}",
            "",
            "# Jarvis local tuned target",
            f'SYSTEM """{SYSTEM_PROMPT}"""',
            "PARAMETER temperature 0.3",
            "PARAMETER num_ctx 8192",
            "",
            "# Create with:",
            f"# ollama create {target_name} -f {modelfile_path}",
        ]
    )
    modelfile_path.write_text(body + "\n", encoding="utf-8")
    return {
        "ok": True,
        "path": str(modelfile_path),
        "target_name": target_name,
        "base_model": base_model,
        "command": f"ollama create {target_name} -f {modelfile_path}",
    }


def status() -> dict:
    _ensure_dirs()
    exports = sorted(EXPORTS_DIR.glob("*.jsonl"))
    distilled = sorted(DISTILLED_DIR.glob("*.jsonl"))
    teachings = sorted(TEACHER_DIR.glob("*.jsonl"))
    preferences = sorted(PREFERENCES_DIR.glob("*.jsonl"))
    modelfiles = sorted(MODELFILES_DIR.glob("*.Modelfile"))
    packs = sorted(PACKS_DIR.glob("*.jsonl"))
    manifests = sorted(PACKS_DIR.glob("*.manifest.json"))
    handoffs = sorted(HANDOFFS_DIR.glob("*/manifest.json"))
    return {
        "exports": len(exports),
        "distilled": len(distilled),
        "teachings": len(teachings),
        "preferences": len(preferences),
        "modelfiles": len(modelfiles),
        "packs": len(packs),
        "manifests": len(manifests),
        "handoffs": len(handoffs),
        "latest_export": str(exports[-1]) if exports else "",
        "latest_distilled": str(distilled[-1]) if distilled else "",
        "latest_teaching": str(teachings[-1]) if teachings else "",
        "latest_preferences": str(preferences[-1]) if preferences else "",
        "latest_modelfile": str(modelfiles[-1]) if modelfiles else "",
        "latest_pack": str(packs[-1]) if packs else "",
        "latest_manifest": str(manifests[-1]) if manifests else "",
        "latest_handoff": str(handoffs[-1]) if handoffs else "",
        "tuned_target": LOCAL_TUNED,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local training step failed.")
    if result.get("source") and result.get("example_count") == 1 and "prompt" in result:
        return (
            f"Stored 1 teacher example for local training at {result['path']}. "
            "Jarvis can use it in the next training-pack build."
        )
    if "example_count" in result and "teacher_model" in result:
        if "pack_path" in result:
            return (
                f"Built a local training pack with {result['example_count']} total examples. "
                f"Exported {result['export']['example_count']} strong examples, distilled {result['distill']['example_count']} corrected failure examples, "
                f"added {result['expert_distill']['example_count']} expert technology and science examples, and included {result.get('teacher_examples', 0)} curated teacher examples with {result['teacher_model']}, "
                f"and wrote the merged pack to {result['pack_path']}."
            )
        if "case_ids" in result:
            return (
                f"Distilled {result['example_count']} expert technology and science examples for local training using {result['teacher_model']}. "
                f"The dataset is at {result['path']}."
            )
        return (
            f"Distilled {result['example_count']} corrected teacher examples for local training using {result['teacher_model']}. "
            f"The dataset is at {result['path']}."
        )
    if "targets" in result:
        target_names = ", ".join(item["target"] for item in result["targets"])
        return (
            f"Built offline fine-tune handoff folders for {target_names} from {result['source_pack']}. "
            f"The splits contain {result['train_examples']} train examples and {result['val_examples']} validation examples."
        )
    if result.get("kind") == "preference_rl_handoff":
        return (
            f"Built a Google Colab preference-RL handoff for {result['target']} from {result['source_preferences']}. "
            f"The notebook is at {result['notebook']} with {result['train_pairs']} train pairs and {result['val_pairs']} validation pairs."
        )
    if "pair_count" in result:
        return (
            f"Exported {result['pair_count']} Jarvis preference pairs for RLHF-style local-model training. "
            f"The dataset is at {result['path']} and skipped {result.get('skipped_failures', 0)} failures without trusted corrections."
        )
    if "notebook" in result:
        return (
            f"Built a Google Colab training handoff for {result['target']} from {result['source_pack']}. "
            f"The notebook is at {result['notebook']} with {result['train_examples']} train examples and {result['val_examples']} validation examples."
        )
    if "example_count" in result:
        return (
            f"Exported {result['example_count']} strong interaction examples for local training. "
            f"The dataset is at {result['path']}."
        )
    if "command" in result:
        return (
            f"Built a local Jarvis Modelfile at {result['path']}. "
            f"To register it in Ollama, run {result['command']}."
        )
    return "Local training step completed."
