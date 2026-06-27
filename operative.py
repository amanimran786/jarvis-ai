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
import uuid
from typing import Callable

from brains.brain_claude import ask_claude
from config import DEFAULT_MODE, HAIKU, LOCAL_DEFAULT, VOICE_ENABLED
from harness.tts import speak_step
from task_planner import TaskStep as Step, plan_task, replan_after_failure
from execution_engine import execute_step
import preflect


def _summarize(prompt: str, system_extra: str = "") -> str:
    """Summarize task results. Uses local model unless in cloud-only mode."""
    if DEFAULT_MODE != "cloud":
        try:
            import ollama as _ollama_lib
            from brains.brain_ollama import get_best_available
            try:
                import httpx
                client = _ollama_lib.Client(
                    timeout=httpx.Timeout(connect=10.0, read=90.0, write=15.0, pool=10.0)
                )
            except ImportError:
                client = _ollama_lib.Client(timeout=90.0)
            model = get_best_available(LOCAL_DEFAULT)
            messages = [{"role": "user", "content": prompt}]
            if system_extra:
                messages = [{"role": "system", "content": system_extra}] + messages
            response = client.chat(model=model, messages=messages, stream=False)
            return (response.message.content or "").strip()
        except Exception:
            pass
        # Graceful local fallback — no model call needed
        return prompt.split("\nTask: ", 1)[-1].split("\n")[0].strip() or "Task complete."
    return ask_claude(prompt, model=HAIKU, system_extra=system_extra or None)


# ── Step definition ───────────────────────────────────────────────────────────


# ── Main entry point ──────────────────────────────────────────────────────────

def run_task(
    task: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
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

    def _prog(msg, detail="", *, announce_step: Step | None = None):
        print(f"[Operative] {msg}" + (f": {detail[:100]}" if detail else ""))
        if on_progress:
            on_progress(msg, detail)
        if VOICE_ENABLED and announce_step is not None:
            speak_step(
                announce_step.number,
                announce_step.description,
                ok=announce_step.ok,
            )

    run_id = f"run_{uuid.uuid4().hex[:12]}"

    _prog("Planning task", task)
    steps = plan_task(task)
    _prog(f"Plan ready — {len(steps)} steps",
          " → ".join(s.description for s in steps))

    if preflect.is_enabled():
        pf = preflect.review_plan(task, steps, task_id=run_id)
        if pf.fired:
            _prog("Plan pre-checked", f"{pf.verdict}: {pf.summary}")

    step_results: dict[int, str] = {}

    for step in steps:
        if cancel_event and cancel_event.is_set():
            _prog("Task cancelled", "stopping before step")
            break
        _prog(f"Step {step.number}: {step.description}")
        ok, result = execute_step(step, step_results, run_id=run_id)
        step.ok     = ok
        step.result = result
        step_results[step.number] = result

        preview = result[:120].replace("\n", " ")
        status  = "✓" if ok else "✗"
        _prog(f"  {status} {step.description}", preview, announce_step=step)

        if not ok:
            _prog(f"Step {step.number} failed — attempting recovery", result)
            corrective = replan_after_failure(
                task,
                completed_steps=[s for s in steps if s.ok],
                failed_step=step,
                error=result,
            )
            if corrective:
                _prog(
                    f"Recovery plan: {len(corrective)} corrective step(s)",
                    " → ".join(s.description for s in corrective),
                )
                steps.extend(corrective)

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
    summary = _summarize(summary_prompt, system_extra=system_extra)

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
    cancel_event: threading.Event | None = None,
) -> threading.Thread:
    """Run task in background. Calls on_complete(result) when done."""
    def _run():
        result = run_task(task, on_progress=on_progress, cancel_event=cancel_event)
        if on_complete:
            on_complete(result)

    t = threading.Thread(target=_run, daemon=True, name="Operative")
    t.start()
    return t
