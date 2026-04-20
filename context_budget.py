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
