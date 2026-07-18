"""
Jarvis Manager Core Loop
--------------------------
Takes a broad human goal and produces a scheduled multi-agent execution plan.

Pipeline per goal:
  1. Retrieve semantic context from memory (project + personal layers)
  2. Decompose goal into independent agent tasks via local LLM (structured JSON)
  3. Security gate: route high-risk tasks through security_reviewer before publish
  4. Publish approved tasks to the event bus (HTTP) or direct dispatch (fallback)
  5. Return an ExecutionPlan with all task_ids for polling

Scheduling fallback order:
  event bus (localhost:8766) → direct agent_dispatch (in-process, synchronous)

The Manager itself runs as identity 'jarvis_manager' (role=manager) so it can
dispatch to any agent in the RBAC registry.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("jarvis.manager")

# Agents whose tasks always get a security review pass regardless of LLM flag
_ALWAYS_REVIEW: frozenset[str] = frozenset({"devops_release"})

# ─── Data types ───────────────────────────────────────────────────────────────

@dataclass
class AgentTask:
    title:                 str
    description:           str
    agent:                 str
    priority:              int  = 5
    needs_security_review: bool = False
    context:               dict = field(default_factory=dict)
    # filled after scheduling
    task_id:               str  = ""
    status:                str  = "pending"
    security_verdict:      str  = ""


@dataclass
class ExecutionPlan:
    goal:        str
    session_id:  str
    project_id:  str
    tasks:       list[AgentTask]
    created_at:  float = field(default_factory=time.time)

    @property
    def task_ids(self) -> list[str]:
        return [t.task_id for t in self.tasks if t.task_id]

    @property
    def blocked_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "security_blocked")

    def to_dict(self) -> dict:
        return {
            "goal":       self.goal,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "tasks": [
                {
                    "task_id":          t.task_id,
                    "title":            t.title,
                    "agent":            t.agent,
                    "priority":         t.priority,
                    "status":           t.status,
                    "security_verdict": t.security_verdict,
                }
                for t in self.tasks
            ],
        }


# ─── Decomposition prompt ─────────────────────────────────────────────────────

def _agent_roster_summary() -> str:
    from config import AGENT_ROSTER
    lines = []
    for name, spec in AGENT_ROSTER.items():
        tools = ", ".join(spec.get("tools", []))
        lines.append(f"  {name}: {spec['role']} Tools: [{tools}]")
    return "\n".join(lines)


_DECOMPOSE_SYSTEM = """You are the Jarvis Manager. Your job is to decompose a broad goal into \
the minimum set of independent, parallelizable tasks that can be assigned to specialist agents.

Available agents:
{agent_roster}

{memory_context}

Rules:
- Produce 1-6 tasks. Fewer is better — only create a task if it genuinely needs that agent.
- Tasks should be independent (parallelizable) where possible. Only add context.depends_on \
  when strict ordering is required.
- Set needs_security_review=true for any task involving: shell execution, file writes, \
  deployments, database mutations, external API calls with credentials, or git operations.
- Set needs_security_review=true for researcher tasks that request cloud research, cloud \
  model escalation, or any context.allow_cloud_research flag.
- devops_release tasks always get security review regardless of your flag.
- Use the agent whose tools best match the task — don't assign web_search to backend_engineer.
- description should be actionable and self-contained (the agent has no other context).

Output a JSON object with this exact structure (no markdown, no prose):
{{
  "goal_summary": "<one sentence>",
  "tasks": [
    {{
      "title": "<short title>",
      "description": "<detailed actionable description>",
      "agent": "<agent name from the list above>",
      "priority": <1-10>,
      "needs_security_review": <true|false>,
      "context": {{}}
    }}
  ]
}}"""


def _build_decompose_prompt(goal: str, memory_ctx: str) -> tuple[str, str]:
    """Return (system_prompt, user_message)."""
    mem_block = f"Relevant memory context:\n{memory_ctx}" if memory_ctx else ""
    system = _DECOMPOSE_SYSTEM.format(
        agent_roster=_agent_roster_summary(),
        memory_context=mem_block,
    )
    user = f"Decompose this goal into agent tasks:\n\n{goal}"
    return system, user


# ─── LLM decomposition ────────────────────────────────────────────────────────

def _decompose_via_llm(goal: str, memory_ctx: str) -> list[AgentTask]:
    from brains.brain_ollama import ask_local_structured
    from config import LOCAL_DEFAULT, AGENT_ROSTER

    system, user = _build_decompose_prompt(goal, memory_ctx)
    valid_agents  = set(AGENT_ROSTER.keys())

    try:
        raw = ask_local_structured(user, schema="json", system=system)
    except Exception as exc:
        log.warning("decompose LLM call failed: %s — falling back to single researcher task", exc)
        return [AgentTask(
            title="Research and respond",
            description=goal,
            agent="researcher",
            priority=5,
        )]

    if not isinstance(raw, str):
        log.warning("decompose LLM call returned non-string %r — treating as empty", type(raw))
        raw = ""

    # Parse — strip think tags first
    import re
    raw = re.sub(r"<think>.*?</think>|<thinking>.*?</thinking>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        data  = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("decompose JSON parse failed: %s", exc)
        return [AgentTask(title="Execute goal", description=goal, agent="researcher")]

    tasks = []
    for item in data.get("tasks", []):
        agent = item.get("agent", "researcher")
        if agent not in valid_agents:
            log.warning("LLM proposed unknown agent '%s' — remapping to researcher", agent)
            agent = "researcher"
        tasks.append(AgentTask(
            title=str(item.get("title", "Task"))[:120],
            description=str(item.get("description", goal)),
            agent=agent,
            priority=int(item.get("priority", 5)),
            needs_security_review=bool(item.get("needs_security_review", False)),
            context=item.get("context", {}),
        ))

    if not tasks:
        tasks = [AgentTask(title="Execute goal", description=goal, agent="researcher")]

    log.info("Decomposed '%s' → %d tasks", goal[:60], len(tasks))
    return tasks


# ─── Security gate ─────────────────────────────────────────────────────────────

def _run_security_gate(task: AgentTask) -> bool:
    """
    Run security_reviewer on the task payload.
    Returns True (approved) or False (blocked).
    Mutates task.security_verdict and task.status in place.
    """
    try:
        from agents.security_reviewer import review
        payload = {
            "title":       task.title,
            "description": task.description,
            "agent":       task.agent,
            "context":     task.context,
        }
        verdict = review(payload, task_id=task.task_id or str(uuid.uuid4()))
        task.security_verdict = verdict.verdict

        if verdict.is_blocking():
            task.status = "security_blocked"
            log.warning(
                "Security gate blocked task '%s' (agent=%s): %s",
                task.title, task.agent, verdict.summary,
            )
            return False

        log.info("Security gate passed task '%s' (verdict=%s)", task.title, verdict.verdict)
        return True

    except Exception as exc:
        log.warning("Security gate raised exception for '%s': %s — blocking reviewed task", task.title, exc)
        task.security_verdict = "gate_error"
        task.status = "security_blocked"
        return False


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _task_requires_manager_gate(task: AgentTask) -> bool:
    if task.needs_security_review or task.agent in _ALWAYS_REVIEW:
        return True
    if task.agent == "researcher" and (
        _truthy(task.context.get("allow_cloud_research"))
        or _truthy(task.context.get("cloud_research_approved"))
    ):
        task.needs_security_review = True
        return True
    return False


# ─── Task publishing ───────────────────────────────────────────────────────────

def _event_bus_url() -> str:
    return os.getenv("EVENT_BUS_URL", "http://localhost:8766").rstrip("/")

def _publish_via_event_bus(task: AgentTask) -> str | None:
    """POST to event bus. Returns task_id on success, None on failure."""
    try:
        import httpx
        payload = {
            "title":       task.title,
            "description": task.description,
            "agent":       task.agent,
            "priority":    task.priority,
            "context":     task.context,
        }
        resp = httpx.post(
            f"{_event_bus_url()}/tasks",
            json=payload,
            headers={"X-Jarvis-Agent-ID": "jarvis_manager"},
            timeout=3.0,
        )
        if resp.status_code == 202:
            return resp.json().get("task_id")
    except Exception as exc:
        log.debug("Event bus unreachable: %s", exc)
    return None


def _publish_direct(task: AgentTask) -> str:
    """
    In-process fallback: dispatch synchronously via agent_dispatch.
    Runs the agent generator to completion and stores output in task.context.
    Returns a synthetic task_id.
    """
    from agent_dispatch import dispatch
    task_id = str(uuid.uuid4())
    try:
        chunks = list(dispatch(task.agent, task.description, json.dumps(task.context)))
        task.context["_output"] = "".join(chunks)
        task.status = "done"
    except Exception as exc:
        log.warning("Direct dispatch failed for '%s': %s", task.title, exc)
        task.status = "failed"
        task.context["_error"] = str(exc)
    return task_id


def _publish(task: AgentTask) -> str:
    """Try event bus, fall back to direct dispatch. Returns task_id."""
    task_id = _publish_via_event_bus(task)
    if task_id:
        task.status = "scheduled"
        log.info("Published '%s' → event bus (task_id=%s)", task.title, task_id)
        return task_id

    log.info("Event bus unreachable — dispatching '%s' directly", task.title)
    return _publish_direct(task)


# ─── Manager ──────────────────────────────────────────────────────────────────

class JarvisManager:
    """
    Stateless coordinator. Each call to run() is independent.
    Use the module-level `manager` singleton for convenience.
    """

    def decompose(
        self,
        goal:       str,
        session_id: str = "",
        project_id: str = "",
    ) -> list[AgentTask]:
        """
        Retrieve memory context, then decompose the goal into agent tasks.
        Returns raw tasks (not yet security-gated or scheduled).
        """
        memory_ctx = self._get_memory_context(goal, session_id, project_id)
        return _decompose_via_llm(goal, memory_ctx)

    def run(
        self,
        goal:       str,
        session_id: str = "",
        project_id: str = "",
    ) -> ExecutionPlan:
        """
        Full pipeline: decompose → security gate → schedule.
        Returns an ExecutionPlan; blocked tasks are included with status='security_blocked'.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        tasks = self.decompose(goal, session_id, project_id)

        for task in tasks:
            task.task_id = str(uuid.uuid4())

            if _task_requires_manager_gate(task):
                approved = _run_security_gate(task)
                if not approved:
                    continue   # task.status already set to security_blocked

            task_id = _publish(task)
            task.task_id = task_id

        plan = ExecutionPlan(
            goal=goal,
            session_id=session_id,
            project_id=project_id,
            tasks=tasks,
        )

        # Write goal + plan summary to working memory
        self._record_to_memory(goal, plan, session_id, project_id)

        log.info(
            "ExecutionPlan: goal='%s' total=%d blocked=%d scheduled=%d",
            goal[:60], len(tasks), plan.blocked_count,
            len([t for t in tasks if t.status in ("scheduled", "done")]),
        )
        return plan

    def status(self, plan: ExecutionPlan) -> dict:
        """
        Poll current status for all tasks in the plan.
        Checks event bus if reachable; otherwise returns last known status from plan.
        """
        out: dict[str, str] = {}
        for task in plan.tasks:
            if not task.task_id or task.status in ("security_blocked", "done", "failed"):
                out[task.task_id or task.title] = task.status
                continue
            try:
                import httpx
                resp = httpx.get(
                    f"{_event_bus_url()}/tasks/{task.task_id}/status",
                    timeout=2.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    task.status = _map_event_type(data.get("type", ""))
            except Exception:
                logging.debug("[Manager] silent failure in status", exc_info=True)
            out[task.task_id] = task.status
        return out

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_memory_context(self, goal: str, session_id: str, project_id: str) -> str:
        try:
            from infra.memory import store
            return store.context_for_prompt(
                goal,
                top_k=6,
                session_id=session_id or None,
                project_id=project_id or None,
                max_chars=1500,
            )
        except Exception as exc:
            log.debug("Memory context unavailable: %s", exc)
            return ""

    def _record_to_memory(
        self,
        goal:       str,
        plan:       ExecutionPlan,
        session_id: str,
        project_id: str,
    ) -> None:
        try:
            from infra.memory import store
            summary = (
                f"Goal: {goal}\n"
                f"Tasks: {', '.join(t.title for t in plan.tasks)}\n"
                f"Agents: {', '.join(t.agent for t in plan.tasks)}"
            )
            store.write(
                "working",
                summary,
                agent_id="jarvis_manager",
                session_id=session_id or None,
                project_id=project_id or None,
                key="execution_plan",
                importance=7,
            )
        except Exception as exc:
            log.debug("Memory write failed (non-fatal): %s", exc)


def _map_event_type(event_type: str) -> str:
    return {
        "task.created":   "pending",
        "task.assigned":  "assigned",
        "task.started":   "running",
        "task.completed": "done",
        "task.failed":    "failed",
        "task.cancelled": "cancelled",
    }.get(event_type, "unknown")


# Module-level singleton
manager = JarvisManager()
