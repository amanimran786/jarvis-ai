"""Exact-model guards for local model evaluation."""

from unittest.mock import patch

from local_runtime import local_model_eval


def test_preflight_accepts_latest_alias_for_local_models():
    with patch.object(
        local_model_eval,
        "list_local_models",
        return_value=["glm-5.2:latest", "glm-4.7-flash:latest"],
    ):
        result = local_model_eval._eval_preflight("glm-5.2", "glm-4.7-flash")

    assert result["ok"] is True


def test_run_eval_rejects_missing_candidate_before_inference():
    with patch.object(
        local_model_eval,
        "list_local_models",
        return_value=["glm-4.7-flash:latest"],
    ), patch.object(local_model_eval, "_run_model_on_case") as run_model:
        result = local_model_eval.run_eval(
            candidate_model="glm-5.2",
            baseline_model="glm-4.7-flash",
            limit=1,
        )

    assert result["ok"] is False
    assert "Missing: glm-5.2" in result["error"]
    run_model.assert_not_called()


def test_run_eval_rejects_cloud_candidate_before_inference():
    with patch.object(local_model_eval, "_run_model_on_case") as run_model:
        result = local_model_eval.run_eval(
            candidate_model="glm-5.2:cloud",
            baseline_model="glm-4.7-flash",
            limit=1,
        )

    assert result == {
        "ok": False,
        "error": "Local model evaluation rejects cloud-tagged models.",
    }
    run_model.assert_not_called()


def test_local_judge_uses_exact_local_model_by_default():
    with patch.object(
        local_model_eval,
        "ask_local",
        return_value='{"pass": true, "score": 5, "rationale": "local"}',
    ) as ask_local, patch.object(local_model_eval, "ask_with_priority") as cloud:
        result = local_model_eval._judge_answer(
            {"category": "quality", "prompt": "p", "expected": "e"},
            "candidate",
            "answer",
            "glm-4.7-flash",
        )

    assert result["pass"] is True
    ask_local.assert_called_once()
    assert ask_local.call_args.kwargs["strict_model"] is True
    cloud.assert_not_called()


def test_glm52_generic_promotion_is_always_denied():
    eval_result = {
        "candidate_model": "glm-5.2",
        "candidate_summary": {"pass_rate": 1.0},
        "score_delta": 5.0,
    }
    with patch.object(local_model_eval, "_load_eval_result", return_value=eval_result), \
         patch.object(local_model_eval, "_save_state") as save_state:
        result = local_model_eval.promote_candidate(candidate_model="glm-5.2")

    assert result["ok"] is False
    assert "Generic promotion is disabled" in result["error"]
    save_state.assert_not_called()


def test_glm52_quality_eval_requires_matching_digest_before_inference():
    client = type("Client", (), {
        "list": lambda self: type("Response", (), {
            "models": [type("Model", (), {
                "model": "glm-5.2:latest",
                "digest": "sha256:actual",
            })()],
        })(),
    })()
    with patch.object(
        local_model_eval,
        "list_local_models",
        return_value=["glm-5.2:latest", "glm-4.7-flash:latest"],
    ), patch.object(local_model_eval.brain_ollama, "_client", return_value=client), \
         patch.object(local_model_eval, "_run_model_on_case") as run_model, \
         patch.object(local_model_eval.glm52_readiness, "LOCAL_GLM52_DIGEST", "sha256:expected"):
        result = local_model_eval.run_eval(
            candidate_model="glm-5.2",
            baseline_model="glm-4.7-flash",
            teacher_model="glm-4.7-flash",
            limit=1,
        )

    assert result["ok"] is False
    assert "digest does not match" in result["error"]
    run_model.assert_not_called()
