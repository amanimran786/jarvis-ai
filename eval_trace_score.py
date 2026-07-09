"""Deterministic, local-first trace scorer for Jarvis agent runs (R1 / gap G1).

Reads the per-step JSON traces written by ``execution_engine._trace_step()`` and
computes step-level agentic-eval metrics — Tool Correctness, Step Efficiency,
Plan Adherence — with **no model call**. This is the local-first baseline that
honors ``DEFAULT_MODE="open-source"``; an LLM-jury upgrade can read the same
artifacts later without changing this surface.

Per B's 19:40 ratchet call this is a standalone, observe-only ``--trace-score``
report. It is intentionally NOT wired into the release ratchet (eval_delta /
golden cases) until a score distribution exists across >=50 runs.

Trace field names (run_id, step_number, tool, ok, attempts, normalized_params,
elapsed_ms) are kept stable so the same traces can be mapped onto OpenInference
spans for optional Phoenix/DeepEval ingest.

CLI:
    python3 -m eval_trace_score --list
    python3 -m eval_trace_score <run_id> [--expected tool_a,tool_b] [--min-steps N]
"""
from __future__ import annotations

import json
from typing import Any

import runtime_state

TRACE_DIR = runtime_state.writable_data_path("training", "execution_traces")

# Expected-trace targets per capability_evals case id. Kept here (not in
# capability_evals.py) to stay new-file-only during the release freeze; promote
# into the case dicts post-freeze once the metric is calibrated.
EXPECTED_BY_CASE: dict[str, dict[str, Any]] = {
    # Illustrative seeds — fill from real runs once run_id traces accumulate.
    # "id": {"expected_tools": ["search", "file"], "min_steps": 2},
}


# ── IO ────────────────────────────────────────────────────────────────────────

def _iter_trace_files():
    if not TRACE_DIR.exists():
        return []
    return sorted(TRACE_DIR.glob("*.json"))


def load_run(run_id: str) -> list[dict[str, Any]]:
    """Return all step traces for ``run_id``, ordered by (step_number, timestamp)."""
    run: list[dict[str, Any]] = []
    for path in _iter_trace_files():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("run_id") == run_id:
            run.append(rec)
    run.sort(key=lambda r: (int(r.get("step_number", 0)), str(r.get("timestamp", ""))))
    return run


def list_runs() -> dict[str, int]:
    """Map run_id -> step count across all on-disk traces (skips legacy, unkeyed)."""
    counts: dict[str, int] = {}
    for path in _iter_trace_files():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rid = rec.get("run_id") if isinstance(rec, dict) else None
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


# ── Pure metric functions (operate on an in-memory run) ─────────────────────────

def _executed_tools(run: list[dict[str, Any]]) -> list[str]:
    return [str(t.get("tool", "")) for t in run]


def tool_correctness(run: list[dict[str, Any]], expected_tools: list[str], strict: bool = False) -> dict[str, Any]:
    """Jaccard of expected vs executed tool sets; ``strict`` also checks subsequence order.

    Mirrors DeepEval's Tool Correctness: were the expected tools called, and were
    extra (non-optimal) tools avoided. Self-explaining (returns ``reason``).
    """
    executed = _executed_tools(run)
    exp_set, exec_set = set(expected_tools), set(executed)
    if not exp_set:
        return {"score": None, "reason": "no expected_tools provided"}
    inter = exp_set & exec_set
    union = exp_set | exec_set
    score = len(inter) / len(union) if union else 1.0
    missing = sorted(exp_set - exec_set)
    extra = sorted(exec_set - exp_set)
    order_ok = _is_subsequence(expected_tools, executed)
    if strict and not order_ok:
        score *= 0.5
    reason = (
        f"matched {sorted(inter)}; missing {missing}; extra {extra}; "
        f"order_ok={order_ok}"
    )
    return {"score": round(score, 3), "missing": missing, "extra": extra,
            "order_ok": order_ok, "reason": reason}


def step_efficiency(run: list[dict[str, Any]], min_steps: int | None = None) -> dict[str, Any]:
    """Penalize failed steps, retries, and overshoot beyond ``min_steps``."""
    total = len(run)
    if total == 0:
        return {"score": None, "reason": "empty run"}
    failed = sum(1 for t in run if not t.get("ok", False))
    retries = sum(max(0, int(t.get("attempts", 1)) - 1) for t in run)
    overshoot = max(0, total - min_steps) if isinstance(min_steps, int) else 0
    wasted = failed + retries + overshoot
    score = max(0.0, 1.0 - wasted / total)
    reason = (
        f"{total} steps; failed={failed}; retries={retries}; "
        f"overshoot={overshoot} (min_steps={min_steps})"
    )
    return {"score": round(score, 3), "total_steps": total, "failed": failed,
            "retries": retries, "overshoot": overshoot, "reason": reason}


def plan_adherence(run: list[dict[str, Any]], planned_tools: list[str]) -> dict[str, Any]:
    """Order-aware alignment of executed vs planned tool sequence (LCS ratio)."""
    executed = _executed_tools(run)
    if not planned_tools:
        return {"score": None, "reason": "no planned_tools provided"}
    lcs = _lcs_len(planned_tools, executed)
    denom = len(planned_tools) + len(executed)
    score = (2 * lcs / denom) if denom else 1.0
    reason = f"planned={planned_tools}; executed={executed}; lcs={lcs}"
    return {"score": round(score, 3), "lcs": lcs, "reason": reason}


def latency(run: list[dict[str, Any]]) -> dict[str, Any]:
    total_ms = sum(int(t.get("elapsed_ms", 0)) for t in run)
    return {"total_ms": total_ms, "steps": len(run)}


def score_run(run_id: str, expected_tools: list[str] | None = None,
              min_steps: int | None = None, run: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Full scorecard for one run. ``run`` may be passed directly (tests); else loaded by id."""
    run = load_run(run_id) if run is None else run
    expected_tools = expected_tools or []
    return {
        "run_id": run_id,
        "steps": len(run),
        "tool_correctness": tool_correctness(run, expected_tools),
        "step_efficiency": step_efficiency(run, min_steps),
        "plan_adherence": plan_adherence(run, expected_tools),
        "latency": latency(run),
    }


# ── helpers ─────────────────────────────────────────────────────────────────────

def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(x in it for x in needle)


def _lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


# ── Aggregate trace-score report (--trace-score) ─────────────────────────────

def _bar(value: float, width: int = 20) -> str:
    """ASCII progress bar for a 0–1 float."""
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def aggregate_trace_score(last_n: int | None = None) -> dict[str, Any]:
    """
    Compute aggregate statistics across all run_id-tagged traces.

    Returns a summary dict. OBSERVE ONLY — no ratchet wiring.
    """
    runs = list_runs()
    if not runs:
        return {"total_runs": 0, "runs": {}}

    run_ids = sorted(runs.keys())
    if last_n is not None:
        run_ids = run_ids[-last_n:]

    cards = {}
    for rid in run_ids:
        cards[rid] = score_run(rid)

    eff_scores = [
        c["step_efficiency"]["score"]
        for c in cards.values()
        if c["step_efficiency"]["score"] is not None
    ]
    avg_eff = round(sum(eff_scores) / len(eff_scores), 3) if eff_scores else None

    total_steps = sum(c["steps"] for c in cards.values())
    total_failed = sum(
        c["step_efficiency"].get("failed", 0) for c in cards.values()
    )
    total_retries = sum(
        c["step_efficiency"].get("retries", 0) for c in cards.values()
    )
    total_ms = sum(c["latency"]["total_ms"] for c in cards.values())

    return {
        "total_runs": len(cards),
        "total_steps": total_steps,
        "total_failed_steps": total_failed,
        "total_retried_steps": total_retries,
        "avg_step_efficiency": avg_eff,
        "total_elapsed_ms": total_ms,
        "observation_note": (
            "OBSERVE ONLY — not wired to release ratchet. "
            "Min window before ratchet wiring: 2 weeks / 50 runs (AGENT_BOARD #13)."
        ),
        "runs": cards,
    }


def print_trace_score(last_n: int | None = None) -> None:
    """Print a human-readable aggregate trace-score report."""
    report = aggregate_trace_score(last_n=last_n)
    sep = "─" * 58

    print(f"\n{'═' * 58}")
    print("  Jarvis Execution Trace Scores  [observe-only, AGENT_BOARD #13]")
    print(f"{'═' * 58}")

    if report["total_runs"] == 0:
        print(f"  No run_id-tagged traces found in:\n  {TRACE_DIR}")
        print("  Run an operative task first, then re-check.")
        print(f"{'═' * 58}\n")
        return

    total = report["total_runs"]
    steps = report["total_steps"]
    failed = report["total_failed_steps"]
    retried = report["total_retried_steps"]
    avg_eff = report["avg_step_efficiency"]
    step_sr = 1.0 - (failed / steps) if steps else 1.0

    print(f"\n  Runs observed  : {total}")
    print(f"  Total steps    : {steps}  (✗ {failed} failed, ↻ {retried} retried)")
    if avg_eff is not None:
        print(f"  Avg efficiency : {_bar(avg_eff)} {avg_eff:.1%}")
    print(f"  Step success   : {_bar(step_sr)} {step_sr:.1%}")
    print(f"  Total time     : {report['total_elapsed_ms']:,} ms")

    # Per-run summary
    print(f"\n  {'Per-Run Scores':}")
    print(sep)
    for rid, card in report["runs"].items():
        eff = card["step_efficiency"]
        e_score = eff.get("score")
        e_str = f"{_bar(e_score, 12)} {e_score:.2f}" if e_score is not None else "  n/a      "
        n_steps = card["steps"]
        n_fail = eff.get("failed", 0)
        icon = "✓" if n_fail == 0 else "✗"
        ms = card["latency"]["total_ms"]
        print(f"  {icon} {rid:20s}  {e_str}  {n_steps:2d} steps  {ms:5d}ms")

    print(f"\n  ⚠  {report['observation_note']}")
    print(f"{'═' * 58}\n")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="eval_trace_score",
                                     description="Deterministic trace scorer (R1, observe-only).")
    parser.add_argument("run_id", nargs="?", help="run_id to score (omit for list/aggregate)")
    parser.add_argument("--list", action="store_true", help="list available run_ids")
    parser.add_argument("--trace-score", action="store_true",
                        help="print aggregate trace-score report across all runs (AGENT_BOARD #13)")
    parser.add_argument("--last", type=int, default=None, metavar="N",
                        help="limit --trace-score to last N runs")
    parser.add_argument("--expected", default="", help="comma-separated expected tools")
    parser.add_argument("--min-steps", type=int, default=None)
    args = parser.parse_args(argv)

    # --trace-score: aggregate report across all runs
    if args.trace_score:
        print_trace_score(last_n=args.last)
        return 0

    if args.list or not args.run_id:
        runs = list_runs()
        if not runs:
            print("No run_id-tagged traces yet. Run an operative task, then re-check.")
            return 0
        print(f"{len(runs)} run(s):")
        for rid, n in sorted(runs.items(), key=lambda kv: kv[0]):
            print(f"  {rid}  ({n} steps)")
        return 0

    expected = [t.strip() for t in args.expected.split(",") if t.strip()]
    card = score_run(args.run_id, expected_tools=expected, min_steps=args.min_steps)
    print(json.dumps(card, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
