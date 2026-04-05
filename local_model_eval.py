"""
Evaluate candidate local Ollama models against Jarvis-specific benchmark prompts
and gate promotion on measured improvement.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from brain_claude import ask_claude
from brain_ollama import ask_local
from config import LOCAL_DEFAULT, LOCAL_TUNED, SONNET, HAIKU
import evals
import skills


ROOT = Path(__file__).resolve().parent / "training" / "model_evals"
RUNS_DIR = ROOT / "runs"
STATE_FILE = ROOT / "promotion.json"

PROMOTION_MIN_PASS_RATE = 0.6
PROMOTION_MIN_SCORE_DELTA = 0.35

CURATED_CASES = [
    {
        "id": "tech_locking",
        "category": "technical_reasoning",
        "prompt": "Explain the difference between optimistic locking and pessimistic locking and tell me when each one is the better choice.",
        "expected": "Lead with the core difference, mention concurrency tradeoffs, and explain when each approach wins in practice.",
    },
    {
        "id": "tech_debug",
        "category": "technical_reasoning",
        "prompt": "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence.",
        "expected": "Rank likely causes, mention exact checks, and avoid generic filler like just saying to check logs.",
    },
    {
        "id": "self_improve_policy",
        "category": "self_improve",
        "prompt": "If I ask you to improve yourself right now, what evidence would you need before changing code?",
        "expected": "Lead with evidence gating, scope, reproducible signal, safe validation, and rollback. Do not jump straight to implementation mechanics only.",
    },
    {
        "id": "personal_interest",
        "category": "memory",
        "prompt": "Tell me something interesting based on what you know about me.",
        "expected": "Use Aman-specific context rather than generic AI trivia or abstract self-help.",
    },
    {
        "id": "source_grounding",
        "category": "knowledge",
        "prompt": "Search the vault for Jarvis Vault Strategy and summarize it in two sentences with the exact local file and heading you used.",
        "expected": "Stay concise, grounded in local knowledge, and cite the exact local file and heading.",
    },
]


def _ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _load_state() -> dict:
    _ensure_dirs()
    if not STATE_FILE.exists():
        return {"preferred_model": "", "last_promotion": "", "source_eval": ""}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(data: dict) -> None:
    _ensure_dirs()
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def promoted_model() -> str:
    state = _load_state()
    return state.get("preferred_model", "").strip()


def _normalize_case_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "case"


def _failure_cases(limit: int = 8) -> list[dict]:
    cases = []
    seen_prompts: set[str] = set()
    for failure in reversed(evals.recent_failures(limit=30, hours=24 * 30)):
        prompt = (failure.get("user_input") or "").strip()
        if not prompt or prompt in seen_prompts:
            continue
        seen_prompts.add(prompt)
        cases.append(
            {
                "id": f"failure_{_normalize_case_id(failure.get('id', prompt[:20]))}",
                "category": failure.get("category", "general_quality"),
                "prompt": prompt,
                "expected": failure.get("expected") or failure.get("issue") or "Answer this more accurately and usefully.",
            }
        )
        if len(cases) >= limit:
            break
    return cases


def benchmark_cases(limit: int = 10) -> list[dict]:
    cases = []
    seen_prompts = set()

    for case in _failure_cases(limit=limit):
        if case["prompt"] in seen_prompts:
            continue
        seen_prompts.add(case["prompt"])
        cases.append(case)
        if len(cases) >= limit:
            return cases

    for case in CURATED_CASES:
        if case["prompt"] in seen_prompts:
            continue
        seen_prompts.add(case["prompt"])
        cases.append(case)
        if len(cases) >= limit:
            break
    return cases


def _judge_prompt(case: dict, model_name: str, answer: str) -> str:
    return (
        "You are evaluating a Jarvis local model answer. Score it for usefulness and correctness.\n"
        "Return only valid JSON with keys: pass, score, rationale.\n"
        "pass must be true or false. score must be a number from 0 to 5.\n"
        "Judge against the expected behavior, not style preferences.\n\n"
        f"Case category: {case['category']}\n"
        f"User prompt: {case['prompt']}\n"
        f"Expected behavior: {case['expected']}\n"
        f"Model under test: {model_name}\n"
        f"Answer:\n{answer}\n"
    )


def _judge_answer(case: dict, model_name: str, answer: str, teacher_model: str) -> dict:
    system_extra, _ = skills.build_system_extra(case["prompt"], tool="chat")
    raw = ask_claude(_judge_prompt(case, model_name, answer), model=teacher_model, system_extra=system_extra).strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1]).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"pass": False, "score": 0.0, "rationale": f"Judge parse failure: {raw[:300]}"}
    return {
        "pass": bool(data.get("pass")),
        "score": float(data.get("score", 0.0)),
        "rationale": str(data.get("rationale", "")).strip(),
    }


def _run_model_on_case(model_name: str, case: dict) -> str:
    system_extra, _ = skills.build_system_extra(case["prompt"], tool="chat")
    return ask_local(case["prompt"], model=model_name, system_extra=system_extra, track_context=False).strip()


def _score_summary(results: list[dict]) -> dict:
    if not results:
        return {"avg_score": 0.0, "pass_rate": 0.0, "passes": 0, "count": 0}
    count = len(results)
    passes = sum(1 for item in results if item.get("judgment", {}).get("pass"))
    avg_score = sum(item.get("judgment", {}).get("score", 0.0) for item in results) / count
    return {
        "avg_score": round(avg_score, 3),
        "pass_rate": round(passes / count, 3),
        "passes": passes,
        "count": count,
    }


def run_eval(
    candidate_model: str,
    baseline_model: str | None = None,
    limit: int = 8,
    teacher_model: str = HAIKU,
) -> dict:
    _ensure_dirs()
    cases = benchmark_cases(limit=limit)
    baseline = baseline_model or promoted_model() or LOCAL_DEFAULT

    candidate_results = []
    baseline_results = []
    comparisons = []

    for case in cases:
        candidate_answer = _run_model_on_case(candidate_model, case)
        baseline_answer = _run_model_on_case(baseline, case)

        candidate_judgment = _judge_answer(case, candidate_model, candidate_answer, teacher_model)
        baseline_judgment = _judge_answer(case, baseline, baseline_answer, teacher_model)

        candidate_results.append({"case": case, "answer": candidate_answer, "judgment": candidate_judgment})
        baseline_results.append({"case": case, "answer": baseline_answer, "judgment": baseline_judgment})
        comparisons.append(
            {
                "case": case,
                "candidate": {"model": candidate_model, "answer": candidate_answer, "judgment": candidate_judgment},
                "baseline": {"model": baseline, "answer": baseline_answer, "judgment": baseline_judgment},
                "delta": round(candidate_judgment["score"] - baseline_judgment["score"], 3),
            }
        )

    candidate_summary = _score_summary(candidate_results)
    baseline_summary = _score_summary(baseline_results)
    score_delta = round(candidate_summary["avg_score"] - baseline_summary["avg_score"], 3)
    promotion_ready = (
        candidate_summary["pass_rate"] >= PROMOTION_MIN_PASS_RATE
        and score_delta >= PROMOTION_MIN_SCORE_DELTA
        and candidate_summary["avg_score"] >= baseline_summary["avg_score"]
    )

    result = {
        "ok": True,
        "candidate_model": candidate_model,
        "baseline_model": baseline,
        "teacher_model": teacher_model,
        "candidate_summary": candidate_summary,
        "baseline_summary": baseline_summary,
        "score_delta": score_delta,
        "promotion_ready": promotion_ready,
        "cases": comparisons,
        "created_at": _timestamp(),
    }

    path = RUNS_DIR / f"eval_{_normalize_case_id(candidate_model)}_{result['created_at']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["path"] = str(path)
    return result


def _load_eval_result(path: str | None = None) -> dict | None:
    _ensure_dirs()
    if path:
        ref = Path(path)
        if ref.exists():
            return json.loads(ref.read_text(encoding="utf-8"))
        return None
    runs = sorted(RUNS_DIR.glob("eval_*.json"))
    if not runs:
        return None
    return json.loads(runs[-1].read_text(encoding="utf-8"))


def promote_candidate(
    candidate_model: str | None = None,
    eval_path: str | None = None,
    min_pass_rate: float = PROMOTION_MIN_PASS_RATE,
    min_score_delta: float = PROMOTION_MIN_SCORE_DELTA,
) -> dict:
    result = _load_eval_result(eval_path)
    if not result:
        return {"ok": False, "error": "No local model eval result found to promote from."}

    if candidate_model and result.get("candidate_model") != candidate_model:
        return {"ok": False, "error": f"Latest eval is for {result.get('candidate_model')}, not {candidate_model}."}

    candidate = result["candidate_model"]
    pass_rate = float(result.get("candidate_summary", {}).get("pass_rate", 0.0))
    score_delta = float(result.get("score_delta", 0.0))

    if pass_rate < min_pass_rate or score_delta < min_score_delta:
        return {
            "ok": False,
            "error": (
                f"Promotion refused. Candidate {candidate} scored pass_rate={pass_rate} and delta={score_delta}, "
                f"which is below the required thresholds of pass_rate>={min_pass_rate} and delta>={min_score_delta}."
            ),
            "eval_path": result.get("path") or eval_path or "",
        }

    state = _load_state()
    state.update(
        {
            "preferred_model": candidate,
            "last_promotion": _timestamp(),
            "source_eval": result.get("path") or eval_path or "",
            "baseline_model": result.get("baseline_model", ""),
            "score_delta": score_delta,
            "pass_rate": pass_rate,
        }
    )
    _save_state(state)
    return {"ok": True, "preferred_model": candidate, "state": state}


def status() -> dict:
    _ensure_dirs()
    runs = sorted(RUNS_DIR.glob("eval_*.json"))
    state = _load_state()
    latest = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
    return {
        "runs": len(runs),
        "latest_eval": str(runs[-1]) if runs else "",
        "preferred_model": state.get("preferred_model", ""),
        "last_promotion": state.get("last_promotion", ""),
        "promotion_source": state.get("source_eval", ""),
        "latest_candidate": latest.get("candidate_model", "") if latest else "",
        "latest_delta": latest.get("score_delta", 0.0) if latest else 0.0,
        "latest_promotion_ready": latest.get("promotion_ready", False) if latest else False,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local model evaluation failed.")
    if "preferred_model" in result:
        return f"Promoted {result['preferred_model']} as the preferred local model based on the latest eval run."
    if "candidate_model" in result:
        summary = result.get("candidate_summary", {})
        baseline = result.get("baseline_summary", {})
        return (
            f"Evaluated {result['candidate_model']} against {result['baseline_model']}. "
            f"The candidate scored {summary.get('avg_score')} average with pass rate {summary.get('pass_rate')}, "
            f"versus {baseline.get('avg_score')} and {baseline.get('pass_rate')} for the baseline. "
            f"Score delta was {result.get('score_delta')}. "
            f"Promotion ready is {result.get('promotion_ready')}."
        )
    return "Local model evaluation step completed."
