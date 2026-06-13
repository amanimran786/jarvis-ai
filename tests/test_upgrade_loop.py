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


def test_create_tickets_dedupes_and_auto_promotes_low_risk(tmp_path: Path):
    signals = upgrade_loop.parse_signals(
        "- FastEmbed memory: install local embeddings and measure recall latency",
        source="test",
    )
    candidates = upgrade_loop.build_candidates(signals)
    path = tmp_path / "tickets.jsonl"

    first = upgrade_loop.create_tickets(candidates, path=path, auto_promote=True, min_score=1)
    second = upgrade_loop.create_tickets(candidates, path=path, auto_promote=True, min_score=1)

    assert len(first) == 1
    assert second == []
    assert first[0]["status"] == "promoted_to_backlog"
    assert upgrade_loop.read_tickets(path)[0]["candidate_id"] == candidates[0].candidate_id


def test_fetch_watchlist_signals_tracks_changed_digest(tmp_path: Path, monkeypatch):
    state = tmp_path / "state.json"
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return f"body {len(calls)}", "text/plain"

    monkeypatch.setattr(upgrade_loop, "_fetch_url", fake_fetch)
    monkeypatch.setattr(upgrade_loop, "DEFAULT_WATCHLIST", [
        {"title": "Ollama docs", "source_url": "https://example.test/ollama", "category": "local_runtime"},
        {"title": "Local audit", "source_url": "local:pipeline", "category": "evaluation"},
    ])

    first, first_report = upgrade_loop.fetch_watchlist_signals(state_path=state)
    second, second_report = upgrade_loop.fetch_watchlist_signals(state_path=state)

    assert len(first) == 1
    assert first[0].title == "Watchlist changed: Ollama docs"
    assert first_report["https://example.test/ollama"]["changed"] is True
    assert len(second) == 1
    assert second_report["https://example.test/ollama"]["changed"] is True


def test_fetch_watchlist_no_signal_when_digest_unchanged(tmp_path: Path, monkeypatch):
    state = tmp_path / "state.json"

    monkeypatch.setattr(upgrade_loop, "_fetch_url", lambda url: ("same body", "text/plain"))
    monkeypatch.setattr(upgrade_loop, "DEFAULT_WATCHLIST", [
        {"title": "Apple docs", "source_url": "https://example.test/apple", "category": "apple_on_device"},
    ])

    first, _ = upgrade_loop.fetch_watchlist_signals(state_path=state)
    second, report = upgrade_loop.fetch_watchlist_signals(state_path=state)

    assert len(first) == 1
    assert second == []
    assert report["https://example.test/apple"]["changed"] is False


def test_run_cycle_can_create_tickets_and_attach_sandbox_result(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JARVIS_UPGRADE_TICKET_PATH", str(tmp_path / "tickets.jsonl"))
    monkeypatch.setattr(upgrade_loop, "sandbox_smoke", lambda: {"ok": True, "cmd": ["pytest"]})

    record = upgrade_loop.run_cycle(
        "- Agent eval: add trace metric regression test",
        source="test",
        create_ticket_records=True,
        auto_promote=True,
        sandbox_test=True,
        path=tmp_path / "ledger.jsonl",
    )

    assert record["tickets_created"] == 1
    assert record["tickets"][0]["status"] == "promoted_to_backlog"
    assert record["sandbox_smoke"]["ok"] is True


def test_empty_summary_is_stable(tmp_path: Path):
    summary = upgrade_loop.summarize(tmp_path / "missing.jsonl")

    assert summary["cycle_count"] == 0
    assert summary["candidate_count"] == 0
    assert summary["latest_cycle_id"] == ""
