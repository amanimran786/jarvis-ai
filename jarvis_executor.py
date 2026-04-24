"""
jarvis_executor.py — Multi-step task executor for Iron Man Jarvis.

When Aman says "do X and Y" or "take care of X", Jarvis:
  1. Plans the steps using a local LLM (or rule-based splitter for simple cases)
  2. Executes each step by calling route_stream (reusing all existing routing)
  3. Collects results, skips failed steps gracefully
  4. Synthesises a spoken completion summary

This is the "Jarvis is in control" execution layer — not just answering, but doing.

Architecture
────────────
  parse_steps(goal)     -> list[str]   Plan the steps (LLM or heuristic)
  execute_steps(steps)  -> list[Result] Run each step through route_stream
  run(goal)             -> str          Full plan → execute → summarise pipeline

Step execution reuses route_stream directly, so every existing Jarvis tool
(messaging, calendar, browser, vault, timer, etc.) works automatically.

Each Result:
  { "step": str, "ok": bool, "output": str }
"""

from __future__ import annotations

import re
import threading
from typing import TypedDict


class StepResult(TypedDict):
    step:   str
    ok:     bool
    output: str


# ── Step detection ────────────────────────────────────────────────────────────

# Conjunctions that typically separate distinct action steps
_STEP_SPLITS = re.compile(
    r"\s+(?:and\s+(?:also\s+)?|then\s+(?:also\s+)?|also\s+|after\s+that\s*,?\s*|"
    r"additionally\s+|plus\s+|as\s+well\s+as\s+)",
    re.I,
)

# Minimum words per step — avoids splitting "message dad and mum"
_MIN_STEP_WORDS = 3


def _heuristic_split(goal: str) -> list[str]:
    """Fast rule-based step splitter — no LLM needed for simple compound requests."""
    parts = _STEP_SPLITS.split(goal.strip())
    steps: list[str] = []
    for part in parts:
        part = part.strip().strip(".,;")
        if part and len(part.split()) >= _MIN_STEP_WORDS:
            steps.append(part)
    return steps if len(steps) > 1 else [goal.strip()]


_PLAN_SYSTEM = """\
You are a task planner for Jarvis. Given a multi-step goal, break it into a \
numbered list of specific, atomic action steps Jarvis can execute one at a time. \
Each step must be a complete command Jarvis can understand on its own (e.g. \
"Message dad: I'll be late tonight" not just "message dad"). \
Output ONLY the numbered list, no explanations. Maximum 6 steps. \
If the request is already a single action, output just: 1. <the request>"""


def parse_steps(goal: str) -> list[str]:
    """Split a compound goal into executable steps.

    Uses the heuristic splitter first (fast, no LLM cost). Falls back to
    LLM planning for complex goals with more than 2 potential steps.
    """
    heuristic = _heuristic_split(goal)

    # If heuristic already found clear steps, use them
    if len(heuristic) >= 2:
        return heuristic

    # Single-action goals — check if they sound genuinely multi-step
    multi_indicators = (
        "and also", "then also", "after that", "first", "followed by",
        "as well as", "additionally", "on top of that",
    )
    if not any(ind in goal.lower() for ind in multi_indicators):
        return [goal.strip()]

    # LLM planning path for complex requests
    try:
        import model_router as mr
        chunks: list[str] = []
        stream, _ = mr.smart_stream(goal, tool="planning", extra_system=_PLAN_SYSTEM)
        for chunk in stream:
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 600:
                break
        raw = "".join(chunks).strip()

        steps: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            # Strip leading "1." / "- " / "• " numbering
            line = re.sub(r"^[\d]+[.)]\s*", "", line).strip()
            line = re.sub(r"^[-•]\s*", "", line).strip()
            if line and len(line.split()) >= _MIN_STEP_WORDS:
                steps.append(line)
        if steps:
            return steps[:6]
    except Exception:
        pass

    return [goal.strip()]


# ── Step executor ─────────────────────────────────────────────────────────────

_STEP_TIMEOUT = 20.0   # seconds per step


def execute_step(step: str) -> StepResult:
    """Execute a single step through route_stream. Returns a StepResult."""
    try:
        from router import route_stream
        chunks: list[str] = []
        stream, _model = route_stream(step)
        deadline = threading.Event()

        def _collect():
            try:
                for chunk in stream:
                    chunks.append(chunk)
                    if sum(len(c) for c in chunks) > 800:
                        break
            except Exception:
                pass
            finally:
                deadline.set()

        t = threading.Thread(target=_collect, daemon=True)
        t.start()
        deadline.wait(timeout=_STEP_TIMEOUT)

        output = "".join(chunks).strip()
        if not output:
            return {"step": step, "ok": False, "output": "No response from Jarvis."}
        return {"step": step, "ok": True, "output": output}
    except Exception as e:
        return {"step": step, "ok": False, "output": f"Error: {e}"}


def execute_steps(steps: list[str]) -> list[StepResult]:
    """Execute steps sequentially, collecting results."""
    results: list[StepResult] = []
    for step in steps:
        result = execute_step(step)
        results.append(result)
    return results


# ── Synthesis ─────────────────────────────────────────────────────────────────

_SYNTHESIS_SYSTEM = """\
You are Jarvis. Summarise the results of a multi-step task execution in 2-4 \
spoken sentences. Say what was done, what succeeded, and flag anything that \
failed or needs Aman's attention. Be direct. No bullet points. Under 80 words."""


def synthesise_results(goal: str, results: list[StepResult]) -> str:
    """Run results through LLM to produce a clean spoken completion summary."""
    if not results:
        return "No steps were executed."

    # Build a compact raw summary for the LLM
    lines = [f"Original goal: {goal}", "Results:"]
    for i, r in enumerate(results, 1):
        status = "✓" if r["ok"] else "✗"
        lines.append(f"  {i}. [{status}] {r['step']}: {r['output'][:200]}")
    raw = "\n".join(lines)

    try:
        import model_router as mr
        chunks: list[str] = []
        stream, _ = mr.smart_stream(raw, tool="synthesis", extra_system=_SYNTHESIS_SYSTEM)
        for chunk in stream:
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 500:
                break
        text = "".join(chunks).strip()
        if text:
            return text
    except Exception:
        pass

    # Fallback: simple concatenation
    ok_steps  = [r for r in results if r["ok"]]
    bad_steps = [r for r in results if not r["ok"]]
    parts: list[str] = []
    if ok_steps:
        parts.append(f"Done: {', '.join(r['step'][:40] for r in ok_steps)}.")
    if bad_steps:
        parts.append(f"Failed: {', '.join(r['step'][:40] for r in bad_steps)}.")
    return " ".join(parts) or "Task complete."


# ── Public API ────────────────────────────────────────────────────────────────

def run(goal: str) -> str:
    """Full pipeline: plan → execute → synthesise. Returns spoken summary."""
    if not goal or not goal.strip():
        return "No goal provided."

    steps = parse_steps(goal)
    results = execute_steps(steps)
    return synthesise_results(goal, results)


def run_async(goal: str, callback=None) -> threading.Thread:
    """Run the executor in a background thread. Calls callback(result) when done."""
    def _go():
        result = run(goal)
        if callback:
            try:
                callback(result)
            except Exception:
                pass

    t = threading.Thread(target=_go, daemon=True, name="jarvis-executor")
    t.start()
    return t


# ── Multi-step detection helper (used by router) ──────────────────────────────

_EXECUTOR_TRIGGER_WORDS = re.compile(
    r"\b(do\s+(?:this|that|it\s+for\s+me|the\s+following)|"
    r"take\s+care\s+of|handle\s+(?:this|that)|"
    r"execute|carry\s+out|go\s+ahead\s+and|"
    r"can\s+you\s+(?:do|handle|take\s+care))\b",
    re.I,
)

_COMPOUND_CONJUNCTIONS = re.compile(
    r"\b(and\s+also|and\s+then|after\s+that|first\s+.+\s+then|as\s+well\s+as)\b",
    re.I,
)


def is_multi_step(text: str) -> bool:
    """Return True if text looks like a compound multi-step request."""
    return bool(_COMPOUND_CONJUNCTIONS.search(text))
