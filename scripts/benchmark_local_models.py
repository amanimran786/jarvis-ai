#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import local_model_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local Ollama models for Jarvis.")
    parser.add_argument("--repeats", type=int, default=1, help="How many repeat rounds to run.")
    args = parser.parse_args()

    result = local_model_benchmark.run_benchmark(repeats=args.repeats)
    print(json.dumps(result, indent=2))
    print()
    print(local_model_benchmark.result_text(result))


if __name__ == "__main__":
    main()
