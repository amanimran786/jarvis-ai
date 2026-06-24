from unittest.mock import patch

from config import LOCAL_GLM52_MODEL
from local_runtime.glm52_readiness import evaluate_glm52_readiness
from local_runtime import glm52_readiness
from local_runtime.model_fleet import MODEL_CANDIDATES


def test_fleet_candidate_is_external_only_and_has_no_command():
    candidate = next(item for item in MODEL_CANDIDATES if item.id == "glm_5_2_external_local")

    assert LOCAL_GLM52_MODEL == "glm-5.2"
    assert not candidate.ollama_tag.endswith(":cloud")
    assert candidate.status == "external_hardware_required"
    assert candidate.priority == "low"
    assert candidate.pull_command == ""
    assert "744B total" in candidate.why
    assert "40B active" in candidate.why
    assert "BF16 about 1.4TiB" in candidate.disk_estimate
    assert "FP8 about 704GiB" in candidate.disk_estimate
    assert "No auto-promotion" in candidate.caution
    assert "M4 Pro 48GiB is a no-go" in candidate.caution
    assert "no official local Ollama quant" in candidate.caution
    assert "https://huggingface.co/zai-org/GLM-5.2" in candidate.source_links
    assert "https://ollama.com/library/glm-5.2" in candidate.source_links


def test_m4_pro_48gib_is_not_eligible_even_when_exact_model_is_visible():
    report = evaluate_glm52_readiness(
        memory_gib=48,
        installed_models=["glm-5.2"],
    )

    assert report["exact_model_visible"] is True
    assert report["fp8_memory_ready"] is False
    assert report["eligible_for_eval"] is True
    assert report["local_weight_fit"] is False
    assert report["status"] == "ready_for_eval"
    assert report["known_constraints"]["m4_pro_48gib"] == "no_go"


def test_exact_non_cloud_model_on_external_hardware_is_eligible():
    installed_models = ["glm-5.2", "qwen3:8b"]

    first = evaluate_glm52_readiness(
        memory_gib=800,
        installed_models=installed_models,
    )
    second = evaluate_glm52_readiness(
        memory_gib=800,
        installed_models=installed_models,
    )

    assert first == second
    assert installed_models == ["glm-5.2", "qwen3:8b"]
    assert first["eligible_for_eval"] is True
    assert first["status"] == "ready_for_eval"
    assert first["auto_promotion_allowed"] is False
    assert first["memory_scope"] == "supplied_serving_host"


def test_cloud_or_inexact_model_visibility_is_never_eligible():
    cloud = evaluate_glm52_readiness(
        memory_gib=2048,
        installed_models=["glm-5.2:cloud"],
        model="glm-5.2:cloud",
    )
    inexact = evaluate_glm52_readiness(
        memory_gib=2048,
        installed_models=["glm-5.2:latest", "glm-5.2:cloud"],
    )

    assert cloud["non_cloud_model"] is False
    assert cloud["exact_model_visible"] is False
    assert cloud["eligible_for_eval"] is False
    assert inexact["exact_model_visible"] is False
    assert inexact["eligible_for_eval"] is False

    disguised = evaluate_glm52_readiness(
        memory_gib=2048,
        installed_models=["glm-5.2:744b-cloud-q4"],
        model="glm-5.2:744b-cloud-q4",
    )
    assert disguised["non_cloud_model"] is False
    assert disguised["eligible_for_eval"] is False


def test_configured_alias_still_requires_digest_pin():
    with patch.object(glm52_readiness, "LOCAL_GLM52_MODEL", "lab-frontier-model"):
        assert glm52_readiness.requires_digest_pin("lab-frontier-model") is True
