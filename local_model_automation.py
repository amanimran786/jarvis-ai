"""
Automate the local-model improvement cycle:

1. Build a fresh training pack.
2. Create a candidate Ollama model from the generated Modelfile.
3. Evaluate it against the baseline.
4. Promote it only if the eval gate clears.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import HAIKU, LOCAL_DEFAULT, LOCAL_TUNED, SONNET
from brain_ollama import list_local_models
import local_model_eval
import local_training
import model_router


ROOT = Path(__file__).resolve().parent / "training" / "automation"
CYCLES_DIR = ROOT / "cycles"


def _ensure_dirs() -> None:
    CYCLES_DIR.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_:" else "-" for ch in text).strip("-") or "candidate"


def _run_ollama_create(target_name: str, modelfile_path: str) -> dict:
    cmd = ["ollama", "create", target_name, "-f", modelfile_path]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to launch ollama create: {exc}"}

    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        return {"ok": False, "error": f"ollama create failed for {target_name}: {output}"}
    model_router.refresh_local_cache()
    return {"ok": True, "command": " ".join(cmd), "output": output}


def _run_ollama_rm(model_name: str) -> dict:
    cmd = ["ollama", "rm", model_name]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        return {"ok": False, "error": f"ollama rm failed for {model_name}: {output}"}
    model_router.refresh_local_cache()
    return {"ok": True, "command": " ".join(cmd), "output": output}


def run_cycle(
    export_limit: int = 40,
    distill_limit: int = 3,
    eval_limit: int = 2,
    base_model: str = LOCAL_DEFAULT,
    baseline_model: str = "",
    candidate_name: str = "",
    teacher_model: str = SONNET,
    judge_model: str = HAIKU,
    promote_if_ready: bool = True,
    cleanup_failed: bool = False,
) -> dict:
    _ensure_dirs()
    stamp = _timestamp()
    candidate = candidate_name.strip() or f"{LOCAL_TUNED}-candidate-{stamp}"

    training_result = local_training.build_training_pack(
        export_limit=export_limit,
        distill_limit=distill_limit,
        teacher_model=teacher_model,
        base_model=base_model,
        target_name=candidate,
    )
    if not training_result.get("ok"):
        return training_result

    create_result = _run_ollama_create(candidate, training_result["modelfile"]["path"])
    if not create_result.get("ok"):
        return {"ok": False, "error": create_result["error"], "training": training_result}

    eval_result = local_model_eval.run_eval(
        candidate_model=candidate,
        baseline_model=baseline_model or local_model_eval.promoted_model() or LOCAL_DEFAULT,
        limit=eval_limit,
        teacher_model=judge_model,
    )

    promotion_result = None
    cleanup_result = None
    if promote_if_ready and eval_result.get("promotion_ready"):
        promotion_result = local_model_eval.promote_candidate(candidate_model=candidate, eval_path=eval_result["path"])
    elif cleanup_failed:
        cleanup_result = _run_ollama_rm(candidate)

    cycle = {
        "ok": True,
        "created_at": stamp,
        "candidate_model": candidate,
        "baseline_model": eval_result.get("baseline_model", ""),
        "training": training_result,
        "created_model": create_result,
        "eval": {
            "path": eval_result.get("path", ""),
            "candidate_summary": eval_result.get("candidate_summary", {}),
            "baseline_summary": eval_result.get("baseline_summary", {}),
            "score_delta": eval_result.get("score_delta", 0.0),
            "promotion_ready": eval_result.get("promotion_ready", False),
        },
        "promotion": promotion_result,
        "cleanup": cleanup_result,
    }

    cycle_path = CYCLES_DIR / f"cycle_{_safe_slug(candidate)}_{stamp}.json"
    cycle_path.write_text(json.dumps(cycle, indent=2), encoding="utf-8")
    cycle["path"] = str(cycle_path)
    return cycle


def status() -> dict:
    _ensure_dirs()
    cycles = sorted(CYCLES_DIR.glob("cycle_*.json"))
    latest = json.loads(cycles[-1].read_text(encoding="utf-8")) if cycles else None
    return {
        "cycles": len(cycles),
        "latest_cycle": str(cycles[-1]) if cycles else "",
        "latest_candidate": latest.get("candidate_model", "") if latest else "",
        "latest_promotion_ready": latest.get("eval", {}).get("promotion_ready", False) if latest else False,
        "latest_delta": latest.get("eval", {}).get("score_delta", 0.0) if latest else 0.0,
        "available_models": list_local_models(),
        "preferred_model": local_model_eval.promoted_model(),
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local model automation failed.")

    eval_result = result.get("eval", {})
    text = (
        f"Built candidate {result.get('candidate_model')} from a fresh training pack and evaluated it against "
        f"{result.get('baseline_model')}. The score delta was {eval_result.get('score_delta')} and promotion ready is "
        f"{eval_result.get('promotion_ready')}."
    )
    if result.get("promotion", {}).get("ok"):
        text += f" I promoted {result['promotion']['preferred_model']} as the preferred local model."
    elif result.get("cleanup", {}).get("ok"):
        text += f" The failed candidate was removed after evaluation."
    return text
