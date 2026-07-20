from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.task_contract import (
    load_contracts,
    normalized_task_spec_digest,
    validate_contract,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_contract_registry_is_unique_and_valid():
    contract_data = json.loads(
        (REPO_ROOT / "TASK_CONTRACTS.json").read_text(encoding="utf-8")
    )
    contracts = load_contracts(REPO_ROOT / "TASK_CONTRACTS.json")

    assert len(contracts) == len(contract_data)
    for contract in contracts.values():
        assert validate_contract(contract) == (True, [])


def test_every_contract_binds_to_exactly_one_current_queue_task():
    queue_path = REPO_ROOT / "WORK_QUEUE.json"
    if not queue_path.exists():
        pytest.skip("WORK_QUEUE.json is daemon-owned runtime state")

    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    contracts = load_contracts(REPO_ROOT / "TASK_CONTRACTS.json")
    queue_contract_ids = {
        str(task.get("contract_id") or "").strip()
        for task in queue
        if str(task.get("contract_id") or "").strip()
    }

    assert set(contracts) == queue_contract_ids
    for contract_id, contract in contracts.items():
        matches = [task for task in queue if task.get("contract_id") == contract_id]
        assert len(matches) == 1, f"{contract_id} matches {len(matches)} queue rows"
        assert contract.task_spec_sha256 == normalized_task_spec_digest(matches[0])
