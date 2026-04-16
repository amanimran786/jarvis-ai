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

import threading
from typing import Callable

from brains.brain_claude import ask_claude
from config import HAIKU
from task_planner import TaskStep as Step, plan_task
from execution_engine import execute_step


# ── Step definition ───────────────────────────────────────────────────────────


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
    steps = plan_task(task)
    _prog(f"Plan ready — {len(steps)} steps",
          " → ".join(s.description for s in steps))

    step_results: dict[int, str] = {}

    for step in steps:
        _prog(f"Step {step.number}: {step.description}")
        ok, result = execute_step(step, step_results)
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
    system_extra = ""
    technical = False
    try:
        import model_router
        technical = model_router._is_engineering_companion_query(task, "chat")
        if technical:
            system_extra = model_router._engineering_companion_grounding(task)
    except Exception:
        system_extra = ""
    if technical:
        summary_prompt = (
            "Summarize what was accomplished in this task in 2-3 spoken sentences. "
            "Lead with the conclusion or fix first. "
            "Then name the key tradeoff, root cause, or next verification step.\n"
            f"Task: {task}\n"
            f"Steps completed: {[s.description for s in completed]}\n"
            f"Final output preview: {step_results.get(len(steps), '')[:500]}"
        )
    summary = ask_claude(summary_prompt, model=HAIKU, system_extra=system_extra or None)

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
