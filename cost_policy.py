"""
Cost-aware routing and local-model improvement policy for Jarvis.

This module turns the rough operating playbook into deterministic decisions:

1. Prefer local inference whenever quality risk is low.
2. Spend cloud tokens on harder or higher-stakes requests.
3. Distill only when failures repeat.
4. Train only when repeated failures justify the GPU spend.
"""

from __future__ import annotations

from collections import Counter

import evals
import usage_tracker


DAILY_CLOUD_SOFT_BUDGET_USD = 0.50
DAILY_CLOUD_HARD_BUDGET_USD = 2.00
DISTILL_REPEAT_THRESHOLD = 2
TRAIN_REPEAT_THRESHOLD = 4
TRAIN_CATEGORY_THRESHOLD = 2

HIGH_STAKES_MARKERS = (
    "security", "vulnerability", "exploit", "authentication", "authorization",
    "medical", "legal", "financial", "production incident", "outage", "data loss",
    "threat model", "compliance", "encryption",
)

HARD_REASONING_MARKERS = (
    "architecture", "system design", "tradeoff", "trade off", "refactor",
    "distributed system", "race condition", "memory leak", "debugging sequence",
    "root cause", "kv cache", "thermodynamics", "information theory", "crispr",
    "semiconductor", "lithography",
)


def _daily_usage() -> dict:
    return usage_tracker.summarize(hours=24, include_recent=0)


def _failure_summary(hours: int = 24 * 7, limit: int = 50) -> tuple[list[dict], Counter]:
    failures = evals.recent_failures(limit=limit, hours=hours)
    counts = Counter(item.get("category", "general_quality") for item in failures)
    return failures, counts


def policy_status() -> dict:
    usage = _daily_usage()
    failures, categories = _failure_summary()
    repeated_categories = {name: count for name, count in categories.items() if count >= DISTILL_REPEAT_THRESHOLD}

    if sum(repeated_categories.values()) >= TRAIN_REPEAT_THRESHOLD and len(repeated_categories) >= TRAIN_CATEGORY_THRESHOLD:
        training_action = "train"
        training_reason = "Repeated failures are clustered enough to justify a real local-model training cycle."
    elif repeated_categories:
        training_action = "distill"
        training_reason = "There are repeated failures, but not enough evidence yet to justify a full training cycle."
    else:
        training_action = "none"
        training_reason = "Failure evidence is still too sparse for distillation or training."

    budget_pressure = usage.get("estimated_cost_usd", 0.0) >= DAILY_CLOUD_SOFT_BUDGET_USD
    hard_budget = usage.get("estimated_cost_usd", 0.0) >= DAILY_CLOUD_HARD_BUDGET_USD

    return {
        "usage_24h": usage,
        "recent_failure_categories": dict(categories),
        "repeated_failure_categories": repeated_categories,
        "budget_pressure": budget_pressure,
        "hard_budget": hard_budget,
        "training_action": training_action,
        "training_reason": training_reason,
    }


def policy_text() -> str:
    data = policy_status()
    usage = data["usage_24h"]
    categories = data["recent_failure_categories"]
    if categories:
        top = ", ".join(f"{name} ({count})" for name, count in sorted(categories.items(), key=lambda kv: kv[1], reverse=True)[:3])
    else:
        top = "no repeated failure categories yet"

    budget_state = "under budget"
    if data["hard_budget"]:
        budget_state = "over the hard cloud budget"
    elif data["budget_pressure"]:
        budget_state = "over the soft cloud budget"

    return (
        f"In the last 24 hours Jarvis logged {usage.get('cloud_call_count', 0)} cloud calls and "
        f"{usage.get('local_call_count', 0)} local calls, with an estimated cloud cost of "
        f"{usage.get('estimated_cost_usd', 0.0):.6f} dollars. "
        f"The current cost policy is {budget_state}. "
        f"The strongest recent failure clusters are {top}. "
        f"My current improvement recommendation is {data['training_action']}: {data['training_reason']}"
    )


def route_decision(user_input: str, base_tier: str, tool: str | None = "chat", local_available: bool = False) -> dict:
    """
    Decide whether to keep the base routing tier or bias cheaper based on
    recent spend and task risk.
    """
    lower = (user_input or "").lower()
    status = policy_status()
    high_stakes = any(marker in lower for marker in HIGH_STAKES_MARKERS)
    hard_reasoning = any(marker in lower for marker in HARD_REASONING_MARKERS)

    if base_tier in {"opus", "sonnet"}:
        return {"tier": base_tier, "provider": "cloud", "reason": "This request is in the high-complexity tier and should stay on cloud."}

    if not local_available:
        return {"tier": base_tier, "provider": "cloud", "reason": "No local model is available, so Jarvis has to use the base cloud tier."}

    if tool and tool != "chat":
        return {"tier": base_tier, "provider": "base", "reason": "Tool formatting and non-chat flows keep the base routing tier."}

    if high_stakes:
        if base_tier in {"local", "mini"}:
            return {"tier": "haiku", "provider": "cloud", "reason": "This request looks high-stakes, so Jarvis should not cheap out to local."}
        return {"tier": base_tier, "provider": "cloud", "reason": "This request looks high-stakes, so Jarvis should stay on the stronger cloud tier."}

    if hard_reasoning and base_tier == "haiku":
        return {"tier": "haiku", "provider": "cloud", "reason": "This request needs more reliable reasoning than the local route should guarantee."}

    if base_tier in {"mini", "haiku"}:
        if status["hard_budget"]:
            return {"tier": "local", "provider": "local", "reason": "Cloud spend is over the hard budget and this request is low enough risk to keep local."}
        if status["budget_pressure"] and base_tier == "mini":
            return {"tier": "local", "provider": "local", "reason": "Cloud spend is over the soft budget, so cheap chat is staying local."}
        if base_tier == "haiku" and not hard_reasoning:
            return {"tier": "haiku", "provider": "local", "reason": "This request is moderate complexity and local inference is still the cheaper viable option."}
        if base_tier == "mini":
            return {"tier": "mini", "provider": "local", "reason": "Simple chat should stay local by default when Ollama is available."}

    return {"tier": base_tier, "provider": "base", "reason": "The base tier is already the best cost and quality tradeoff for this request."}


def training_decision() -> dict:
    status = policy_status()
    action = status["training_action"]
    if action == "train":
        return {"action": "train", "ok": True, "reason": status["training_reason"]}
    if action == "distill":
        return {"action": "distill", "ok": True, "reason": status["training_reason"]}
    return {"action": "none", "ok": False, "reason": status["training_reason"]}
