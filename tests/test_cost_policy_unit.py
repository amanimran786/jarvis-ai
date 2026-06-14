"""Hermetic unit tests for cost_policy decision logic.

The two module-level dependencies (`evals`, `usage_tracker`) are replaced with
MagicMocks *after collection* (in setup_module) so that tests from other files
that run before these tests are not contaminated by a polluted sys.modules.
cost_policy is force-reimported in setup_module after the mocks are in place so
its internal references resolve to the mock stand-ins.
"""

import importlib
import sys
from unittest.mock import MagicMock

import pytest

# These will be set by setup_module so tests can reference them.
cost_policy = None  # type: ignore[assignment]

_orig_evals = None
_orig_usage_tracker = None
_orig_cost_policy = None


def setup_module(module):
    global cost_policy, _orig_evals, _orig_usage_tracker, _orig_cost_policy
    _orig_evals = sys.modules.get("evals")
    _orig_usage_tracker = sys.modules.get("usage_tracker")
    _orig_cost_policy = sys.modules.get("cost_policy")

    # Install mocks AFTER collection so earlier-alphabetical test files are not affected.
    sys.modules["evals"] = MagicMock()
    sys.modules["usage_tracker"] = MagicMock()

    # Force reimport so cost_policy.usage_tracker / cost_policy.evals point to the mocks.
    sys.modules.pop("cost_policy", None)
    cost_policy = importlib.import_module("cost_policy")
    module.cost_policy = cost_policy


def teardown_module(module):
    # Restore sys.modules so later test files see the real modules.
    for key, orig in [
        ("evals", _orig_evals),
        ("usage_tracker", _orig_usage_tracker),
        ("cost_policy", _orig_cost_policy),
    ]:
        if orig is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = orig


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
