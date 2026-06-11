#!/usr/bin/env python3
"""
Live multi-agent run — submits a project goal to the Jarvis Manager and
streams real LLM output from each assigned agent.

Runs in-process: no HTTP server required, no Ollama contention from voice threads.

Usage:
    python scripts/live_agent_run.py
    python scripts/live_agent_run.py --goal "Your project description"
    python scripts/live_agent_run.py --agent backend_engineer --task "Write /briefing"
    python scripts/live_agent_run.py --decompose-only     # show plan without running
    python scripts/live_agent_run.py --http               # use HTTP API (requires server)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load .env before any imports that read config
try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

DEFAULT_GOAL = (
    "Build the Jarvis Daily Briefing Endpoint: a GET /briefing API endpoint that "
    "aggregates today's calendar events, unread email count, pending task count, "
    "and a short LLM-generated summary. "
    "Assign: backend implementation, frontend panel, UX review, security audit, "
    "deployment readiness."
)

# ANSI colors per agent
_COLORS: dict[str, str] = {
    "backend_engineer":   "\033[36m",   # cyan
    "frontend_designer":  "\033[35m",   # magenta
    "ux_researcher":      "\033[33m",   # yellow
    "security_reviewer":  "\033[31m",   # red
    "qa_tester":          "\033[32m",   # green
    "devops_release":     "\033[34m",   # blue
    "researcher":         "\033[37m",   # white
    "memory_librarian":   "\033[90m",   # dark gray
    "ai_safety_agent":    "\033[91m",   # bright red
    "automation_engineer":"\033[92m",   # bright green
    "career_agent":       "\033[93m",   # bright yellow
    "security_analyst":   "\033[94m",   # bright blue
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"


def _c(agent: str) -> str:
    return _COLORS.get(agent, "\033[97m")


def _hdr(agent: str, title: str) -> None:
    c = _c(agent)
    bar = "─" * 70
    print(f"\n{c}{_BOLD}{bar}{_RESET}")
    print(f"{c}{_BOLD}  {agent.upper():28s} {title}{_RESET}")
    print(f"{c}{bar}{_RESET}")


def _info(msg: str)  -> None: print(f"{_DIM}  {msg}{_RESET}")
def _ok(msg: str)    -> None: print(f"\033[32m✓ {msg}{_RESET}")
def _warn(msg: str)  -> None: print(f"\033[33m⚠ {msg}{_RESET}")
def _err(msg: str)   -> None: print(f"\033[31m✗ {msg}{_RESET}", file=sys.stderr)


# ─── In-process execution ─────────────────────────────────────────────────────

def run_decompose(goal: str) -> list:
    """Ask the manager to decompose goal → list of AgentTask objects."""
    from core.manager import manager as _manager
    _info("Manager decomposing goal via local LLM...")
    start = time.time()
    tasks = _manager.decompose(goal)
    elapsed = time.time() - start
    _ok(f"Decomposed into {len(tasks)} task(s) in {elapsed:.1f}s")
    return tasks


def print_plan(tasks: list) -> None:
    print(f"\n{_BOLD}Execution Plan{_RESET}")
    print(f"{'─'*70}")
    for i, t in enumerate(tasks, 1):
        sec = "[security]" if t.needs_security_review else ""
        print(
            f"  {i:>2}. {_BOLD}{t.title[:50]:<50}{_RESET}  "
            f"agent={_c(t.agent)}{t.agent}{_RESET}  "
            f"pri={t.priority}  {_c('security_reviewer')}{sec}{_RESET}"
        )
    print()


def run_security_gate(tasks: list) -> tuple[list, list]:
    """Run security gate on tasks that require it. Returns (approved, blocked)."""
    from core.manager import _task_requires_manager_gate, _run_security_gate
    import uuid
    approved, blocked = [], []
    for t in tasks:
        if _task_requires_manager_gate(t):
            if not t.task_id:
                t.task_id = str(uuid.uuid4())
            ok = _run_security_gate(t)
            if ok:
                approved.append(t)
            else:
                blocked.append(t)
                _warn(f"Security blocked: {t.title} ({t.agent}) — {t.security_verdict}")
        else:
            approved.append(t)
    return approved, blocked


def stream_agent(task, *, allow_cloud_fallback: bool = False) -> None:
    """
    Run an agent dispatch and stream its LLM output to stdout.
    Cloud fallback is disabled unless explicitly requested by the operator.
    """
    import agent_dispatch
    from config import AGENT_ROSTER
    c = _c(task.agent)
    context = json.dumps(task.context) if task.context else ""
    output_chars = 0
    try:
        for chunk in agent_dispatch.dispatch(task.agent, task.description, context):
            if chunk:
                print(f"{c}{chunk}{_RESET}", end="", flush=True)
                output_chars += len(chunk)
    except Exception as exc:
        _err(f"\n  Agent {task.agent} local error: {exc}")

    if not output_chars and allow_cloud_fallback:
        _warn(f"\n  Local LLM returned empty — falling back to cloud for {task.agent}")
        _cloud_stream_agent(task)
        return
    if not output_chars:
        _warn(
            f"\n  Local LLM returned empty for {task.agent}. "
            "Cloud agent fallback is disabled; rerun with --cloud-agents to opt in."
        )
        return

    print()


def _cloud_stream_agent(task) -> None:
    """Explicitly requested cloud fallback for agent tasks when local LLM returns empty."""
    from config import AGENT_ROSTER
    c = _c(task.agent)
    spec = AGENT_ROSTER.get(task.agent, {})
    system = spec.get("system_prompt") or (
        f"You are the {task.agent} agent. Role: {spec.get('role', '')} "
        f"Available tools: {', '.join(spec.get('tools', []))}. "
        "Focus exclusively on the assigned task. Be precise and actionable."
    )

    for provider, model in _CLOUD_PROVIDERS:
        try:
            _info(f"  Trying {provider}/{model}...")
            if provider == "openai":
                raw = _call_openai(system, task.description, model)
            elif provider == "gemini":
                raw = _call_gemini(system, task.description, model)
            else:
                raw = _call_anthropic(system, task.description, model)
            if raw:
                print(f"{c}{raw}{_RESET}")
                print()
                return
        except Exception as exc:
            _warn(f"  {provider}/{model} failed: {str(exc)[:80]}")

    _err(f"  All providers failed for {task.agent}")
    print()


# ─── Cloud-accelerated decomposition ─────────────────────────────────────────

# Priority order: GPT-4o-mini (fast + structured JSON) → Gemini 2.5 Flash Lite (free tier) → Anthropic Haiku
_CLOUD_PROVIDERS = [
    ("openai",    "gpt-4o-mini"),
    ("gemini",    "gemini-2.5-flash-lite"),
    ("anthropic", "claude-haiku-4-5-20251001"),
]


def _build_planner_prompt() -> tuple[str, str]:
    from core.manager import _agent_roster_summary
    roster = _agent_roster_summary()
    system = f"""You are the Jarvis Manager. Decompose a broad goal into the minimum set of \
independent, parallelizable tasks for specialist agents.

Available agents:
{roster}

Rules:
- Produce 1-6 tasks. Fewer is better.
- Set needs_security_review=true for tasks involving shell execution, file writes, deployments, \
external API calls, or git operations. devops_release always gets security review.
- Assign each task to the agent whose tools best match.
- description must be self-contained and actionable.

Output ONLY this JSON object — no markdown, no prose:
{{
  "goal_summary": "<one sentence>",
  "tasks": [
    {{
      "title": "<short title>",
      "description": "<detailed actionable description>",
      "agent": "<agent name from list above>",
      "priority": <1-10>,
      "needs_security_review": <true|false>,
      "context": {{}}
    }}
  ]
}}"""
    return system, "Decompose this goal:\n\n{goal}"


def _call_anthropic(system: str, user: str, model: str) -> str:
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=1024, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


def _call_openai(system: str, user: str, model: str, json_mode: bool = False) -> str:
    import openai
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = openai.OpenAI(api_key=key)
    kwargs: dict = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    r = client.chat.completions.create(
        model=model, max_tokens=2048,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        **kwargs,
    )
    return (r.choices[0].message.content or "").strip()


def _call_gemini(system: str, user: str, model: str, json_mode: bool = False) -> str:
    from google import genai
    from google.genai import types
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=2048,
        **({"response_mime_type": "application/json"} if json_mode else {}),
    )
    r = client.models.generate_content(model=model, contents=user, config=cfg)
    return (r.text or "").strip()


def cloud_decompose(goal: str) -> list:
    """
    Decompose goal via cloud LLM (~3s vs ~90s local).
    Tries Anthropic Haiku first, falls back to OpenAI gpt-4o-mini, then local.
    """
    import re
    from core.manager import AgentTask
    from config import AGENT_ROSTER

    system_tmpl, user_tmpl = _build_planner_prompt()
    system = system_tmpl
    user   = user_tmpl.format(goal=goal)
    valid_agents = set(AGENT_ROSTER.keys())

    raw = ""
    used_model = ""
    for provider, model in _CLOUD_PROVIDERS:
        _info(f"Decomposing via {model}...")
        start = time.time()
        try:
            if provider == "anthropic":
                raw = _call_anthropic(system, user, model)
            elif provider == "gemini":
                raw = _call_gemini(system, user, model, json_mode=True)
            else:
                raw = _call_openai(system, user, model, json_mode=True)
            elapsed = time.time() - start
            used_model = f"{model} ({elapsed:.1f}s)"
            break
        except Exception as exc:
            _warn(f"  {provider}/{model} failed: {str(exc)[:80]} — trying next")

    if not raw:
        _warn("All cloud providers failed — falling back to local LLM")
        return run_decompose(goal)

    raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"Cloud planner JSON parse failed ({exc}) — falling back to local LLM")
        return run_decompose(goal)

    tasks = []
    for item in data.get("tasks", []):
        agent = item.get("agent", "researcher")
        if agent not in valid_agents:
            _warn(f"  Remapping unknown agent '{agent}' → researcher")
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
        _warn("Cloud planner returned no tasks — falling back to local LLM")
        return run_decompose(goal)

    _ok(f"Decomposed into {len(tasks)} task(s) via {used_model}")
    return tasks


# ─── HTTP execution (optional) ────────────────────────────────────────────────

def run_via_http(goal: str, decompose_only: bool = False) -> None:
    import httpx
    base    = "http://127.0.0.1:8765"
    token   = os.getenv("JARVIS_API_TOKEN", "")
    headers = {"X-Jarvis-Token": token, "Content-Type": "application/json"}

    # Check server
    try:
        httpx.get(f"{base}/status", timeout=2.0).raise_for_status()
    except Exception:
        _err("API server not reachable. Start it or run without --http.")
        sys.exit(1)

    # Decompose
    _info("Submitting to /manager/run...")
    r = httpx.post(f"{base}/manager/run", json={"goal": goal, "project_id": "live-run"}, headers=headers, timeout=60.0)
    r.raise_for_status()
    plan  = r.json()["plan"]
    tasks = plan["tasks"]

    print(f"\n{_BOLD}Execution Plan ({len(tasks)} task(s)){_RESET}")
    for i, t in enumerate(tasks, 1):
        sc = "\033[32m" if t["status"] == "scheduled" else "\033[31m"
        print(f"  {i:>2}. {_BOLD}{t['title'][:50]:<50}{_RESET}  agent={_c(t['agent'])}{t['agent']}{_RESET}  {sc}{t['status']}{_RESET}")

    if decompose_only:
        return

    runnable = [t for t in tasks if t["status"] != "security_blocked"]
    print(f"\n{_BOLD}Streaming {len(runnable)} agent(s) via SSE...{_RESET}\n")

    for t in runnable:
        agent = t["agent"]
        _hdr(agent, t["title"])
        try:
            with httpx.stream(
                "POST", f"{base}/agents/{agent}/run",
                json={"task": t.get("description", t["title"]), "context": ""},
                headers=headers, timeout=120.0,
            ) as resp:
                if resp.status_code != 200:
                    _err(f"  HTTP {resp.status_code}")
                    continue
                c = _c(agent)
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        p = line[6:]
                        if p == "[DONE]":
                            break
                        try:
                            chunk = json.loads(p).get("chunk", "")
                            if chunk:
                                print(f"{c}{chunk}{_RESET}", end="", flush=True)
                        except json.JSONDecodeError:
                            pass
                print()
        except Exception as exc:
            _err(f"  {exc}")

    print(f"\n{_BOLD}Done.{_RESET}")
    print(f"{_DIM}Dashboard: http://127.0.0.1:8765/dashboard{_RESET}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Live Jarvis multi-agent run")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--goal",   default="", help="Project goal (full manager flow)")
    g.add_argument("--agent",  default="", help="Run single agent directly")
    parser.add_argument("--task",           default="", help="Task for --agent mode")
    parser.add_argument("--context",        default="", help="Context for --agent mode")
    parser.add_argument("--decompose-only", action="store_true", help="Show plan without running agents")
    parser.add_argument("--http",           action="store_true", help="Use HTTP API instead of in-process")
    parser.add_argument("--cloud-plan",     action="store_true", help="Use explicit cloud planner for manager decomposition (~3s vs ~90s local)")
    parser.add_argument("--cloud-agents",   action="store_true", help="Allow cloud fallback for agent execution when local output is empty")
    args = parser.parse_args()

    goal = args.goal or DEFAULT_GOAL

    planner_label = "GPT-4o-mini → Gemini 2.5 Flash Lite → Claude Haiku (cloud)" if args.cloud_plan else f"{os.getenv('LOCAL_DEFAULT_MODEL', 'glm-4.7-flash')} (Ollama)"
    agent_label = "Ollama + explicit cloud fallback" if args.cloud_agents else "Ollama only"
    print(f"\n{_BOLD}Jarvis Live Agent Run{_RESET}")
    print(f"{_DIM}Planner: {planner_label}{_RESET}")
    print(f"{_DIM}Agents:  {os.getenv('LOCAL_DEFAULT_MODEL', 'glm-4.7-flash')} ({agent_label}){_RESET}")
    print(f"{_DIM}Goal:    {goal[:80]}...{_RESET}\n")

    if args.http:
        run_via_http(goal, decompose_only=args.decompose_only)
        return

    # ── In-process path ──────────────────────────────────────────────────────

    if args.agent:
        # Direct single-agent run
        from core.manager import AgentTask
        task = AgentTask(
            title=args.task or f"Demo run for {args.agent}",
            description=args.task or f"Demonstrate your capabilities as the {args.agent}.",
            agent=args.agent,
            context={"context": args.context} if args.context else {},
        )
        _hdr(args.agent, task.title)
        stream_agent(task, allow_cloud_fallback=args.cloud_agents)
        return

    # Full manager flow
    try:
        tasks = cloud_decompose(goal) if args.cloud_plan else run_decompose(goal)
    except Exception as exc:
        _err(f"Manager decompose failed: {exc}")
        sys.exit(1)

    print_plan(tasks)

    if args.decompose_only:
        return

    # Security gate
    approved, blocked = run_security_gate(tasks)
    if blocked:
        print(f"{_BOLD}Security-blocked ({len(blocked)}):{_RESET}")
        for t in blocked:
            _warn(f"  {t.title} ({t.agent}) — {t.security_verdict}")
        print()

    if not approved:
        _warn("All tasks blocked by security gate.")
        return

    print(f"{_BOLD}Running {len(approved)} approved task(s) with live Ollama LLM...{_RESET}\n")

    for t in approved:
        _hdr(t.agent, t.title)
        _info(f"Description: {t.description[:100]}")
        print()
        start = time.time()
        stream_agent(t, allow_cloud_fallback=args.cloud_agents)
        elapsed = time.time() - start
        print(f"\n{_DIM}  [{t.agent} finished in {elapsed:.1f}s]{_RESET}")

    print(f"\n{'─'*70}")
    print(f"{_BOLD}All agents complete.{_RESET}")
    print(f"{_DIM}Start the dashboard:  python main.py --no-ui && open http://127.0.0.1:8765/dashboard{_RESET}\n")


if __name__ == "__main__":
    main()
