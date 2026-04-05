"""
Jarvis Operative Agent — autonomous multi-step task execution.

The operative takes a high-level goal, breaks it into steps using Claude,
executes each step using Jarvis's tool suite, and reports progress.

Example tasks:
  "Research the best Python async frameworks, write a report, and save it"
  "Check my emails, summarize the urgent ones, and create a to-do note"
  "Search for news about AI safety, write a briefing, and email it to me"
  "Find top 5 Python repos for ML, save the list, and open VS Code"

Usage:
  from operative import run_task
  result = run_task("research X and save a report", on_progress=callback)
"""

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

from brain_claude import ask_claude
from config import SONNET, HAIKU


# ── Step definition ───────────────────────────────────────────────────────────

@dataclass
class Step:
    number:      int
    description: str
    tool:        str
    params:      dict = field(default_factory=dict)
    result:      str  = ""
    ok:          bool = False


# ── Task planning ─────────────────────────────────────────────────────────────

_PLAN_SYSTEM = """You are Jarvis's task planner. Break a user's goal into sequential steps.

Available tools and what they do:
  research  — deep web research, returns a written report with sources
  search    — quick web search, returns short snippets
  notes     — save text content to notes
  email     — read emails or send an email
  calendar  — read or create calendar events
  terminal  — run a shell command
  file      — read or write a file
  weather   — get current weather
  chat      — generate text, write content, answer questions

Return ONLY a valid JSON array of steps. Each step:
{
  "number": 1,
  "description": "what this step does",
  "tool": "<tool name>",
  "params": {"key": "value"}
}

Rules:
- Use the minimum steps needed
- Pass outputs from one step to the next via params where needed (use placeholder: "$step_N_result")
- Maximum 6 steps
- If unclear, ask via a single "chat" step"""

_PLAN_USER = "Plan this task: {task}"


def _plan_steps(task: str) -> list[Step]:
    """Use Sonnet to break the task into executable steps."""
    try:
        raw = ask_claude(
            _PLAN_USER.format(task=task),
            model=SONNET,
            system=_PLAN_SYSTEM,
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = raw[:-3]

        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON array in response")

        data = json.loads(match.group())
        return [
            Step(
                number=int(s.get("number", i + 1)),
                description=str(s.get("description", "")),
                tool=str(s.get("tool", "chat")),
                params=dict(s.get("params", {})),
            )
            for i, s in enumerate(data)
        ]
    except Exception as e:
        print(f"[Operative] Planning failed: {e}")
        # Fallback: single chat step
        return [Step(1, f"Execute: {task}", "chat", {"prompt": task})]


# ── Step execution ────────────────────────────────────────────────────────────

def _resolve_params(params: dict, step_results: dict) -> dict:
    """Replace $step_N_result placeholders with actual outputs."""
    resolved = {}
    for k, v in params.items():
        if isinstance(v, str) and v.startswith("$step_"):
            m = re.match(r"\$step_(\d+)_result", v)
            if m:
                n = int(m.group(1))
                v = step_results.get(n, "")
        resolved[k] = v
    return resolved


def _execute_step(step: Step, step_results: dict) -> tuple[bool, str]:
    """Execute a single step. Returns (ok, result_text)."""
    params = _resolve_params(step.params, step_results)
    tool = step.tool.lower()

    try:
        # ── Research ──────────────────────────────────────────────────────
        if tool == "research":
            from research import deep_research
            query = params.get("query", params.get("topic", step.description))
            r = deep_research(query, depth=2)
            return True, r["report"]

        # ── Quick search ──────────────────────────────────────────────────
        elif tool == "search":
            from tools import web_search
            query = params.get("query", step.description)
            return True, web_search(query, max_results=5)

        # ── Notes ─────────────────────────────────────────────────────────
        elif tool == "notes":
            import notes as notes_mod
            content = params.get("content", params.get("text", step_results.get(
                max(step_results.keys(), default=0), ""
            )))
            title = params.get("title", "Jarvis Note")
            result = notes_mod.add_note(f"# {title}\n\n{content}")
            return True, result

        # ── File write ────────────────────────────────────────────────────
        elif tool == "file":
            import terminal
            action = params.get("action", "write")
            path = params.get("path", "~/Desktop/jarvis_output.md")
            if action == "write":
                content = params.get("content",
                    step_results.get(max(step_results.keys(), default=0), ""))
                return True, terminal.write_file(path, content)
            else:
                return True, terminal.read_file(path)

        # ── Email ─────────────────────────────────────────────────────────
        elif tool == "email":
            import google_services as gs
            action = params.get("action", "read")
            if action == "read":
                return True, gs.get_unread_emails(max_results=5)
            elif action == "send":
                to      = params.get("to", "")
                subject = params.get("subject", "Jarvis Report")
                body    = params.get("body",
                    step_results.get(max(step_results.keys(), default=0), ""))
                if not to:
                    return False, "No recipient specified."
                return True, gs.send_email(to, subject, body)

        # ── Calendar ──────────────────────────────────────────────────────
        elif tool == "calendar":
            import google_services as gs
            return True, gs.get_todays_events()

        # ── Terminal ──────────────────────────────────────────────────────
        elif tool == "terminal":
            import terminal
            cmd = params.get("command", params.get("cmd", ""))
            if not cmd:
                return False, "No command specified."
            return True, terminal.run_command(cmd)

        # ── Weather ───────────────────────────────────────────────────────
        elif tool == "weather":
            from tools import get_weather
            return True, get_weather()

        # ── Chat / generate content ───────────────────────────────────────
        elif tool == "chat":
            prompt = params.get("prompt", params.get("content", step.description))
            # Inject previous step context
            if step_results:
                last = step_results.get(max(step_results.keys()))
                if last and "$" not in prompt:
                    prompt = f"Context from previous step:\n{last[:1500]}\n\nTask: {prompt}"
            result = ask_claude(prompt, model=SONNET)
            return True, result

        else:
            return False, f"Unknown tool: {tool}"

    except Exception as e:
        return False, f"Step failed: {e}"


# ── Main entry point ──────────────────────────────────────────────────────────

def run_task(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
) -> dict:
    """
    Execute a multi-step task autonomously.

    Args:
        task:        Natural language description of what to do
        on_progress: Optional callback(step_description, result_preview)

    Returns:
        {
          "task":    original task,
          "steps":   list of Step objects,
          "summary": final summary,
          "ok":      bool,
        }
    """

    def _prog(msg, detail=""):
        print(f"[Operative] {msg}" + (f": {detail[:100]}" if detail else ""))
        if on_progress:
            on_progress(msg, detail)

    _prog("Planning task", task)
    steps = _plan_steps(task)
    _prog(f"Plan ready — {len(steps)} steps",
          " → ".join(s.description for s in steps))

    step_results: dict[int, str] = {}

    for step in steps:
        _prog(f"Step {step.number}: {step.description}")
        ok, result = _execute_step(step, step_results)
        step.ok     = ok
        step.result = result
        step_results[step.number] = result

        preview = result[:120].replace("\n", " ")
        status  = "✓" if ok else "✗"
        _prog(f"  {status} {step.description}", preview)

        if not ok:
            _prog(f"Step {step.number} failed — continuing", result)

    # Final summary
    completed = [s for s in steps if s.ok]
    failed    = [s for s in steps if not s.ok]

    summary_prompt = (
        f"Summarize what was accomplished in this task in 2-3 spoken sentences.\n"
        f"Task: {task}\n"
        f"Steps completed: {[s.description for s in completed]}\n"
        f"Final output preview: {step_results.get(len(steps), '')[:500]}"
    )
    summary = ask_claude(summary_prompt, model=HAIKU)

    _prog("Task complete", summary[:100])

    return {
        "task":    task,
        "steps":   steps,
        "summary": summary,
        "results": step_results,
        "ok":      len(failed) == 0,
    }


def run_task_async(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    on_complete: Callable[[dict], None] | None = None,
) -> threading.Thread:
    """Run task in background. Calls on_complete(result) when done."""
    def _run():
        result = run_task(task, on_progress=on_progress)
        if on_complete:
            on_complete(result)

    t = threading.Thread(target=_run, daemon=True, name="Operative")
    t.start()
    return t
