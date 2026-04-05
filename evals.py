import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

EVALS_FILE = os.path.join(os.path.dirname(__file__), "evals.json")

_DEFAULTS = {
    "interactions": [],
    "failures": [],
    "improvements": [],
    "last_updated": None,
}

_LOCK = threading.Lock()
_MAX_INTERACTIONS = 300
_MAX_FAILURES = 200
_MAX_IMPROVEMENTS = 100

_CATEGORY_FILE_MAP = {
    "routing": ["router.py", "orchestrator.py", "model_router.py"],
    "browser": ["browser.py", "router.py"],
    "formatting": ["brain.py", "brain_claude.py", "brain_ollama.py", "config.py"],
    "memory": ["memory.py", "learner.py", "brain.py", "brain_claude.py", "brain_ollama.py"],
    "self_improve": ["self_improve.py"],
    "tool_execution": ["terminal.py", "tools.py", "router.py"],
    "hallucination": ["config.py", "model_router.py", "brain.py", "brain_claude.py", "brain_ollama.py"],
    "latency": ["model_router.py", "brain.py", "brain_claude.py", "brain_ollama.py"],
    "general_quality": ["model_router.py", "router.py"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_unlocked() -> dict:
    if not os.path.exists(EVALS_FILE):
        return dict(_DEFAULTS)
    try:
        with open(EVALS_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return dict(_DEFAULTS)
        data = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    for key, value in _DEFAULTS.items():
        data.setdefault(key, value)
    return data


def load() -> dict:
    with _LOCK:
        return _load_unlocked()


def save(data: dict) -> None:
    with _LOCK:
        data["interactions"] = data.get("interactions", [])[-_MAX_INTERACTIONS:]
        data["failures"] = data.get("failures", [])[-_MAX_FAILURES:]
        data["improvements"] = data.get("improvements", [])[-_MAX_IMPROVEMENTS:]
        data["last_updated"] = _now_iso()
        tmp = EVALS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, EVALS_FILE)


def _find_interaction(data: dict, interaction_id: str) -> dict | None:
    for item in reversed(data.get("interactions", [])):
        if item.get("id") == interaction_id:
            return item
    return None


def log_interaction(user_input: str, response: str, model: str, source: str = "api") -> dict:
    data = load()
    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "source": source,
        "user_input": user_input,
        "response": response,
        "model": model,
    }
    data["interactions"].append(entry)
    save(data)
    return entry


def classify_failure(issue: str = "", user_input: str = "", response: str = "", expected: str = "") -> str:
    text = " ".join(x for x in (issue, user_input, response, expected) if x).lower()

    if any(t in text for t in ("markdown", "bullet", "bullets", "formatted wrong", "numbered list", "read aloud", "spoken aloud", "formatting")):
        return "formatting"
    if any(t in text for t in ("hallucinat", "made up", "invented", "false", "wrong fact", "not true", "claimed it was using")):
        return "hallucination"
    if any(t in text for t in ("browser", "page", "site", "openai.com", "clicked the wrong", "opened the wrong", "search instead", "tab", "safari", "chrome")):
        return "browser"
    if any(t in text for t in ("route", "routing", "wrong tool", "wrong intent", "misclassified", "should have used")):
        return "routing"
    if any(t in text for t in ("remember", "forgot", "memory", "context", "personalization", "personalised", "personalized")):
        return "memory"
    if any(t in text for t in ("self-improve", "self improve", "rewrite its own code", "syntax validation", "backup", "corrupt file", "restart yourself")):
        return "self_improve"
    if any(t in text for t in ("slow", "latency", "too long", "took forever", "token cost", "expensive")):
        return "latency"
    if any(t in text for t in ("error", "failed", "couldn't", "crash", "exception", "not working", "didn't run", "administrator privileges")):
        return "tool_execution"
    return "general_quality"


def log_failure(
    issue: str,
    interaction_id: str | None = None,
    expected: str = "",
    user_input: str = "",
    response: str = "",
    model: str = "",
    source: str = "user_feedback",
) -> dict:
    data = load()
    linked = _find_interaction(data, interaction_id) if interaction_id else None
    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "source": source,
        "interaction_id": interaction_id,
        "issue": issue,
        "expected": expected,
        "user_input": user_input or (linked.get("user_input", "") if linked else ""),
        "response": response or (linked.get("response", "") if linked else ""),
        "model": model or (linked.get("model", "") if linked else ""),
    }
    entry["category"] = classify_failure(entry["issue"], entry["user_input"], entry["response"], entry["expected"])
    data["failures"].append(entry)
    save(data)
    return entry


def maybe_log_automatic_failure(interaction: dict) -> dict | None:
    if _response_has_unhandled_failure(interaction.get("response", "")):
        return log_failure(
            issue="Automatic runtime failure detected from response text.",
            interaction_id=interaction["id"],
            source="runtime",
        )
    return None


def _response_has_unhandled_failure(response: str) -> bool:
    text = (response or "").strip().lower()
    if not text:
        return False

    recovered_markers = (
        "so i opened the matching link directly",
        "so i opened the closest matching link directly",
        "opened https://",
    )
    if any(marker in text for marker in recovered_markers) and "aborted before applying changes" not in text:
        if "couldn't click" in text or "didn't find an exact clickable element" in text:
            return False

    failure_markers = (
        "couldn't ",
        "failed",
        "error:",
        "local model error",
        "aborted before applying changes",
        "no browser window is open",
    )
    return any(marker in text for marker in failure_markers)


def _is_resolved_by_later_success(failure: dict, interactions: list[dict]) -> bool:
    failure_ts = _parse_iso(failure.get("timestamp"))
    if not failure_ts:
        return False

    user_input = failure.get("user_input", "")
    model = failure.get("model", "")
    for interaction in interactions:
        ts = _parse_iso(interaction.get("timestamp"))
        if not ts or ts <= failure_ts:
            continue
        if interaction.get("user_input", "") != user_input:
            continue
        if model and interaction.get("model", "") != model:
            continue
        if not _response_has_unhandled_failure(interaction.get("response", "")):
            return True
    return False


def recent_failures(limit: int = 20, hours: int = 24 * 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    data = load()
    interactions = data.get("interactions", [])
    failures = []
    for item in data.get("failures", []):
        ts = _parse_iso(item.get("timestamp"))
        if ts and ts >= cutoff:
            if _is_resolved_by_later_success(item, interactions):
                continue
            failures.append(item)
    return failures[-limit:]


def recent_interactions(limit: int = 20) -> list[dict]:
    return load().get("interactions", [])[-limit:]


def _matches_area(failure: dict, area: str) -> bool:
    haystack = " ".join(
        str(failure.get(key, "")) for key in ("category", "issue", "expected", "user_input", "response")
    ).lower()
    return area.lower() in haystack


def _choose_target_file(failures: list[dict], preferred: str | None = None) -> str:
    if preferred and preferred.endswith(".py"):
        return preferred
    scores = Counter()
    for failure in failures:
        for filename in _CATEGORY_FILE_MAP.get(failure.get("category", "general_quality"), []):
            scores[filename] += 1
    if preferred:
        preferred = preferred.lower()
        for category, files in _CATEGORY_FILE_MAP.items():
            if preferred in category:
                for filename in files:
                    scores[filename] += 2
    return scores.most_common(1)[0][0] if scores else "router.py"


def build_improvement_brief(area: str | None = None, min_failures: int = 2, hours: int = 24 * 7) -> dict:
    failures = recent_failures(limit=20, hours=hours)
    if area:
        filtered = [f for f in failures if _matches_area(f, area)]
        if filtered:
            failures = filtered

    if len(failures) < min_failures:
        return {
            "ok": False,
            "reason": f"Not enough recent eval evidence. Need at least {min_failures} recent logged failures.",
            "failures": failures,
        }

    category_counts = Counter(f["category"] for f in failures)
    top_categories = ", ".join(f"{name} ({count})" for name, count in category_counts.most_common(3))
    target_file = _choose_target_file(failures, preferred=area)
    evidence_lines = [
        f"[{f['id']}] {f['category']}: {f['issue']}" + (f" Expected: {f['expected']}" if f.get("expected") else "")
        for f in failures[-5:]
    ]
    instruction = (
        f"Fix the recurring failures evidenced by recent eval logs, focusing on {target_file}. "
        f"The strongest failure categories are {top_categories}. "
        f"Address the concrete issues from these recent failures: {' | '.join(evidence_lines)}. "
        "Preserve existing behavior outside the failing path and improve reliability rather than adding broad complexity."
    )
    return {
        "ok": True,
        "target_file": target_file,
        "instruction": instruction,
        "summary": f"{len(failures)} recent failures. Top categories: {top_categories}.",
        "failures": failures,
        "failure_ids": [f["id"] for f in failures],
        "evidence_lines": evidence_lines,
    }


def record_improvement(result: dict) -> None:
    data = load()
    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "file": result.get("file"),
        "instruction": result.get("instruction"),
        "evidence_ids": result.get("evidence_ids", []),
        "validation": result.get("validation", {}),
        "backup": result.get("backup"),
    }
    data["improvements"].append(entry)
    save(data)


def summary(hours: int = 24 * 7) -> dict:
    failures = recent_failures(limit=50, hours=hours)
    categories = Counter(f["category"] for f in failures)
    return {
        "recent_failure_count": len(failures),
        "categories": dict(categories),
        "recent_failures": failures[-10:],
        "recent_interactions": recent_interactions(10),
    }
