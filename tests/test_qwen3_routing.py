from unittest.mock import patch
import pytest

import model_router
from brains.brain_ollama import _model_context_limit, _fits_local
from config import LOCAL_DEFAULT, LOCAL_GLM_FLASH, LOCAL_QWEN3_MID


def _best_local_with(models: set[str], prompt: str) -> str:
    with patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch("model_router._cached_local_models", return_value=sorted(models)):
        return model_router._best_local(prompt)


def test_qwen36_context_size():
    # Verify qwen3.6:35b maps to its correct context size
    assert _model_context_limit("qwen3.6:35b") == 262144
    assert _model_context_limit("qwen3.6:35b-instruct") == 262144


def test_glm52_context_size_is_recognized_for_candidate_evals():
    assert _model_context_limit("glm-5.2") == 64_000


def test_glm_flash_escalates_properly():
    # Verify glm-4.7-flash escalates properly when prompt exceeds smaller model context headroom
    # gemma4 has 8192 limit. 8192 * 0.8 = 6553 headroom.
    # 7000 tokens * 4 = 28000 characters prompt.
    large_prompt = "x" * 28000
    
    # When gemma4:e4b is requested but prompt is too large, it escalates to first fallback that fits.
    with patch("brains.brain_ollama._LOCAL_FALLBACK_ORDER", (LOCAL_GLM_FLASH, LOCAL_QWEN3_MID)):
        escalated_model = _fits_local(large_prompt, "gemma4:e4b")
        assert escalated_model == LOCAL_GLM_FLASH


def test_glm_flash_selected_for_coding_route():
    # Coding route: glm-4.7-flash is prioritized
    assert _best_local_with(
        {LOCAL_GLM_FLASH, "qwen2.5-coder:7b", LOCAL_DEFAULT},
        "write a python function to parse csv",
    ) == LOCAL_GLM_FLASH


def test_glm_flash_selected_for_deep_reasoning_route():
    # Deep reasoning / complex route selects glm-4.7-flash as the manager model
    assert _best_local_with(
        {LOCAL_GLM_FLASH, LOCAL_DEFAULT},
        "walk me through a detailed analysis of this architecture decision and evaluate tradeoffs carefully",
    ) == LOCAL_GLM_FLASH
