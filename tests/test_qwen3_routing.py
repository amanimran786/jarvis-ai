from unittest.mock import patch
import pytest

import model_router
from brains.brain_ollama import _model_context_limit, _fits_local
from config import LOCAL_DEFAULT, LOCAL_QWEN3_6


def _best_local_with(models: set[str], prompt: str) -> str:
    with patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch("model_router._cached_local_models", return_value=sorted(models)):
        return model_router._best_local(prompt)


def test_qwen36_context_size():
    # Verify qwen3.6:35b maps to its correct context size
    assert _model_context_limit("qwen3.6:35b") == 262144
    assert _model_context_limit("qwen3.6:35b-instruct") == 262144


def test_qwen36_escalates_properly():
    # Verify qwen3.6:35b escalates properly when prompt exceeds smaller model context headroom
    # gemma4 has 8192 limit. 8192 * 0.8 = 6553 headroom.
    # 7000 tokens * 4 = 28000 characters prompt.
    large_prompt = "x" * 28000
    
    # When gemma4:e4b is requested but prompt is too large, it escalates to first fallback that fits.
    # _LOCAL_FALLBACK_ORDER starts with "qwen3.6:35b".
    with patch("brains.brain_ollama._LOCAL_FALLBACK_ORDER", ("qwen3.6:35b", "qwen3-coder:30b")):
        escalated_model = _fits_local(large_prompt, "gemma4:e4b")
        assert escalated_model == "qwen3.6:35b"


def test_qwen36_selected_for_coding_route():
    # Coding route: qwen3.6:35b is prioritized
    assert _best_local_with(
        {"qwen3.6:35b", "qwen2.5-coder:7b", LOCAL_DEFAULT},
        "write a python function to parse csv",
    ) == "qwen3.6:35b"


def test_qwen36_selected_for_deep_reasoning_route():
    # Deep reasoning / complex route selects qwen3.6:35b when deepseek/gemma4 strong are missing
    assert _best_local_with(
        {"qwen3.6:35b", LOCAL_DEFAULT},
        "walk me through a detailed analysis of this architecture decision and evaluate tradeoffs carefully",
    ) == "qwen3.6:35b"
