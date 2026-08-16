from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

import config
import main
import model_router
import orchestrator
from brains import brain_ollama
from local_runtime import local_model_benchmark


def _structured_response(content: str = '{"tool":"notes","confidence":0.9,"action":"read","params":{}}'):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        prompt_eval_count=32,
        eval_count=16,
    )


def test_classifier_uses_dedicated_exact_model_and_bounded_request():
    expected_model = getattr(config, "LOCAL_CLASSIFIER", "qwen3.5:4b")

    with patch(
        "brains.brain_ollama.ask_local_structured",
        return_value='{"tool":"notes","confidence":0.9,"action":"read","params":{}}',
    ) as ask_structured:
        decision = orchestrator._classify_with_local_structured("Read my latest note")

    assert decision is not None
    assert decision.tool == "notes"
    ask_structured.assert_called_once()
    kwargs = ask_structured.call_args.kwargs
    assert kwargs["model"] == expected_model
    assert kwargs["strict_model"] is True
    assert kwargs["max_context"] == 4096
    assert kwargs["max_output"] == 96
    assert kwargs["timeout_seconds"] == 3.0
    assert kwargs["think"] is False
    assert kwargs["keep_alive"] == "5m"


def test_local_capabilities_reports_exact_classifier_role():
    classifier = config.LOCAL_CLASSIFIER

    with patch.object(
        brain_ollama,
        "list_local_models",
        return_value=[classifier],
    ), patch.object(
        brain_ollama,
        "get_best_available",
        side_effect=lambda preferred, **_kwargs: preferred,
    ), patch.object(
        brain_ollama,
        "_vision_runtime_status",
        return_value={"state": "unavailable", "detail": "not installed"},
    ):
        capabilities = brain_ollama.local_capabilities()

    assert capabilities["selected_classifier"] == classifier


def test_timed_classifier_delegates_timeout_to_transport_without_worker_thread():
    expected = orchestrator.ToolDecision("notes", 0.9, "read")

    with patch.object(
        orchestrator,
        "_classify_with_local_structured",
        return_value=expected,
    ) as classify_local:
        result = orchestrator._classify_with_local_structured_timed(
            "Read my latest note",
            timeout=2.5,
        )

    assert result is expected
    classify_local.assert_called_once_with(
        "Read my latest note",
        timeout_seconds=2.5,
    )


def test_unambiguous_short_knowledge_queries_bypass_model_classification():
    define = orchestrator._local_short_query_classify("define database indexing")
    explain = orchestrator._local_short_query_classify(
        "explain optimistic versus pessimistic locking"
    )

    assert define is not None and define.tool == "chat"
    assert explain is not None and explain.tool == "chat"
    assert orchestrator._local_short_query_classify("explain my latest email") is None


def test_structured_call_enforces_exact_model_context_output_and_transport_bounds():
    client = SimpleNamespace()
    client.chat = lambda **_kwargs: _structured_response()

    with patch.object(brain_ollama, "_check_ollama_liveness", return_value=True), \
         patch.object(brain_ollama, "_exact_available_model", return_value="qwen3.5:4b") as exact_model, \
         patch.object(brain_ollama, "_structured_client", return_value=client) as structured_client, \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        content = brain_ollama.ask_local_structured(
            "Read my latest note",
            schema={"type": "object"},
            model="qwen3.5:4b",
            strict_model=True,
            max_context=4096,
            max_output=96,
            timeout_seconds=3.0,
            think=False,
            keep_alive="5m",
        )

    assert content.startswith("{")
    exact_model.assert_called_once_with("qwen3.5:4b")
    structured_client.assert_called_once_with(3.0)


def test_structured_call_passes_bounded_options_to_ollama():
    calls = []

    class Client:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return _structured_response()

    with patch.object(brain_ollama, "_check_ollama_liveness", return_value=True), \
         patch.object(brain_ollama, "_exact_available_model", return_value="qwen3.5:4b"), \
         patch.object(brain_ollama, "_structured_client", return_value=Client()), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        brain_ollama.ask_local_structured(
            "Read my latest note",
            schema={"type": "object"},
            model="qwen3.5:4b",
            strict_model=True,
            max_context=4096,
            max_output=96,
            timeout_seconds=3.0,
            think=False,
            keep_alive="5m",
        )

    assert len(calls) == 1
    assert calls[0]["options"]["num_ctx"] == 4096
    assert calls[0]["options"]["num_predict"] == 96
    assert calls[0]["think"] is False
    assert calls[0]["keep_alive"] == "5m"


def test_general_chat_prefers_configured_default_before_glm_fallback():
    with patch.object(model_router, "LOCAL_DEFAULT", "qwen3:8b"), \
         patch.object(model_router, "LOCAL_GLM_FLASH", "glm-4.7-flash"), \
         patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch(
             "model_router._cached_local_models",
             return_value=["glm-4.7-flash", "qwen3:8b"],
         ):
        selected = model_router._best_local("Summarize this in two sentences")

    assert selected == "qwen3:8b"


def test_long_routine_prompt_does_not_evict_default_for_reasoning_model():
    routine_extraction = " ".join(
        ["Extract durable facts from this ordinary conversation turn"] * 8
    )
    with patch.object(model_router, "LOCAL_DEFAULT", "qwen3:8b"), \
         patch.object(model_router, "LOCAL_REASONING", "qwen3:30b-a3b"), \
         patch("model_router.local_model_eval.promoted_model", return_value=None), \
         patch(
             "model_router._cached_local_models",
             return_value=["qwen3:8b", "qwen3:30b-a3b"],
         ):
        routine = model_router._best_local(routine_extraction)
        explicit_deep = model_router._best_local(
            "Give me a deep dive and evaluate tradeoffs for this architecture decision"
        )

    assert routine == "qwen3:8b"
    assert explicit_deep == "qwen3:30b-a3b"


def test_fast_context_is_limited_to_local_default_chat_lane():
    assert model_router._use_fast_local_context(
        model=config.LOCAL_DEFAULT,
        tool="chat",
        local=True,
    ) is True
    assert model_router._use_fast_local_context(
        model=config.LOCAL_CODER,
        tool="chat",
        local=True,
    ) is False
    assert model_router._use_fast_local_context(
        model=config.LOCAL_DEFAULT,
        tool="memory",
        local=True,
    ) is False
    assert model_router._use_fast_local_context(
        model=config.LOCAL_DEFAULT,
        tool="chat",
        local=False,
    ) is False


def test_default_chat_uses_compact_memory_and_bounded_non_reasoning_stream():
    with patch.object(model_router, "_current_mode", "open-source"), \
         patch.object(model_router, "_has_local", return_value=True), \
         patch.object(model_router, "_best_local", return_value=config.LOCAL_DEFAULT), \
         patch.object(model_router, "forced_model_status", return_value={"active": False}), \
         patch.object(model_router._core_brain, "core_context", return_value="Expanded core") as expanded_core, \
         patch.object(model_router.skills, "build_system_extra", return_value=("", [])), \
         patch.object(model_router, "_user_snapshot_grounding", return_value="Compact profile"), \
         patch.object(model_router._mem, "get_context") as working_memory, \
         patch.object(model_router._repeat_context, "context_for_prompt") as repeat_context, \
         patch.object(model_router.vault, "build_context") as vault_context, \
         patch.object(model_router._gctx, "context_for_query") as graph_context, \
         patch.object(model_router._smem, "retrieve") as semantic_context, \
         patch.object(model_router._m0, "search") as episodic_context, \
         patch("brains.brain_ollama.start_keepalive"), \
         patch.object(model_router, "ask_local_stream", return_value=iter(["Fast answer."])) as ask_local:
        stream, _label = model_router.smart_stream(
            "Define database indexing.",
            tool="chat",
            local_only=True,
        )
        assert "".join(stream) == "Fast answer."

    repeat_context.assert_not_called()
    vault_context.assert_not_called()
    graph_context.assert_not_called()
    semantic_context.assert_not_called()
    episodic_context.assert_not_called()
    working_memory.assert_not_called()
    expanded_core.assert_not_called()
    kwargs = ask_local.call_args.kwargs
    assert "Compact profile" in kwargs["system_extra"]
    assert "Expanded core" not in kwargs["system_extra"]
    assert kwargs["include_memory"] is False
    assert kwargs["max_context"] == config.LOCAL_FAST_CHAT_CONTEXT_TOKENS
    assert kwargs["max_output"] == config.LOCAL_FAST_CHAT_MAX_TOKENS
    assert kwargs["think"] is False
    assert kwargs["keep_alive"] == "5m"


def test_stream_call_applies_fast_chat_transport_options():
    calls = []

    class Client:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return iter([
                SimpleNamespace(
                    message=SimpleNamespace(content="Done."),
                    prompt_eval_count=20,
                    eval_count=4,
                )
            ])

    with patch.object(brain_ollama, "_check_ollama_liveness", return_value=True), \
         patch.object(brain_ollama, "_client", return_value=Client()), \
         patch.object(brain_ollama, "_fits_local", side_effect=lambda _prompt, model: model), \
         patch.object(brain_ollama, "get_best_available", side_effect=lambda model: model), \
         patch.object(brain_ollama, "SYSTEM_PROMPT", "SYSTEM"), \
         patch.object(brain_ollama.context_budget, "target_tokens_for", return_value=24_000), \
         patch.object(brain_ollama.usage_tracker, "record"):
        chunks = list(brain_ollama.ask_local_stream(
            "Define indexing.",
            model=config.LOCAL_DEFAULT,
            include_memory=False,
            max_context=4096,
            max_output=384,
            think=False,
            keep_alive="5m",
        ))

    assert chunks == ["Done."]
    assert calls[0]["options"]["num_ctx"] == 4096
    assert calls[0]["options"]["num_predict"] == 384
    assert calls[0]["think"] is False
    assert calls[0]["keep_alive"] == "5m"


@pytest.mark.parametrize("bound", ["max_context", "max_output"])
def test_stream_call_rejects_nonpositive_request_bounds(bound):
    kwargs = {bound: 0}
    with patch.object(brain_ollama, "_check_ollama_liveness", return_value=True), \
         pytest.raises(ValueError):
        list(brain_ollama.ask_local_stream(
            "Define indexing.",
            include_memory=False,
            **kwargs,
        ))


def test_structured_client_rejects_nonpositive_transport_timeout():
    with pytest.raises(ValueError):
        brain_ollama._structured_client(0)


def test_warm_model_cache_uses_exact_role_and_bounded_context():
    calls = []

    class Client:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return _structured_response("ready")

    with patch.object(
        brain_ollama,
        "get_best_available",
        return_value="qwen3.5:4b",
    ) as get_best, patch.object(brain_ollama, "_client", return_value=Client()):
        brain_ollama.warm_model_cache(
            "qwen3.5:4b",
            require_preferred=True,
            max_context=4096,
        )

    get_best.assert_called_once_with("qwen3.5:4b", require_preferred=True)
    assert calls[0]["model"] == "qwen3.5:4b"
    assert calls[0]["options"]["num_ctx"] == 4096
    assert calls[0]["options"]["num_predict"] == 1
    assert calls[0]["think"] is False
    assert calls[0]["keep_alive"] == "5m"


def test_warm_model_cache_defaults_to_general_role():
    class Client:
        def chat(self, **_kwargs):
            return _structured_response("ready")

    with patch.object(
        brain_ollama,
        "get_best_available",
        return_value=config.LOCAL_DEFAULT,
    ) as get_best, patch.object(brain_ollama, "_client", return_value=Client()):
        brain_ollama.warm_model_cache()

    get_best.assert_called_once_with(config.LOCAL_DEFAULT, require_preferred=False)


def test_deferred_startup_prewarms_classifier_and_default_before_keepalive():
    classifier = getattr(config, "LOCAL_CLASSIFIER", "qwen3.5:4b")
    default = config.LOCAL_DEFAULT

    with patch("config.warn_missing_specialist_models"), \
         patch("local_runtime.local_stt.preload"), \
         patch("local_runtime.local_kokoro_subprocess_tts.prewarm_phrase_cache"), \
         patch("brains.brain_ollama.get_best_available", side_effect=lambda model, **_kwargs: model), \
         patch("brains.brain_ollama.warm_model_cache") as warm_model, \
         patch("brains.brain_ollama.start_keepalive") as keepalive, \
         patch.dict("os.environ", {
             "JARVIS_REQUEST_STARTUP_PERMISSIONS": "0",
             "JARVIS_REQUEST_STARTUP_ADMIN": "0",
         }):
        main._run_deferred_startup_tasks()

    assert warm_model.call_args_list == [
        call(
            classifier,
            require_preferred=True,
            max_context=getattr(config, "LOCAL_CLASSIFIER_CONTEXT_TOKENS", 4096),
        ),
        call(default, require_preferred=True, max_context=None),
    ]
    keepalive.assert_called_once_with(default)


def test_local_benchmark_uses_fixed_context_and_output_for_requested_models():
    calls = []
    response = SimpleNamespace(
        message=SimpleNamespace(content="bounded answer"),
        load_duration=25_000_000,
        eval_count=20,
        eval_duration=500_000_000,
    )

    class Client:
        def chat(self, **kwargs):
            calls.append(kwargs)
            return response

    with patch.object(
        local_model_benchmark,
        "list_local_models",
        return_value=["qwen3:8b"],
    ), patch.object(local_model_benchmark, "get_client", return_value=Client()):
        result = local_model_benchmark.run_benchmark(
            prompts=["Explain one database lock."],
            models=["qwen3:8b"],
            max_context=4096,
            max_output=64,
        )

    assert result["ok"] is True
    assert result["rows"][0]["avg_tokens_per_second"] == 40.0
    assert calls[0]["options"] == {
        "num_ctx": 4096,
        "num_predict": 64,
        "temperature": 0,
    }
    assert calls[0]["think"] is False
