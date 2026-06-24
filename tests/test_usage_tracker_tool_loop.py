from unittest.mock import patch

import usage_tracker


def _row(seq, tool_loop=None):
    metadata = {} if tool_loop is None else {"tool_loop": tool_loop}
    return {
        "seq": seq,
        "timestamp": f"2026-06-20T12:{seq:02d}:00+00:00",
        "provider": "ollama",
        "model": "glm-4.7-flash",
        "local": True,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "metadata": metadata,
    }


def test_summarize_aggregates_mixed_legacy_and_tool_loop_rows():
    rows = [
        _row(1),
        _row(2, {
            "invocation_id": "inv-a",
            "call_type": "decision",
            "iteration": 1,
            "tool_names": ["search_vault", "read_file"],
            "truncated": True,
            "dropped_tool_round_count": 2,
            "dropped_message_count": 4,
            "dropped_estimated_tokens": 900,
            "governor_eligible": True,
            "governor_applied": True,
            "max_iteration_exhausted": False,
            "error": False,
        }),
        _row(3, {
            "invocation_id": "inv-a",
            "call_type": "synthesis",
            "tool_names": [],
            "governor_eligible": False,
            "governor_applied": False,
            "error": False,
        }),
        _row(4, {
            "invocation_id": "inv-b",
            "call_type": "decision",
            "iteration": 1,
            "tool_names": ["search_vault"],
            "dropped_message_count": 1,
            "dropped_estimated_tokens": 100,
            "governor_eligible": True,
            "governor_applied": False,
            "max_iteration_exhausted": True,
            "error": True,
        }),
    ]

    with patch.object(usage_tracker, "entries", return_value=rows):
        tool_loop = usage_tracker.summarize(include_recent=0)["tool_loop"]

    recent = tool_loop.pop("recent")
    assert tool_loop == {
        "invocation_count": 2,
        "provider_call_count": 3,
        "decision_call_count": 2,
        "synthesis_call_count": 1,
        "tool_iteration_count": 2,
        "tool_call_count": 3,
        "tool_calls_by_name": {"search_vault": 2, "read_file": 1},
        "truncated_call_count": 1,
        "dropped_tool_round_count": 2,
        "dropped_message_count": 5,
        "dropped_estimated_tokens": 1000,
        "governor_eligible_call_count": 2,
        "governor_applied_call_count": 1,
        "governor_coverage_ratio": 0.5,
        "max_iteration_exhaustion_count": 1,
        "error_call_count": 1,
    }
    assert len(recent) == 3


def test_tool_loop_recent_is_sanitized_and_capped_at_ten():
    rows = [
        _row(seq, {
            "invocation_id": f"inv-{seq}",
            "call_type": "decision",
            "iteration": seq,
            "tool_names": ["run_tool"],
            "truncated": False,
        })
        for seq in range(1, 13)
    ]

    with patch.object(usage_tracker, "entries", return_value=rows):
        recent = usage_tracker.summarize()["tool_loop"]["recent"]

    assert len(recent) == 10
    assert recent[0]["invocation_id"] == "inv-3"
    assert recent[-1]["invocation_id"] == "inv-12"
    assert recent[-1]["tool_names"] == ["run_tool"]
    assert all("args" not in item and "results" not in item for item in recent)
