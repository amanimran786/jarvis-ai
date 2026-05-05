"""Tests for training benchmark category mapping."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


TRAINING_ROOT = Path(__file__).resolve().parent.parent / "training"
if str(TRAINING_ROOT) not in sys.path:
    sys.path.insert(0, str(TRAINING_ROOT))

import benchmark_tracker


def test_category_map_points_to_existing_test_files():
    """Every benchmark target should reference a real pytest file."""
    missing = []
    for targets in benchmark_tracker.CATEGORY_TEST_MAP.values():
        for target in targets:
            file_name = target.split("::", 1)[0]
            if not (benchmark_tracker.TESTS_DIR / file_name).exists():
                missing.append(target)

    assert missing == []


def test_parse_pytest_counts_includes_failures_and_errors():
    output = "312 passed, 1 failed, 2 errors in 12.34s"

    passed, failed, errored = benchmark_tracker._parse_pytest_counts(output)

    assert passed == 312
    assert failed == 1
    assert errored == 2


def test_run_category_tests_accepts_nodeids():
    """Nodeids like file.py::Class should be passed through to pytest."""
    with patch("benchmark_tracker.TESTS_DIR", Path("/tmp/tests")):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("benchmark_tracker.subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.stdout = "2 passed in 0.01s"
                mock_result.stderr = ""
                mock_run.return_value = mock_result

                result = benchmark_tracker._run_category_tests([
                    "test_example.py::SomeClass",
                ])

    assert result["passed"] == 2
    assert result["total"] == 2
    assert result["skipped"] is False
    command = mock_run.call_args.args[0]
    assert "/tmp/tests/test_example.py::SomeClass" in command

