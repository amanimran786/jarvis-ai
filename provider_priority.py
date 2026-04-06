from brain import ask
from brain_gemini import ask_gemini
from brain_claude import ask_claude
from config import GPT_MINI, GPT_FULL, GEMINI_FLASH, GEMINI_PRO, HAIKU, SONNET, OPUS


def ask_with_priority(
    prompt: str,
    tier: str = "cheap",
    system_extra: str = "",
    system: str | None = None,
) -> str:
    tier = (tier or "cheap").strip().lower()
    plans = {
        "cheap": [
            ("openai", lambda: ask(prompt, model=GPT_MINI, system_extra=system_extra)),
            ("gemini", lambda: ask_gemini(prompt, model=GEMINI_FLASH, system=system, system_extra=system_extra)),
            ("anthropic", lambda: ask_claude(prompt, model=HAIKU, system=system, system_extra=system_extra)),
        ],
        "strong": [
            ("openai", lambda: ask(prompt, model=GPT_FULL, system_extra=system_extra)),
            ("gemini", lambda: ask_gemini(prompt, model=GEMINI_PRO, system=system, system_extra=system_extra)),
            ("anthropic", lambda: ask_claude(prompt, model=SONNET, system=system, system_extra=system_extra)),
        ],
        "deep": [
            ("gemini", lambda: ask_gemini(prompt, model=GEMINI_PRO, system=system, system_extra=system_extra)),
            ("openai", lambda: ask(prompt, model=GPT_FULL, system_extra=system_extra)),
            ("anthropic", lambda: ask_claude(prompt, model=OPUS, system=system, system_extra=system_extra)),
        ],
    }

    last_error = None
    for _, runner in plans.get(tier, plans["cheap"]):
        try:
            return runner()
        except Exception as exc:
            last_error = exc
            print(f"[ProviderPriority] provider failed for tier {tier}: {exc}")

    if last_error is not None:
        raise last_error
    raise RuntimeError("No provider plan available.")
