"""
Agent dispatch — routes tasks to specialist agents defined in AGENT_ROSTER.
Local-only: all inference runs via Ollama. No cloud fallback.

Agents with web_search, file_read, get_weather, or memory tools in their roster
use the full agentic tool-calling loop (ask_local_with_tools). Others fall back
to plain streaming (ask_local_stream).
"""
from typing import Generator
from config import AGENT_ROSTER

# Maps roster tool names to the callable tool names in ask_local_with_tools.
# Omits shell/code/file_write — those require sandboxing before LLM dispatch.
_TOOL_NAME_MAP: dict[str, str] = {
    "web_search": "web_search",
    "file_read": "read_file",
    "file_write": "write_file",
    "run_tests": "run_tests",
    "memory": "memory_lookup",
    "get_weather": "get_weather",
}


def dispatch(
    agent_name: str, task: str, context: str = ""
) -> Generator[str, None, None]:
    agent = AGENT_ROSTER.get(agent_name)
    if agent is None:
        raise RuntimeError(
            f"Unknown agent: '{agent_name}'. Available: {sorted(AGENT_ROSTER)}"
        )

    if "system_prompt" in agent:
        system_extra = agent["system_prompt"]
        if context:
            system_extra += f"\n\nContext: {context}"
    else:
        tools_str = ", ".join(agent["tools"]) if agent["tools"] else "none"
        system_extra = (
            f"You are the {agent_name} agent. "
            f"Role: {agent['role']} "
            f"Available tools: {tools_str}. "
            f"Focus exclusively on the assigned task. Be precise and actionable."
        )
        if context:
            system_extra += f"\n\nContext: {context}"

    callable_tools = [
        _TOOL_NAME_MAP[t] for t in agent["tools"] if t in _TOOL_NAME_MAP
    ]

    from brains.brain_ollama import ask_local_with_tools
    yield from ask_local_with_tools(
        task,
        model=agent["model"],
        system_extra=system_extra,
        tools=callable_tools or None,
        workspace_confined=agent_name == "backend_engineer",
    )


def list_agents() -> list[dict]:
    return [
        {"id": name, "name": name, "role": agent["role"], "model": agent["model"]}
        for name, agent in AGENT_ROSTER.items()
    ]
