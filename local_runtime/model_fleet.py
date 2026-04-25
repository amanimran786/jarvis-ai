"""
Local model fleet and training-lane status for Jarvis.

This module is deliberately descriptive, not mutating. It tells Jarvis what is
installed, what is worth pulling next, and which "free training" lanes are real
without pretending Colab is a production host.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import (
    LOCAL_CODER, LOCAL_CODER_RECOMMENDED, LOCAL_DEFAULT, LOCAL_REASONING, LOCAL_TUNED,
    LOCAL_QWEN3_FAST, LOCAL_QWEN3_MID, LOCAL_QWEN3_STRONG, LOCAL_DEVSTRAL, LOCAL_PHI4_MINI,
    LOCAL_GEMMA4_STRONG, LOCAL_GEMMA4_MOE, LOCAL_QWEN3_6, LOCAL_DEEPSEEK_V4_FLASH,
    LOCAL_LLAMA4_MAVERICK,
)
from brains import brain_ollama
from local_runtime import local_model_automation, local_model_eval, local_training


@dataclass(frozen=True)
class ModelCandidate:
    id: str
    role: str
    label: str
    ollama_tag: str
    status: str
    priority: str
    pull_command: str
    disk_estimate: str
    context_window: str
    why: str
    caution: str


@dataclass(frozen=True)
class TrainingLane:
    id: str
    label: str
    status: str
    cost: str
    local_first: bool
    action: str
    use_for: str
    caveat: str
    source: str


MODEL_CANDIDATES: tuple[ModelCandidate, ...] = (
    ModelCandidate(
        id="qwen3_coder_30b",
        role="coding_agent",
        label="Qwen3-Coder 30B",
        ollama_tag=LOCAL_CODER_RECOMMENDED,
        status="recommended",
        priority="high",
        pull_command=f"ollama pull {LOCAL_CODER_RECOMMENDED}",
        disk_estimate="about 19GB",
        context_window="256K listed by Ollama",
        why="Best next local coding-agent candidate for a Claude/Codex-style terminal loop.",
        caution="Needs enough unified memory and a raised Ollama context setting; do not auto-pull.",
    ),
    ModelCandidate(
        id="gpt_oss_20b",
        role="general_reasoning",
        label="gpt-oss 20B",
        ollama_tag="gpt-oss:20b",
        status="optional",
        priority="medium",
        pull_command="ollama pull gpt-oss:20b",
        disk_estimate="large local model",
        context_window="use at least 64K when available",
        why="Good open general-purpose candidate for local frontier-style reasoning tests.",
        caution="Validate latency and tool behavior before promoting it over current defaults.",
    ),
    ModelCandidate(
        id="glm_4_7_flash",
        role="coding_agent",
        label="GLM 4.7 Flash",
        ollama_tag="glm-4.7-flash",
        status="optional",
        priority="medium",
        pull_command="ollama pull glm-4.7-flash",
        disk_estimate="requires high VRAM at long context",
        context_window="use at least 64K for coding tools",
        why="Ollama recommends it for coding-tool integrations.",
        caution="The official blog notes about 23GB VRAM for 64K context; check Mac memory first.",
    ),
    ModelCandidate(
        id="qwen2_5_coder_7b",
        role="fast_coder",
        label="Qwen2.5-Coder 7B",
        ollama_tag="qwen2.5-coder:7b",
        status="baseline",
        priority="ready",
        pull_command="ollama pull qwen2.5-coder:7b",
        disk_estimate="about 5GB",
        context_window="32K family baseline",
        why="Fast installed coding baseline for edits, explanations, and smoke tasks.",
        caution="Not enough by itself for frontier-level long-horizon coding.",
    ),
    ModelCandidate(
        id="devstral",
        role="coding_agent",
        label="Devstral (Mistral coder)",
        ollama_tag=LOCAL_DEVSTRAL,
        status="recommended",
        priority="high",
        pull_command=f"ollama pull {LOCAL_DEVSTRAL}",
        disk_estimate="about 14GB",
        context_window="128K",
        why="Mistral's dedicated coding model — outperforms Qwen2.5-Coder on SWE-bench. Current preferred coder.",
        caution="Requires ~14GB disk; confirm memory headroom before pulling.",
    ),
    ModelCandidate(
        id="qwen3_4b",
        role="fast_general",
        label="Qwen3 4B",
        ollama_tag=LOCAL_QWEN3_FAST,
        status="recommended",
        priority="high",
        pull_command=f"ollama pull {LOCAL_QWEN3_FAST}",
        disk_estimate="about 3GB",
        context_window="128K",
        why="Fastest Qwen3 tier; replaces phi4-mini for quick general queries. Hybrid reasoning.",
        caution="Thinking mode adds latency — use fast path for simple queries.",
    ),
    ModelCandidate(
        id="qwen3_8b",
        role="general",
        label="Qwen3 8B",
        ollama_tag=LOCAL_QWEN3_MID,
        status="recommended",
        priority="high",
        pull_command=f"ollama pull {LOCAL_QWEN3_MID}",
        disk_estimate="about 5GB",
        context_window="128K",
        why="Mid-tier Qwen3; stronger than gemma4 at same size. Main daily-driver general model.",
        caution="Pull after qwen3:4b to evaluate memory/latency before committing to 8B.",
    ),
    ModelCandidate(
        id="qwen3_30b_a3b",
        role="strong_general",
        label="Qwen3 30B-A3B (MoE)",
        ollama_tag=LOCAL_QWEN3_STRONG,
        status="optional",
        priority="medium",
        pull_command=f"ollama pull {LOCAL_QWEN3_STRONG}",
        disk_estimate="about 19GB",
        context_window="128K",
        why="MoE model — 30B parameters but only 3B active per token. Near-frontier quality at low compute cost.",
        caution="19GB disk; validate latency on Mac before promoting over 8B for daily use.",
    ),
    ModelCandidate(
        id="phi4_mini",
        role="ultra_fast",
        label="Phi-4 Mini",
        ollama_tag=LOCAL_PHI4_MINI,
        status="optional",
        priority="medium",
        pull_command=f"ollama pull {LOCAL_PHI4_MINI}",
        disk_estimate="about 3GB",
        context_window="128K",
        why="Microsoft ultra-fast model; good fallback when Qwen3:4b is too slow.",
        caution="Weaker on reasoning than Qwen3:4b; use for speed-critical paths only.",
    ),
    ModelCandidate(
        id="deepseek_r1_14b",
        role="reasoning",
        label="DeepSeek R1 14B",
        ollama_tag="deepseek-r1:14b",
        status="baseline",
        priority="ready",
        pull_command="ollama pull deepseek-r1:14b",
        disk_estimate="about 9GB",
        context_window="use capped context for latency",
        why="Installed local reasoning model for multi-step thinking.",
        caution="Can be slow; keep timeouts and context caps explicit.",
    ),
    ModelCandidate(
        id="gemma4_31b",
        role="strong_general",
        label="Gemma 4 31B",
        ollama_tag=LOCAL_GEMMA4_STRONG,
        status="recommended_eval",
        priority="medium",
        pull_command=f"ollama pull {LOCAL_GEMMA4_STRONG}",
        disk_estimate="about 20GB",
        context_window="256K listed by Ollama",
        why="Verified Ollama workstation model; strong local reasoning, coding, multimodal, and agentic candidate without changing defaults.",
        caution="Evaluate latency and memory on this Mac before promoting over gemma4:e4b or qwen3:8b.",
    ),
    ModelCandidate(
        id="gemma4_26b_moe",
        role="balanced_general",
        label="Gemma 4 26B MoE",
        ollama_tag=LOCAL_GEMMA4_MOE,
        status="optional_eval",
        priority="medium",
        pull_command=f"ollama pull {LOCAL_GEMMA4_MOE}",
        disk_estimate="about 18GB",
        context_window="256K listed by Ollama",
        why="Verified Ollama MoE candidate with lower active-parameter cost than a dense workstation model.",
        caution="Treat as an eval candidate; do not promote until Jarvis latency and tool-use tests pass.",
    ),
    ModelCandidate(
        id="qwen3_6_35b",
        role="coding_agent",
        label="Qwen3.6 35B",
        ollama_tag=LOCAL_QWEN3_6,
        status="optional_eval",
        priority="medium",
        pull_command=f"ollama pull {LOCAL_QWEN3_6}",
        disk_estimate="large local model",
        context_window="verify with Ollama model card before promotion",
        why="Newer Qwen-family open-weight coding/general candidate visible in Ollama; useful for local agentic coding evals.",
        caution="Do not assume claimed 1M context locally; measure context, latency, and memory before changing router defaults.",
    ),
    ModelCandidate(
        id="deepseek_v4_flash_cloud",
        role="cloud_optional_reasoning",
        label="DeepSeek V4 Flash via Ollama Cloud",
        ollama_tag=LOCAL_DEEPSEEK_V4_FLASH,
        status="cloud_optional",
        priority="low",
        pull_command=f"ollama run {LOCAL_DEEPSEEK_V4_FLASH}",
        disk_estimate="cloud model, no local weights",
        context_window="1M listed by DeepSeek/Ollama",
        why="Verified new DeepSeek V4 Flash option for agentic coding/reasoning experiments through Ollama Cloud.",
        caution="Not 100% local and may involve cloud cost/data transfer; never use as a Jarvis default in open-source/local-first mode.",
    ),
    ModelCandidate(
        id="llama4_maverick_heavy",
        role="heavy_multimodal",
        label="Llama 4 Maverick",
        ollama_tag=LOCAL_LLAMA4_MAVERICK,
        status="heavy_optional",
        priority="low",
        pull_command=f"ollama pull {LOCAL_LLAMA4_MAVERICK}",
        disk_estimate="about 245GB in Ollama",
        context_window="128K listed by Ollama",
        why="Verified Ollama model card; multimodal 400B MoE candidate for specialized experiments.",
        caution="Too large for normal Jarvis pulls on this Mac; only pull intentionally after checking disk and memory.",
    ),
)


TRAINING_LANES: tuple[TrainingLane, ...] = (
    TrainingLane(
        id="teacher_examples",
        label="Manual teacher examples",
        status="ready",
        cost="free",
        local_first=True,
        action='jarvis --teach "<prompt>" "<ideal answer>"',
        use_for="Correct specific Jarvis failures with curated examples.",
        caveat="This teaches the dataset, not the model weights, until a pack is trained and promoted.",
        source="Jarvis local training pipeline",
    ),
    TrainingLane(
        id="training_pack",
        label="Local SFT training pack export",
        status="ready",
        cost="free",
        local_first=True,
        action="POST /local/training/run",
        use_for="Build merged JSONL packs from strong interactions, failures, expert cases, and teacher rows.",
        caveat="Pack generation is local; actual weight tuning still needs a training runtime.",
        source="Jarvis local training pipeline",
    ),
    TrainingLane(
        id="preference_pairs",
        label="Preference/RL pair export",
        status="ready",
        cost="free",
        local_first=True,
        action="POST /local/training/preferences",
        use_for="Turn failed Jarvis answers plus trusted corrections into DPO/ORPO-style preference pairs.",
        caveat="Requires trusted corrected answers; never train on secrets, prompt leaks, or unreviewed hostile text.",
        source="Jarvis local training pipeline",
    ),
    TrainingLane(
        id="ollama_modelfile",
        label="Ollama Modelfile packaging",
        status="ready",
        cost="free",
        local_first=True,
        action="POST /local/training/modelfile",
        use_for="Package Jarvis behavior and parameters into a local Ollama target.",
        caveat="A Modelfile changes prompting and parameters; it is not LoRA or full fine-tuning.",
        source="Jarvis local training pipeline",
    ),
    TrainingLane(
        id="google_colab_gemma_lora",
        label="Google Colab Gemma LoRA",
        status="external_optional",
        cost="free tier, not guaranteed",
        local_first=False,
        action="Export Jarvis pack, upload to Colab, train LoRA, import adapter or converted model back locally.",
        use_for="Small supervised tuning experiments without renting a GPU server.",
        caveat="Colab free GPUs are not guaranteed, runtimes can terminate, and it cannot host Jarvis 24/7.",
        source="https://ai.google.dev/gemma/docs/core/lora_tuning",
    ),
    TrainingLane(
        id="unsloth_colab_grpo",
        label="Unsloth Colab GRPO/LoRA notebooks",
        status="external_optional",
        cost="free tier, not guaranteed",
        local_first=False,
        action="Use Jarvis packs or task rewards in an Unsloth Colab notebook, then eval locally before promotion.",
        use_for="Reasoning-style RL and adapter experiments such as GRPO on small open models.",
        caveat="Treat social claims about specific new model names as unverified until the notebook/source is checked.",
        source="https://unsloth.ai/docs/get-started/unsloth-notebooks",
    ),
    TrainingLane(
        id="jarvis_colab_dpo",
        label="Jarvis Colab DPO preference-RL handoff",
        status="ready",
        cost="free tier, not guaranteed",
        local_first=False,
        action="POST /local/training/rl-colab",
        use_for="Train a small open model adapter from Jarvis preference pairs, then eval locally before promotion.",
        caveat="Colab is a best-effort training lab; DPO improves behavior on covered failures but does not guarantee frontier parity.",
        source="Jarvis local training pipeline",
    ),
    TrainingLane(
        id="autonomous_self_learning",
        label="Autonomous self-learning promotion",
        status="gated",
        cost="free local loop after setup",
        local_first=True,
        action="Require eval pass-rate, score delta, rollback path, and explicit promotion before changing defaults.",
        use_for="Let Jarvis improve continuously without silently degrading itself.",
        caveat="Do not let background learning mutate production behavior without tests and promotion gates.",
        source="Jarvis eval and promotion contract",
    ),
)


def _installed_models() -> list[str]:
    return sorted(set(brain_ollama.list_local_models()))


def _is_installed(tag: str, installed: list[str]) -> bool:
    if not tag:
        return False
    if ":" in tag:
        return any(model == tag for model in installed)
    return any(model == tag or model.startswith(tag + ":") for model in installed)


def _candidate_dict(candidate: ModelCandidate, installed: list[str]) -> dict[str, Any]:
    ready = _is_installed(candidate.ollama_tag, installed)
    status = "installed" if ready else candidate.status
    return {
        "id": candidate.id,
        "role": candidate.role,
        "label": candidate.label,
        "ollama_tag": candidate.ollama_tag,
        "installed": ready,
        "status": status,
        "priority": candidate.priority,
        "pull_command": candidate.pull_command,
        "disk_estimate": candidate.disk_estimate,
        "context_window": candidate.context_window,
        "why": candidate.why,
        "caution": candidate.caution,
    }


def _training_lane_dict(lane: TrainingLane) -> dict[str, Any]:
    return {
        "id": lane.id,
        "label": lane.label,
        "status": lane.status,
        "cost": lane.cost,
        "local_first": lane.local_first,
        "action": lane.action,
        "use_for": lane.use_for,
        "caveat": lane.caveat,
        "source": lane.source,
    }


def fleet_status() -> dict[str, Any]:
    installed = _installed_models()
    candidates = [_candidate_dict(candidate, installed) for candidate in MODEL_CANDIDATES]
    training_status = local_training.status()
    eval_status = local_model_eval.status()
    automation_status = local_model_automation.status()

    configured_roles = {
        "default": LOCAL_DEFAULT,
        "coder": LOCAL_CODER,
        "reasoning": LOCAL_REASONING,
        "tuned_target": LOCAL_TUNED,
    }
    ready_roles = {
        role: _is_installed(model, installed)
        for role, model in configured_roles.items()
        if model
    }
    high_priority_missing = [
        item for item in candidates
        if item["priority"] == "high" and not item["installed"]
    ]
    ready_count = sum(1 for ready in ready_roles.values() if ready)

    return {
        "ok": True,
        "purpose": "Local model fleet and free training-lane status for Jarvis.",
        "installed_models": installed,
        "installed_count": len(installed),
        "configured_roles": configured_roles,
        "ready_roles": ready_roles,
        "ready_role_count": ready_count,
        "candidates": candidates,
        "recommended_next": high_priority_missing[:2],
        "training_lanes": [_training_lane_dict(lane) for lane in TRAINING_LANES],
        "training_status": training_status,
        "eval_status": eval_status,
        "automation_status": automation_status,
        "policy": {
            "download_all_models": "no",
            "why": "Model pulls consume disk, memory, and latency budget. Jarvis should install role-based candidates, then eval before promotion.",
            "hosting": "24/7 Jarvis should run on the local Mac daemon. Colab is a training lab, not a production host.",
            "self_learning": "Allowed only through recorded examples, packs, evals, and explicit promotion gates.",
        },
        "sources": [
            {
                "label": "Ollama launch",
                "url": "https://ollama.com/blog/launch",
                "claim": "Ollama can launch Claude Code, OpenCode, Codex, and Droid with local or cloud models.",
            },
            {
                "label": "Google Colab FAQ",
                "url": "https://research.google.com/colaboratory/faq.html",
                "claim": "Colab offers free compute including GPUs/TPUs, but resources are not guaranteed or unlimited.",
            },
            {
                "label": "Google Gemma LoRA",
                "url": "https://ai.google.dev/gemma/docs/core/lora_tuning",
                "claim": "Google documents Gemma LoRA fine-tuning in Colab.",
            },
            {
                "label": "Google Gemma 4",
                "url": "https://developers.googleblog.com/bring-state-of-the-art-agentic-skills-to-the-edge-with-gemma-4/",
                "claim": "Google announced Gemma 4 under Apache 2.0 with on-device agentic capabilities.",
            },
            {
                "label": "Ollama Gemma 4",
                "url": "https://ollama.com/library/gemma4",
                "claim": "Ollama lists Gemma 4 edge and workstation tags including e4b, 26b, and 31b.",
            },
            {
                "label": "DeepSeek V4 API",
                "url": "https://api-docs.deepseek.com/updates",
                "claim": "DeepSeek lists V4-Pro and V4-Flash model names as of 2026-04-24.",
            },
            {
                "label": "Ollama DeepSeek V4 Flash",
                "url": "https://ollama.com/library/deepseek-v4-flash",
                "claim": "Ollama exposes deepseek-v4-flash:cloud; it is cloud, not local weights.",
            },
            {
                "label": "Unsloth notebooks",
                "url": "https://unsloth.ai/docs/get-started/unsloth-notebooks",
                "claim": "Unsloth publishes Colab/Kaggle notebooks for SFT, LoRA, GRPO, and related experiments.",
            },
        ],
    }


def summary_text() -> str:
    status = fleet_status()
    installed = ", ".join(status["installed_models"][:8]) or "none"
    if len(status["installed_models"]) > 8:
        installed += f", plus {len(status['installed_models']) - 8} more"

    roles = status["configured_roles"]
    role_bits = [
        f"default={roles.get('default', 'unknown')}",
        f"coder={roles.get('coder', 'unknown')}",
        f"reasoning={roles.get('reasoning', 'unknown')}",
        f"tuned={roles.get('tuned_target', 'unknown')}",
    ]
    next_items = status["recommended_next"]
    next_line = (
        f"Next pull candidate: {next_items[0]['ollama_tag']} via {next_items[0]['pull_command']}."
        if next_items else
        "Next pull candidate: none required until evals show a gap."
    )
    lanes = ", ".join(
        f"{lane['id']}={lane['status']}"
        for lane in status["training_lanes"][:4]
    )
    return (
        "Local model fleet: installed "
        f"{status['installed_count']} models: {installed}. "
        f"Configured roles: {', '.join(role_bits)}. "
        f"{next_line} "
        "Do not download every model; install by role, measure, then promote. "
        f"Training lanes: {lanes}. "
        "Google Colab and Unsloth are valid free/low-cost training labs, not guaranteed 24/7 Jarvis hosting. "
        "Self-learning must stay gated through teacher examples, training packs, evals, and explicit promotion."
    )
