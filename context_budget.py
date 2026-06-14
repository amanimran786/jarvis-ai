"""
Jarvis context budget policy.

This is the native version of the "token saver" idea: keep task prompts
small, keep raw dumps out of model context, and make coding lanes explicit.
"""

from __future__ import annotations

from typing import Any

import usage_tracker
from config import LOCAL_CODER, LOCAL_DEFAULT, LOCAL_REASONING


PROFILES: dict[str, dict[str, Any]] = {
    "lite": {
        "label": "quick",
        "terse_mode": "lite",
        "best_for": "small edits, short answers, command triage",
        "rule": "answer in the fewest useful lines; no broad exploration",
    },
    "full": {
        "label": "default",
        "terse_mode": "full",
        "best_for": "normal implementation, debugging, repo-grounded answers",
        "rule": "inspect only relevant files, summarize logs, verify narrowly",
    },
    "ultra": {
        "label": "hard cap",
        "terse_mode": "ultra",
        "best_for": "large repos, long logs, repeated agent work",
        "rule": "symbol-first navigation; store bulky output outside context",
    },
}


def estimate_tokens(text: str) -> int:
    """Cheap local token estimate used before provider calls expose real usage."""
    cleaned = (text or "").strip()
    return max(1, len(cleaned) // 4) if cleaned else 0


def target_tokens_for(tool: str | None = None, *, default: int | None = None) -> int:
    """Return the prompt target for the request lane.

    This is intentionally conservative. The model may support a larger context,
    but keeping a smaller active working set preserves latency and KV-cache reuse.
    """
    import os

    raw = os.getenv("JARVIS_CONTEXT_TARGET_TOKENS")
    if raw:
        try:
            return max(2048, int(raw))
        except ValueError:
            pass
    lane = (tool or "chat").strip().lower()
    if default:
        return default
    if lane in {"code", "terminal", "shell"}:
        return 32_000
    if lane in {"research", "browser", "vault"}:
        return 24_000
    if lane in {"task", "agent"}:
        return 16_000
    return 12_000


def _trim_chars(text: str, max_chars: int | None) -> str:
    text = (text or "").strip()
    if not max_chars or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 16)].rstrip() + "\n[truncated]"


def compile_context_blocks(
    blocks: list[dict[str, Any]],
    *,
    base_text: str = "",
    user_input: str = "",
    target_tokens: int = 12_000,
    reserve_response_tokens: int = 1_024,
) -> dict[str, Any]:
    """Select context blocks under one prompt budget.

    Callers pass candidate blocks as dictionaries:
    {"label": str, "content": str, "priority": int, "max_chars": int | None}

    Higher-priority blocks win when the prompt is tight. The function returns
    selected text plus a report suitable for dashboard/debug surfaces.
    """
    base_tokens = estimate_tokens(base_text) + estimate_tokens(user_input)
    budget = max(0, int(target_tokens) - int(reserve_response_tokens) - base_tokens)
    ordered = sorted(
        enumerate(blocks or []),
        key=lambda item: (-int(item[1].get("priority", 0)), item[0]),
    )
    selected: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    used = 0
    for original_index, block in ordered:
        label = str(block.get("label") or f"block_{original_index}")
        content = _trim_chars(str(block.get("content") or ""), block.get("max_chars"))
        if not content:
            continue
        tokens = estimate_tokens(content)
        entry = {
            "label": label,
            "tokens": tokens,
            "priority": int(block.get("priority", 0)),
        }
        if tokens <= max(0, budget - used):
            selected.append({**entry, "content": content, "index": original_index})
            used += tokens
        else:
            dropped.append(entry)

    selected.sort(key=lambda item: item["index"])
    return {
        "text": "\n\n".join(item["content"] for item in selected),
        "target_tokens": int(target_tokens),
        "base_tokens": base_tokens,
        "reserve_response_tokens": int(reserve_response_tokens),
        "context_budget_tokens": budget,
        "context_used_tokens": used,
        "selected": [{k: v for k, v in item.items() if k not in {"content", "index"}} for item in selected],
        "dropped": dropped,
    }


def policy_status(hours: int = 24) -> dict[str, Any]:
    usage = usage_tracker.summarize(hours=hours, include_recent=5)
    local_tokens = sum(
        int(bucket.get("total_tokens") or 0)
        for bucket in (usage.get("by_model") or {}).values()
        if bucket.get("local")
    )
    cloud_tokens = int(usage.get("total_tokens") or 0) - local_tokens
    usage = {**usage, "local_tokens": local_tokens, "cloud_tokens": max(0, cloud_tokens)}
    return {
        "ok": True,
        "purpose": "Keep Jarvis local coding and agent work repo-grounded without wasting context.",
        "models": {
            "default": LOCAL_DEFAULT,
            "coder": LOCAL_CODER,
            "reasoning": LOCAL_REASONING,
        },
        "defaults": {
            "chat": "normal streaming chat",
            "task": "managed task with terse_mode=full",
            "code": "isolated workspace task with terse_mode=full",
            "vault": "curator/proposal-first where ambiguity exists",
            "skill": "proposal-first via skill_builder",
        },
        "profiles": PROFILES,
        "commands": {
            "/context-budget": "show this policy",
            "/tokens": "alias for /context-budget",
            "/task-lite <prompt>": "quick managed task",
            "/task <prompt>": "default managed task",
            "/task-ultra <prompt>": "hard-capped managed task",
            "/code-lite <prompt>": "quick isolated coding task",
            "/code <prompt>": "default isolated coding task",
            "/code-ultra <prompt>": "hard-capped isolated coding task",
        },
        "rules": [
            "Prefer targeted file reads over dumping directories or logs into chat.",
            "Use managed tasks for multi-step implementation so output is streamed and persisted.",
            "Use isolated coding workspaces for code changes by default.",
            "Summarize terminal output before feeding it back into the model.",
            "Promote repeated workflows into proposal-first local skills instead of longer prompts.",
            "Keep cloud tools optional; the main coding loop stays local-first.",
        ],
        "usage": usage,
    }


def policy_text(hours: int = 24) -> str:
    status = policy_status(hours=hours)
    usage = status.get("usage") or {}
    profiles = status["profiles"]
    command_lines = [
        "/code <prompt>       default isolated coding loop",
        "/code-lite <prompt>  small coding change with tighter output",
        "/code-ultra <prompt> large-repo/log-heavy coding with hard compression",
        "/task <prompt>       managed non-code task",
        "/task-ultra <prompt> managed task with maximum compression",
    ]
    profile_lines = [
        f"{name}: {profile['best_for']} -> {profile['rule']}"
        for name, profile in profiles.items()
    ]
    return "\n".join(
        [
            "Context budget policy: keep Jarvis repo-grounded, local-first, and terse by default.",
            f"Local models: default={LOCAL_DEFAULT}, coder={LOCAL_CODER}, reasoning={LOCAL_REASONING}.",
            f"Last {hours}h usage: total={usage.get('total_tokens', 0)} tokens, local={usage.get('local_tokens', 0)}, cloud={usage.get('cloud_tokens', 0)}.",
            "",
            "Profiles:",
            *[f"- {line}" for line in profile_lines],
            "",
            "Console commands:",
            *[f"- {line}" for line in command_lines],
            "",
            "Rules:",
            "- inspect symbols and narrow files before reading whole files",
            "- summarize raw logs before sending them back into the model",
            "- use isolated /code lanes for implementation",
            "- turn repeated workflows into proposal-first local skills",
        ]
    )
