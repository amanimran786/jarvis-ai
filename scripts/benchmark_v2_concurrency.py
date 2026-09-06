#!/usr/bin/env python3
"""Benchmark 1/2/4 concurrent Jarvis V2 workers against one local model."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_v2.agent import AgentLimits
from jarvis_v2.config import LocalModelConfig
from jarvis_v2.model import LocalMLXClient
from jarvis_v2.team import (
    AcceptanceContract,
    AgentAssignment,
    LocalAgentTeam,
    ToolCallContract,
)
from jarvis_v2.tools import ReadOnlyLocalTools


def benchmark_assignments(
    *,
    concurrency: int,
    expected_status: str,
    expected_status_digest: str,
) -> list[AgentAssignment]:
    """Build assignments whose instructions match their exact evidence contract."""
    status_arguments = {"action": "status"}
    canonical_arguments = json.dumps(
        status_arguments,
        sort_keys=True,
        separators=(",", ":"),
    )
    arguments_digest = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()
    return [
        AgentAssignment(
            agent_id=f"observer-{index + 1}",
            role="independent repository observer",
            task=(
                "Use Git status exactly once with exactly this arguments object: "
                f"{canonical_arguments}. Do not add n, ref, or any other argument. "
                "Report the exact modified and untracked paths by returning only this "
                "JSON object, with the git_status value copied exactly from the tool "
                "result and no Markdown: "
                + json.dumps(
                    {
                        "marker": f"EVIDENCE-{index + 1}",
                        "git_status": expected_status,
                    },
                    sort_keys=True,
                )
            ),
            acceptance=AcceptanceContract(
                required_tools=("git",),
                required_answer_markers=(f"EVIDENCE-{index + 1}",),
                required_calls=(
                    ToolCallContract(
                        tool="git",
                        arguments_sha256=arguments_digest,
                        result_sha256=expected_status_digest,
                    ),
                ),
                expected_answer_json=json.dumps(
                    {
                        "marker": f"EVIDENCE-{index + 1}",
                        "git_status": expected_status,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                exact_total_tool_calls=1,
            ),
        )
        for index in range(concurrency)
    ]


def model_server_pid(port: int = 8080) -> int | None:
    completed = subprocess.run(
        ["/usr/sbin/lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    values = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return int(values[0]) if values else None


def process_rss_bytes(pid: int) -> int | None:
    completed = subprocess.run(
        ["/bin/ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    try:
        return int(completed.stdout.strip()) * 1024
    except ValueError:
        return None


def peak_overlapping_request_count(intervals: list[tuple[float, float]]) -> int:
    events = [
        event
        for started, completed in intervals
        for event in ((started, 1), (completed, -1))
        if completed > started
    ]
    active = 0
    peak = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def benchmark_level(
    *,
    concurrency: int,
    workspace: Path,
    state_dir: Path,
    config: LocalModelConfig,
    pid: int | None,
) -> dict:
    stop_sampling = threading.Event()
    rss_samples: list[int] = []

    def sample_memory() -> None:
        while not stop_sampling.wait(0.1):
            if pid is not None:
                value = process_rss_bytes(pid)
                if value is not None:
                    rss_samples.append(value)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    tools = ReadOnlyLocalTools(workspace)
    status_arguments = {"action": "status"}
    expected_status = tools("git", status_arguments)
    expected_status_digest = hashlib.sha256(expected_status.encode("utf-8")).hexdigest()
    assignments = benchmark_assignments(
        concurrency=concurrency,
        expected_status=expected_status,
        expected_status_digest=expected_status_digest,
    )
    started = time.monotonic()
    try:
        result = LocalAgentTeam(
            model_factory=lambda: LocalMLXClient(config),
            execute_tool=tools,
            state_dir=state_dir / f"c{concurrency}",
            limits=AgentLimits(max_steps=4, max_seconds=180.0),
            max_workers=concurrency,
        ).run(
            goal=(
                f"Measure whether {concurrency} independent local agents can inspect "
                "the same repository concurrently and communicate results for synthesis."
            ),
            assignments=assignments,
        )
    finally:
        stop_sampling.set()
        sampler.join(timeout=2)
    elapsed = time.monotonic() - started
    completed = sum(item.passed for item in result.verification)
    malformed = sum("malformed" in item.reason.lower() for item in result.evidence)
    markers = sum(
        f"EVIDENCE-{index + 1}" in item.answer
        for index, item in enumerate(result.evidence)
    )
    worker_timings = [
        timing for evidence in result.evidence for timing in evidence.model_timings
    ]
    request_intervals = [
        (timing.request_started_at, timing.completed_at) for timing in worker_timings
    ]
    first_delta_samples = [
        timing.first_delta_at - timing.request_started_at
        for timing in worker_timings
        if timing.first_delta_at is not None
    ]
    generation_samples = [
        timing.terminal_at - timing.first_delta_at
        for timing in worker_timings
        if timing.first_delta_at is not None
    ]
    worker_completion_tokens = sum(item.completion_tokens for item in result.evidence)
    total_generation_seconds = sum(generation_samples)
    return {
        "concurrency": concurrency,
        "team_status": result.status,
        "workers_completed": completed,
        "workers_total": len(result.evidence),
        "success_rate": completed / len(result.evidence),
        "required_markers_present": markers,
        "workers_verified": completed,
        "malformed_tool_call_rate": malformed / len(result.evidence),
        "elapsed_seconds": elapsed,
        "worker_lifetime_overlap_seconds": result.worker_lifetime_overlap_seconds,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "peak_model_process_rss_bytes": max(rss_samples) if rss_samples else None,
        "time_to_first_delivered_delta_seconds": first_delta_samples,
        "model_request_seconds": [
            timing.completed_at - timing.request_started_at
            for timing in worker_timings
        ],
        "generation_seconds": generation_samples,
        "worker_completion_tokens_per_generation_second": (
            worker_completion_tokens / total_generation_seconds
            if total_generation_seconds > 0
            else None
        ),
        "peak_concurrent_worker_model_requests": peak_overlapping_request_count(
            request_intervals
        ),
        "timing_note": (
            "TTFD measures the first semantic SSE delta delivered by MLX-LM, not "
            "the first raw decoder token; tool calls may be buffered by the server"
        ),
        "team_run_id": result.team_run_id,
        "event_log_path": str(result.event_log_path),
    }


def benchmark_passed(levels: list[dict]) -> bool:
    return all(
        level["team_status"] == "completed"
        and level["success_rate"] == 1.0
        and level["workers_verified"] == level["workers_total"]
        and level["required_markers_present"] == level["workers_total"]
        and level["malformed_tool_call_rate"] == 0.0
        and bool(level["time_to_first_delivered_delta_seconds"])
        and level["peak_concurrent_worker_model_requests"] >= level["concurrency"]
        and (
            level["concurrency"] == 1
            or level["worker_lifetime_overlap_seconds"] > 0
        )
        for level in levels
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path(".jarvis-v2/benchmarks"))
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="mlx-community/Qwen3-8B-4bit")
    args = parser.parse_args()
    config = LocalModelConfig(
        base_url=args.endpoint,
        model=args.model,
        max_output_tokens=512,
        request_timeout_seconds=180.0,
    )
    if not LocalMLXClient(config).ready():
        parser.error(f"local MLX server is not ready at {config.base_url}")
    output_dir = args.output_dir.expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    port = urllib.parse.urlparse(config.base_url).port or 80
    pid = model_server_pid(port)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": config.model,
        "endpoint": config.base_url,
        "model_server_pid": pid,
        "levels": [
            benchmark_level(
                concurrency=level,
                workspace=args.workspace,
                state_dir=output_dir,
                config=config,
                pid=pid,
            )
            for level in (1, 2, 4)
        ],
    }
    output = output_dir / f"benchmark-{int(time.time())}.json"
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    output.chmod(0o600)
    print(json.dumps({**payload, "output": str(output)}, indent=2))
    return 0 if benchmark_passed(payload["levels"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
