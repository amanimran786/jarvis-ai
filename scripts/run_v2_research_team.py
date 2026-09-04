#!/usr/bin/env python3
"""Run the first live Jarvis V2 concurrent research team."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_v2.agent import AgentLimits
from jarvis_v2.config import LocalModelConfig
from jarvis_v2.model import LocalMLXClient
from jarvis_v2.team import AcceptanceContract, AgentAssignment, LocalAgentTeam
from jarvis_v2.tools import ReadOnlyLocalTools


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, default=Path(".jarvis-v2/team-runs"))
    args = parser.parse_args()

    config = LocalModelConfig(max_output_tokens=1024, request_timeout_seconds=180.0)
    client = LocalMLXClient(config)
    if not client.ready():
        parser.error(f"local MLX server is not ready at {config.base_url}")
    team = LocalAgentTeam(
        model_factory=lambda: LocalMLXClient(config),
        execute_tool=ReadOnlyLocalTools(args.workspace),
        state_dir=args.state_dir,
        limits=AgentLimits(max_steps=4, max_seconds=180.0),
        max_workers=3,
    )
    result = team.run(
        goal="Assess whether the current Jarvis V2 foundation is ready for deeper multi-agent development.",
        assignments=[
            AgentAssignment(
                agent_id="repo-observer",
                role="repository observer",
                task="Use Git status once and report the exact current repository state.",
                acceptance=AcceptanceContract(required_tools=("git",)),
            ),
            AgentAssignment(
                agent_id="runtime-reader",
                role="runtime architecture reader",
                task="Read jarvis_v2/team.py and report how agents exchange evidence plus one concrete limitation.",
                acceptance=AcceptanceContract(required_tools=("file",)),
            ),
            AgentAssignment(
                agent_id="test-reader",
                role="test evidence reader",
                task="Read tests/test_jarvis_v2_local_runtime.py and report which failure modes are covered.",
                acceptance=AcceptanceContract(required_tools=("file",)),
            ),
        ],
    )
    payload = asdict(result)
    payload["event_log_path"] = str(result.event_log_path)
    print(json.dumps(payload, indent=2))
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
