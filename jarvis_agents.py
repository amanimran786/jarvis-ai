"""
jarvis_agents.py — Parallel agent dispatcher for Iron Man Jarvis.

Aman says "run agents on X" or "brief me" and Jarvis fans out to multiple
specialist sub-agents concurrently, collects their results, synthesises a
single coherent briefing, and surfaces only what needs Aman's attention.

Architecture
────────────
  dispatch(tasks) → ThreadPoolExecutor fan-out → merge → optional escalation

Sub-agent types
  "calendar"    — today's events, upcoming deadlines
  "tasks"       — pending tasks from vault/task hub
  "vault"       — latest vault / brain notes of interest
  "messages"    — last known message context (intent-only, no content read)
  "news"        — web search summary on a topic
  "code"        — code-status / coder workbench snapshot
  "research"    — targeted web research on a question
  "briefing"    — full Iron Man morning briefing (bundles calendar+tasks+vault)

Each sub-agent returns a dict:
  { "agent": str, "status": "ok"|"error", "result": str, "escalate": bool }

Public API
──────────
  run_briefing()           → str   Morning/status briefing (all agents)
  run_parallel(agents)     → str   Run named agents, merge results
  dispatch_single(agent, context) → dict  One agent call

Router wires:
  "brief me" / "morning briefing" / "what's my status"  → run_briefing()
  "run agents on X" / "parallel X"                       → run_parallel(["research"], context=X)
  "what needs my attention"                               → escalation_summary()
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

# ── Internal imports (graceful degradation if not available) ──────────────────

def _safe_import(name: str, attr: str | None = None):
    try:
        import importlib
        mod = importlib.import_module(name)
        if attr:
            return getattr(mod, attr, None)
        return mod
    except Exception:
        return None


# ── Agent timeout (seconds per agent) ─────────────────────────────────────────
_AGENT_TIMEOUT = 12.0
_MAX_WORKERS   = 5


# ── Escalation rules ──────────────────────────────────────────────────────────
# If an agent marks escalate=True the result will be surfaced separately.
# Conditions that trigger escalation:
_ESCALATION_KEYWORDS = (
    "urgent", "overdue", "blocked", "action required", "attention needed",
    "deadline today", "failed", "error", "unread", "high priority",
)


def _needs_escalation(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _ESCALATION_KEYWORDS)


# ── Sub-agent implementations ─────────────────────────────────────────────────

def _agent_calendar(context: str = "") -> dict:
    """Pull today's calendar events."""
    try:
        gs = _safe_import("google_services")
        if gs and hasattr(gs, "get_todays_events"):
            events = gs.get_todays_events()
            if events:
                result = "Calendar today:\n" + "\n".join(f"  • {e}" for e in events[:8])
            else:
                result = "Calendar: no events today."
        else:
            result = "Calendar: not connected."
        return {"agent": "calendar", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "calendar", "status": "error", "result": f"Calendar error: {e}", "escalate": False}


def _agent_tasks(context: str = "") -> dict:
    """Read pending tasks from the vault task hub."""
    try:
        vault_capture = _safe_import("vault_capture")
        if vault_capture and hasattr(vault_capture, "read_note"):
            note = vault_capture.read_note("90 Task Hub", max_chars=1200)
            if isinstance(note, dict):
                result = note.get("content", "")
            else:
                result = str(note) if note else ""
            if not result.strip():
                result = "Tasks: task hub is empty or unreadable."
            else:
                # Filter to open tasks only
                lines = [l for l in result.splitlines() if "- [ ]" in l]
                if lines:
                    result = "Open tasks:\n" + "\n".join(f"  {l.strip()}" for l in lines[:10])
                else:
                    result = "Tasks: no open tasks found in task hub."
        else:
            result = "Tasks: vault not connected."
        return {"agent": "tasks", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "tasks", "status": "error", "result": f"Tasks error: {e}", "escalate": False}


def _agent_vault(context: str = "") -> dict:
    """Surface recent vault updates and brain context."""
    try:
        import vault
        query = context or "recent updates projects decisions"
        ctx = vault.build_context(query, tool="chat")
        if ctx and ctx.strip():
            result = "Brain context:\n" + ctx[:600]
        else:
            result = "Brain: no recent vault context found."
        return {"agent": "vault", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "vault", "status": "error", "result": f"Vault error: {e}", "escalate": False}


def _agent_code(context: str = "") -> dict:
    """Pull coder workbench status."""
    try:
        cw = _safe_import("coder_workbench")
        if cw and hasattr(cw, "status"):
            status_data = cw.status()
            if isinstance(status_data, dict):
                branch = status_data.get("branch", "unknown")
                changed = status_data.get("changed_files", [])
                result = f"Code status: branch={branch}, {len(changed)} changed files"
                if changed:
                    result += "\n  Changed: " + ", ".join(changed[:5])
            else:
                result = f"Code status: {status_data}"
        else:
            result = "Code: workbench not available."
        return {"agent": "code", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "code", "status": "error", "result": f"Code error: {e}", "escalate": False}


def _agent_research(context: str = "") -> dict:
    """Quick web research on a topic."""
    if not context:
        return {"agent": "research", "status": "ok",
                "result": "Research: no topic provided.", "escalate": False}
    try:
        # Use model_router to do a lightweight web-grounded lookup
        import model_router as mr
        prompt = (
            f"Brief, factual summary (3–5 bullet points) on: {context}\n"
            "Focus on what's new, relevant, or actionable in 2026."
        )
        chunks: list[str] = []
        stream, _ = mr.smart_stream(prompt, tool="research")
        for chunk in stream:
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 800:
                break
        result = "Research — " + context + ":\n" + "".join(chunks)[:800]
        return {"agent": "research", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "research", "status": "error",
                "result": f"Research error: {e}", "escalate": False}


def _agent_week(context: str = "") -> dict:
    """Pull the next 7 days of calendar events."""
    try:
        gs = _safe_import("google_services")
        if gs and hasattr(gs, "get_week_events"):
            events = gs.get_week_events(days=7)
            if events:
                result = "This week's calendar:\n" + "\n".join(f"  • {e}" for e in events[:15])
            else:
                result = "Nothing on the calendar this week."
        else:
            result = "Calendar not connected."
        return {"agent": "week", "status": "ok", "result": result,
                "escalate": _needs_escalation(result)}
    except Exception as e:
        return {"agent": "week", "status": "error", "result": f"Week calendar error: {e}", "escalate": False}


# ── Agent registry ─────────────────────────────────────────────────────────────

_AGENTS: dict[str, Callable[[str], dict]] = {
    "calendar": _agent_calendar,
    "week":     _agent_week,
    "tasks":    _agent_tasks,
    "vault":    _agent_vault,
    "code":     _agent_code,
    "research": _agent_research,
}

_BRIEFING_AGENTS = ["calendar", "tasks", "vault"]
_WEEK_AGENTS     = ["week", "tasks"]


# ── Dispatcher ────────────────────────────────────────────────────────────────

def dispatch_single(agent: str, context: str = "") -> dict:
    """Run one agent synchronously."""
    fn = _AGENTS.get(agent)
    if not fn:
        return {"agent": agent, "status": "error",
                "result": f"Unknown agent: {agent}", "escalate": False}
    return fn(context)


def dispatch_parallel(agents: list[str], context: str = "") -> list[dict]:
    """Run multiple agents concurrently and return all results."""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(agents))) as pool:
        futures = {pool.submit(_AGENTS.get(a, _unknown_agent(a)), context): a for a in agents}
        for future in as_completed(futures, timeout=_AGENT_TIMEOUT * 2):
            try:
                results.append(future.result(timeout=_AGENT_TIMEOUT))
            except Exception as e:
                agent_name = futures[future]
                results.append({"agent": agent_name, "status": "error",
                                 "result": f"{agent_name} timed out: {e}", "escalate": False})
    return results


def _unknown_agent(name: str) -> Callable[[str], dict]:
    def _fn(context: str = "") -> dict:
        return {"agent": name, "status": "error",
                "result": f"No agent registered for '{name}'.", "escalate": False}
    return _fn


# ── Synthesiser ───────────────────────────────────────────────────────────────

def _merge_results(results: list[dict], include_errors: bool = False) -> str:
    """Merge agent results into a clean briefing string."""
    escalations: list[str] = []
    sections: list[str] = []
    errors: list[str] = []

    for r in results:
        if r["status"] == "error":
            if include_errors:
                errors.append(r["result"])
            continue
        text = r.get("result", "").strip()
        if not text:
            continue
        if r.get("escalate"):
            escalations.append(f"⚠️  {text}")
        else:
            sections.append(text)

    lines: list[str] = []
    if escalations:
        lines.append("── Needs your attention ──")
        lines.extend(escalations)
        lines.append("")
    lines.extend(sections)
    if errors:
        lines.append("\n── Agent errors ──")
        lines.extend(errors)
    return "\n\n".join(lines).strip()


# ── LLM synthesis ─────────────────────────────────────────────────────────────

_SYNTH_SYSTEM = (
    "You are Jarvis, Aman's local-first AI runtime. "
    "You speak in a calm, direct, slightly formal tone — think JARVIS from Iron Man, not a chatbot. "
    "Convert the raw agent data below into a concise spoken briefing. "
    "Use natural sentences. Do not use bullet points or markdown. "
    "Prioritise urgent/escalated items first. "
    "Keep the total response under 120 words. "
    "Start directly with the content — no 'Here is your briefing' preamble."
)

_SYNTH_ESCALATION_SYSTEM = (
    "You are Jarvis. Convert the following raw escalation data into 1-3 spoken sentences "
    "that tell Aman exactly what needs his attention right now. "
    "Be direct. No bullet points. Under 60 words."
)


def _synthesise(raw: str, system: str = _SYNTH_SYSTEM) -> str:
    """Run raw agent output through the fastest available local model.

    Falls back to returning the raw merged text if synthesis fails or times out.
    Hard timeout: 8s so briefings never feel sluggish.
    """
    if not raw or not raw.strip():
        return raw
    try:
        import model_router as mr

        result_holder: list[str] = []

        def _run():
            try:
                chunks: list[str] = []
                # Inject synthesis persona via extra_system; raw data is the user turn
                stream, _ = mr.smart_stream(
                    raw,
                    tool="briefing",
                    extra_system=system,
                )
                for chunk in stream:
                    chunks.append(chunk)
                    if sum(len(c) for c in chunks) > 600:
                        break
                result_holder.append("".join(chunks).strip())
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=8.0)
        if result_holder and result_holder[0]:
            return result_holder[0]
    except Exception:
        pass
    return raw


# ── Public API ────────────────────────────────────────────────────────────────

def run_briefing() -> str:
    """Full Iron Man morning briefing: calendar + tasks + brain context."""
    results = dispatch_parallel(_BRIEFING_AGENTS)
    body = _merge_results(results)
    if not body:
        return "All clear. Nothing on the calendar or task list that needs attention."
    synthesised = _synthesise(body)
    return synthesised


def run_parallel(agents: list[str], context: str = "") -> str:
    """Run named agents in parallel and return merged result."""
    if not agents:
        return "No agents specified."
    results = dispatch_parallel(agents, context=context)
    raw = _merge_results(results)
    return _synthesise(raw)


def escalation_summary() -> str:
    """Return only the items that need Aman's attention."""
    results = dispatch_parallel(_BRIEFING_AGENTS)
    escalations = [r for r in results if r.get("escalate") and r["status"] == "ok"]
    if not escalations:
        return "Nothing needs your attention right now."
    raw = "\n".join(r["result"] for r in escalations)
    return _synthesise(raw, system=_SYNTH_ESCALATION_SYSTEM)


def week_ahead() -> str:
    """What's coming up this week — calendar + open tasks, synthesised."""
    results = dispatch_parallel(_WEEK_AGENTS)
    raw = _merge_results(results)
    if not raw:
        return "Nothing scheduled or pending this week."
    return _synthesise(
        raw,
        system=(
            "You are Jarvis. Summarise the week ahead for Aman in 2-4 natural spoken sentences. "
            "Mention the number of events, any deadlines or tasks, and flag anything urgent. "
            "No bullet points. Under 80 words."
        ),
    )


def research_and_brief(topic: str) -> str:
    """Run research agent on a topic and return the briefing."""
    return run_parallel(["research", "vault"], context=topic)
