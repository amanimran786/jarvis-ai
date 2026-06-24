"""Pure readiness checks for the non-default GLM 5.2 evaluation candidate."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable
from typing import Any

from config import LOCAL_GLM52_DIGEST, LOCAL_GLM52_MODEL


GLM52_TOTAL_PARAMETERS_BILLION = 744
GLM52_REPORTED_STORED_PARAMETERS_BILLION = 753
GLM52_ACTIVE_PARAMETERS_BILLION = 40
GLM52_BF16_ESTIMATE_TIB = 1.4
GLM52_FP8_ESTIMATE_GIB = 704
GLM52_FP8_RUNTIME_FLOOR_GIB = 800

GLM52_SOURCE_LINKS = {
    "official_model_card": "https://huggingface.co/zai-org/GLM-5.2",
    "official_fp8_weights": "https://huggingface.co/zai-org/GLM-5.2-FP8",
    "official_ollama_listing": "https://ollama.com/library/glm-5.2",
}


def system_memory_gib() -> float:
    """Return physical memory without shelling out or probing an endpoint."""
    pages = int(os.sysconf("SC_PHYS_PAGES"))
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    return round((pages * page_size) / (1024 ** 3), 3)


def _is_cloud_model(model: str) -> bool:
    tag = (model or "").strip().lower().rsplit(":", 1)[-1]
    parts = tag.replace("_", "-").replace(".", "-").split("-")
    return "cloud" in {part for part in parts if part}


def normalize_model_tag(model: str) -> str:
    value = (model or "").strip()
    return value[:-7] if value.endswith(":latest") else value


def requires_digest_pin(model: str) -> bool:
    normalized = normalize_model_tag(model).lower()
    configured = normalize_model_tag(LOCAL_GLM52_MODEL).lower()
    return bool(normalized) and (normalized == configured or "glm-5.2" in normalized)


def validate_candidate_digest(
    client: Any,
    model: str,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Verify immutable endpoint identity for GLM 5.2 or its configured alias."""
    if not requires_digest_pin(model):
        return {"ok": True, "required": False}
    pinned = (expected_digest if expected_digest is not None else LOCAL_GLM52_DIGEST).strip()
    if not pinned:
        return {"ok": False, "required": True, "error": "LOCAL_GLM52_DIGEST is required."}
    try:
        response = client.list()
        items = response.get("models", []) if isinstance(response, dict) else getattr(response, "models", [])
        visible = ""
        for item in items or []:
            name = item.get("model", "") if isinstance(item, dict) else getattr(item, "model", "")
            if normalize_model_tag(str(name)) != normalize_model_tag(model):
                continue
            visible = str(
                item.get("digest", "") if isinstance(item, dict) else getattr(item, "digest", "")
            )
            break
    except Exception as exc:
        return {
            "ok": False,
            "required": True,
            "error": f"Endpoint model metadata failed: {type(exc).__name__}",
        }
    if visible != pinned:
        return {
            "ok": False,
            "required": True,
            "error": "Configured GLM 5.2 digest does not match the endpoint model.",
        }
    return {"ok": True, "required": True, "digest_verified": True}


def evaluate_glm52_readiness(
    *,
    memory_gib: float,
    installed_models: Iterable[str],
    model: str = LOCAL_GLM52_MODEL,
) -> dict[str, Any]:
    """Return readiness from supplied facts without probing or changing the host."""
    available_memory_gib = float(memory_gib)
    if not math.isfinite(available_memory_gib) or available_memory_gib < 0:
        raise ValueError("memory_gib must be a finite non-negative number")

    configured_model = model.strip()
    visible_models = tuple(installed_models)
    non_cloud_model = bool(configured_model) and not _is_cloud_model(configured_model)
    exact_model_visible = non_cloud_model and configured_model in visible_models
    fp8_memory_ready = available_memory_gib >= GLM52_FP8_RUNTIME_FLOOR_GIB
    eligible_for_eval = exact_model_visible

    reasons: list[str] = []
    if not non_cloud_model:
        reasons.append("Configured model must be a non-cloud identifier.")
    if not exact_model_visible:
        reasons.append("The exact configured non-cloud model is not visible on the endpoint.")
    if not fp8_memory_ready:
        reasons.append(
            "Local memory is below the conservative official-FP8 runtime floor of "
            f"{GLM52_FP8_RUNTIME_FLOOR_GIB}GiB."
        )
    if eligible_for_eval:
        reasons.append("External hardware and exact non-cloud model visibility are ready for evaluation.")

    if eligible_for_eval:
        status = "ready_for_eval"
    elif not fp8_memory_ready:
        status = "external_hardware_required"
    else:
        status = "model_not_visible"

    return {
        "candidate": "glm-5.2",
        "configured_model": configured_model,
        "status": status,
        "eligible_for_eval": eligible_for_eval,
        "exact_model_visible": exact_model_visible,
        "non_cloud_model": non_cloud_model,
        "available_memory_gib": available_memory_gib,
        "memory_scope": "supplied_serving_host",
        "fp8_memory_ready": fp8_memory_ready,
        "official_profile": {
            "total_parameters_billion": GLM52_TOTAL_PARAMETERS_BILLION,
            "parameter_count_convention": "nominal architecture count",
            "reported_stored_parameters_billion": GLM52_REPORTED_STORED_PARAMETERS_BILLION,
            "active_parameters_billion": GLM52_ACTIVE_PARAMETERS_BILLION,
            "bf16_estimate_tib": GLM52_BF16_ESTIMATE_TIB,
            "fp8_estimate_gib": GLM52_FP8_ESTIMATE_GIB,
            "fp8_runtime_floor_gib": GLM52_FP8_RUNTIME_FLOOR_GIB,
        },
        "known_constraints": {
            "m4_pro_48gib": "no_go",
            "official_ollama_tag": "cloud_only",
            "official_local_ollama_quant": "unavailable",
            "external_hardware_required": True,
        },
        "auto_promotion_allowed": False,
        "local_weight_fit": fp8_memory_ready,
        "caution": "Evaluation only; never auto-promote this candidate.",
        "reasons": reasons,
        "source_links": dict(GLM52_SOURCE_LINKS),
    }


def local_readiness(*, installed_models: Iterable[str] = ()) -> dict[str, Any]:
    """Report this Mac's readiness; external hosts should supply their own memory."""
    report = evaluate_glm52_readiness(
        memory_gib=system_memory_gib(),
        installed_models=installed_models,
    )
    report["memory_scope"] = "controller_mac_only"
    report["external_endpoint_memory_verified"] = False
    return report
