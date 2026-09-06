from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis_v2.agent import AgentLimits
from jarvis_v2.model import ModelTurn
from jarvis_v2.team import (
    AcceptanceContract,
    AgentAssignment,
    LocalAgentTeam,
    ToolCallContract,
)
from scripts.benchmark_v2_concurrency import (
    benchmark_assignments,
    benchmark_passed,
    peak_overlapping_request_count,
)


class CoordinatedFakeModel:
    def __init__(self, barrier: threading.Barrier, answer: str) -> None:
        self.barrier = barrier
        self.answer = answer

    def complete(self, messages, tools):
        if "UNTRUSTED WORKER EVIDENCE JSON" not in messages[-1]["content"]:
            self.barrier.wait(timeout=1)
            time.sleep(0.03)
        return ModelTurn(content=self.answer, tool_calls=())


def assignment(agent_id: str) -> AgentAssignment:
    return AgentAssignment(agent_id=agent_id, role="reader", task="Report evidence")


def test_team_workers_overlap_and_synthesizer_receives_typed_evidence(tmp_path: Path):
    barrier = threading.Barrier(2)
    created = 0

    def factory():
        nonlocal created
        created += 1
        if created <= 2:
            return CoordinatedFakeModel(barrier, f"worker-{created}")
        return CoordinatedFakeModel(barrier, "combined report")

    result = LocalAgentTeam(
        model_factory=factory,
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        max_workers=2,
        require_worker_evidence=False,
    ).run(goal="Assess", assignments=[assignment("one"), assignment("two")])

    assert result.status == "completed"
    assert result.synthesis == "combined report"
    assert [item.agent_id for item in result.evidence] == ["one", "two"]
    assert result.worker_lifetime_overlap_seconds > 0
    events = [json.loads(line) for line in result.event_log_path.read_text().splitlines()]
    assert sum(event["event"] == "worker_started" for event in events) == 2
    assert events[-1] == {"event": "team_finished", "status": "completed"}


def test_team_isolates_worker_crash_and_returns_partial_result(tmp_path: Path):
    calls = 0

    class Model:
        def complete(self, messages, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TypeError("boom")
            return ModelTurn(content="survived", tool_calls=())

    result = LocalAgentTeam(
        model_factory=Model,
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        max_workers=2,
        limits=AgentLimits(max_consecutive_errors=1),
        require_worker_evidence=False,
    ).run(goal="Assess", assignments=[assignment("one"), assignment("two")])

    assert result.status == "partial"
    assert any(item.status in {"failed", "blocked"} for item in result.evidence)
    assert any(item.status == "completed" for item in result.evidence)


def test_team_rejects_duplicate_or_excess_assignments(tmp_path: Path):
    team = LocalAgentTeam(
        model_factory=lambda: None,
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        max_workers=2,
    )

    with pytest.raises(ValueError, match="unique"):
        team.run(goal="Assess", assignments=[assignment("same"), assignment("same")])
    with pytest.raises(ValueError, match="exceeds"):
        team.run(
            goal="Assess",
            assignments=[assignment("one"), assignment("two"), assignment("three")],
        )


def test_team_blocks_workers_that_claim_completion_without_tool_evidence(tmp_path: Path):
    result = LocalAgentTeam(
        model_factory=lambda: CoordinatedFakeModel(threading.Barrier(1), "unsupported"),
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path,
        max_workers=1,
        limits=AgentLimits(max_consecutive_errors=1),
    ).run(goal="Assess", assignments=[assignment("one")])

    assert result.status == "blocked"
    assert result.evidence[0].status == "blocked"
    assert "requires tool evidence" in result.evidence[0].reason


def test_verifier_rejects_irrelevant_tool_and_skips_synthesis(tmp_path: Path):
    class IrrelevantToolModel:
        def complete(self, messages, tools):
            if not any(message["role"] == "tool" for message in messages):
                return ModelTurn(
                    content="",
                    tool_calls=(
                        {
                            "id": "wrong-tool",
                            "type": "function",
                            "function": {
                                "name": "file",
                                "arguments": json.dumps(
                                    {"action": "read", "path": "README.md"}
                                ),
                            },
                        },
                    ),
                )
            return ModelTurn(content="EVIDENCE-1", tool_calls=())

    result = LocalAgentTeam(
        model_factory=IrrelevantToolModel,
        execute_tool=lambda name, arguments: "unrelated",
        state_dir=tmp_path,
        max_workers=1,
    ).run(
        goal="Inspect Git",
        assignments=[
            AgentAssignment(
                agent_id="one",
                role="reader",
                task="Report Git status",
                acceptance=AcceptanceContract(
                    required_tools=("git",),
                    required_answer_markers=("EVIDENCE-1",),
                ),
            )
        ],
    )

    assert result.status == "blocked"
    assert result.verification[0].passed is False
    assert result.verification[0].reasons == ("required tools missing: git",)
    assert result.synthesis == ""
    events = [json.loads(line) for line in result.event_log_path.read_text().splitlines()]
    assert any(event["event"] == "worker_verification_started" for event in events)
    assert any(event["event"] == "worker_verification_finished" for event in events)


def test_verifier_rejects_marker_only_answer_after_correct_tool_call(tmp_path: Path):
    class MarkerOnlyModel:
        def complete(self, messages, tools):
            if not any(message["role"] == "tool" for message in messages):
                return ModelTurn(
                    content="",
                    tool_calls=(
                        {
                            "id": "correct-tool",
                            "type": "function",
                            "function": {
                                "name": "git",
                                "arguments": json.dumps({"action": "status"}),
                            },
                        },
                    ),
                )
            return ModelTurn(content='{"marker":"EVIDENCE-1"}', tool_calls=())

    arguments_digest = hashlib.sha256(b'{"action":"status"}').hexdigest()
    result_digest = hashlib.sha256(b" M tracked.py").hexdigest()
    result = LocalAgentTeam(
        model_factory=MarkerOnlyModel,
        execute_tool=lambda name, arguments: " M tracked.py",
        state_dir=tmp_path,
        max_workers=1,
    ).run(
        goal="Inspect Git",
        assignments=[
            AgentAssignment(
                agent_id="one",
                role="reader",
                task="Report Git status",
                acceptance=AcceptanceContract(
                    required_calls=(
                        ToolCallContract(
                            tool="git",
                            arguments_sha256=arguments_digest,
                            result_sha256=result_digest,
                        ),
                    ),
                    required_answer_markers=("EVIDENCE-1",),
                    expected_answer_json=(
                        '{"git_status":" M tracked.py","marker":"EVIDENCE-1"}'
                    ),
                ),
            )
        ],
    )

    assert result.status == "blocked"
    assert result.verification[0].passed is False
    assert any(
        "does not match the expected structured result" in reason
        for reason in result.verification[0].reasons
    )


def test_verifier_enforces_exact_total_tool_call_count(tmp_path: Path):
    class ExtraCallModel:
        def complete(self, messages, tools):
            completed_calls = sum(message["role"] == "tool" for message in messages)
            if completed_calls < 2:
                return ModelTurn(
                    content="",
                    tool_calls=(
                        {
                            "id": f"call-{completed_calls}",
                            "type": "function",
                            "function": {
                                "name": "git",
                                "arguments": json.dumps({"action": "status"}),
                            },
                        },
                    ),
                )
            return ModelTurn(content='{"marker":"EVIDENCE-1"}', tool_calls=())

    result = LocalAgentTeam(
        model_factory=ExtraCallModel,
        execute_tool=lambda name, arguments: "clean",
        state_dir=tmp_path,
        max_workers=1,
    ).run(
        goal="Inspect Git",
        assignments=[
            AgentAssignment(
                agent_id="one",
                role="reader",
                task="Report Git status",
                acceptance=AcceptanceContract(
                    exact_total_tool_calls=1,
                    required_answer_markers=("EVIDENCE-1",),
                ),
            )
        ],
    )

    assert result.status == "blocked"
    assert any("observed 2" in reason for reason in result.verification[0].reasons)


def test_team_records_synthesis_crash_and_protects_event_log(tmp_path: Path):
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            return CoordinatedFakeModel(threading.Barrier(1), "worker evidence")
        raise RuntimeError("synth unavailable")

    result = LocalAgentTeam(
        model_factory=factory,
        execute_tool=lambda name, arguments: "unused",
        state_dir=tmp_path / "private-team",
        max_workers=1,
        require_worker_evidence=False,
    ).run(goal="Assess", assignments=[assignment("one")])

    events = [json.loads(line) for line in result.event_log_path.read_text().splitlines()]
    assert result.status == "partial"
    assert events[-2]["event"] == "synthesis_crashed"
    assert events[-1] == {"event": "team_finished", "status": "partial"}
    assert (result.event_log_path.stat().st_mode & 0o777) == 0o600
    assert (result.event_log_path.parent.stat().st_mode & 0o777) == 0o700


def test_research_team_script_can_run_directly_from_repo(tmp_path: Path):
    script = Path(__file__).resolve().parents[1] / "scripts/run_v2_research_team.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "first live Jarvis V2 concurrent research team" in completed.stdout


def test_concurrency_benchmark_script_can_run_directly_from_repo(tmp_path: Path):
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark_v2_concurrency.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "Benchmark 1/2/4 concurrent Jarvis V2 workers" in completed.stdout
    assert "--endpoint" in completed.stdout
    assert "--model" in completed.stdout


def test_benchmark_assignment_instructions_match_exact_argument_contract():
    expected_result_digest = hashlib.sha256(b"(no output)").hexdigest()

    assignments = benchmark_assignments(
        concurrency=2,
        expected_status="(no output)",
        expected_status_digest=expected_result_digest,
    )

    expected_arguments = '{"action":"status"}'
    expected_arguments_digest = hashlib.sha256(expected_arguments.encode()).hexdigest()
    assert len(assignments) == 2
    for item in assignments:
        assert f"exactly this arguments object: {expected_arguments}" in item.task
        assert "Do not add n, ref, or any other argument" in item.task
        assert item.acceptance.required_calls[0].arguments_sha256 == (
            expected_arguments_digest
        )


def test_benchmark_gate_rejects_team_or_overlap_failure():
    passing = {
        "concurrency": 2,
        "team_status": "completed",
        "success_rate": 1.0,
        "workers_verified": 2,
        "workers_total": 2,
        "required_markers_present": 2,
        "malformed_tool_call_rate": 0.0,
        "worker_lifetime_overlap_seconds": 0.5,
        "time_to_first_delivered_delta_seconds": [0.25, 0.4],
        "peak_concurrent_worker_model_requests": 2,
    }

    assert benchmark_passed([passing]) is True
    assert benchmark_passed([{**passing, "team_status": "partial"}]) is False
    assert benchmark_passed(
        [{**passing, "worker_lifetime_overlap_seconds": 0.0}]
    ) is False
    assert benchmark_passed(
        [{**passing, "time_to_first_delivered_delta_seconds": []}]
    ) is False
    assert benchmark_passed(
        [{**passing, "peak_concurrent_worker_model_requests": 1}]
    ) is False


def test_peak_request_overlap_counts_in_flight_intervals():
    assert peak_overlapping_request_count([(0.0, 3.0), (1.0, 2.0), (1.5, 4.0)]) == 3
    assert peak_overlapping_request_count([(0.0, 1.0), (1.0, 2.0)]) == 1
