"""PreFlect — prospective (pre-execution) plan critique for Jarvis agent tasks.

Closes gap G3 (reflection was post-hoc only). Mechanism follows PreFlect
(arxiv 2602.07187): a lightweight critic inspects a plan against a planning-error
taxonomy and flags risky plans *before* execution, instead of only recovering
after a failure.

Opt-in: disabled unless ``JARVIS_PREFLECT=1``. When disabled, ``review_plan``
is a no-op that makes no model call — behavior is byte-identical to before.

Cost control: the critic fires ONLY when the plan contains a side-effecting or
non-idempotent step (email / terminal / file / notes — flagged in
``tool_registry``). Read-only plans (search / research / calendar-read) skip the
critic entirely and pay zero token overhead.

Reused in-repo patterns:
- C's structured local verdict — ``brains.brain_ollama.ask_local_structured``
  with a grammar schema (no ``<think>``/markdown strip, temperature 0, local).
  That helper builds messages from only system+user, so it carries neither the
  Jarvis persona nor user memory — the same token discipline as F's
  ``bare_system=True``, for free.
- C's audit stream — verdicts emit via ``audit_event(action="plan_precheck", …)``
  so they inherit S5 tamper-evidence and the dashboard source.

v1 is **advisory**: it audits and surfaces the critique but still executes the
original plan. Auto-revision of a plan before execution is deferred — a malformed
revised plan could break execution, and this path is execution, not a hard gate
(fail-open, never block). Default-off keeps it safe during release freeze.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import tool_registry

log = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.getenv("JARVIS_PREFLECT", "").strip().lower() in {"1", "true", "yes", "on"}


_CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "risks", "summary"],
}

# Seed planning-error taxonomy (PreFlect distills this from trajectories; v1 ships
# a hand-written seed and can be replaced by run_id-grouped trace mining later).
_STATIC_TAXONOMY: tuple[str, ...] = (
    "write-before-read: a side-effecting write/send runs before the step that reads or produces its input",
    "missing verification: no step checks the result of a side-effecting action",
    "irreversible-without-guard: email send / file overwrite / shell command with no confirmation or rollback path",
    "wrong tool order: a dependent step runs before the step that produces the value it needs",
    "destructive-unscoped: a shell or file action that could delete/overwrite outside a narrow, intended path",
)

_CRITIC_SYSTEM = (
    "You are a terse pre-execution plan critic. You are given a task and an ordered plan of "
    "tool-backed steps that has NOT run yet. Some steps have side effects or are not idempotent. "
    "Check the plan ONLY against these known planning-failure patterns:\n"
    + "\n".join(f"- {p}" for p in _STATIC_TAXONOMY)
    + "\nReturn verdict 'revise' with specific risks if the plan matches any pattern, else 'approve'. "
    "Keep risks concrete and the summary to one line. Do not invent issues outside the listed patterns."
)


@dataclass
class PreflectResult:
    """Outcome of a pre-execution review. ``fired`` is False when the critic was skipped."""
    fired: bool = False
    verdict: str = "approve"
    risks: list[str] = field(default_factory=list)
    summary: str = ""
    risky_tools: list[str] = field(default_factory=list)


def _plan_has_irreversible_step(steps: list[Any]) -> tuple[bool, list[str]]:
    """Return (has_risky, distinct risky tool names) using tool_registry side-effect flags."""
    risky: list[str] = []
    for step in steps:
        tool = (getattr(step, "tool", "") or "chat").strip().lower()
        spec = tool_registry.get_tool_spec(tool)
        if spec is None:
            continue  # falls back to chat at execution time — no side effects
        if getattr(spec, "side_effects", False) or not getattr(spec, "idempotent", True):
            if tool not in risky:
                risky.append(tool)
    return (bool(risky), risky)


def _render_plan(steps: list[Any]) -> str:
    lines = []
    for s in steps:
        num = getattr(s, "number", "?")
        tool = getattr(s, "tool", "chat")
        desc = getattr(s, "description", "")
        lines.append(f"  {num}. [{tool}] {desc}")
    return "\n".join(lines)


def _severity_for(verdict: str, risks: list[str]) -> str:
    return "warning" if (verdict == "revise" and risks) else "info"


def review_plan(task: str, steps: list[Any], task_id: str = "") -> PreflectResult:
    """Critique a plan before execution. No-op (no model call) unless enabled AND a risky step exists."""
    if not is_enabled():
        return PreflectResult(fired=False)

    has_risky, risky_tools = _plan_has_irreversible_step(steps)
    if not has_risky:
        return PreflectResult(fired=False, risky_tools=[])

    payload = (
        f"TASK: {task}\n\nPLAN (not yet executed):\n{_render_plan(steps)}\n\n"
        f"Side-effecting / non-idempotent tools in this plan: {', '.join(risky_tools)}"
    )

    verdict, risks, summary = "approve", [], ""
    try:
        import json
        from brains.brain_ollama import ask_local_structured

        raw = ask_local_structured(
            payload, _CRITIQUE_SCHEMA, system=_CRITIC_SYSTEM, raise_on_error=False
        )
        data = json.loads(raw) if raw else {}
        verdict = str(data.get("verdict", "approve")).strip().lower()
        if verdict not in {"approve", "revise"}:
            verdict = "approve"
        risks = [str(r) for r in (data.get("risks") or [])][:8]
        summary = str(data.get("summary", "")).strip()
    except Exception:
        # Fail-open: this is execution, not a hard gate. Log, audit as a degraded
        # check, and let the original plan proceed.
        log.exception("preflect critic failed; proceeding with original plan")
        summary = "preflect critic unavailable (failed open)"

    severity = _severity_for(verdict, risks)
    try:
        from infra.security_audit import audit_event

        audit_event(
            action="plan_precheck",
            actor="preflect",
            target=str(task)[:80],
            decision=verdict,
            severity=severity,
            reason=summary or "; ".join(risks)[:1024],
            task_id=task_id,
            extra={"risky_tools": ",".join(risky_tools), "risk_count": len(risks)},
        )
    except Exception:
        log.exception("preflect audit emit failed")

    return PreflectResult(
        fired=True, verdict=verdict, risks=risks, summary=summary, risky_tools=risky_tools
    )
