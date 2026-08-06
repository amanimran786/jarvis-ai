import importlib
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if isinstance(sys.modules.get("config"), MagicMock):
    sys.modules.pop("config", None)
config = importlib.import_module("config")

if isinstance(sys.modules.get("model_router"), MagicMock):
    sys.modules.pop("model_router", None)
model_router = importlib.import_module("model_router")
from brains import brain_ollama


def test_missing_specialist_models_accepts_latest_for_untagged_model():
    assert config.missing_specialist_models(
        ["devstral:latest", "qwen3:30b-a3b"],
        ["devstral", "qwen3:30b-a3b"],
    ) == ()


def test_missing_specialist_models_requires_exact_model_reference():
    assert config.missing_specialist_models(
        ["my-devstral:latest", "qwen3:30b-a3b-extra"],
        ["devstral", "qwen3:30b-a3b"],
    ) == ("devstral", "qwen3:30b-a3b")


def test_missing_specialist_models_normalizes_and_deduplicates_configuration():
    assert config.missing_specialist_models(
        [" DEVSTRAL:LATEST "],
        ["devstral", "", "devstral"],
    ) == ()


def test_startup_check_warns_without_failing_when_models_are_missing(caplog):
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        missing = config.warn_missing_specialist_models(["devstral:latest"])

    assert missing == ("qwen3:30b-a3b",)
    assert "Missing expected Ollama specialist model(s): qwen3:30b-a3b" in caplog.text
    assert "Routing will use installed local fallbacks" in caplog.text


def test_startup_check_logs_ready_inventory(caplog):
    with caplog.at_level(logging.INFO, logger=config.__name__):
        missing = config.warn_missing_specialist_models(
            ["devstral:latest", "qwen3:30b-a3b"]
        )

    assert missing == ()
    assert "Ollama specialist models ready" in caplog.text


def test_startup_check_fails_open_when_inventory_probe_raises(caplog):
    with patch(
        "brains.brain_ollama.local_model_inventory",
        side_effect=RuntimeError("ollama unavailable"),
    ), caplog.at_level(logging.WARNING, logger=config.__name__):
        missing = config.warn_missing_specialist_models()

    assert missing == config.EXPECTED_SPECIALIST_MODELS
    assert "Ollama specialist inventory check failed" in caplog.text


def test_startup_check_distinguishes_unreachable_ollama(caplog):
    with patch(
        "brains.brain_ollama.local_model_inventory",
        return_value=(False, []),
    ), caplog.at_level(logging.WARNING, logger=config.__name__):
        missing = config.warn_missing_specialist_models()

    assert missing == config.EXPECTED_SPECIALIST_MODELS
    assert "Ollama is unavailable" in caplog.text
    assert "Missing expected Ollama specialist" not in caplog.text


def test_router_rejects_similarly_named_or_different_tagged_models():
    available = ["my-devstral:latest", "qwen3:8b", "qwen3:30b-a3b-extra"]

    assert model_router._has_model("devstral", available) is False
    assert model_router._has_model("qwen3:30b-a3b", available) is False


def test_configured_specialist_roles_win_routing_precedence():
    available = [
        "custom-coder:latest",
        "devstral:latest",
        "custom-reasoner:latest",
        "qwen3:30b-a3b",
    ]
    with patch.object(model_router, "LOCAL_CODER", "custom-coder"), \
         patch.object(model_router, "LOCAL_REASONING", "custom-reasoner"), \
         patch.object(model_router.local_model_eval, "promoted_model", return_value=None), \
         patch.object(model_router, "_cached_local_models", return_value=available):
        code_model = model_router._best_local("debug this Python test")
        reasoning_model = model_router._best_local(
            "evaluate tradeoffs in this architecture decision"
        )

    assert code_model == "custom-coder"
    assert reasoning_model == "custom-reasoner"


def test_exact_specialist_selection_never_substitutes_unrelated_model():
    client = SimpleNamespace(
        list=lambda: SimpleNamespace(
            models=[SimpleNamespace(model="qwen3:8b")]
        )
    )
    with patch.object(brain_ollama, "_client", return_value=client):
        try:
            brain_ollama.get_best_available(
                "qwen3:30b-a3b",
                require_preferred=True,
            )
        except RuntimeError as exc:
            assert "Exact local model is unavailable" in str(exc)
            assert "ollama pull qwen3:30b-a3b" in str(exc)
            assert "ollama serve" not in str(exc)
        else:
            raise AssertionError("missing specialist must fail closed")


def test_exact_specialist_selection_returns_installed_reference():
    client = SimpleNamespace(
        list=lambda: SimpleNamespace(
            models=[SimpleNamespace(model="devstral:latest")]
        )
    )
    with patch.object(brain_ollama, "_client", return_value=client):
        selected = brain_ollama.get_best_available(
            "devstral",
            require_preferred=True,
        )

    assert selected == "devstral:latest"


def test_inventory_reports_unreachable_when_list_fails_after_liveness():
    client = SimpleNamespace(
        list=lambda: (_ for _ in ()).throw(RuntimeError("connection lost"))
    )
    with patch.object(brain_ollama, "_check_ollama_liveness", return_value=True), \
         patch.object(brain_ollama, "_client", return_value=client):
        reachable, models = brain_ollama.local_model_inventory()

    assert reachable is False
    assert models == []
