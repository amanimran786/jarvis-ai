from __future__ import annotations

from dataclasses import dataclass

from config import (
    GPT_MINI,
    GPT_FULL,
    GEMINI_FLASH,
    GEMINI_PRO,
    HAIKU,
    SONNET,
    OPUS,
    FREE_FIRST_ENABLED,
    PAID_FALLBACK_ENABLED,
    LOCAL_STRICT_FIRST,
    PROVIDER_PRIORITY_MINI,
    PROVIDER_PRIORITY_HAIKU,
    PROVIDER_PRIORITY_SONNET,
    PROVIDER_PRIORITY_OPUS,
    provider_runtime_config,
)


_CLOUD_MODEL_BY_PROVIDER_TIER = {
    "mini": {"openai": GPT_MINI, "gemini": GEMINI_FLASH, "anthropic": HAIKU},
    "haiku": {"openai": GPT_MINI, "gemini": GEMINI_FLASH, "anthropic": HAIKU},
    "sonnet": {"openai": GPT_FULL, "gemini": GEMINI_PRO, "anthropic": SONNET},
    "opus": {"openai": GPT_FULL, "gemini": GEMINI_PRO, "anthropic": OPUS},
}

_PRIORITY_BY_TIER = {
    "mini": PROVIDER_PRIORITY_MINI,
    "haiku": PROVIDER_PRIORITY_HAIKU,
    "sonnet": PROVIDER_PRIORITY_SONNET,
    "opus": PROVIDER_PRIORITY_OPUS,
}


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    model: str
    local: bool
    label: str


@dataclass(frozen=True)
class RoutePlan:
    mode: str
    tier: str
    candidates: tuple[RouteCandidate, ...]
    reason: str


def runtime_policy() -> dict:
    return provider_runtime_config()


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower().replace("_", "-")
    if m == "opensource":
        return "open-source"
    return m or "auto"


def _normalize_tier(tier: str) -> str:
    t = (tier or "mini").strip().lower()
    return t if t in {"local", "mini", "haiku", "sonnet", "opus"} else "mini"


def _cloud_candidates_for_tier(tier: str) -> list[RouteCandidate]:
    normalized = "mini" if tier == "local" else tier
    providers = _PRIORITY_BY_TIER.get(normalized, PROVIDER_PRIORITY_MINI)
    model_map = _CLOUD_MODEL_BY_PROVIDER_TIER.get(normalized, _CLOUD_MODEL_BY_PROVIDER_TIER["mini"])
    candidates: list[RouteCandidate] = []
    for provider in providers:
        model = model_map.get(provider)
        if not model:
            continue
        label = {
            "openai": "GPT-mini" if model == GPT_MINI else "GPT-4o",
            "gemini": "Gemini Flash" if model == GEMINI_FLASH else "Gemini Pro",
            "anthropic": "Claude Haiku" if model == HAIKU else ("Claude Sonnet" if model == SONNET else "Claude Opus"),
        }.get(provider, model)
        candidates.append(RouteCandidate(provider=provider, model=model, local=False, label=label))
    return candidates


def build_plan(
    *,
    mode: str,
    tier: str,
    local_available: bool,
    local_model: str = "",
    explicit_cloud: bool = False,
) -> RoutePlan:
    normalized_mode = _normalize_mode(mode)
    normalized_tier = _normalize_tier(tier)
    candidates: list[RouteCandidate] = []

    if normalized_mode == "open-source":
        if local_available and local_model:
            candidates.append(RouteCandidate(provider="ollama", model=local_model, local=True, label="Open-Source"))
        return RoutePlan(
            mode=normalized_mode,
            tier=normalized_tier,
            candidates=tuple(candidates),
            reason="Open-source mode uses local runtime only.",
        )

    if normalized_mode == "cloud":
        explicit_cloud = True

    should_prefer_local = (
        FREE_FIRST_ENABLED
        and not explicit_cloud
        and local_available
        and bool(local_model)
        and (normalized_mode in {"local", "auto"} or (normalized_mode == "cloud" and not explicit_cloud))
    )
    if normalized_mode == "local":
        should_prefer_local = local_available and bool(local_model)

    if should_prefer_local:
        candidates.append(RouteCandidate(provider="ollama", model=local_model, local=True, label="Local"))
        if not PAID_FALLBACK_ENABLED:
            return RoutePlan(
                mode=normalized_mode,
                tier=normalized_tier,
                candidates=tuple(candidates),
                reason="Local-first policy active; paid fallback disabled.",
            )
        if LOCAL_STRICT_FIRST or normalized_mode == "local":
            candidates.extend(_cloud_candidates_for_tier(normalized_tier))
        else:
            cloud = _cloud_candidates_for_tier(normalized_tier)
            if cloud:
                candidates.append(cloud[0])
    else:
        if PAID_FALLBACK_ENABLED or explicit_cloud:
            candidates.extend(_cloud_candidates_for_tier(normalized_tier))
        if local_available and local_model and normalized_mode in {"auto", "cloud"}:
            candidates.append(RouteCandidate(provider="ollama", model=local_model, local=True, label="Local"))

    reason = "Free-first routing with paid fallback." if should_prefer_local else "Cloud-priority routing due to mode or local unavailability."
    return RoutePlan(
        mode=normalized_mode,
        tier=normalized_tier,
        candidates=tuple(candidates),
        reason=reason,
    )
