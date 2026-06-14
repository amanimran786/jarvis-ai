"""Hermetic unit tests for cost_policy decision logic.

The two module-level dependencies (`evals`, `usage_tracker`) are replaced with
MagicMocks in sys.modules *before* importing cost_policy, so importing and
exercising the policy never makes real calls.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Inject mocks before importing cost_policy so its module-level
# `import evals` / `import usage_tracker` resolve to hermetic stand-ins.
sys.modules["evals"] = MagicMock()
sys.modules["usage_tracker"] = MagicMock()

import cost_policy  # noqa: E402


@pytest.fixture(autouse=True)
def reset_dependency_mocks():
    """Reset the mocked deps to sane, low-pressure defaults before each test."""
    cost_policy.usage_tracker.summarize.return_value = {
        "estimated_cost_usd": 0.0,
        "cloud_call_count": 0,
        "local_call_count": 0,
    }
    cost_policy.evals.recent_failures.return_value = []
    yield


def test_policy_status_returns_dict():
    # Act
    status = cost_policy.policy_status()

    # Assert
    assert isinstance(status, dict)
    for key in (
        "usage_24h",
        "recent_failure_categories",
        "repeated_failure_categories",
        "budget_pressure",
        "hard_budget",
        "training_action",
        "training_reason",
    ):
        assert key in status


def test_route_decision_returns_dict_with_expected_keys():
    # Act
    result = cost_policy.route_decision(
        "hello there", base_tier="mini", local_available=True
    )

    # Assert
    assert isinstance(result, dict)
    assert set(result.keys()) == {"tier", "provider", "reason"}


def test_route_decision_no_local_uses_cloud_base():
    # Act
    result = cost_policy.route_decision(
        "hello there", base_tier="mini", local_available=False
    )

    # Assert
    assert result["provider"] == "cloud"
    assert result["tier"] == "mini"


def test_route_decision_local_available_simple_chat_stays_local():
    # Act
    result = cost_policy.route_decision(
        "what time is it", base_tier="mini", tool="chat", local_available=True
    )

    # Assert
    assert result["provider"] == "local"
    assert result["tier"] == "mini"


def test_route_decision_high_tier_stays_cloud():
    # Act
    result = cost_policy.route_decision(
        "simple question", base_tier="opus", local_available=True
    )

    # Assert
    assert result["provider"] == "cloud"
    assert result["tier"] == "opus"


def test_route_decision_high_stakes_does_not_cheap_out():
    # Act
    result = cost_policy.route_decision(
        "review this authentication flow for vulnerabilities",
        base_tier="mini",
        local_available=True,
    )

    # Assert
    assert result["provider"] == "cloud"
    assert result["tier"] == "haiku"
