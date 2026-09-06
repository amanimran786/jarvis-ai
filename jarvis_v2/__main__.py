"""Command-line entry point for the Jarvis V2 local bootstrap."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .agent import AgentLimits, AgentResult, LocalAgentLoop
from .config import LocalModelConfig
from .model import LocalMLXClient
from .tools import ReadOnlyLocalTools


def _result_payload(result: AgentResult) -> dict:
    payload = asdict(result)
    payload["checkpoint_path"] = str(result.checkpoint_path)
    payload["event_log_path"] = str(result.event_log_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a strictly local Jarvis V2 task")
    parser.add_argument("task", nargs="?", help="task for the local agent")
    parser.add_argument("--resume", help="resume a checkpointed run id")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, default=Path(".jarvis-v2/runs"))
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model", default="mlx-community/Qwen3-8B-4bit")
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()

    if not args.task and not args.resume:
        parser.error("provide a task or --resume RUN_ID")
    config = LocalModelConfig(base_url=args.endpoint, model=args.model)
    client = LocalMLXClient(config)
    if not client.ready():
        parser.error(f"local MLX server is not ready at {config.base_url}")
    loop = LocalAgentLoop(
        model=client,
        execute_tool=ReadOnlyLocalTools(args.workspace),
        state_dir=args.state_dir,
        limits=AgentLimits(max_steps=args.max_steps),
    )
    result = loop.run(args.task, resume_run_id=args.resume)
    print(json.dumps(_result_payload(result), indent=2))
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
