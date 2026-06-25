"""
harness/self_eval_log.py — 3-axis response quality scorer for Jarvis.

Scores every response on three axes drawn from 04_BEHAVIORAL_RULES.md:
  1. routing_accuracy  — did the right module handle the query? (0–1)
  2. response_relevance — does the response address the actual query? (0–1)
  3. conciseness       — is the response appropriately brief? (0–1)

Composite response_quality = weighted average (routing 0.25, relevance 0.40, conciseness 0.35).

Flags are string tokens from the Section 8 failure taxonomy:
  "routing_mismatch", "generic_response", "verbose", "filler_heavy",
  "error_response", "no_content", "over_hedged"

Log format (one JSON per line in logs/self_eval.jsonl):
  {
    "ts":               "2026-06-14T06:40:00Z",
    "query":            "what's on my calendar",
    "route":            "Calendar",
    "routing_accuracy": 0.90,
    "response_relevance": 0.80,
    "conciseness":      0.75,
    "response_quality": 0.82,
    "flags":            [],
    "reflection_note":  "Good routing, response was slightly verbose"
  }

Public API:
  score(query, response, route, *, interaction_id=None) -> dict
  score_async(query, response, route, *, interaction_id=None)  — fire-and-forget
  rolling_average(n=50) -> dict
  score_report(n=50) -> str   — for the /score router command
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Path resolution ───────────────────────────────────────────────────────────

def _logs_dir() -> Path:
    """Return project-root/logs/, consistent with harness/audit.py."""
    try:
        from harness.audit import _base_dir
        return _base_dir() / "logs"
    except Exception:
        return Path(__file__).resolve().parent.parent / "logs"


def _log_path() -> Path:
    return _logs_dir() / "self_eval.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Failure-mode vocabulary (Section 8 of 04_BEHAVIORAL_RULES.md) ────────────

_GENERIC_PHRASES = frozenset({
    "absolutely", "certainly", "definitely", "of course", "sure thing",
    "great question", "excellent question", "happy to help",
    "i can help with that", "i would be happy to", "feel free to",
    "in conclusion", "in summary", "to summarize", "as mentioned",
    "let me walk you through", "please don't hesitate",
    "i hope this helps", "it is important to note",
    "it is worth noting", "it should be noted",
    "leveraged", "synergized", "passionate about",
    "as an ai language model",
})

_HEDGE_PHRASES = (
    "i think", "i believe", "i'm not sure", "it seems", "perhaps",
    "maybe", "possibly", "might be", "could be", "it appears",
)

_ERROR_MARKERS = (
    "error:", "local model error", "couldn't ", "failed to",
    "i encountered an error", "something went wrong", "aborted before",
    "no browser window", "model unavailable",
)

# Domain→expected-route-tag alignment table.
# Keys are lowercased query signals; values are sets of route tags that fit.
_DOMAIN_ROUTING: list[tuple[tuple[str, ...], frozenset[str]]] = [
    (("calendar", "schedule", "remind", "reminder", "event", "meeting today", "what's on"),
     frozenset({"Calendar", "DailyOS", "Meeting"})),
    (("interview", "mock", "behavioral", "star story", "prep for", "hiring", "practice"),
     frozenset({"InterviewIntel", "Interview", "Career"})),
    (("apply", "job search", "resume", "recruiter", "outreach", "linkedin", "application"),
     frozenset({"CareerOS", "Career", "CommunicationCraft"})),
    (("code", "debug", "error", "sql", "query", "script", "function", "build", "test"),
     frozenset({"TechAssist", "Technical", "Code"})),
    (("decision", "tradeoff", "should i", "thinking through", "weigh", "strategy"),
     frozenset({"StrategyOS", "Strategy"})),
    (("note", "task", "draft", "email", "message", "today", "this week", "write"),
     frozenset({"DailyOS", "CommunicationCraft", "Communication"})),
    (("research", "find out", "what is", "explain", "summarize", "who is", "look up"),
     frozenset({"ResearchEngine", "Research"})),
    (("remember", "memory", "forget", "what do you know", "recall", "update"),
     frozenset({"MemoryManager", "Memory"})),
    (("vault", "knowledge base", "wiki", "source", "citation"),
     frozenset({"Vault", "Knowledge", "ResearchEngine"})),
]


# ── Axis 1: routing_accuracy ──────────────────────────────────────────────────

def _score_routing_accuracy(query: str, route: str) -> float:
    """Estimate how well the chosen route fits the query domain."""
    if not route:
        return 0.5  # unknown route — neutral

    lower_q = query.lower()

    for signals, expected_tags in _DOMAIN_ROUTING:
        has_signal = any(sig in lower_q for sig in signals)
        if has_signal:
            # Query matches this domain
            if any(tag in route for tag in expected_tags):
                return 0.90  # strong alignment
            # Signal exists but route is a different domain
            return 0.35  # likely mismatch

    # No strong domain signal in query — could be general chat or Status
    # These legitimately hit many different routes; don't penalize
    return 0.75


# ── Axis 2: response_relevance ────────────────────────────────────────────────

def _score_response_relevance(query: str, response: str) -> float:
    """Does the response actually address the query?"""
    if not response or not response.strip():
        return 0.0

    lower_r = response.lower()
    lower_q = query.lower()

    # Error signals
    if any(marker in lower_r for marker in _ERROR_MARKERS):
        return 0.15

    # Very short responses to substantive queries
    q_words = len(query.split())
    r_words = len(response.split())
    if r_words < 5 and q_words > 5:
        return 0.30

    # Query term overlap — how many content words from the query appear in the response
    q_tokens = {w for w in re.findall(r"\b[a-z]{4,}\b", lower_q)
                if w not in {"what", "when", "where", "which", "that", "this", "with", "from", "have"}}
    if q_tokens:
        overlap = sum(1 for tok in q_tokens if tok in lower_r)
        overlap_ratio = overlap / len(q_tokens)
    else:
        overlap_ratio = 0.5  # short query, assume relevance is OK

    # Generic-phrase penalty
    filler_hits = sum(1 for phrase in _GENERIC_PHRASES if phrase in lower_r)
    filler_penalty = min(0.25, filler_hits * 0.05)

    score = 0.55 + (overlap_ratio * 0.35) - filler_penalty
    return max(0.0, min(1.0, round(score, 3)))


# ── Axis 3: conciseness ───────────────────────────────────────────────────────

# Expected word count ranges per query type (from 04_BEHAVIORAL_RULES.md § SECTION 1)
_LENGTH_RULES: list[tuple[tuple[str, ...], int, int]] = [
    (("interview", "mock", "behavioral", "star", "tell me about", "walk me through"),
     150, 300),   # 90s spoken ≈ 200-250 words; give ±50 buffer
    (("write", "draft", "email", "message", "compose"),
     20, 250),    # match the medium
    (("explain", "how does", "what is", "describe"),
     30, 200),    # technical explanation: as needed
    (("sql", "query", "code", "script", "function"),
     10, 400),    # code + explanation
    (("research", "summarize", "brief", "report"),
     50, 300),    # structured, lead with finding
    (("should i", "decision", "tradeoff", "strategy"),
     30, 250),    # structured as needed
]
_DEFAULT_MIN = 3   # fewer than 3 words suggests an aborted/incomplete response
_DEFAULT_MAX = 80  # conversational: 1-3 sentences ≈ 15-60 words


def _expected_length(query: str) -> tuple[int, int]:
    lower_q = query.lower()
    for signals, min_w, max_w in _LENGTH_RULES:
        if any(sig in lower_q for sig in signals):
            return min_w, max_w
    return _DEFAULT_MIN, _DEFAULT_MAX


def _score_conciseness(query: str, response: str) -> float:
    """Penalize verbosity beyond what the query type warrants."""
    if not response:
        return 0.5

    r_words = len(response.split())
    min_w, max_w = _expected_length(query)

    if r_words < min_w:
        # Too short — mild penalty for likely incomplete answer
        shortfall = (min_w - r_words) / max(1, min_w)
        return round(max(0.3, 1.0 - shortfall * 0.5), 3)

    if r_words <= max_w:
        return 1.0  # within target range

    # Over max — penalize proportionally
    overshoot_ratio = (r_words - max_w) / max(1, max_w)
    penalty = min(0.7, overshoot_ratio * 0.5)
    return max(0.0, round(1.0 - penalty, 3))


# ── Flags (Section 8 failure modes) ──────────────────────────────────────────

def _detect_flags(
    query: str,
    response: str,
    route: str,
    routing_accuracy: float,
    response_relevance: float,
    conciseness: float,
) -> list[str]:
    flags: list[str] = []
    lower_r = response.lower()

    if not response or not response.strip():
        flags.append("no_content")
        return flags

    if any(marker in lower_r for marker in _ERROR_MARKERS):
        flags.append("error_response")

    if routing_accuracy < 0.50:
        flags.append("routing_mismatch")

    if response_relevance < 0.50:
        flags.append("poor_relevance")

    if conciseness < 0.50:
        flags.append("verbose")

    # Generic-phrase count
    filler_hits = sum(1 for phrase in _GENERIC_PHRASES if phrase in lower_r)
    if filler_hits >= 2:
        flags.append("filler_heavy")

    # Hedge cascade — 3+ hedges in one response
    hedge_hits = sum(1 for phrase in _HEDGE_PHRASES if phrase in lower_r)
    if hedge_hits >= 3:
        flags.append("over_hedged")

    # Generic response — could apply to anyone (no specific names/numbers/context)
    has_specifics = bool(re.search(r"\b(\d+[\%\+\-x]|aman|youtube|sjsu|meta|google)\b", lower_r))
    if not has_specifics and len(response.split()) > 40:
        flags.append("generic_response")

    return flags


# ── Reflection note ───────────────────────────────────────────────────────────

_FLAG_NOTES: dict[str, str] = {
    "no_content": "Empty response — routing or model call may have failed",
    "error_response": "Response contains an error marker — check execution path",
    "routing_mismatch": "Route doesn't match query domain",
    "poor_relevance": "Response doesn't appear to address the query",
    "verbose": "Response is longer than the query type warrants",
    "filler_heavy": "Too many generic filler phrases — voice fidelity issue",
    "over_hedged": "Multiple hedges in a single response — pick the one real uncertainty",
    "generic_response": "Response is not specific to Aman's context",
}


def _reflection_note(flags: list[str], routing_accuracy: float,
                     response_relevance: float, conciseness: float) -> str:
    if not flags:
        # All clean — pick the weakest axis for a positive note
        axes = [
            ("routing", routing_accuracy),
            ("relevance", response_relevance),
            ("conciseness", conciseness),
        ]
        weakest_name, weakest_val = min(axes, key=lambda kv: kv[1])
        if weakest_val >= 0.80:
            return "Good routing, relevant, concise"
        return f"Good quality overall; slight room to improve {weakest_name} ({weakest_val:.2f})"

    # Report the top 1-2 flags
    primary = flags[0]
    note = _FLAG_NOTES.get(primary, f"Flag: {primary}")
    if len(flags) > 1:
        secondary = _FLAG_NOTES.get(flags[1], flags[1])
        note = f"{note}; also: {secondary.lower()}"
    return note


# ── Composite score ───────────────────────────────────────────────────────────

_WEIGHTS = {"routing_accuracy": 0.25, "response_relevance": 0.40, "conciseness": 0.35}


def _composite(routing_accuracy: float, response_relevance: float,
               conciseness: float) -> float:
    score = (
        routing_accuracy * _WEIGHTS["routing_accuracy"]
        + response_relevance * _WEIGHTS["response_relevance"]
        + conciseness * _WEIGHTS["conciseness"]
    )
    return round(max(0.0, min(1.0, score)), 3)


# ── Public: score one response ────────────────────────────────────────────────

def score(
    query: str,
    response: str,
    route: str = "",
    *,
    interaction_id: str | None = None,
) -> dict[str, Any]:
    """Score one query/response pair. Appends to logs/self_eval.jsonl and returns the record.

    Never raises — on error returns a minimal record with error=True.
    """
    try:
        ra = _score_routing_accuracy(query, route)
        rr = _score_response_relevance(query, response)
        cs = _score_conciseness(query, response)
        rq = _composite(ra, rr, cs)
        flags = _detect_flags(query, response, route, ra, rr, cs)
        note = _reflection_note(flags, ra, rr, cs)

        record: dict[str, Any] = {
            "ts": _now_iso(),
            "query": query[:300],
            "route": route or "",
            "routing_accuracy": ra,
            "response_relevance": rr,
            "conciseness": cs,
            "response_quality": rq,
            "flags": flags,
            "reflection_note": note,
        }
        if interaction_id:
            record["interaction_id"] = interaction_id

        _append(record)
        return record

    except Exception:
        log.exception("[self_eval_log] score() failed")
        return {"ts": _now_iso(), "query": query[:80], "route": route, "error": True}


def score_async(
    query: str,
    response: str,
    route: str = "",
    *,
    interaction_id: str | None = None,
) -> None:
    """Fire-and-forget wrapper — never blocks the caller."""
    t = threading.Thread(
        target=score,
        args=(query, response, route),
        kwargs={"interaction_id": interaction_id},
        daemon=True,
    )
    t.start()


# ── Log IO ────────────────────────────────────────────────────────────────────

def _append(record: dict[str, Any]) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("[self_eval_log] write failed (disk full?)", exc_info=True)


def load_recent(n: int = 50) -> list[dict[str, Any]]:
    """Return the most recent n scored records (newest last)."""
    path = _log_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not rec.get("error"):
                records.append(rec)
        except json.JSONDecodeError:
            continue
    return records[-n:]


# ── Rolling average ───────────────────────────────────────────────────────────

def rolling_average(n: int = 50) -> dict[str, Any]:
    """Compute per-axis averages and flag counts across the last n records."""
    records = load_recent(n)
    if not records:
        return {
            "count": 0,
            "routing_accuracy": None,
            "response_relevance": None,
            "conciseness": None,
            "response_quality": None,
            "top_flags": {},
        }

    from collections import Counter

    ra_vals = [r["routing_accuracy"] for r in records if "routing_accuracy" in r]
    rr_vals = [r["response_relevance"] for r in records if "response_relevance" in r]
    cs_vals = [r["conciseness"] for r in records if "conciseness" in r]
    rq_vals = [r["response_quality"] for r in records if "response_quality" in r]
    flag_counts: Counter = Counter()
    for rec in records:
        for flag in rec.get("flags", []):
            flag_counts[flag] += 1

    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "count": len(records),
        "routing_accuracy": avg(ra_vals),
        "response_relevance": avg(rr_vals),
        "conciseness": avg(cs_vals),
        "response_quality": avg(rq_vals),
        "top_flags": dict(flag_counts.most_common(5)),
    }


# ── /score command text ───────────────────────────────────────────────────────

def score_report(n: int = 50) -> str:
    """Human-readable quality report for the /score router command."""
    avg = rolling_average(n)
    count = avg["count"]
    if count == 0:
        return (
            f"No self-eval scores logged yet (last {n} responses). "
            "Scores accumulate automatically as responses are delivered. "
            "Run a few queries and check back."
        )

    rq = avg["response_quality"]
    ra = avg["routing_accuracy"]
    rr = avg["response_relevance"]
    cs = avg["conciseness"]

    def fmt(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "—"

    def label(v: float | None) -> str:
        if v is None:
            return ""
        if v >= 0.80:
            return "strong"
        if v >= 0.65:
            return "adequate"
        return "needs work"

    lines = [
        f"Quality score — last {count} responses (of {n} requested).",
        f"Overall: {fmt(rq)}/1.0  ({label(rq)}).",
        f"  routing_accuracy:   {fmt(ra)}  ({label(ra)})",
        f"  response_relevance: {fmt(rr)}  ({label(rr)})",
        f"  conciseness:        {fmt(cs)}  ({label(cs)})",
    ]

    # Weakest axis
    axes = [(ra, "routing_accuracy"), (rr, "response_relevance"), (cs, "conciseness")]
    axes_valid = [(v, n) for v, n in axes if v is not None]
    if axes_valid:
        weakest_val, weakest_name = min(axes_valid, key=lambda kv: kv[0])
        if weakest_val < 0.70:
            lines.append(
                f"Weakest axis: {weakest_name} ({fmt(weakest_val)}). "
                "Focus improvements here first."
            )

    # Top flags
    top_flags = avg.get("top_flags", {})
    if top_flags:
        flag_str = ", ".join(f"{flag} ({cnt}×)" for flag, cnt in list(top_flags.items())[:3])
        lines.append(f"Recurring issues: {flag_str}.")

    lines.append(
        "Run `python3 -m harness.self_eval_log --report` for the full breakdown."
    )
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="harness.self_eval_log")
    parser.add_argument("--report", action="store_true", help="Show rolling score report")
    parser.add_argument("--n", type=int, default=50, help="Window size")
    parser.add_argument("--test", action="store_true", help="Score a synthetic response and print the record")
    args = parser.parse_args(argv)

    if args.test:
        rec = score(
            query="what's on my calendar today",
            response=(
                "You have a 2pm design review and a 4pm 1:1 with your manager. "
                "The design review is the one to prep for — it's on the Jarvis data pipeline."
            ),
            route="Calendar",
            interaction_id="test-001",
        )
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return 0

    print(score_report(n=args.n))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
