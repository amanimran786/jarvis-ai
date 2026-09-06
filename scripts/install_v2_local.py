#!/usr/bin/env python3
"""Install the Jarvis V2 MLX server as the only active Jarvis runtime."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path


LABEL = "com.jarvis.v2.model"
V1_LABELS = (
    "com.jarvis.loop",
    "com.jarvis.dashboard",
    "ai.jarvis.overnight-training",
)
V1_PATHS = (
    Path.home() / "Applications/Jarvis.app",
    Path.home() / "Library/LaunchAgents/com.jarvis.loop.plist",
    Path.home() / "Library/LaunchAgents/com.jarvis.dashboard.plist",
    Path.home() / "Library/LaunchAgents/jarvis.overnight-training.plist",
)


def run_launchctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def build_plist(*, executable: Path, model_path: Path, log_dir: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(executable),
            "--model",
            str(model_path),
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--decode-concurrency",
            "4",
            "--prompt-concurrency",
            "2",
            "--prompt-cache-size",
            "8",
            "--max-tokens",
            "1024",
            "--temp",
            "0.0",
        ],
        "EnvironmentVariables": {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "model-server.log"),
        "StandardErrorPath": str(log_dir / "model-server.error.log"),
    }


def remove_v1(domain: str) -> None:
    for label in V1_LABELS:
        run_launchctl("bootout", f"{domain}/{label}")
        run_launchctl("disable", f"{domain}/{label}")
    for path in V1_PATHS:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remove-v1",
        action="store_true",
        help="remove the installed V1 app and legacy launch-agent files",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    executable = repo / "venv/bin/mlx_lm.server"
    model_root = (
        Path.home()
        / ".cache/huggingface/hub/models--mlx-community--Qwen3-8B-4bit/snapshots"
    )
    snapshots = sorted(path for path in model_root.glob("*") if path.is_dir())
    if not executable.is_file():
        raise SystemExit(f"MLX-LM executable is missing: {executable}")
    if not snapshots:
        raise SystemExit("Qwen3-8B-4bit is not cached locally; refusing a network download")

    domain = f"gui/{os.getuid()}"
    if args.remove_v1:
        remove_v1(domain)

    launch_agents = Path.home() / "Library/LaunchAgents"
    log_dir = Path.home() / "Library/Logs/JarvisV2"
    launch_agents.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{LABEL}.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            build_plist(
                executable=executable,
                model_path=snapshots[-1],
                log_dir=log_dir,
            ),
            handle,
            sort_keys=True,
        )

    run_launchctl("bootout", f"{domain}/{LABEL}")
    run_launchctl("enable", f"{domain}/{LABEL}", check=True)
    run_launchctl("bootstrap", domain, str(plist_path), check=True)
    run_launchctl("kickstart", "-k", f"{domain}/{LABEL}", check=True)
    print(f"Installed {LABEL} from local weights at {snapshots[-1]}")
    print("Local endpoint: http://127.0.0.1:8080/v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
