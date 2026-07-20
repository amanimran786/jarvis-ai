"""
harness/prompt_tuner.py — Adaptive system-prompt hint generator.

Reads recent quality scores from logs/self_eval.jsonl and returns a compact
(<200 chars) prompt appendix that reinforces the sections of 04_BEHAVIORAL_RULES.md
most relevant to current failure modes.

Injected into every prompt via learner.get_learning_context() → memory.get_context().

Design constraint: total output must stay under 200 chars (one short paragraph),
because this fires on every single LLM call. Prioritize the single highest-signal
issue — do not dump every flag.

Public API:
    prompt_appendix(n=20) -> str     — adaptive hint; empty if no issues
    quality_status(n=20) -> dict     — raw quality state for inspection / tests
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ── Thresholds — only emit a hint if an issue appears this many times ─────────

_FLAG_THRESHOLD = 2     # flags must recur at least this often to trigger a hint
_RELEVANCE_FLOOR = 0.62  # below this → always inject specificity reminder
_QUALITY_FLOOR = 0.60    # below this → emit quality-degraded warning


# ── Hint templates (ordered by priority — first match wins) ───────────────────

def _build_hints(
    top_flags: dict[str, int],
    avg_relevance: float | None,
    avg_quality: float | None,
    n: int,
) -> list[str]:
    """Return priority-ordered list of hint strings (most critical first)."""
    hints: list[str] = []

    # Priority 1: response quality floor
    if avg_quality is not None and avg_quality < _QUALITY_FLOOR:
        hints.append(
            f"Quality avg {avg_quality:.2f} — prioritize specific, direct answers over safe generalities."
        )

    # Priority 2: specificity (low relevance or generic_response flag)
    if avg_relevance is not None and avg_relevance < _RELEVANCE_FLOOR:
        hints.append(
            "Relevance low — ground responses in Aman's actual context: his projects, metrics, companies, timeline."
        )
    elif top_flags.get("generic_response", 0) >= _FLAG_THRESHOLD:
        cnt = top_flags["generic_response"]
        hints.append(
            f"Generic response flagged {cnt}× — name specific projects, numbers, or companies rather than abstract advice."
        )

    # Priority 3: filler phrases
    if top_flags.get("filler_heavy", 0) >= _FLAG_THRESHOLD:
        cnt = top_flags["filler_heavy"]
        hints.append(
            f"Filler phrases flagged {cnt}× — skip 'absolutely', 'certainly', 'I hope this helps'. Lead with the answer."
        )

    # Priority 4: verbosity
    if top_flags.get("verbose", 0) >= _FLAG_THRESHOLD:
        cnt = top_flags["verbose"]
        hints.append(
            f"Verbose flagged {cnt}× — match response length to query type (conversational = 1-3 sentences)."
        )

    # Priority 5: hedge cascade
    if top_flags.get("over_hedged", 0) >= _FLAG_THRESHOLD:
        hints.append("Over-hedging detected — state one uncertainty clearly, don't stack 'maybe/perhaps/could be'.")

    return hints


# ── Main public function ───────────────────────────────────────────────────────

def quality_status(n: int = 20) -> dict[str, Any]:
    """Return the raw quality state used to generate the hint."""
    try:
        from harness.self_eval_log import rolling_average
        avg = rolling_average(n=n)
        return avg
    except Exception:
        return {"count": 0}


def prompt_appendix(n: int = 20) -> str:
    """Return a compact adaptive prompt hint, or '' if everything looks clean.

    Emits at most ONE hint (the highest priority issue). This keeps token
    overhead minimal — typically 30-100 chars when active, zero when quality is good.
    """
    try:
        avg = quality_status(n=n)
        count = avg.get("count", 0)
        if count < 3:
            # Not enough data to make a reliable judgment — stay silent
            return ""

        top_flags: dict[str, int] = avg.get("top_flags", {})
        avg_relevance: float | None = avg.get("response_relevance")
        avg_quality: float | None = avg.get("response_quality")

        hints = _build_hints(top_flags, avg_relevance, avg_quality, count)
        if not hints:
            return ""

        # Emit only the top priority hint to stay compact
        top_hint = hints[0]
        prefix = f"[Self-eval hint, last {count} responses]"
        result = f"{prefix}: {top_hint}"

        # Hard cap — if somehow over 220 chars, truncate cleanly
        if len(result) > 220:
            result = result[:217] + "..."
        return result

    except Exception:
        log.debug("[prompt_tuner] prompt_appendix() failed", exc_info=True)
        return ""


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse, json

    parser = argparse.ArgumentParser(prog="harness.prompt_tuner")
    parser.add_argument("--n", type=int, default=20, help="Number of recent scores to read")
    parser.add_argument("--status", action="store_true", help="Show raw quality status JSON")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(quality_status(n=args.n), indent=2, default=str))
        return 0

    hint = prompt_appendix(n=args.n)
    if hint:
        print(hint)
    else:
        print("(no hint — quality is clean or insufficient data)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
