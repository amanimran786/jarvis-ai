"""
harness/prompt_generator.py — Generate structured session prompts from task specs.

Renders a deterministic agent packet from a task contract + repo context.  An
optional legacy LLM path remains available for experiments, but orchestration
uses the deterministic path so workflow policy cannot drift with model output.

Public API:
    generate_session_prompt(task, repo_context) -> str
        Returns a complete prompt string ready to pass to start_task / LAUNCH_QUEUE.
"""
from __future__ import annotations

import logging
import textwrap
from typing import Any

from harness.task_contract import TaskSpec

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_PLANNING_MODEL_DEFAULT = "qwen3:30b-a3b"
_LLM_TIMEOUT_SECONDS    = 90   # generous — planning model is large


# ── Meta-prompt template ──────────────────────────────────────────────────────

_META_SYSTEM = """\
You are a senior engineering lead at Jarvis AI writing detailed session prompts \
for AI coding agents (Claude, Codex, etc.).

A session prompt tells the agent:
  • Its role and the codebase context
  • Exactly what to build, referencing specific files
  • The acceptance criteria it must satisfy
  • How to format its commit message

Your output is the COMPLETE session prompt — nothing else.  No preamble, no \
explanation.  Start with the <role> block and end after the commit format line.
"""

_META_PROMPT_TEMPLATE = """\
Write a complete session prompt for the following engineering task.

=== TASK ===
ID:          {task_id}
Title:       {title}
Description:
{description}

Key files:
{files_hint}

Acceptance criteria:
{acceptance_criteria}

=== REPO CONTEXT ===
Recent commits (last 5):
{recent_commits}

Active files changed recently:
{active_files}

Test count (current): {test_count}

=== PROMPT STRUCTURE TO FOLLOW ===
1. <role> block     — one sentence: what this agent is and what its mission is
2. <context> XML    — repo snapshot: what already shipped, what's broken, what's next
3. <instructions>   — numbered list of concrete implementation steps
4. <acceptance>     — copy the acceptance criteria verbatim, formatted as a checklist
5. Commit format    — exactly: [CLAUDE] <type>(<scope>): <description>

Generate the complete prompt now:
"""


# ── Core function ─────────────────────────────────────────────────────────────

def generate_session_prompt(
    task: dict[str, Any],
    repo_context: dict[str, Any],
    *,
    use_llm: bool = False,
) -> str:
    """
    Generate a complete session prompt for a task.

    Args:
        task: {
            id, title, description,
            files_hint:           list[str] | str,
            acceptance_criteria:  list[str] | str,
            domain:               str (optional),
        }
        repo_context: {
            recent_commits:  str | list[str],  # last 5 git log lines
            test_count:      int,
            active_files:    list[str],
        }

    Returns:
        Full prompt string. The default is deterministic. ``use_llm=True`` is a
        compatibility experiment and still falls back to the contract renderer.
    """
    if use_llm:
        try:
            return _generate_via_llm(task, repo_context)
        except Exception as exc:
            log.warning("[PromptGen] LLM unavailable (%s) — using contract renderer", exc)
    return _generate_fallback(task, repo_context)


# ── LLM path ─────────────────────────────────────────────────────────────────

def _resolve_model() -> str:
    """Return the configured planning model, defaulting to qwen3:30b-a3b."""
    try:
        from config import LOCAL_REASONING  # type: ignore[import]
        return LOCAL_REASONING or _PLANNING_MODEL_DEFAULT
    except Exception:
        return _PLANNING_MODEL_DEFAULT


def _generate_via_llm(task: dict[str, Any], repo_context: dict[str, Any]) -> str:
    """Call Ollama and return the generated prompt."""
    from brains.brain_ollama import ask_local  # type: ignore[import]

    meta_prompt = _build_meta_prompt(task, repo_context)
    model = _resolve_model()

    log.debug("[PromptGen] Calling %s for task %s", model, task.get("id", "?"))
    result: str = ask_local(
        meta_prompt,
        model=model,
        system=_META_SYSTEM,
        timeout=_LLM_TIMEOUT_SECONDS,
    )

    if not result or not result.strip():
        raise ValueError("LLM returned empty response")

    return result.strip()


def _build_meta_prompt(task: dict[str, Any], repo_context: dict[str, Any]) -> str:
    """Render the meta-prompt template with task + repo data."""
    files_hint = task.get("allowed_files", task.get("files_hint", []))
    if isinstance(files_hint, list):
        files_hint = "\n".join(f"  • {f}" for f in files_hint) or "  (not specified)"
    else:
        files_hint = str(files_hint) or "  (not specified)"

    criteria = task.get("acceptance_criteria", [])
    if isinstance(criteria, list):
        criteria = "\n".join(f"  - {c}" for c in criteria) or "  (not specified)"
    else:
        criteria = str(criteria) or "  (not specified)"

    commits = repo_context.get("recent_commits", [])
    if isinstance(commits, list):
        commits = "\n".join(f"  {c}" for c in commits) or "  (none)"
    else:
        commits = str(commits) or "  (none)"

    active_files = repo_context.get("active_files", [])
    if isinstance(active_files, list):
        active_files = ", ".join(active_files) or "(none)"
    else:
        active_files = str(active_files) or "(none)"

    description = textwrap.indent(task.get("description", "").strip(), "    ")

    return _META_PROMPT_TEMPLATE.format(
        task_id=task.get("id", "TASK-???"),
        title=task.get("title", "(untitled)"),
        description=description,
        files_hint=files_hint,
        acceptance_criteria=criteria,
        recent_commits=commits,
        active_files=active_files,
        test_count=repo_context.get("test_count", "unknown"),
    )


# ── Fallback path (no LLM needed) ────────────────────────────────────────────

def _generate_fallback(task: dict[str, Any], repo_context: dict[str, Any]) -> str:
    """
    Build a structured prompt purely from the task spec — no LLM required.
    Used when Ollama is down or times out.
    """
    spec = TaskSpec.from_queue_task(task)

    files_str = "\n".join(f"  - {f}" for f in spec.allowed_files) or "  (not declared)"
    forbidden_str = "\n".join(f"  - {f}" for f in spec.forbidden_files) or "  (none declared)"
    criteria_str = (
        "\n".join(f"  - [ ] {criterion}" for criterion in spec.acceptance_criteria)
        or "  - [ ] Produce the requested artifact for loop inspection"
    )
    verification_str = (
        "\n".join(f"  - {command}" for command in spec.verification_commands)
        or "  (no deterministic command declared; loop must not infer verification)"
    )

    commits = repo_context.get("recent_commits", [])
    if isinstance(commits, list):
        commits_str = "\n".join(f"  {c}" for c in commits[:5]) or "  (none)"
    else:
        commits_str = str(commits)

    return textwrap.dedent(f"""\
        <role>
        You are the execution agent for one transition in the Jarvis engineering loop.
        </role>

        <task_contract>
        Task ID: {spec.task_id}
        Contract SHA-256: {spec.contract_hash}
        Goal: {spec.goal}
        Title: {spec.title}
        Repository: jarvis-ai
        Domain: {spec.domain}
        Local-first: {bool(spec.constraints.get("local_first", True))}

        Description:
        {spec.description}

        Allowed files:
{files_str}

        Forbidden files:
{forbidden_str}

        Acceptance criteria:
{criteria_str}

        Verification commands owned by the loop:
{verification_str}

        Budget: attempts={spec.budget.max_attempts}, wall_time={spec.budget.wall_time_seconds}s, \
tool_calls={spec.budget.tool_calls}
        </task_contract>

        <context>

        Recent commits:
{commits_str}
        </context>

        <loop_protocol>
        1. Work only inside the contract boundaries.
        2. Inspect before editing and make the smallest correct change.
        3. Do not claim the task is complete or tests passed; report actions and evidence.
        4. The loop, not this response, runs verification and decides done/retry/escalate.
        5. Stop and report a contract conflict instead of editing a forbidden or undeclared path.
        </loop_protocol>

        Commit format: [{spec.assigned_ai.upper()}] feat({spec.domain}): {spec.title.lower().replace(" ", "-")}
    """)
