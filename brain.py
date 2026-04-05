from openai import OpenAI
from config import OPENAI_API_KEY, GPT_MINI, SYSTEM_PROMPT
import memory as mem

client = OpenAI(api_key=OPENAI_API_KEY)

conversation_history = []


def ask(user_input: str, model: str = GPT_MINI) -> str:
    return "".join(ask_stream(user_input, model))


def ask_stream(user_input: str, model: str = GPT_MINI):
    conversation_history.append({"role": "user", "content": user_input})
    system = SYSTEM_PROMPT + mem.get_context()
    messages = [{"role": "system", "content": system}] + conversation_history

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True
    )

    full_reply = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        full_reply += delta
        yield delta

    conversation_history.append({"role": "assistant", "content": full_reply})
