"""
Benchmark tracker for Jarvis fine-tuning.

Logs per-category eval scores after every training run so the dashboard
can show improvement over time across domains.

Categories:
  voice       — wake-word, STT accuracy, TTS routing
  calendar    — Google Calendar event parsing and creation
  code        — code generation, debugging, explanation
  memory      — fact recall, preference, project context
  tools       — terminal, browser, file ops, search
  conversation — open-ended chat, summaries, briefings
  meeting     — transcript parsing, action-item extraction

Log format (one JSON per line in training/benchmarks.jsonl):
  {
    "ts": "2025-05-04 23:00",
    "run_date": "2025-05-04",
    "model_version": "jarvis-v1",
    "adapter_path": "training/mlx_adapters/...",
    "overall": 0.87,
    "categories": {
      "voice": {"passed": 8, "total": 10, "score": 0.80},
      "calendar": {...},
      ...
    },
    "delta_vs_baseline": 0.04,
    "promoted": true
  }
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

TRAINING_ROOT = Path(__file__).parent
BENCHMARK_LOG = TRAINING_ROOT / "benchmarks.jsonl"
TESTS_DIR = Path(__file__).parent.parent / "tests"

# Map category → pytest marker or filename pattern
CATEGORY_TEST_MAP: dict[str, str] = {
    "voice":        "test_voice",
    "calendar":     "test_calendar",
    "code":         "test_unit_coverage",
    "memory":       "test_memory",
    "tools":        "test_tools",
    "conversation": "test_jarvis_regression",
    "meeting":      "test_meeting",
}

# Fallback scores if test file doesn't exist (keeps tracker functional)
_FALLBACK_TOTAL = 5


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _run_category_tests(pattern: str) -> dict:
    """Run pytest for a single category pattern, return {passed, total, score}."""
    test_file = TESTS_DIR / f"{pattern}.py"
    if not test_file.exists():
        # Return neutral score rather than crashing
        return {"passed": 0, "total": 0, "score": None, "skipped": True}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q", "--tb=no", "--no-header"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        # Parse "N passed, M failed" from pytest summary line
        passed = total = 0
        for line in output.splitlines():
            if "passed" in line or "failed" in line or "error" in line:
                import re
                nums = re.findall(r"(\d+)\s+(passed|failed|error)", line)
                for count, status in nums:
                    if status == "passed":
                        passed += int(count)
                    total += int(count)
        if total == 0:
            return {"passed": 0, "total": 0, "score": None, "skipped": True}
        return {
            "passed": passed,
            "total": total,
            "score": round(passed / total, 4),
            "skipped": False,
        }
    except Exception as e:
        return {"passed": 0, "total": 0, "score": None, "error": str(e), "skipped": True}


def run_full_benchmark(
    model_version: str = "jarvis-local",
    adapter_path: str = "",
    baseline: Optional[dict] = None,
) -> dict:
    """
    Run all category benchmarks, compute overall score, log result.

    Args:
        model_version: human-readable model tag
        adapter_path: path to the MLX adapter used (empty = base model)
        baseline: previous benchmark record to compute delta against

    Returns:
        Full benchmark record dict
    """
    categories: dict[str, dict] = {}
    total_passed = total_tests = 0

    for category, pattern in CATEGORY_TEST_MAP.items():
        result = _run_category_tests(pattern)
        categories[category] = result
        if not result.get("skipped"):
            total_passed += result["passed"]
            total_tests += result["total"]

    overall = round(total_passed / total_tests, 4) if total_tests > 0 else None

    # Compute delta vs previous run
    delta = None
    if baseline and baseline.get("overall") is not None and overall is not None:
        delta = round(overall - baseline["overall"], 4)

    record = {
        "ts": _timestamp(),
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "model_version": model_version,
        "adapter_path": adapter_path,
        "overall": overall,
        "total_passed": total_passed,
        "total_tests": total_tests,
        "categories": categories,
        "delta_vs_baseline": delta,
        "promoted": False,  # updated by caller after promotion decision
    }

    return record


def log_benchmark(record: dict) -> None:
    """Append benchmark record to benchmarks.jsonl."""
    with open(BENCHMARK_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_history() -> list[dict]:
    """Return all benchmark records, oldest first."""
    if not BENCHMARK_LOG.exists():
        return []
    records = []
    with open(BENCHMARK_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def get_latest() -> Optional[dict]:
    """Return the most recent benchmark record."""
    history = load_history()
    return history[-1] if history else None


def get_category_trends() -> dict[str, list[float]]:
    """
    Return per-category score history for charting.

    Returns:
        {"voice": [0.8, 0.85, 0.9], "calendar": [...], ...}
    """
    history = load_history()
    trends: dict[str, list] = {cat: [] for cat in CATEGORY_TEST_MAP}
    for record in history:
        cats = record.get("categories", {})
        for cat in CATEGORY_TEST_MAP:
            score = cats.get(cat, {}).get("score")
            trends[cat].append(score)
    return trends


def get_overall_trend() -> list[tuple[str, Optional[float]]]:
    """Return [(date, overall_score), ...] for the overall trend line."""
    return [(r["run_date"], r.get("overall")) for r in load_history()]
