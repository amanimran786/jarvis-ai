from __future__ import annotations

import memory as _memory
import semantic_memory as _semantic_memory


def runtime_context(user_input: str = "") -> str:
    base = _memory.get_context()
    semantic = _semantic_memory.context_for_query(user_input or "", top_k=3, max_chars=1200) if user_input else ""
    if base and semantic:
        return base + "\n\n" + semantic
    return base or semantic or ""


def status() -> dict:
    data = _memory.memory_status()
    data["semantic_ready"] = bool(_semantic_memory.status().get("record_count", 0))
    return data


def remember(fact: str) -> None:
    _memory.add_fact(fact)


def forget(keyword: str) -> bool:
    return _memory.forget(keyword)
