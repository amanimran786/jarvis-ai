"""Shared cloud->local teacher capture lane.

When `JARVIS_TEACHER_CAPTURE=1` is set, successful cloud answers from the
strong/deep tiers are recorded as JSONL teacher examples for fine-tuning the
local open-source model. This module is the single capture surface used by
both `provider_priority` (one-shot lane) and `model_router` (streaming lane)
so we never double-implement the gate.

Best-effort by design: any failure here is logged and swallowed so user-facing
responses are never affected by the capture lane.
"""
from __future__ import annotations

import os


def is_enabled() -> bool:
    """Return True iff the teacher-capture flag is set in the environment."""
    return os.getenv("JARVIS_TEACHER_CAPTURE", "").strip().lower() in {"1", "true", "yes", "on"}


def capture(
    prompt: str,
    answer: str,
    *,
    tier: str,
    provider: str,
    model: str,
    source: str = "cloud_teacher",
) -> None:
    """Record a successful cloud answer as a teacher example.

    No-op unless `JARVIS_TEACHER_CAPTURE=1` is set and the tier is strong/deep.
    Any failure is logged and swallowed.
    """
    if not is_enabled():
        return
    if tier not in {"strong", "deep"}:
        return
    if not (prompt and answer):
        return
    try:
        from local_runtime import local_training

        result = local_training.record_teacher_example(
            prompt,
            answer,
            source=source,
            tags=[f"tier:{tier}", f"provider:{provider}", f"model:{model}"],
            meta={"tier": tier, "provider": provider, "model": model},
        )
        # Be tolerant of older/stub recorders that return None or a non-dict.
        if not isinstance(result, dict):
            return
        if not result.get("ok"):
            print(f"[TeacherCapture] skipped: {result.get('error')}")
    except Exception as exc:
        print(f"[TeacherCapture] failed: {exc}")


def wrap_stream(prompt, tier, candidate, raw_stream, source: str = "model_router_cloud_teacher"):
    """Yield from a cloud stream while capturing the full answer for the
    teacher pack on successful completion. Partial streams (mid-stream
    exception) are NOT captured to avoid poisoning training data.

    `candidate` is duck-typed: must expose `.provider` and `.model`.
    """
    parts = []
    completed = False
    try:
        for chunk in raw_stream:
            parts.append(chunk)
            yield chunk
        completed = True
    finally:
        if completed:
            try:
                capture(
                    prompt,
                    "".join(parts),
                    tier=tier,
                    provider=candidate.provider,
                    model=candidate.model,
                    source=source,
                )
            except Exception:
                pass
