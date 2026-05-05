"""Tests for the local training dashboard data normalization."""

from __future__ import annotations

import json
import sys
from pathlib import Path


TRAINING_ROOT = Path(__file__).resolve().parent.parent / "training"
if str(TRAINING_ROOT) not in sys.path:
    sys.path.insert(0, str(TRAINING_ROOT))

import dashboard_generator


def test_run_eval_derives_nested_stage_eval():
    run = {"stages": {"eval": {"passed": 9, "total": 10}}}

    result = dashboard_generator._run_eval(run)

    assert result == {"passed": 9, "total": 10, "score": 0.9}


def test_run_examples_counts_pack_when_top_level_is_stale(tmp_path):
    pack = tmp_path / "pack.jsonl"
    pack.write_text('{"a":1}\n\n{"a":2}\n', encoding="utf-8")
    run = {
        "examples_count": 0,
        "stages": {"build": {"pack_path": str(pack)}},
    }

    assert dashboard_generator._run_examples(run) == 2


def test_generate_repairs_stale_run_history_fields(tmp_path, monkeypatch):
    pack = tmp_path / "pack.jsonl"
    pack.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    overnight = tmp_path / "overnight_log.jsonl"
    benchmark = tmp_path / "benchmarks.jsonl"
    state = tmp_path / "overnight_state.json"
    output = tmp_path / "dashboard.html"

    overnight.write_text(
        json.dumps({
            "timestamp": "2026-05-05T06:35:16Z",
            "date": "2026-05-04",
            "promoted": True,
            "eval_passed": 0,
            "eval_total": 0,
            "examples_count": 0,
            "duration_seconds": 0,
            "stages": {
                "build": {"ok": True, "pack_path": str(pack)},
                "training": {"ok": True, "duration_sec": 17.5},
                "eval": {"passed": 312, "total": 313},
                "promotion": {"promoted": True},
            },
        }) + "\n",
        encoding="utf-8",
    )
    benchmark.write_text(
        json.dumps({
            "run_date": "2026-05-04",
            "overall": 0.99,
            "total_passed": 99,
            "total_tests": 100,
            "categories": {},
        }) + "\n",
        encoding="utf-8",
    )
    state.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dashboard_generator, "OVERNIGHT_LOG", overnight)
    monkeypatch.setattr(dashboard_generator, "BENCHMARK_LOG", benchmark)
    monkeypatch.setattr(dashboard_generator, "STATE_FILE", state)
    monkeypatch.setattr(dashboard_generator, "OUTPUT", output)

    dashboard_generator.generate()
    html = output.read_text(encoding="utf-8")

    assert "312/313" in html
    assert ">2</td>" in html
    assert "0m 17s" in html
    assert "99/100 tests passing" in html
    assert "LATEST EVAL" in html


def test_generate_routes_new_dashboard_cards_through_real_grid(tmp_path, monkeypatch):
    overnight = tmp_path / "overnight_log.jsonl"
    benchmark = tmp_path / "benchmarks.jsonl"
    state = tmp_path / "overnight_state.json"
    output = tmp_path / "dashboard.html"
    routing_log = tmp_path / "routing_log.jsonl"

    overnight.write_text("", encoding="utf-8")
    benchmark.write_text("", encoding="utf-8")
    state.write_text("{}", encoding="utf-8")
    routing_log.write_text(
        json.dumps({"tier": "local"}) + "\n" + json.dumps({"tier": "sonnet"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(dashboard_generator, "OVERNIGHT_LOG", overnight)
    monkeypatch.setattr(dashboard_generator, "BENCHMARK_LOG", benchmark)
    monkeypatch.setattr(dashboard_generator, "STATE_FILE", state)
    monkeypatch.setattr(dashboard_generator, "OUTPUT", output)
    monkeypatch.setattr(dashboard_generator, "TRAINING_ROOT", tmp_path / "training")

    dashboard_generator.generate()
    html = output.read_text(encoding="utf-8")

    assert 'class="grid" style="grid-template-columns' not in html
    assert 'class="grid-2" style="margin-top:20px"' in html
    assert "MODEL ROUTING" in html
    assert "50.0%" in html
