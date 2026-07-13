from __future__ import annotations

import json
from pathlib import Path

from harness.task_contract import (
    load_contracts,
    normalized_task_spec_digest,
    validate_contract,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_contract_binds_to_exactly_one_current_queue_task():
    queue = json.loads((REPO_ROOT / "WORK_QUEUE.json").read_text(encoding="utf-8"))
    contract_data = json.loads(
        (REPO_ROOT / "TASK_CONTRACTS.json").read_text(encoding="utf-8")
    )
    contracts = load_contracts(REPO_ROOT / "TASK_CONTRACTS.json")
    queue_contract_ids = {
        str(task.get("contract_id") or "").strip()
        for task in queue
        if str(task.get("contract_id") or "").strip()
    }

    assert len(contracts) == len(contract_data)
    assert set(contracts) == queue_contract_ids
    for contract_id, contract in contracts.items():
        matches = [task for task in queue if task.get("contract_id") == contract_id]
        assert len(matches) == 1, f"{contract_id} matches {len(matches)} queue rows"
        assert contract.task_spec_sha256 == normalized_task_spec_digest(matches[0])
        assert validate_contract(contract) == (True, [])
