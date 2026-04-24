from config import (
    FREE_FIRST_ENABLED,
    GEMINI_FLASH,
    GEMINI_PRO,
    GPT_FULL,
    GPT_MINI,
    HAIKU,
    LOCAL_DEFAULT,
    LOCAL_REASONING,
    OPUS,
    SONNET,
)
from brains import _teacher_capture


def _local_model_for_tier(tier: str) -> str:
    return LOCAL_REASONING if tier in {"strong", "deep"} else LOCAL_DEFAULT


def _open_source_mode() -> bool:
    try:
        import model_router

        return model_router.is_open_source_mode()
    except Exception:
        return True


def _try_local(prompt: str, tier: str, system_extra: str = "") -> str:
    from brains.brain_ollama import ask_local

    return ask_local(
        prompt,
        model=_local_model_for_tier(tier),
        system_extra=system_extra,
        raise_on_error=True,
    )


def _ask_openai(prompt: str, *, model: str, system_extra: str = "") -> str:
    from brains.brain import ask

    # bypass_local=True: ask_with_priority already tried the local lane at the
    # top of its plan. By the time we reach this helper we have explicitly
    # chosen the cloud, so brain.ask should not re-run the local-first gate.
    return ask(prompt, model=model, system_extra=system_extra, bypass_local=True)


def _ask_gemini(prompt: str, *, model: str, system: str | None, system_extra: str = "") -> str:
    from brains.brain_gemini import ask_gemini

    return ask_gemini(prompt, model=model, system=system, system_extra=system_extra)


def _ask_anthropic(prompt: str, *, model: str, system: str | None, system_extra: str = "") -> str:
    from brains.brain_claude import ask_claude

    return ask_claude(prompt, model=model, system=system, system_extra=system_extra)


def ask_with_priority(
    prompt: str,
    tier: str = "cheap",
    system_extra: str = "",
    system: str | None = None,
) -> str:
    tier = (tier or "cheap").strip().lower()
    plans = {
        "cheap": [
            ("openai", lambda: _ask_openai(prompt, model=GPT_MINI, system_extra=system_extra)),
            ("gemini", lambda: _ask_gemini(prompt, model=GEMINI_FLASH, system=system, system_extra=system_extra)),
            ("anthropic", lambda: _ask_anthropic(prompt, model=HAIKU, system=system, system_extra=system_extra)),
        ],
        "strong": [
            ("openai", lambda: _ask_openai(prompt, model=GPT_FULL, system_extra=system_extra)),
            ("gemini", lambda: _ask_gemini(prompt, model=GEMINI_PRO, system=system, system_extra=system_extra)),
            ("anthropic", lambda: _ask_anthropic(prompt, model=SONNET, system=system, system_extra=system_extra)),
        ],
        "deep": [
            ("gemini", lambda: _ask_gemini(prompt, model=GEMINI_PRO, system=system, system_extra=system_extra)),
            ("openai", lambda: _ask_openai(prompt, model=GPT_FULL, system_extra=system_extra)),
            ("anthropic", lambda: _ask_anthropic(prompt, model=OPUS, system=system, system_extra=system_extra)),
        ],
    }

    last_error = None
    open_source_mode = _open_source_mode()
    if FREE_FIRST_ENABLED or open_source_mode:
        try:
            return _try_local(prompt, tier, system_extra=system_extra)
        except Exception as exc:
            last_error = exc
            print(f"[ProviderPriority] local provider failed for tier {tier}: {exc}")
            if open_source_mode:
                raise

    for provider_name, runner in plans.get(tier, plans["cheap"]):
        try:
            answer = runner()
            # Best-effort teacher capture: high-tier cloud answers can train the
            # local open-source model. No-op unless JARVIS_TEACHER_CAPTURE=1.
            try:
                _teacher_capture.capture(
                    prompt,
                    answer,
                    tier=tier,
                    provider=provider_name,
                    model="",  # exact model is in the lambda; left blank to avoid coupling
                    source="provider_priority_cloud_teacher",
                )
            except Exception:
                pass
            return answer
        except Exception as exc:
            last_error = exc
            print(f"[ProviderPriority] provider failed for tier {tier}: {exc}")

    if last_error is not None:
        raise last_error
    raise RuntimeError("No provider plan available.")
