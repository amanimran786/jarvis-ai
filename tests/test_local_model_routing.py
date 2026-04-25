from unittest.mock import patch

import model_router
from config import LOCAL_DEFAULT


def _best_local_with(models: set[str], prompt: str) -> str:
    with patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch("model_router._cached_local_models", return_value=sorted(models)):
        return model_router._best_local(prompt)


def test_coding_tasks_prefer_installed_qwen36_before_baseline_coder():
    assert _best_local_with(
        {"qwen3.6:35b", "qwen2.5-coder:7b", LOCAL_DEFAULT},
        "debug this python function",
    ) == "qwen3.6:35b"


def test_deep_reasoning_can_use_gemma4_workstation_lane():
    assert _best_local_with(
        {"gemma4:31b", "gemma4:26b", LOCAL_DEFAULT},
        "walk me through a detailed analysis of this architecture decision and evaluate tradeoffs carefully",
    ) == "gemma4:31b"


def test_simple_chat_stays_on_configured_default_not_big_eval_models():
    assert _best_local_with(
        {"gemma4:31b", "gemma4:26b", LOCAL_DEFAULT},
        "hello",
    ) == LOCAL_DEFAULT


def test_coding_falls_back_to_existing_fast_coder_when_new_models_absent():
    assert _best_local_with(
        {"qwen2.5-coder:7b", LOCAL_DEFAULT},
        "fix this test failure",
    ) == "qwen2.5-coder:7b"
