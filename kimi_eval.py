"""
Kimi K2.7 Code parity-eval stub.

Purpose: decide whether Kimi (the staged candidate in brains/brain_kimi.py) is
worth promoting into the coder routing tier — on OUR tasks, not Moonshot's
self-reported benchmarks. This runs the existing coder-group eval prompts
(capability_evals._CASES, group == "coding_agent") through Kimi and one or more
incumbent baselines, then reports each model's output alongside the case rubric,
latency, token usage, and (for Kimi) estimated OpenRouter cost.

It is deliberately a STUB in one respect: there is no automated grader wired in.
Scoring the `checks` rubric is left to a human or a follow-up LLM-judge pass —
faking a pass/fail here would be exactly the kind of unverified claim this
project's audit lane exists to catch. The harness gives you a clean, side-by-side
artifact to grade; it does not invent the grade.

Usage:
    JARVIS_KIMI_ENABLED=1 OPENROUTER_API_KEY=sk-or-... \
        python3 kimi_eval.py                 # Kimi vs local coder baseline
    ... python3 kimi_eval.py --cloud-baseline # also run GPT-4o for comparison
    ... python3 kimi_eval.py --json           # machine-readable report
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Callable

from config import (
    GPT_FULL,
    KIMI_CODER,
    KIMI_PRICE_IN_PER_M,
    KIMI_PRICE_OUT_PER_M,
    LOCAL_CODER,
)


def coder_cases() -> list[dict[str, Any]]:
    """The coding_agent eval cases — the ratchet Kimi has to clear."""
    from capability_evals import _CASES

    return [dict(case) for case in _CASES if case.get("group") == "coding_agent"]


def _time_call(ask_fn: Callable[[str], str], prompt: str) -> dict[str, Any]:
    start = time.monotonic()
    try:
        output = ask_fn(prompt)
        return {
            "ok": True,
            "output": output,
            "latency_s": round(time.monotonic() - start, 2),
        }
    except Exception as exc:  # surface, never swallow — a failed run is a result
        return {
            "ok": False,
            "output": "",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_s": round(time.monotonic() - start, 2),
        }


def _kimi_runner() -> tuple[str, Callable[[str], str]]:
    from brains.brain_kimi import ask_kimi

    return KIMI_CODER, lambda prompt: ask_kimi(prompt)


def _local_baseline_runner() -> tuple[str, Callable[[str], str]]:
    from brains.brain_ollama import ask_local

    return f"local:{LOCAL_CODER}", lambda prompt: ask_local(
        prompt, model=LOCAL_CODER, raise_on_error=True
    )


def _cloud_baseline_runner() -> tuple[str, Callable[[str], str]]:
    from brains.brain import ask

    # bare_system + bypass_local: we want raw GPT-4o coding, no persona, no
    # local-first detour, so it's a fair head-to-head against Kimi.
    return f"cloud:{GPT_FULL}", lambda prompt: ask(
        prompt, model=GPT_FULL, bypass_local=True
    )


def _estimate_kimi_cost(usage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate Kimi spend for this run from usage_tracker rows (list-price)."""
    prompt_tokens = sum(int(r.get("prompt_tokens") or 0) for r in usage_rows)
    completion_tokens = sum(int(r.get("completion_tokens") or 0) for r in usage_rows)
    cost = (
        prompt_tokens / 1_000_000 * KIMI_PRICE_IN_PER_M
        + completion_tokens / 1_000_000 * KIMI_PRICE_OUT_PER_M
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_usd": round(cost, 4),
        "note": "OpenRouter list price; ignores cache-hit discount.",
    }


def run_parity(*, cloud_baseline: bool = False) -> dict[str, Any]:
    from brains.brain_kimi import is_available

    if not is_available():
        return {
            "ok": False,
            "error": (
                "Kimi candidate is not available. Set JARVIS_KIMI_ENABLED=1 and "
                "OPENROUTER_API_KEY (or JARVIS_KIMI_API_KEY) before running."
            ),
        }

    runners: list[tuple[str, Callable[[str], str]]] = [_kimi_runner()]
    runners.append(_local_baseline_runner())
    if cloud_baseline:
        runners.append(_cloud_baseline_runner())

    # Snapshot the usage sequence so we can attribute exactly this run's spend.
    import usage_tracker

    start_seq = usage_tracker.current_seq()

    cases = coder_cases()
    results: list[dict[str, Any]] = []

    for case in cases:
        prompt = case["prompt"]
        per_model: dict[str, Any] = {}
        for label, fn in runners:
            run = _time_call(fn, prompt)
            per_model[label] = run
        results.append(
            {
                "id": case["id"],
                "prompt": prompt,
                "checks": case.get("checks", []),  # rubric — grade these by hand/judge
                "models": per_model,
            }
        )

    # Attribute exactly this run's Kimi spend via the usage-sequence snapshot.
    try:
        rows = usage_tracker.entries(hours=24, since_seq=start_seq)
        kimi_usage_rows = [r for r in rows if r.get("provider") == "kimi"]
    except Exception:
        kimi_usage_rows = []

    return {
        "ok": True,
        "candidate": KIMI_CODER,
        "baselines": [label for label, _ in runners[1:]],
        "case_count": len(cases),
        "graded": False,
        "grading_note": (
            "Outputs + rubric emitted for manual/LLM-judge scoring. "
            "Promote into the coder tier only after a graded win vs baselines."
        ),
        "kimi_cost_estimate": _estimate_kimi_cost(kimi_usage_rows),
        "results": results,
    }


def _print_report(report: dict[str, Any]) -> None:
    if not report.get("ok"):
        print(f"[kimi_eval] {report.get('error')}")
        return
    print(f"Kimi parity eval — candidate {report['candidate']}")
    print(f"Baselines: {', '.join(report['baselines']) or '(none)'}")
    print(f"Cases: {report['case_count']}  |  graded: {report['graded']}")
    cost = report["kimi_cost_estimate"]
    print(
        f"Kimi spend (est): ${cost['estimated_usd']} "
        f"({cost['prompt_tokens']} in / {cost['completion_tokens']} out tok)"
    )
    print(f"NOTE: {report['grading_note']}\n")
    for case in report["results"]:
        print(f"── {case['id']} ──")
        print(f"   rubric: {', '.join(case['checks'])}")
        for label, run in case["models"].items():
            if run["ok"]:
                preview = run["output"].strip().replace("\n", " ")[:160]
                print(f"   [{label}] {run['latency_s']}s: {preview}…")
            else:
                print(f"   [{label}] FAILED ({run['latency_s']}s): {run['error']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi K2.7 coder parity eval")
    parser.add_argument(
        "--cloud-baseline",
        action="store_true",
        help=f"also run {GPT_FULL} as a cloud baseline (costs OpenAI tokens)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args()

    report = run_parity(cloud_baseline=args.cloud_baseline)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)


if __name__ == "__main__":
    main()
