import re

from config import GEMINI_API_KEY, GEMINI_FLASH, SYSTEM_PROMPT
import memory as mem
import conversation_context as ctx
import usage_tracker

try:
    from google import genai
except Exception:  # pragma: no cover - import failure handled at runtime
    genai = None


client = genai.Client(api_key=GEMINI_API_KEY) if genai and GEMINI_API_KEY else None


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.)](?:\s+|$)", "", text, flags=re.M)
    text = re.sub(r"```\w*\n?", "", text)
    return text


def _ensure_client():
    if client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("Gemini API key is not configured.")
        raise RuntimeError("google-genai is not installed.")


def _to_contents(messages: list[dict]) -> list[dict]:
    contents = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if not content:
            continue
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": content}],
            }
        )
    return contents


def ask_gemini(
    user_input: str,
    model: str = GEMINI_FLASH,
    system: str = None,
    system_extra: str = "",
    track_context: bool = False,
) -> str:
    return "".join(
        ask_gemini_stream(
            user_input,
            model=model,
            system=system,
            system_extra=system_extra,
            track_context=track_context,
        )
    )


def ask_gemini_stream(
    user_input: str,
    model: str = GEMINI_FLASH,
    system: str = None,
    system_extra: str = "",
    track_context: bool = False,
):
    _ensure_client()

    system_base = system if system is not None else (SYSTEM_PROMPT + mem.get_context())
    if track_context:
        ctx.begin_turn(user_input)
        effective_system, messages, _ = ctx.build_prompt_state(system_base, system_extra=system_extra)
    else:
        effective_system = system_base
        if system_extra:
            effective_system += "\n\n" + system_extra
        messages = [{"role": "user", "content": user_input}]

    response = client.models.generate_content(
        model=model,
        contents=_to_contents(messages),
        config={"system_instruction": effective_system},
    )

    full_reply = _strip_markdown(getattr(response, "text", "") or "")
    buffer = ""
    for char in full_reply:
        buffer += char
        if char in ".!?\n":
            yield buffer
            buffer = ""
    if buffer:
        yield buffer

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    completion_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    total_tokens = getattr(usage, "total_token_count", None) if usage else None
    usage_tracker.record(
        provider="gemini",
        model=model,
        local=False,
        source="brain_gemini.ask_gemini_stream",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        messages=[{"role": "system", "content": effective_system}] + messages,
        response_text=full_reply,
        estimated=usage is None,
        metadata={"track_context": track_context},
    )

    if track_context:
        ctx.end_turn(full_reply)
