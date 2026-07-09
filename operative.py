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

import datetime
import logging
import threading
import uuid
from typing import Callable

log = logging.getLogger(__name__)

from brains.brain_claude import ask_claude
from config import DEFAULT_MODE, HAIKU, LOCAL_DEFAULT, VOICE_ENABLED
from harness.tts import speak_step
from harness.audit import set_run_id
from task_planner import TaskStep as Step, plan_task, replan_after_failure
from execution_engine import execute_step
import preflect


# ── Persistence helpers ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _persist_task_start(run_id: str, task: str, steps: "list[Step]") -> None:
    try:
        import task_persistence as _tp
        _tp.upsert_task({
            "id": run_id,
            "status": "running",
            "task": task,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "finished_at": "",
            "steps_total": len(steps),
            "steps_done": 0,
            "plan": [
                {"number": s.number, "description": s.description, "tool": s.tool, "params": s.params}
                for s in steps
            ],
            "result": "",
        })
    except Exception:
        log.debug("task_persistence unavailable — skipping checkpoint", exc_info=True)


def _checkpoint_step(run_id: str, step: Step) -> None:
    try:
        import task_persistence as _tp
        _tp.checkpoint_step(
            run_id=run_id,
            step_number=step.number,
            description=step.description,
            tool=step.tool,
            ok=step.ok,
            result=step.result,
        )
    except Exception:
        log.debug("checkpoint_step failed", exc_info=True)


def _persist_task_finish(run_id: str, task: str, steps: "list[Step]", summary: str, ok: bool) -> None:
    try:
        import task_persistence as _tp
        completed = [s for s in steps if s.ok]
        _tp.upsert_task({
            "id": run_id,
            "status": "succeeded" if ok else "failed",
            "task": task,
            "created_at": "",   # upsert preserves existing value when non-empty
            "updated_at": _now_iso(),
            "finished_at": _now_iso(),
            "steps_total": len(steps),
            "steps_done": len(completed),
            "plan": [],          # already stored at start
            "result": summary[:500],
        })
    except Exception:
        log.debug("persist_task_finish failed", exc_info=True)


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
    set_run_id(run_id)   # thread-local: all audit_log() calls in this thread carry run_id

    _prog("Planning task", task)
    steps = plan_task(task)
    _prog(f"Plan ready — {len(steps)} steps",
          " → ".join(s.description for s in steps))

    if preflect.is_enabled():
        pf = preflect.review_plan(task, steps, task_id=run_id)
        if pf.fired:
            _prog("Plan pre-checked", f"{pf.verdict}: {pf.summary}")

    # Persist task as running so a crash leaves a resumable checkpoint.
    _persist_task_start(run_id, task, steps)

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

        # Checkpoint immediately so a crash after this step is recoverable.
        _checkpoint_step(run_id, step)

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

    task_ok = len(failed) == 0
    _persist_task_finish(run_id, task, steps, summary, task_ok)
    _prog("Task complete", summary[:100])
    set_run_id("")  # clear thread-local so a reused thread doesn't leak run_id

    return {
        "task":    task,
        "steps":   steps,
        "summary": summary,
        "results": step_results,
        "ok":      task_ok,
    }


def resume_task(
    run_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Resume an interrupted task from its last checkpoint.

    Loads the stored plan and completed steps from task_persistence, then
    re-executes only the remaining steps.  Returns the same shape as run_task().
    """
    import task_persistence as _tp

    interrupted = _tp.find_interrupted_tasks()
    match = next((t for t in interrupted if t.get("id") == run_id), None)
    if match is None:
        return {
            "task": run_id,
            "steps": [],
            "summary": f"No interrupted task found with run_id={run_id!r}.",
            "results": {},
            "ok": False,
        }

    task: str = match.get("task", "")
    raw_plan: list = match.get("plan", [])
    step_events: list = match.get("step_events", [])

    # Reconstruct Step objects from stored plan.
    steps = [
        Step(
            number=p["number"],
            description=p["description"],
            tool=p.get("tool", "chat"),
            params=p.get("params", {}),
        )
        for p in raw_plan
    ]

    # Inject already-completed results so downstream $step_N_result refs resolve.
    step_results: dict[int, str] = {}
    done_numbers: set[int] = set()
    for ev in step_events:
        n = ev.get("step_number")
        if n is not None:
            step_results[n] = ev.get("result", "")
            if ev.get("ok"):
                done_numbers.add(n)
            # Mark step objects that are already done.
            for s in steps:
                if s.number == n:
                    s.ok = ev.get("ok", False)
                    s.result = ev.get("result", "")

    def _prog(msg: str, detail: str = "", *, announce_step: Step | None = None) -> None:
        print(f"[Operative/resume] {msg}" + (f": {detail[:100]}" if detail else ""))
        if on_progress:
            on_progress(msg, detail)
        if VOICE_ENABLED and announce_step is not None:
            speak_step(announce_step.number, announce_step.description, ok=announce_step.ok)

    already_done = len(done_numbers)
    _prog(f"Resuming '{task[:60]}'", f"{already_done}/{len(steps)} steps already done")

    # Mark task as running again so a second crash is also recoverable.
    _persist_task_start(run_id, task, steps)

    for step in steps:
        if step.number in done_numbers:
            _prog(f"  ✓ (skip) Step {step.number}: {step.description}")
            continue
        if cancel_event and cancel_event.is_set():
            _prog("Task cancelled during resume")
            break
        _prog(f"Step {step.number}: {step.description}")
        ok, result = execute_step(step, step_results, run_id=run_id)
        step.ok = ok
        step.result = result
        step_results[step.number] = result
        _checkpoint_step(run_id, step)

        preview = result[:120].replace("\n", " ")
        status = "✓" if ok else "✗"
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
                steps.extend(corrective)
                _persist_task_start(run_id, task, steps)

    completed = [s for s in steps if s.ok]
    failed = [s for s in steps if not s.ok]

    summary_prompt = (
        f"Summarize what was accomplished in this resumed task in 2-3 sentences.\n"
        f"Task: {task}\n"
        f"Steps completed: {[s.description for s in completed]}\n"
        f"Final output preview: {step_results.get(max(step_results.keys(), default=0), '')[:500]}"
    )
    summary = _summarize(summary_prompt)

    task_ok = len(failed) == 0
    _persist_task_finish(run_id, task, steps, summary, task_ok)
    _prog("Resume complete", summary[:100])

    return {
        "task": task,
        "steps": steps,
        "summary": summary,
        "results": step_results,
        "ok": task_ok,
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
