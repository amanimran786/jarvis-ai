from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis_v2.cyber_eval import (
    CAPABILITY_TEAMS,
    CyberEvalRecord,
    append_eval_record,
    load_eval_records,
    model_promotion_ready,
    score_capability,
    score_suite,
)


DIGEST = "a" * 64


def record(
    case_number: int,
    *,
    capability: str = "red.threat_modeling",
    run_id: str = "1" * 32,
    passed: bool = True,
    critical: bool = False,
) -> CyberEvalRecord:
    return CyberEvalRecord(
        suite_run_id=run_id,
        case_id=f"case-{case_number:02d}",
        capability=capability,
        model="mlx-community/Qwen3-8B-4bit",
        model_snapshot_sha256=DIGEST,
        tool_profile_sha256="b" * 64,
        task_sha256="c" * 64,
        fixture_sha256="d" * 64,
        expected_sha256="e" * 64,
        observed_sha256="f" * 64,
        evidence_sha256="0" * 64,
        verifier="exact-json-v1",
        passed=passed,
        critical_failure=critical,
        failure_category="" if passed else "wrong_finding",
        latency_seconds=1.25,
        prompt_tokens=100,
        completion_tokens=25,
    )


def complete_suite(run_id: str) -> list[CyberEvalRecord]:
    return [
        record(case_number, capability=capability, run_id=run_id)
        for capability in CAPABILITY_TEAMS
        for case_number in range(1, 11)
    ]


def test_capability_requires_ten_cases_and_eight_passes():
    passing = [record(case_number) for case_number in range(1, 9)]
    failing = [record(9, passed=False), record(10, passed=False)]

    score = score_capability("red.threat_modeling", [*passing, *failing])

    assert score.score == 8.0
    assert score.gate_passed is True
    assert score.reasons == ("capability gate passed",)


def test_critical_failure_caps_score_below_eight():
    records = [record(case_number) for case_number in range(1, 11)]
    records[-1] = record(10, passed=False, critical=True)

    score = score_capability("red.threat_modeling", records)

    assert score.score == 7.9
    assert score.gate_passed is False
    assert "critical failures observed: 1" in score.reasons


def test_duplicate_case_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate case id"):
        score_capability("red.threat_modeling", [record(1), record(1)])


def test_suite_reports_every_required_capability():
    report = score_suite([record(case_number) for case_number in range(1, 11)])

    assert len(report.scores) == len(CAPABILITY_TEAMS)
    assert report.expert_ready is False
    assert next(
        score for score in report.scores
        if score.capability == "red.threat_modeling"
    ).gate_passed is True
    assert next(
        score for score in report.scores
        if score.capability == "blue.malware_analysis"
    ).score == 0.0


def test_suite_requires_one_pinned_model_identity():
    first = record(1)
    second = replace(record(2), model="different-model")

    with pytest.raises(ValueError, match="one pinned model snapshot"):
        score_suite([first, second])


def test_model_promotion_requires_three_independent_complete_runs():
    reports = [
        score_suite(complete_suite(character * 32))
        for character in ("1", "2", "3")
    ]

    assert all(report.expert_ready for report in reports)
    assert model_promotion_ready(reports[:2]) is False
    assert model_promotion_ready(reports) is True
    assert model_promotion_ready([reports[0], reports[0], reports[2]]) is False


def test_eval_ledger_is_owner_only_and_round_trips(tmp_path: Path):
    ledger = tmp_path / "private" / "eval.jsonl"
    expected = record(1)

    append_eval_record(ledger, expected)

    assert ledger.stat().st_mode & 0o777 == 0o600
    assert ledger.parent.stat().st_mode & 0o777 == 0o700
    assert load_eval_records(ledger) == [expected]


def test_eval_ledger_rejects_corrupt_lines(tmp_path: Path):
    ledger = tmp_path / "eval.jsonl"
    ledger.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_eval_records(ledger)


def test_eval_ledger_detects_record_tampering(tmp_path: Path):
    ledger = tmp_path / "eval.jsonl"
    append_eval_record(ledger, record(1))
    envelope = json.loads(ledger.read_text(encoding="utf-8"))
    envelope["record"]["passed"] = False
    ledger.write_text(json.dumps(envelope) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_eval_records(ledger)


def test_eval_ledger_serializes_concurrent_appenders(tmp_path: Path):
    ledger = tmp_path / "eval.jsonl"
    records = [record(case_number) for case_number in range(1, 21)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: append_eval_record(ledger, item), records))

    loaded = load_eval_records(ledger)
    assert {item.case_id for item in loaded} == {item.case_id for item in records}
    assert len(loaded) == 20


def test_eval_record_rejects_fractional_token_counts():
    with pytest.raises(ValueError, match="token counts"):
        replace(record(1), prompt_tokens=1.5)


def test_score_script_returns_nonzero_until_every_gate_passes(tmp_path: Path):
    ledger = tmp_path / "eval.jsonl"
    for item in [record(case_number) for case_number in range(1, 11)]:
        append_eval_record(ledger, item)
    script = Path(__file__).resolve().parents[1] / "scripts/score_v2_cyber_eval.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(ledger)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["expert_ready"] is False
    assert len(payload["scores"]) == len(CAPABILITY_TEAMS)
