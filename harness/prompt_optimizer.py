"""
harness/prompt_optimizer.py — Prompt self-optimization via self-eval analysis.

Reads the last 200 self-eval entries, identifies which patterns (route, query type,
flag) correlate with low scores, then asks LOCAL_REASONING to suggest edits to
kb/core/identity.md or system prompt sections.

Suggestions are written to kb/prompt_suggestions.md for human review.
Nothing is auto-applied — /optimize shows the diff and waits for confirmation.

Public API:
    run_optimizer(n=200) -> OptimizationResult
    optimize_text(n=200) -> str          — for the /optimize router command
    apply_suggestion(suggestion_id) -> str  — applies one approved suggestion
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Score thresholds that flag a pattern as problematic
_ROUTING_FLOOR = 0.60
_RELEVANCE_FLOOR = 0.70
_QUALITY_FLOOR = 0.65

# Minimum samples in a pattern bucket before we surface it
_MIN_BUCKET_SAMPLES = 3


# ── Paths ─────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    try:
        from harness.audit import _base_dir as _ab
        return _ab()
    except Exception:
        return Path(__file__).resolve().parent.parent


def _suggestions_path() -> Path:
    return _base_dir() / "kb" / "prompt_suggestions.md"


def _identity_path() -> Path:
    return _base_dir() / "kb" / "core" / "identity.md"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PatternBucket:
    key: str                        # e.g. "route:Calendar" or "flag:poor_relevance"
    label: str                      # human-readable
    count: int = 0
    avg_routing: float | None = None
    avg_relevance: float | None = None
    avg_quality: float | None = None
    sample_queries: list[str] = field(default_factory=list)


@dataclass
class Suggestion:
    id: str                         # e.g. "s001"
    pattern: str                    # what triggered this suggestion
    target: str                     # "identity.md §section" or "system prompt"
    current_text: str               # excerpt of current content
    suggested_text: str             # what to replace it with
    rationale: str                  # why this change helps
    confidence: float               # 0.0-1.0, from LLM


@dataclass
class OptimizationResult:
    generated_at: str
    n_analyzed: int
    low_score_patterns: list[PatternBucket]
    suggestions: list[Suggestion]
    output_path: str
    error: str = ""


# ── Analysis ──────────────────────────────────────────────────────────────────

def _load_records(n: int) -> list[dict[str, Any]]:
    from harness.self_eval_log import load_recent
    return load_recent(n)


def _find_low_patterns(records: list[dict[str, Any]]) -> list[PatternBucket]:
    """Group records by route and flag, surface buckets with low avg scores."""
    # Bucket by route
    route_records: dict[str, list[dict]] = defaultdict(list)
    flag_records: dict[str, list[dict]] = defaultdict(list)

    for r in records:
        route = r.get("route", "") or "Unknown"
        route_records[route].append(r)
        for flag in r.get("flags", []):
            flag_records[flag].append(r)

    buckets: list[PatternBucket] = []

    def _avg(recs: list[dict], key: str) -> float | None:
        vals = [r[key] for r in recs if key in r]
        return round(sum(vals) / len(vals), 3) if vals else None

    def _is_low(b: PatternBucket) -> bool:
        if b.avg_routing is not None and b.avg_routing < _ROUTING_FLOOR:
            return True
        if b.avg_relevance is not None and b.avg_relevance < _RELEVANCE_FLOOR:
            return True
        if b.avg_quality is not None and b.avg_quality < _QUALITY_FLOOR:
            return True
        return False

    for route, recs in route_records.items():
        if len(recs) < _MIN_BUCKET_SAMPLES:
            continue
        b = PatternBucket(
            key=f"route:{route}",
            label=f"Route '{route}'",
            count=len(recs),
            avg_routing=_avg(recs, "routing_accuracy"),
            avg_relevance=_avg(recs, "response_relevance"),
            avg_quality=_avg(recs, "response_quality"),
            sample_queries=[r.get("query", "")[:80] for r in recs[:3]],
        )
        if _is_low(b):
            buckets.append(b)

    for flag, recs in flag_records.items():
        if len(recs) < _MIN_BUCKET_SAMPLES:
            continue
        b = PatternBucket(
            key=f"flag:{flag}",
            label=f"Flag '{flag}'",
            count=len(recs),
            avg_routing=_avg(recs, "routing_accuracy"),
            avg_relevance=_avg(recs, "response_relevance"),
            avg_quality=_avg(recs, "response_quality"),
            sample_queries=[r.get("query", "")[:80] for r in recs[:3]],
        )
        if _is_low(b):
            buckets.append(b)

    # Sort: worst average quality first
    buckets.sort(key=lambda b: b.avg_quality if b.avg_quality is not None else 1.0)
    return buckets[:6]  # cap at 6 patterns to keep prompt tight


# ── LLM suggestion generation ─────────────────────────────────────────────────

_OPTIMIZER_PROMPT = """\
You are Jarvis's prompt engineer. Jarvis is a personal AI assistant for Aman Imran \
(Trust & Safety / AI product professional, San Jose State, San Francisco).

You have identified quality problems in Jarvis's recent responses. Your job is to \
suggest SPECIFIC edits to Jarvis's system prompt or identity file that would fix them.

## Current identity.md excerpt
{identity_excerpt}

## Low-scoring patterns (from last {n} responses)
{patterns}

## Overall scores
- Overall quality: {quality}/1.0  (target: 0.80+)
- Routing accuracy: {routing}/1.0  (floor: 0.60)
- Response relevance: {relevance}/1.0  (floor: 0.70)

## Your task
Generate 2-4 specific suggestions. Each suggestion must follow this EXACT format:

SUGGESTION_START
id: s{idx_placeholder}
pattern: <which pattern this addresses>
target: <"identity.md §SectionName" or "system prompt §BehaviorSection">
rationale: <one sentence: why this change improves quality>
confidence: <0.0-1.0>
current_text: |
  <exact quoted text to replace, or "(none — add new section)">
suggested_text: |
  <replacement text — be specific, not generic>
SUGGESTION_END

Rules:
- Each suggestion must address a specific scored problem, not a vague improvement
- suggested_text must be concise and immediately usable
- Do not suggest changes to user facts (name, employer) — only behavior/routing rules
- Do not add more than 3 sentences to any section
- If routing accuracy is low: suggest routing clarification rules
- If relevance is low: suggest response specificity rules
- Output ONLY the SUGGESTION_START/END blocks, nothing else"""


def _parse_suggestions(raw: str) -> list[Suggestion]:
    """Parse SUGGESTION_START/END blocks from LLM output."""
    suggestions: list[Suggestion] = []
    blocks = re.findall(
        r"SUGGESTION_START\s*(.*?)\s*SUGGESTION_END",
        raw,
        re.DOTALL,
    )
    for i, block in enumerate(blocks):
        try:
            def _field(name: str) -> str:
                m = re.search(rf"^{name}:\s*(.+?)(?=\n\w+:|\Z)", block, re.MULTILINE | re.DOTALL)
                return m.group(1).strip().strip("|").strip() if m else ""

            sid = _field("id") or f"s{i+1:03d}"
            pattern = _field("pattern")
            target = _field("target")
            rationale = _field("rationale")
            conf_str = _field("confidence")
            current = _field("current_text")
            suggested = _field("suggested_text")

            try:
                confidence = float(conf_str)
            except (ValueError, TypeError):
                confidence = 0.5

            suggestions.append(Suggestion(
                id=sid,
                pattern=pattern,
                target=target,
                current_text=current,
                suggested_text=suggested,
                rationale=rationale,
                confidence=round(confidence, 2),
            ))
        except Exception:
            log.debug("[prompt_optimizer] failed to parse suggestion block %d", i)
    return suggestions


# ── Output rendering ──────────────────────────────────────────────────────────

def _render_markdown(result: OptimizationResult) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Jarvis Prompt Suggestions",
        f"*Generated {now_str} — {result.n_analyzed} responses analyzed*",
        "*Review each suggestion. Apply with: `/optimize apply <id>`*",
        "",
    ]

    if result.error:
        lines += [f"> **Error:** {result.error}", ""]

    if not result.low_score_patterns:
        lines += ["## No low-scoring patterns found", "Quality looks good — nothing to optimize.", ""]
    else:
        lines += ["## Low-Scoring Patterns Detected", ""]
        for b in result.low_score_patterns:
            q = f"{b.avg_quality:.2f}" if b.avg_quality is not None else "—"
            ra = f"{b.avg_routing:.2f}" if b.avg_routing is not None else "—"
            rr = f"{b.avg_relevance:.2f}" if b.avg_relevance is not None else "—"
            lines.append(f"### {b.label} ({b.count} responses)")
            lines.append(f"quality={q}  routing={ra}  relevance={rr}")
            if b.sample_queries:
                lines.append("Sample queries:")
                for q_str in b.sample_queries:
                    lines.append(f"  - \"{q_str}\"")
            lines.append("")

    if result.suggestions:
        lines += ["## Suggestions", ""]
        for s in result.suggestions:
            conf_label = "high" if s.confidence >= 0.7 else ("medium" if s.confidence >= 0.4 else "low")
            lines += [
                f"### [{s.id}] {s.target}  *(confidence: {s.confidence:.0%} — {conf_label})*",
                f"**Pattern addressed:** {s.pattern}",
                f"**Rationale:** {s.rationale}",
                "",
                "**Current:**",
                "```",
                s.current_text or "(none — new addition)",
                "```",
                "",
                "**Suggested:**",
                "```",
                s.suggested_text,
                "```",
                "",
                f"*Apply: `/optimize apply {s.id}`*",
                "",
            ]
    else:
        lines += ["## No suggestions generated", ""]

    lines.append(f"*Generated by harness/prompt_optimizer.py at {result.generated_at}*")
    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_optimizer(n: int = 200) -> OptimizationResult:
    """Analyze last n scored responses and generate prompt improvement suggestions."""
    from harness.self_eval_log import rolling_average

    records = _load_records(n)
    if not records:
        return OptimizationResult(
            generated_at=datetime.now(timezone.utc).isoformat(),
            n_analyzed=0,
            low_score_patterns=[],
            suggestions=[],
            output_path=str(_suggestions_path()),
            error="No scored records found in logs/self_eval.jsonl.",
        )

    avg = rolling_average(n)
    overall = avg.get("response_quality") or 0.0
    routing = avg.get("routing_accuracy") or 0.0
    relevance = avg.get("response_relevance") or 0.0

    patterns = _find_low_patterns(records)

    suggestions: list[Suggestion] = []
    error_msg = ""

    if patterns:
        # Build patterns summary for prompt
        pattern_lines = []
        for b in patterns:
            q = f"{b.avg_quality:.2f}" if b.avg_quality is not None else "—"
            ra = f"{b.avg_routing:.2f}" if b.avg_routing is not None else "—"
            rr = f"{b.avg_relevance:.2f}" if b.avg_relevance is not None else "—"
            pattern_lines.append(
                f"- {b.label}: {b.count}× responses, quality={q}, routing={ra}, relevance={rr}"
            )
            for sq in b.sample_queries:
                pattern_lines.append(f'    example: "{sq}"')

        # Read identity.md excerpt (first 60 lines — behavior sections)
        identity_text = ""
        try:
            identity_path = _identity_path()
            if identity_path.exists():
                lines = identity_path.read_text(encoding="utf-8").splitlines()
                identity_text = "\n".join(lines[:60])
        except Exception:
            identity_text = "(could not read identity.md)"

        prompt = _OPTIMIZER_PROMPT.format(
            identity_excerpt=identity_text,
            n=len(records),
            patterns="\n".join(pattern_lines),
            quality=f"{overall:.2f}",
            routing=f"{routing:.2f}",
            relevance=f"{relevance:.2f}",
            idx_placeholder="NNN",
        )

        try:
            from brains.brain_ollama import ask_local
            from config import LOCAL_REASONING
            raw = ask_local(
                prompt,
                model=LOCAL_REASONING,
                system_extra="",
                include_memory=False,
                raise_on_error=True,
            ).strip()
            suggestions = _parse_suggestions(raw)
            # Re-number suggestions sequentially
            for i, s in enumerate(suggestions):
                s.id = f"s{i+1:03d}"
        except Exception as exc:
            log.warning("[prompt_optimizer] LLM call failed: %s", exc)
            error_msg = f"LLM suggestion generation failed: {exc}"

    result = OptimizationResult(
        generated_at=datetime.now(timezone.utc).isoformat(),
        n_analyzed=len(records),
        low_score_patterns=patterns,
        suggestions=suggestions,
        output_path=str(_suggestions_path()),
        error=error_msg,
    )

    # Write suggestions file
    md = _render_markdown(result)
    out = _suggestions_path()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        log.info("[prompt_optimizer] Suggestions written to %s", out)
    except OSError as exc:
        log.warning("[prompt_optimizer] Could not write suggestions file: %s", exc)

    return result


# ── /optimize command text ────────────────────────────────────────────────────

def optimize_text(n: int = 200) -> str:
    """Short text summary for the /optimize router command."""
    result = run_optimizer(n=n)

    if result.n_analyzed == 0:
        return "No self-eval data yet. Run a few queries first, then /optimize."

    lines = [f"Prompt optimization — analyzed {result.n_analyzed} responses."]

    if result.error and not result.suggestions:
        lines.append(f"Warning: {result.error}")

    if not result.low_score_patterns:
        lines.append("No low-scoring patterns detected — quality looks good.")
    else:
        lines.append(f"Low-scoring patterns: {len(result.low_score_patterns)}")
        for b in result.low_score_patterns[:3]:
            q = f"{b.avg_quality:.2f}" if b.avg_quality is not None else "—"
            lines.append(f"  • {b.label}: quality={q} ({b.count}×)")

    if result.suggestions:
        lines.append(f"\n{len(result.suggestions)} suggestion(s) generated:")
        for s in result.suggestions:
            conf_pct = f"{s.confidence:.0%}"
            lines.append(f"  [{s.id}] {s.target} ({conf_pct} confidence)")
            lines.append(f"    → {s.rationale}")
    else:
        lines.append("No suggestions generated.")

    lines.append(f"\nFull diff → {result.output_path}")
    lines.append("Apply a suggestion: `/optimize apply <id>`  (e.g. `/optimize apply s001`)")
    return "\n".join(lines)


# ── apply_suggestion ──────────────────────────────────────────────────────────

def apply_suggestion(suggestion_id: str) -> str:
    """Apply a specific suggestion from kb/prompt_suggestions.md.

    Reads the suggestions file, finds the matching id, shows a diff of
    current vs suggested text, and writes the change to the target file.
    Returns a status message (never raises).
    """
    out_path = _suggestions_path()
    if not out_path.exists():
        return "No suggestions file found. Run /optimize first."

    content = out_path.read_text(encoding="utf-8")

    # Find the suggestion block by id
    pattern = rf"### \[{re.escape(suggestion_id)}\].*?(?=### \[s\d|\Z)"
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return f"Suggestion '{suggestion_id}' not found. Run /optimize to regenerate."

    block = m.group(0)

    # Extract current and suggested text from the block
    cur_m = re.search(r"\*\*Current:\*\*\n```\n(.*?)\n```", block, re.DOTALL)
    sug_m = re.search(r"\*\*Suggested:\*\*\n```\n(.*?)\n```", block, re.DOTALL)
    target_m = re.search(r"### \[" + re.escape(suggestion_id) + r"\] (.+?)  \*", block)

    if not sug_m:
        return f"Could not parse suggested text for '{suggestion_id}'."

    current_text = cur_m.group(1).strip() if cur_m else "(none)"
    suggested_text = sug_m.group(1).strip()
    target_desc = target_m.group(1).strip() if target_m else "unknown target"

    # Determine target file — only identity.md is auto-writable
    if "identity.md" in target_desc.lower():
        target_file = _identity_path()
    else:
        return (
            f"Suggestion '{suggestion_id}' targets '{target_desc}' — "
            "system prompt edits require manual application. "
            f"Suggested text:\n\n{suggested_text}"
        )

    if not target_file.exists():
        return f"Target file not found: {target_file}"

    file_content = target_file.read_text(encoding="utf-8")

    # Show diff first, then apply if current_text found
    if current_text == "(none — new addition)":
        # Append to end of file
        target_file.write_text(file_content.rstrip() + f"\n\n{suggested_text}\n", encoding="utf-8")
        return (
            f"Applied [{suggestion_id}]: appended new section to {target_file.name}.\n"
            f"Added:\n{suggested_text}"
        )

    if current_text not in file_content:
        return (
            f"Could not find the current text in {target_file.name} — "
            "the file may have changed since suggestions were generated. "
            "Re-run /optimize to refresh.\n\n"
            f"Looking for:\n{current_text[:200]}"
        )

    # Show diff
    diff_lines = [
        f"Applying [{suggestion_id}] to {target_file.name}:",
        "--- current",
        "+++ suggested",
    ]
    for line in current_text.splitlines():
        diff_lines.append(f"- {line}")
    for line in suggested_text.splitlines():
        diff_lines.append(f"+ {line}")

    new_content = file_content.replace(current_text, suggested_text, 1)
    target_file.write_text(new_content, encoding="utf-8")

    return "\n".join(diff_lines) + f"\n\nApplied. {target_file.name} updated."


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="harness.prompt_optimizer")
    parser.add_argument("--n", type=int, default=200, help="Number of recent records to analyze")
    parser.add_argument("--apply", metavar="ID", help="Apply suggestion by id (e.g. s001)")
    args = parser.parse_args(argv)

    if args.apply:
        print(apply_suggestion(args.apply))
        return 0

    print(optimize_text(n=args.n))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
