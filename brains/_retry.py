"""Shared rate-limit retry helper for the cloud brains.

Retries ONLY rate-limit errors (HTTP 429 / quota / overloaded) with exponential
backoff — 2s, 4s, 8s, max 3 retries — then re-raises so the caller's provider
fallback chain (model_router._execute_plan_stream, provider_priority) moves to
the next candidate. Non-rate-limit errors are never retried: they re-raise
immediately so failover stays fast.

Marker heuristics match provider_priority._RATE_LIMIT_MARKERS so both layers
classify the same errors as rate limits. Note the OpenAI and Anthropic SDKs
also do their own short internal retries (max_retries=2 by default); this
wrapper adds the longer, bounded backoff those defaults are too short for.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2.0

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "overloaded",
    "too many requests",
    "resource_exhausted",
)


def is_rate_limit_error(exc: Exception) -> bool:
    """Heuristic 429 detection across OpenAI/Anthropic/Gemini SDK exception types."""
    if getattr(exc, "status_code", None) == 429:
        return True
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(marker in haystack for marker in _RATE_LIMIT_MARKERS)


def backoff_delay(attempt: int) -> float:
    """Delay before retrying 0-based `attempt`: 2s, 4s, 8s."""
    return BASE_DELAY_SECONDS * (2 ** attempt)


def sleep_before_retry(provider: str, attempt: int, exc: Exception) -> None:
    delay = backoff_delay(attempt)
    log.warning(
        "[%s] rate limited (attempt %d/%d) — retrying in %.0fs: %s",
        provider, attempt + 1, MAX_RETRIES, delay, exc,
    )
    time.sleep(delay)


def call_with_backoff(fn, provider: str):
    """Call fn(), retrying rate-limit errors with exponential backoff.

    Non-rate-limit errors and the final rate-limit failure re-raise unchanged
    so existing provider failover behavior is preserved.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= MAX_RETRIES or not is_rate_limit_error(exc):
                raise
            sleep_before_retry(provider, attempt, exc)
