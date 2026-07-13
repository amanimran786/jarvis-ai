"""
self_eval.py — Jarvis per-response quality scorer and performance reporter.

Implements Section 10 of 00_ARCHITECTURE.md: the five response quality dimensions.

  1. specificity      — Does the response reference Aman's actual context?
  2. memory_util      — Did Jarvis use relevant memory that was available?
  3. voice_fidelity   — Does the response avoid generic filler and verbose padding?
  4. module_correct   — Was the right module/routing domain activated?
  5. contamination    — Did role-specific content leak into universal responses?

Design constraints:
- Local-first: heuristic scoring is zero-model-calls.
- Optional LLM judge (allow_cloud=True) for deeper grading.
- Async scoring: score_async() fires after response delivery — never blocks.
- Log path: runtime_state.writable_data_path("evals", "response_scores.jsonl")
- performance_report() — human-readable summary of recent scores for "how am I doing?"
- jarvis_reflection() — synthesizes score trends into improvement notes for KB.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import runtime_state

try:
    from harness.audit import audit_log as _audit_log
except Exception:
    def _audit_log(*a, **kw): pass

log = logging.getLogger(__name__)

# ── Storage ────────────────────────────────────────────────────────────────────

_SCORE_LOG_PARTS = ("evals", "response_scores.jsonl")


def _score_log_path() -> Path:
    return runtime_state.writable_data_path(*_SCORE_LOG_PARTS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Generic-phrase lists (heuristic voice-fidelity and specificity checks) ────

_GENERIC_FILLER = frozenset({
    "absolutely", "certainly", "definitely", "of course", "sure thing",
    "great question", "excellent question", "happy to help", "i can help with that",
    "i would be happy to", "feel free to", "in conclusion", "in summary",
    "to summarize", "as mentioned", "as i said", "let me explain",
    "it is important to note", "it is worth noting", "it should be noted",
    "needless to say", "without further ado", "having said that",
})

_SPECIFICITY_ANCHORS = frozenset({
    "aman", "sjsu", "jarvis", "interview", "youtube", "linkedin",
    "recall", "you mentioned", "based on what you", "your project",
    "your experience", "your background", "your preference",
})

_CROSS_DOMAIN_LEAKAGE = frozenset({
    "youtube pem", "youtube product", "meta pm", "apple pm",
    "hiring committee", "loop interview", "behavioral round",
})

# Domains where cross-domain material is expected (safe to mention interview terms)
_INTERVIEW_DOMAINS = frozenset({
    "InterviewIntel", "CareerOS", "Interview", "Career",
})


# ── Heuristic scorers (no model call, 0–1 float) ─────────────────────────────

def _score_specificity(response: str, context: dict) -> float:
    """Higher when response references user-specific entities vs generic advice."""
    lower = response.lower()
    anchor_hits = sum(1 for anchor in _SPECIFICITY_ANCHORS if anchor in lower)
    filler_hits = sum(1 for phrase in _GENERIC_FILLER if phrase in lower)
    word_count = max(1, len(response.split()))

    # Specificity = anchor density, penalised by filler density
    anchor_density = min(1.0, anchor_hits / 3)
    filler_penalty = min(0.4, filler_hits * 0.1)
    return max(0.0, min(1.0, round(anchor_density - filler_penalty + 0.2, 3)))


def _score_memory_util(response: str, context: dict) -> float | None:
    """Estimate memory utilization from interaction context metadata.

    Returns None when context doesn't carry enough info to judge.
    When memory_chunks_retrieved > 0 and response references personal context,
    score is high. When chunks were available but response is fully generic, low.
    """
    chunks = context.get("memory_chunks_retrieved", context.get("memory_chunks", None))
    if chunks is None:
        return None
    if chunks == 0:
        return None

    lower = response.lower()
    used_memory = any(anchor in lower for anchor in _SPECIFICITY_ANCHORS)
    if used_memory:
        return 0.85
    filler_heavy = sum(1 for p in _GENERIC_FILLER if p in lower) >= 3
    return 0.25 if filler_heavy else 0.50


def _score_voice_fidelity(response: str, context: dict) -> float:
    """Lower penalty for filler phrases, verbose padding, and passive voice."""
    lower = response.lower()
    word_count = max(1, len(response.split()))
    filler_hits = sum(1 for phrase in _GENERIC_FILLER if phrase in lower)
    filler_rate = filler_hits / (word_count / 30)  # filler per 30 words
    passive_hits = len(re.findall(r"\b(is|was|were|are|been)\s+\w+ed\b", lower))
    passive_rate = passive_hits / (word_count / 50)
    score = 1.0 - min(0.5, filler_rate * 0.15) - min(0.3, passive_rate * 0.05)
    return max(0.0, round(score, 3))


def _score_module_correct(response: str, context: dict) -> float | None:
    """Heuristic: if routing tag is in context, check it makes sense for the query."""
    routing_tag = context.get("routing_tag", context.get("module", ""))
    user_input = context.get("user_input", "")
    if not routing_tag or not user_input:
        return None

    lower_input = user_input.lower()
    # Simple domain→tag alignment checks
    domain_hints: dict[str, list[str]] = {
        "InterviewIntel": ["interview", "mock", "behavioral", "star", "prep", "hiring"],
        "CareerOS": ["apply", "resume", "job search", "recruiter", "outreach", "linkedin"],
        "TechAssist": ["code", "sql", "debug", "error", "build", "script", "query", "function"],
        "StrategyOS": ["tradeoff", "decision", "should i", "thinking through", "long-term"],
        "DailyOS": ["remind", "schedule", "task", "note", "draft", "email", "today"],
        "ResearchEngine": ["research", "find out", "what is", "explain", "summarize", "who is"],
        "CommunicationCraft": ["write", "draft", "respond to", "message", "reply", "tone"],
    }
    for tag, hints in domain_hints.items():
        if tag in routing_tag:
            aligned = any(h in lower_input for h in hints)
            return 0.9 if aligned else 0.4
    return 0.7  # Unknown tag — neutral


def _score_contamination(response: str, context: dict) -> float:
    """0 = no contamination (good), 1 = contaminated (bad). Inverted for storage."""
    routing_tag = context.get("routing_tag", context.get("module", ""))
    if routing_tag in _INTERVIEW_DOMAINS:
        return 1.0  # Interview context, leakage expected and fine
    lower = response.lower()
    leakage_hits = sum(1 for phrase in _CROSS_DOMAIN_LEAKAGE if phrase in lower)
    contamination = min(1.0, leakage_hits * 0.5)
    return round(1.0 - contamination, 3)  # 1.0 = clean, 0.0 = contaminated


# ── Composite score ────────────────────────────────────────────────────────────

def _composite(scores: dict[str, float | None]) -> float:
    """Weighted average of non-None dimension scores."""
    weights = {
        "specificity": 0.30,
        "voice_fidelity": 0.20,
        "module_correct": 0.20,
        "contamination": 0.15,
        "memory_util": 0.15,
    }
    total_w = 0.0
    total_s = 0.0
    for dim, score in scores.items():
        if score is None:
            continue
        w = weights.get(dim, 0.1)
        total_w += w
        total_s += score * w
    if total_w == 0:
        return 0.5
    return round(total_s / total_w, 3)


# ── Public: score a single response ──────────────────────────────────────────

def score_response(
    user_input: str,
    response: str,
    context: dict | None = None,
    *,
    interaction_id: str | None = None,
    routing_tag: str = "",
    allow_cloud: bool = False,
) -> dict[str, Any]:
    """Score one response on all five quality dimensions.

    Returns a score record suitable for appending to the log.
    Never raises — on any error returns a degraded record with error=True.
    """
    ctx: dict[str, Any] = dict(context or {})
    if routing_tag and "routing_tag" not in ctx:
        ctx["routing_tag"] = routing_tag
    if user_input and "user_input" not in ctx:
        ctx["user_input"] = user_input

    try:
        dimensions: dict[str, float | None] = {
            "specificity": _score_specificity(response, ctx),
            "memory_util": _score_memory_util(response, ctx),
            "voice_fidelity": _score_voice_fidelity(response, ctx),
            "module_correct": _score_module_correct(response, ctx),
            "contamination": _score_contamination(response, ctx),
        }

        if allow_cloud:
            _llm_rescore(user_input, response, dimensions, ctx)

        composite = _composite(dimensions)

        record: dict[str, Any] = {
            "id": interaction_id or uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "routing_tag": routing_tag or ctx.get("routing_tag", ""),
            "composite": composite,
            "dimensions": {k: v for k, v in dimensions.items() if v is not None},
            "user_input_len": len(user_input),
            "response_len": len(response),
        }
        _append_score(record)
        _audit_log("self_eval_score", interaction_id=record["id"], composite=composite, routing_tag=routing_tag or ctx.get("routing_tag", ""))
        return record

    except Exception:
        log.exception("[self_eval] score_response failed")
        return {"id": interaction_id or "", "ts": _now_iso(), "error": True}


def score_async(
    user_input: str,
    response: str,
    context: dict | None = None,
    *,
    interaction_id: str | None = None,
    routing_tag: str = "",
    allow_cloud: bool = False,
) -> None:
    """Fire-and-forget wrapper — scores in a daemon thread, never blocks caller."""
    t = threading.Thread(
        target=score_response,
        args=(user_input, response),
        kwargs={
            "context": context,
            "interaction_id": interaction_id,
            "routing_tag": routing_tag,
            "allow_cloud": allow_cloud,
        },
        daemon=True,
    )
    t.start()


# ── Optional LLM re-score ─────────────────────────────────────────────────────

_LLM_JUDGE_SYSTEM = (
    "You are a terse quality auditor for an AI assistant called Jarvis. "
    "Given a user query and the AI's response, rate these two dimensions on 0.0–1.0 "
    "where 1.0 is excellent. Return JSON only: "
    '{"specificity": 0.0-1.0, "voice_fidelity": 0.0-1.0}'
)

_LLM_JUDGE_PROMPT = """\
User query: {user_input}

AI response:
{response}

Rate specificity (does it reference the user's actual context?) and voice_fidelity \
(concise, no filler, direct tone). JSON only."""


def _llm_rescore(
    user_input: str,
    response: str,
    dimensions: dict[str, float | None],
    ctx: dict,
) -> None:
    """Optionally replace heuristic scores with LLM grades. Mutates dimensions in-place."""
    try:
        from provider_priority import ask_with_priority

        prompt = _LLM_JUDGE_PROMPT.format(
            user_input=user_input[:400],
            response=response[:800],
        )
        raw = ask_with_priority(prompt, tier="cheap", system_extra=_LLM_JUDGE_SYSTEM)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1]).strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if "specificity" in data:
                dimensions["specificity"] = max(0.0, min(1.0, float(data["specificity"])))
            if "voice_fidelity" in data:
                dimensions["voice_fidelity"] = max(0.0, min(1.0, float(data["voice_fidelity"])))
    except Exception:
        log.debug("[self_eval] LLM rescore failed — keeping heuristic values", exc_info=True)


# ── Log IO ────────────────────────────────────────────────────────────────────

def _append_score(record: dict[str, Any]) -> None:
    path = _score_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_scores(hours: int = 24 * 7) -> list[dict[str, Any]]:
    """Return recent score records, newest last."""
    path = _score_log_path()
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts_str = rec.get("ts", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            records.append(rec)
        except json.JSONDecodeError:
            continue
    return records


# ── Performance report ────────────────────────────────────────────────────────

def performance_report(hours: int = 24 * 7) -> str:
    """Return a concise human-readable report of Jarvis's self-assessed quality.

    Suitable for the "how am I doing?" router handler.
    """
    scores = load_scores(hours=hours)
    if not scores:
        return _no_data_report()

    # Aggregate dimension stats
    dim_totals: dict[str, list[float]] = defaultdict(list)
    composites: list[float] = []

    for rec in scores:
        if rec.get("error"):
            continue
        if "composite" in rec:
            composites.append(min(1.0, max(0.0, float(rec["composite"]))))
        for dim, val in rec.get("dimensions", {}).items():
            if val is not None:
                dim_totals[dim].append(min(1.0, max(0.0, float(val))))

    if not composites:
        return _no_data_report()

    n = len(composites)
    overall = sum(composites) / n
    dim_avgs = {dim: sum(vals) / len(vals) for dim, vals in dim_totals.items() if vals}

    # Tag breakdown
    tag_scores: dict[str, list[float]] = defaultdict(list)
    for rec in scores:
        tag = rec.get("routing_tag", "unknown") or "unknown"
        if "composite" in rec and not rec.get("error"):
            tag_scores[tag].append(float(rec["composite"]))
    tag_avgs = {tag: sum(vals) / len(vals) for tag, vals in tag_scores.items()}
    weakest_tag = min(tag_avgs, key=tag_avgs.get) if tag_avgs else None
    strongest_tag = max(tag_avgs, key=tag_avgs.get) if tag_avgs else None

    # Trend (compare first half vs second half of window)
    mid = n // 2
    trend = ""
    if mid >= 3:
        first_half = sum(composites[:mid]) / mid
        second_half = sum(composites[mid:]) / (n - mid)
        delta = second_half - first_half
        if abs(delta) > 0.03:
            direction = "improving" if delta > 0 else "declining"
            trend = f" Trend: {direction} ({delta:+.2f} from first to second half of window)."

    # Weakest dimension
    dim_label = {
        "specificity": "specificity (personal context use)",
        "memory_util": "memory utilization",
        "voice_fidelity": "voice fidelity (avoiding filler)",
        "module_correct": "module routing correctness",
        "contamination": "context contamination control",
    }
    weakest_dim = min(dim_avgs, key=dim_avgs.get) if dim_avgs else None

    lines = [
        f"Self-eval report: {n} responses scored over the last "
        f"{'week' if hours >= 168 else f'{hours}h'}.",
        f"Overall quality score: {overall:.2f}/1.0 "
        f"({'strong' if overall >= 0.75 else 'needs work' if overall < 0.55 else 'adequate'}).",
    ]

    if dim_avgs:
        dim_str = ", ".join(
            f"{dim_label.get(d, d)} {v:.2f}"
            for d, v in sorted(dim_avgs.items(), key=lambda kv: kv[1])
        )
        lines.append(f"Dimension breakdown (low→high): {dim_str}.")

    if weakest_dim:
        lines.append(
            f"Weakest dimension: {dim_label.get(weakest_dim, weakest_dim)} "
            f"({dim_avgs[weakest_dim]:.2f}). This is the clearest target for improvement."
        )

    if weakest_tag and tag_avgs[weakest_tag] < 0.6:
        lines.append(
            f"Weakest routing domain: {weakest_tag} (avg {tag_avgs[weakest_tag]:.2f}). "
            f"Responses in this domain are consistently below average quality."
        )

    if strongest_tag and tag_avgs.get(strongest_tag, 0) > 0.75:
        lines.append(f"Strongest domain: {strongest_tag} (avg {tag_avgs[strongest_tag]:.2f}).")

    if trend:
        lines.append(trend.strip())

    lines.append(
        "To improve: run `python3 self_eval.py --report` for the full breakdown, "
        "or `python3 self_eval.py --reflect` to generate a KB improvement note."
    )

    return " ".join(lines)


def _no_data_report() -> str:
    return (
        "No self-eval scores logged yet. Scores accumulate automatically as responses are delivered. "
        "The scoring runs asynchronously on every interaction via evals.log_interaction(). "
        "Check back after a few conversations, or run `python3 self_eval.py --test` to seed test data."
    )


# ── Jarvis reflection: patterns → KB note ────────────────────────────────────

def jarvis_reflection(hours: int = 24 * 7 * 2) -> str:
    """Synthesize score trends into a brief KB improvement note.

    Returns a markdown-ready reflection string. Does not write to disk —
    the caller decides where to persist it (typically the KB or reflection log).
    """
    scores = load_scores(hours=hours)
    if len(scores) < 5:
        return ""

    dim_totals: dict[str, list[float]] = defaultdict(list)
    composites: list[float] = []
    for rec in scores:
        if rec.get("error"):
            continue
        if "composite" in rec:
            composites.append(min(1.0, max(0.0, float(rec["composite"]))))
        for dim, val in rec.get("dimensions", {}).items():
            if val is not None:
                dim_totals[dim].append(min(1.0, max(0.0, float(val))))

    if len(composites) < 5:
        return ""

    overall = sum(composites) / len(composites)
    dim_avgs = {dim: sum(vals) / len(vals) for dim, vals in dim_totals.items() if vals}
    weak_dims = sorted(
        [(dim, avg) for dim, avg in dim_avgs.items() if avg < 0.65],
        key=lambda kv: kv[1],
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"## Jarvis Self-Eval Reflection — {now}",
        "",
        f"**Scored responses:** {len(composites)}  |  "
        f"**Overall composite:** {overall:.2f}/1.0",
        "",
    ]

    if weak_dims:
        lines.append("### Identified Weaknesses")
        for dim, avg in weak_dims:
            dim_name = dim.replace("_", " ")
            lines.append(f"- **{dim_name}** ({avg:.2f}): below 0.65 threshold")
        lines.append("")

    if weak_dims:
        first_dim, first_avg = weak_dims[0]
        action_map = {
            "specificity": (
                "Anchor more responses in Aman's stored context (projects, preferences, goals). "
                "Reference `mem.list_facts()` output and inject it into responses when relevant."
            ),
            "memory_util": (
                "Memory chunks are being retrieved but not surfaced in responses. "
                "Check that context assembly is injecting retrieved chunks into the prompt."
            ),
            "voice_fidelity": (
                "Response language has too many filler phrases. "
                "Add the never-use list to the system prompt and enforce it in brain.py."
            ),
            "module_correct": (
                "Routing is landing in wrong modules for some queries. "
                "Extend domain hint patterns in router.py for the weakest routing tag."
            ),
            "contamination": (
                "Cross-domain content is leaking into universal responses. "
                "Check that interview packs are only loaded when JARVIS_ACTIVE_ROLE is set."
            ),
        }
        action = action_map.get(first_dim, f"Investigate {first_dim} degradation path.")
        lines.append("### Recommended Action")
        lines.append(action)
        lines.append("")

    lines.append("### Next Reflection")
    lines.append("Run `self_eval.jarvis_reflection()` again after 50+ new interactions.")

    return "\n".join(lines)


# ── Append reflection to the reflection log ───────────────────────────────────

def write_reflection_note(note: str) -> Path | None:
    """Append the reflection note to evals/reflection_log.md."""
    if not note:
        return None
    path = runtime_state.writable_data_path("evals", "reflection_log.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "\n\n---\n\n"
    with open(path, "a", encoding="utf-8") as f:
        if path.stat().st_size > 0:
            f.write(separator)
        f.write(note)
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="self_eval", description="Jarvis self-eval scorer")
    parser.add_argument("--report", action="store_true", help="Print performance report")
    parser.add_argument("--reflect", action="store_true", help="Generate and write KB reflection note")
    parser.add_argument("--hours", type=int, default=24 * 7, help="Lookback window in hours")
    parser.add_argument("--test", action="store_true", help="Score a synthetic test response")
    args = parser.parse_args(argv)

    if args.test:
        print("[self_eval] Scoring synthetic test response...")
        rec = score_response(
            user_input="What's the best way to prepare for my YouTube PM interview?",
            response=(
                "Based on your experience at Aman, the most important thing is to ground your STAR stories "
                "in the metrics you already own. You mentioned the data quality project — that's your anchor. "
                "For YouTube specifically, tie everything back to creator ecosystem impact."
            ),
            context={"routing_tag": "InterviewIntel", "memory_chunks_retrieved": 3},
            routing_tag="InterviewIntel",
        )
        print(json.dumps(rec, indent=2))
        return 0

    if args.reflect:
        note = jarvis_reflection(hours=args.hours)
        if not note:
            print("[self_eval] Not enough data for reflection (need 5+ scored responses).")
            return 0
        path = write_reflection_note(note)
        print(note)
        if path:
            print(f"\n[self_eval] Appended to {path}")
        return 0

    # Default: report
    print(performance_report(hours=args.hours))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
