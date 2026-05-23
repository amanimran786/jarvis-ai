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

import runtime_state

_BUNDLED_TRAINING_ROOT = Path(__file__).parent
TRAINING_ROOT = runtime_state.writable_data_path(
    "training",
    seed_from=_BUNDLED_TRAINING_ROOT,
)
BENCHMARK_LOG = TRAINING_ROOT / "benchmarks.jsonl"
TESTS_DIR = Path(__file__).parent.parent / "tests"

# Map category → concrete pytest files. Keep this aligned with real files so
# the dashboard does not show false SKIP rows just because a legacy filename
# like test_voice.py does not exist.
CATEGORY_TEST_MAP: dict[str, list[str]] = {
    "voice": [
        "test_config_local_stt.py",
        "test_local_stt_runtime.py",
        "test_local_tts_runtime.py",
        "test_voice_tts_regression.py",
    ],
    "calendar": [
        "test_proactive_watcher.py",
        "test_jarvis_regression_suite.py::MeetingPrepFastPathTests",
        "test_jarvis_regression_suite.py::ReminderFastPathTests",
    ],
    "code": [
        "test_unit_coverage.py",
        "test_local_model_routing.py",
        "test_provider_router_free_first.py",
    ],
    "memory": [
        "test_mem0_layer.py",
        "test_persistent_jarvis_v1.py",
        "test_teacher_capture.py",
    ],
    "tools": [
        "test_agent_tooling.py",
        "test_browser.py",
        "test_jarvis_executor.py",
        "test_osint_tools.py",
        "test_skill_audit.py",
        "test_skill_export.py",
    ],
    "conversation": [
        "test_jarvis_new_fastpaths.py",
        "test_message_intent_parsing.py",
        "test_messages_contacts.py",
        "test_messages_thread.py",
    ],
    "meeting": [
        "test_jarvis_regression_suite.py::LocalVisionFallbackTests::test_meeting_transcribe_prefers_local_before_openai",
        "test_jarvis_regression_suite.py::LocalVisionFallbackTests::test_meeting_transcribe_skips_openai_when_fallback_disallowed",
        "test_jarvis_regression_suite.py::RouterTests::test_focus_meeting_fast_path",
        "test_jarvis_regression_suite.py::RouterTests::test_meeting_captions_fast_path",
        "test_jarvis_regression_suite.py::RouterTests::test_meeting_diagnostics_fast_path",
        "test_jarvis_regression_suite.py::RouterTests::test_meeting_safe_mode_enable_fast_path",
        "test_jarvis_regression_suite.py::RouterTests::test_meeting_safe_mode_status_fast_path",
        "test_jarvis_regression_suite.py::OverlayMeetingDetectionTests",
        "test_jarvis_regression_suite.py::BrowserMeetingCaptionTests",
        "test_jarvis_regression_suite.py::MeetingListenerTests",
        "test_jarvis_regression_suite.py::CallPrivacyTests",
    ],
}


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _parse_pytest_counts(output: str) -> tuple[int, int, int]:
    """Return passed, failed, errored counts from pytest output."""
    import re

    counts = {"passed": 0, "failed": 0, "error": 0, "errors": 0}
    for line in output.splitlines():
        if not any(word in line for word in ("passed", "failed", "error")):
            continue
        for count, status in re.findall(r"(\d+)\s+(passed|failed|errors?|error)", line):
            counts[status] = counts.get(status, 0) + int(count)

    errored = counts.get("error", 0) + counts.get("errors", 0)
    return counts["passed"], counts["failed"], errored


def _run_category_tests(test_files: list[str]) -> dict:
    """Run pytest for a category file group, return {passed, total, score}."""
    targets = [(TESTS_DIR / name.split("::", 1)[0], name) for name in test_files]
    missing = [name for path, name in targets if not path.exists()]
    existing = [str(TESTS_DIR / name) for path, name in targets if path.exists()]
    if not existing:
        return {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total": 0,
            "score": None,
            "skipped": True,
            "missing": missing,
        }

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *existing, "-q", "--tb=no", "--no-header"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = result.stdout + result.stderr
        passed, failed, errored = _parse_pytest_counts(output)
        total = passed + failed + errored
        if total == 0:
            return {
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "total": 0,
                "score": None,
                "skipped": True,
                "files": [Path(target).name for target in existing],
                "missing": missing,
            }
        return {
            "passed": passed,
            "failed": failed,
            "errors": errored,
            "total": total,
            "score": round(passed / total, 4),
            "skipped": False,
            "files": [Path(target).name for target in existing],
            "missing": missing,
        }
    except Exception as e:
        return {
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "total": 0,
            "score": None,
            "error": str(e),
            "files": [Path(target).name for target in existing],
            "missing": missing,
            "skipped": True,
        }


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
