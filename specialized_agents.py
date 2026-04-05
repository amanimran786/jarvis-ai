"""
Scoped specialist-agent coordinator for Jarvis.

Each role has its own instruction file under agents/ and runs in isolated
context. The coordinator uses a small multi-pass flow only when explicitly
requested or when the router selects the specialized-agent tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from brain_claude import ask_claude
from config import HAIKU, SONNET, SYSTEM_PROMPT
import skills


AGENTS_DIR = Path(__file__).resolve().parent / "agents"


@dataclass(frozen=True)
class AgentSpec:
    role: str
    path: Path
    model: str


AGENTS = {
    "planner": AgentSpec("planner", AGENTS_DIR / "planner.md", HAIKU),
    "executor": AgentSpec("executor", AGENTS_DIR / "executor.md", SONNET),
    "reviewer": AgentSpec("reviewer", AGENTS_DIR / "reviewer.md", HAIKU),
    "science_expert": AgentSpec("science_expert", AGENTS_DIR / "science_expert.md", SONNET),
    "security_reviewer": AgentSpec("security_reviewer", AGENTS_DIR / "security_reviewer.md", SONNET),
    "self_improve_critic": AgentSpec("self_improve_critic", AGENTS_DIR / "self_improve_critic.md", HAIKU),
}


def _load_agent_instructions(role: str) -> str:
    spec = AGENTS[role]
    return spec.path.read_text(encoding="utf-8").strip()


def available_roles() -> list[str]:
    return list(AGENTS)


def _explicit_roles(user_input: str) -> list[str]:
    lower = user_input.lower()
    aliases = {
        "planner": ("planner", "plan this"),
        "executor": ("executor", "execute this"),
        "reviewer": ("reviewer", "review this"),
        "science_expert": ("science expert", "science_expert", "technology expert"),
        "security_reviewer": ("security reviewer", "security audit", "security review"),
        "self_improve_critic": ("self improve critic", "self-improve critic", "self review critic"),
    }
    selected = []
    for role, triggers in aliases.items():
        if any(trigger in lower for trigger in triggers):
            selected.append(role)
    return selected


def choose_roles(user_input: str) -> list[str]:
    lower = user_input.lower()
    science_markers = (
        "transformer", "kv cache", "entropy", "thermodynamics", "information theory",
        "crispr", "biology", "physics", "chemistry", "lithography", "semiconductor",
        "materials science", "science", "technology",
    )
    security_markers = ("security", "auth", "authentication", "authorization", "exploit", "vulnerability")

    explicit = _explicit_roles(user_input)
    if explicit:
        if any(t in lower for t in security_markers) and "security_reviewer" not in explicit:
            return ["security_reviewer", "reviewer"]
        if any(t in lower for t in science_markers) and "science_expert" not in explicit:
            return ["science_expert", "reviewer"]
        return explicit

    roles = ["planner", "executor", "reviewer"]
    if any(t in lower for t in science_markers):
        return ["science_expert", "reviewer"]
    if any(t in lower for t in security_markers):
        return ["security_reviewer", "reviewer"]
    if any(t in lower for t in ("improve yourself", "self improve", "self-improve", "should you change your code")):
        return ["self_improve_critic", "reviewer"]
    return roles


def _run_role(role: str, task: str, context: str = "") -> dict:
    spec = AGENTS[role]
    system_extra, _ = skills.build_system_extra(task, tool="chat")
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Specialized agent role for this request:\n{_load_agent_instructions(role)}\n\n"
        "Keep the output plain text and voice-safe. No markdown, no headers, no bullets, and no code fences."
    )
    prompt = task
    if context:
        prompt += f"\n\nContext from other agents:\n{context}"
    output = ask_claude(
        prompt,
        model=spec.model,
        system=system,
        system_extra=system_extra,
    ).strip()
    return {"role": role, "model": spec.model, "output": output}


def run(user_input: str, roles: list[str] | None = None) -> dict:
    selected = roles or choose_roles(user_input)
    stages = []
    shared_context = ""

    for role in selected:
        result = _run_role(role, user_input, context=shared_context)
        stages.append(result)
        if role in {"planner", "science_expert", "security_reviewer", "self_improve_critic"}:
            shared_context += f"{role}: {result['output']}\n\n"

    if selected == ["planner", "executor", "reviewer"]:
        plan = stages[0]["output"]
        execution = _run_role("executor", user_input, context=f"planner: {plan}")
        stages[1] = execution
        review = _run_role("reviewer", user_input, context=f"planner: {plan}\n\nexecutor: {execution['output']}")
        stages[2] = review
        final_output = execution["output"]
    elif selected and selected[-1] == "reviewer" and len(stages) >= 2:
        final_output = stages[-2]["output"]
    else:
        final_output = stages[-1]["output"] if stages else ""

    return {
        "ok": True,
        "roles": selected,
        "stages": stages,
        "final": final_output,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Specialized agent run failed.")
    final = (result.get("final", "") or "").strip()
    if final:
        return final
    return "The specialist pass completed, but it did not produce a final answer."
