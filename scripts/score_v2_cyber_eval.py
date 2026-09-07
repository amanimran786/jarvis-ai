#!/usr/bin/env python3
"""Score one local Jarvis V2 cyber-evaluation JSONL ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis_v2.cyber_eval import load_eval_records, score_suite


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score an evidence-bound Jarvis V2 cyber evaluation"
    )
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    report = score_suite(load_eval_records(args.ledger))
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.expert_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
