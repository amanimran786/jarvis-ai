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
from brain_claude import ask_claude
from config import LOCAL_DEFAULT, LOCAL_TUNED, SONNET, SYSTEM_PROMPT


TRAINING_ROOT = Path(__file__).resolve().parent / "training"
EXPORTS_DIR = TRAINING_ROOT / "exports"
DISTILLED_DIR = TRAINING_ROOT / "distilled"
MODELFILES_DIR = TRAINING_ROOT / "modelfiles"
PACKS_DIR = TRAINING_ROOT / "packs"
HANDOFFS_DIR = TRAINING_ROOT / "handoffs"

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


def _ensure_dirs() -> None:
    for path in (EXPORTS_DIR, DISTILLED_DIR, MODELFILES_DIR, PACKS_DIR, HANDOFFS_DIR):
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
            "context": interaction.get("context", {}),
        },
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
    data = evals.load()
    interactions_by_id = {item.get("id"): item for item in data.get("interactions", []) if item.get("id")}
    grouped = _linked_failure_map(data)

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
    if source == "failure_distillation":
        return 3
    if source == "successful_interaction":
        return 2
    return 1


def build_training_pack(
    export_limit: int = 150,
    distill_limit: int = 8,
    teacher_model: str = SONNET,
    cloud_only_export: bool = True,
    base_model: str = LOCAL_DEFAULT,
    target_name: str = LOCAL_TUNED,
    categories: list[str] | None = None,
) -> dict:
    _ensure_dirs()
    export_result = export_sft_dataset(limit=export_limit, cloud_only=cloud_only_export)
    distill_result = distill_failures(limit=distill_limit, teacher_model=teacher_model, categories=categories)
    modelfile_result = build_modelfile(base_model=base_model, target_name=target_name)

    exported_examples = _read_jsonl(Path(export_result["path"]))
    distilled_examples = _read_jsonl(Path(distill_result["path"]))

    deduped = {}
    for example in exported_examples + distilled_examples:
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
        "merged_examples": len(merged_examples),
        "distilled_categories": distill_result.get("categories", []),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "pack_path": str(pack_path),
        "manifest_path": str(manifest_path),
        "export": export_result,
        "distill": distill_result,
        "modelfile": modelfile_result,
        "example_count": len(merged_examples),
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
    modelfiles = sorted(MODELFILES_DIR.glob("*.Modelfile"))
    packs = sorted(PACKS_DIR.glob("*.jsonl"))
    manifests = sorted(PACKS_DIR.glob("*.manifest.json"))
    handoffs = sorted(HANDOFFS_DIR.glob("*/manifest.json"))
    return {
        "exports": len(exports),
        "distilled": len(distilled),
        "modelfiles": len(modelfiles),
        "packs": len(packs),
        "manifests": len(manifests),
        "handoffs": len(handoffs),
        "latest_export": str(exports[-1]) if exports else "",
        "latest_distilled": str(distilled[-1]) if distilled else "",
        "latest_modelfile": str(modelfiles[-1]) if modelfiles else "",
        "latest_pack": str(packs[-1]) if packs else "",
        "latest_manifest": str(manifests[-1]) if manifests else "",
        "latest_handoff": str(handoffs[-1]) if handoffs else "",
        "tuned_target": LOCAL_TUNED,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local training step failed.")
    if "example_count" in result and "teacher_model" in result:
        if "pack_path" in result:
            return (
                f"Built a local training pack with {result['example_count']} total examples. "
                f"Exported {result['export']['example_count']} strong examples, distilled {result['distill']['example_count']} corrected examples with {result['teacher_model']}, "
                f"and wrote the merged pack to {result['pack_path']}."
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
