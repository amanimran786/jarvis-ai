#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tool_registry


def main() -> None:
    payload = []
    for name, spec in sorted(tool_registry.TOOL_SPECS.items()):
        payload.append(
            {
                "name": spec.name,
                "description": spec.description,
                "args_schema": spec.args_schema,
                "required": list(spec.required),
                "side_effects": spec.side_effects,
                "timeout_seconds": spec.timeout_seconds,
                "verifier": spec.verifier,
                "idempotent": spec.idempotent,
            }
        )
    print(json.dumps({"tools": payload}, indent=2))


if __name__ == "__main__":
    main()
