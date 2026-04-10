import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re

import evals
from local_runtime import local_training
from tests.jarvis_golden_cases import GOLDEN_CASES

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = REPO_ROOT / "training" / "beta"
RUNS_DIR = ROOT / "runs"


def _ensure_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _selected_cases(include_browser: bool = False, limit: int = 0, suite: str = "all") -> list[dict]:
    suite = (suite or "all").strip().lower()
    cases = []
    seen_prompts = set()
    for case in GOLDEN_CASES:
        case_suite = str(case.get("suite", "core")).strip().lower()
        if suite != "all" and case_suite != suite:
            continue
        if not include_browser and case.get("expected_label") == "Browser":
            continue
        prompt = case.get("prompt", "")
        if prompt in seen_prompts:
            continue
        seen_prompts.add(prompt)
        cases.append(case)
        if limit and len(cases) >= limit:
            break
    if suite == "engineering" and (not limit or len(cases) < limit):
        for case in _recent_engineering_failure_cases(limit=5):
            prompt = case.get("prompt", "")
            if prompt in seen_prompts:
                continue
            seen_prompts.add(prompt)
            cases.append(case)
            if limit and len(cases) >= limit:
                break
    return cases


def _case_failures(case: dict, label: str, text: str) -> list[str]:
    failures = []
    expected_label = case.get("expected_label", "")
    if expected_label and label != expected_label:
        failures.append(f"Expected label {case['expected_label']} but got {label}.")

    for needle in case.get("must_include_all", []):
        if needle not in text:
            failures.append(f"Missing required substring {needle!r}.")

    if case.get("must_include_any") and not any(needle in text for needle in case["must_include_any"]):
        failures.append(
            f"Missing every allowed substring from {case['must_include_any']!r}."
        )

    for needle in case.get("must_exclude_all", []):
        if needle in text:
            failures.append(f"Found forbidden substring {needle!r}.")

    return failures


def _is_engineering_prompt(prompt: str) -> bool:
    lower = (prompt or "").lower()
    markers = (
        "python", "fastapi", "nginx", "docker", "postgres", "database", "index",
        "race condition", "cache invalidation", "replica lag", "migration", "schema",
        "locking", "memory leak", "jwt", "authentication", "authorization", "worker",
        "api", "concurrency", "timeout", "proxy",
    )
    return any(marker in lower for marker in markers)


def _expected_label_for_prompt(prompt: str) -> str:
    lower = (prompt or "").lower()
    if any(term in lower for term in ("security", "authentication", "authorization", "jwt", "token", "vulnerability")):
        return "Specialized Agents"
    if any(term in lower for term in ("memory leak", "leaking memory", "race condition", "replica lag", "cache invalidation", "fastapi", "nginx", "migration", "schema")):
        return "Specialized Agents"
    if any(term in lower for term in ("locking", "database index", "add a database index", "db index")):
        return "Sonnet"
    return "Local Model"


def _recent_engineering_failure_cases(limit: int = 5) -> list[dict]:
    cases = []
    seen_prompts = {case.get("prompt", "") for case in GOLDEN_CASES}
    for failure in reversed(evals.recent_failures(limit=40, hours=24 * 90)):
        prompt = (failure.get("user_input") or "").strip()
        if not prompt or prompt in seen_prompts:
            continue
        if not _is_engineering_prompt(prompt):
            continue
        seen_prompts.add(prompt)
        case_id = re.sub(r"[^a-z0-9]+", "_", failure.get("id", prompt[:24]).lower()).strip("_") or "recent_engineering"
        cases.append(
            {
                "id": f"recent_{case_id}",
                "suite": "engineering",
                "prompt": prompt,
                "expected_label": _expected_label_for_prompt(prompt),
                "must_exclude_all": [
                    "credit balance is too low",
                    "Beta runner caught an exception",
                    "Local model error:",
                    "I hit an upstream model error",
                ],
                "expected": failure.get("expected", "") or failure.get("issue", ""),
            }
        )
        if len(cases) >= limit:
            break
    return cases


def run_beta_suite(
    include_browser: bool = False,
    limit: int = 0,
    log_failures: bool = True,
    build_training_pack: bool = False,
    teacher_model: str = "claude-sonnet-4-6",
    suite: str = "all",
) -> dict:
    _ensure_dirs()
    from router import route_stream

    cases = _selected_cases(include_browser=include_browser, limit=limit, suite=suite)

    passed = 0
    failed = 0
    results = []
    failed_categories = Counter()

    for case in cases:
        try:
            stream, label = route_stream(case["prompt"])
            text = "".join(stream)
        except Exception as exc:
            label = "Error"
            text = f"Beta runner caught an exception while executing this case: {exc}"
        failures = _case_failures(case, label, text)
        ok = not failures
        if ok:
            passed += 1
        else:
            failed += 1
            failed_categories[case.get("id", "general")] += 1
            if log_failures:
                expected = case.get("expected", "")
                if case.get("must_include_all"):
                    expected += " Required substrings: " + ", ".join(case["must_include_all"]) + "."
                if case.get("must_include_any"):
                    expected += " Should include at least one of: " + ", ".join(case["must_include_any"]) + "."
                evals.log_failure(
                    issue=f"Golden beta failure for {case['id']}: {' '.join(failures)}",
                    expected=expected.strip(),
                    user_input=case["prompt"],
                    response=text,
                    model=label,
                    source="beta_test",
                )
        results.append(
            {
                "id": case["id"],
                "prompt": case["prompt"],
                "expected_label": case["expected_label"],
                "label": label,
                "ok": ok,
                "failures": failures,
                "response": text,
            }
        )

    result = {
        "ok": True,
        "created_at": _timestamp(),
        "case_count": len(cases),
        "passed": passed,
        "failed": failed,
        "include_browser": include_browser,
        "suite": suite,
        "logged_failures": log_failures,
        "failed_case_ids": [item["id"] for item in results if not item["ok"]],
        "results": results,
        "failed_case_counts": dict(failed_categories),
    }

    if build_training_pack:
        result["training"] = local_training.build_training_pack(
            distill_limit=0,
            expert_distill_limit=0,
            teacher_model=teacher_model,
        )

    path = RUNS_DIR / f"beta_{result['created_at']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["path"] = str(path)
    return result


def status() -> dict:
    _ensure_dirs()
    runs = sorted(RUNS_DIR.glob("beta_*.json"))
    latest = json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None
    return {
        "runs": len(runs),
        "latest_run": str(runs[-1]) if runs else "",
        "latest_passed": latest.get("passed", 0) if latest else 0,
        "latest_failed": latest.get("failed", 0) if latest else 0,
        "latest_failed_case_ids": latest.get("failed_case_ids", []) if latest else [],
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local beta run failed.")

    text = (
        f"Ran {result.get('case_count', 0)} "
        f"{result.get('suite', 'all')} beta cases. "
        f"{result.get('passed', 0)} passed and {result.get('failed', 0)} failed."
    )
    if result.get("failed_case_ids"):
        text += f" Failed cases: {', '.join(result['failed_case_ids'])}."
    if result.get("training", {}).get("ok"):
        text += (
            f" I also built a new local training pack with "
            f"{result['training'].get('example_count', 0)} merged examples."
        )
        if result.get("failed", 0):
            text += " The failed cases were logged into evals for later teacher distillation."
    return text
