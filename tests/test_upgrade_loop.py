import json
from pathlib import Path

from core import upgrade_loop


def test_run_cycle_scores_and_gates_risky_local_upgrade(tmp_path: Path):
    brief = json.dumps({
        "signals": [
            {
                "title": "Use Apple Foundation Models for local fast path",
                "summary": "On-device guided generation could reduce latency and token cost for simple local tasks.",
                "source_url": "https://developer.apple.com/documentation/foundationmodels/",
            },
            {
                "title": "Deploy cloud callback worker",
                "summary": "Use cloud callback with API key to deploy production automation.",
            },
        ]
    })

    record = upgrade_loop.run_cycle(
        brief, source="test", append=True, path=tmp_path / "upgrade.jsonl"
    )

    assert record["execute"] is False
    assert record["signal_count"] == 2
    assert record["metrics"]["candidate_count"] == 2
    risky = next(c for c in record["candidates"] if c["title"] == "Deploy cloud callback worker")
    assert risky["risk_level"] == "high"
    assert risky["requires_review"] is True
    assert risky["work_order"]["execute"] is False
    assert risky["work_order"]["review_required_count"] >= 1


def test_parse_bullet_signal_and_builds_measurable_tasks():
    signals = upgrade_loop.parse_signals(
        "- GRPO training: add local MLX reward training benchmark for routing quality",
        source="research_note",
    )

    candidates = upgrade_loop.build_candidates(signals)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.category == "training"
    assert candidate.recommended_agent == "backend_engineer"
    tasks = candidate.work_order["tasks"]
    assert [task["agent"] for task in tasks[:3]] == ["researcher", "backend_engineer", "qa_tester"]
    assert any("metric" in task["description"].lower() for task in tasks)


def test_source_url_metadata_does_not_force_review():
    signals = [
        upgrade_loop.UpgradeSignal(
            title="Apple Foundation Models local fast path",
            summary="On-device local generation could reduce latency.",
            source="watchlist",
            source_url="https://developer.apple.com/documentation/foundationmodels/",
            category="apple_on_device",
        )
    ]

    candidate = upgrade_loop.build_candidates(signals)[0]

    assert candidate.risk_level == "low"
    assert candidate.work_order["review_required_count"] == 0
    assert candidate.work_order["tasks"][0]["context"]["source_url"].startswith("https://")
    assert "https://" not in candidate.work_order["tasks"][0]["description"]


def test_decisions_are_recorded_and_summarized(tmp_path: Path):
    path = tmp_path / "upgrade.jsonl"
    record = upgrade_loop.run_cycle(
        "- FastEmbed memory: install local embeddings and measure recall latency",
        source="test",
        path=path,
    )
    candidate_id = record["candidates"][0]["candidate_id"]

    upgrade_loop.record_decision(candidate_id, "accepted", reason="good local-first fit", path=path)
    summary = upgrade_loop.summarize(path)

    assert summary["cycle_count"] == 1
    assert summary["candidate_count"] == 1
    assert summary["decision_count"] == 1
    assert summary["decisions"]["accepted"] == 1


def test_empty_summary_is_stable(tmp_path: Path):
    summary = upgrade_loop.summarize(tmp_path / "missing.jsonl")

    assert summary["cycle_count"] == 0
    assert summary["candidate_count"] == 0
    assert summary["latest_cycle_id"] == ""
