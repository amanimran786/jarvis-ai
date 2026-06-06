from unittest.mock import patch

import model_router
from config import LOCAL_DEFAULT, LOCAL_GLM_FLASH, LOCAL_QWEN3_MID


def _best_local_with(models: set[str], prompt: str) -> str:
    with patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch("model_router._cached_local_models", return_value=sorted(models)):
        return model_router._best_local(prompt)


def test_coding_tasks_prefer_installed_glm_flash_before_legacy_coders():
    assert _best_local_with(
        {LOCAL_GLM_FLASH, "qwen2.5-coder:7b", LOCAL_DEFAULT},
        "debug this python function",
    ) == LOCAL_GLM_FLASH


def test_deep_reasoning_uses_glm_flash_manager_lane():
    assert _best_local_with(
        {LOCAL_GLM_FLASH, "gemma4:31b", LOCAL_DEFAULT},
        "walk me through a detailed analysis of this architecture decision and evaluate tradeoffs carefully",
    ) == LOCAL_GLM_FLASH


def test_simple_chat_stays_on_configured_default_not_big_eval_models():
    assert _best_local_with(
        {"gemma4:31b", "gemma4:26b", LOCAL_DEFAULT},
        "hello",
    ) == LOCAL_DEFAULT


def test_coding_falls_back_to_qwen8_when_glm_absent():
    assert _best_local_with(
        {LOCAL_QWEN3_MID},
        "fix this test failure",
    ) == LOCAL_QWEN3_MID


def test_prune_prompt_dynamics():
    from brains.brain_ollama import _prune_prompt
    
    mock_system = (
        "You are Jarvis.\n\n"
        "Voice output rules — TTS reads every response aloud:\n"
        "- No markdown: no **, ##, bullets, or code fences.\n\n"
        "Honesty rules:\n"
        "- Never invent capabilities.\n\n"
        "Terminal console: Plain-English console requests are action intents...\n\n"
        "Message composition: compose message bodies exactly as requested..."
    )
    
    # Coding query: should strip voice rules and message composition rules
    coding_res = _prune_prompt("fix this python script", mock_system)
    assert "Voice output rules" not in coding_res
    assert "Message composition" not in coding_res
    assert "Terminal console" in coding_res
    assert "Honesty rules" in coding_res
    
    # Message compose query: should strip terminal console
    msg_res = _prune_prompt("send an email to Alice", mock_system)
    assert "Terminal console" not in msg_res
    assert "Message composition" in msg_res
    assert "Voice output rules" in msg_res
    
    # Casual/general chat query: should strip terminal console and message composition
    chat_res = _prune_prompt("what is the capital of France?", mock_system)
    assert "Terminal console" not in chat_res
    assert "Message composition" not in chat_res
    assert "Voice output rules" in chat_res
    assert "Honesty rules" in chat_res
