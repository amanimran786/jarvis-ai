"""
harness/reflection.py — Jarvis reflection pipeline.

Reads from logs/self_eval.jsonl and memory/episodic/ to surface quality
patterns, then writes a living performance document to kb/core/jarvis_self_eval.md.

Triggered by the /reflect router command or by learner.py on the daily cycle.

Public API:
    run_reflection(hours=168) -> ReflectionResult
    reflect_text(hours=168) -> str            — for the /reflect router command
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    try:
        from harness.audit import _base_dir as _ab
        return _ab()
    except Exception:
        return Path(__file__).resolve().parent.parent


def _self_eval_log() -> Path:
    return _base_dir() / "logs" / "self_eval.jsonl"


def _episodic_dir() -> Path:
    return _base_dir() / "memory" / "episodic"


def _reflection_output() -> Path:
    return _base_dir() / "kb" / "core" / "jarvis_self_eval.md"


def _reflection_history_path() -> Path:
    return _base_dir() / "evals" / "reflection_history.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class DomainStats:
    route: str
    count: int = 0
    routing_accuracy_sum: float = 0.0
    response_relevance_sum: float = 0.0
    conciseness_sum: float = 0.0
    response_quality_sum: float = 0.0
    flags: list[str] = field(default_factory=list)

    def avg(self, axis: str) -> float | None:
        if self.count == 0:
            return None
        total = getattr(self, f"{axis}_sum")
        return round(total / self.count, 3)

    def quality(self) -> float:
        return round(self.response_quality_sum / max(1, self.count), 3)


@dataclass
class ReflectionResult:
    generated_at: str
    lookback_hours: int
    total_scored: int
    overall_quality: float | None
    axis_averages: dict[str, float | None]
    top_flags: dict[str, int]
    domain_stats: dict[str, DomainStats]
    episodic_context: list[str]
    insights: list[str]
    output_path: str
    # Diff vs previous reflection (None fields mean no previous snapshot)
    axis_deltas: dict[str, float | None] = field(default_factory=dict)
    overall_delta: float | None = None
    new_flags: list[str] = field(default_factory=list)     # flags that appeared since last run
    resolved_flags: list[str] = field(default_factory=list)  # flags that went away


# ── Load quality log ──────────────────────────────────────────────────────────

def _load_scores(hours: int) -> list[dict[str, Any]]:
    path = _self_eval_log()
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
            if rec.get("error"):
                continue
            ts_str = rec.get("ts", "")
            if ts_str:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            records.append(rec)
        except (json.JSONDecodeError, ValueError):
            continue
    return records


# ── Compute quality stats ─────────────────────────────────────────────────────

def _compute_stats(records: list[dict[str, Any]]) -> tuple[
    float | None, dict[str, float | None], dict[str, int], dict[str, DomainStats]
]:
    if not records:
        return None, {}, {}, {}

    ra_vals, rr_vals, cs_vals, rq_vals = [], [], [], []
    flag_counter: Counter = Counter()
    domains: dict[str, DomainStats] = {}

    for rec in records:
        ra = rec.get("routing_accuracy")
        rr = rec.get("response_relevance")
        cs = rec.get("conciseness")
        rq = rec.get("response_quality")

        if ra is not None:
            ra_vals.append(ra)
        if rr is not None:
            rr_vals.append(rr)
        if cs is not None:
            cs_vals.append(cs)
        if rq is not None:
            rq_vals.append(rq)

        for flag in rec.get("flags", []):
            flag_counter[flag] += 1

        route = rec.get("route", "") or "Unknown"
        if route not in domains:
            domains[route] = DomainStats(route=route)
        ds = domains[route]
        ds.count += 1
        if ra is not None:
            ds.routing_accuracy_sum += ra
        if rr is not None:
            ds.response_relevance_sum += rr
        if cs is not None:
            ds.conciseness_sum += cs
        if rq is not None:
            ds.response_quality_sum += rq
        ds.flags.extend(rec.get("flags", []))

    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 3) if vals else None

    overall = avg(rq_vals)
    axes = {
        "routing_accuracy": avg(ra_vals),
        "response_relevance": avg(rr_vals),
        "conciseness": avg(cs_vals),
    }
    return overall, axes, dict(flag_counter.most_common(8)), domains


# ── Read episodic memory ──────────────────────────────────────────────────────

def _load_episodic_context() -> list[str]:
    """Return brief summaries from episodic memory files."""
    ep_dir = _episodic_dir()
    if not ep_dir.is_dir():
        return []

    snippets: list[str] = []
    for json_file in sorted(ep_dir.rglob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data[:2]:
                    if isinstance(item, dict) and item.get("content"):
                        snippets.append(str(item["content"])[:150])
            elif isinstance(data, dict):
                content = data.get("content") or data.get("summary") or ""
                domain = data.get("domain", json_file.parent.name)
                if content:
                    snippets.append(f"[{domain}] {str(content)[:150]}")
        except Exception:
            continue
    return snippets[:8]  # cap to avoid bloat


# ── Synthesize insights ───────────────────────────────────────────────────────

_AXIS_LABELS = {
    "routing_accuracy": "routing accuracy",
    "response_relevance": "response relevance",
    "conciseness": "conciseness",
}


def _label(v: float | None) -> str:
    if v is None:
        return "no data"
    if v >= 0.80:
        return "strong"
    if v >= 0.65:
        return "adequate"
    return "needs work"


def _generate_insights(
    overall: float | None,
    axes: dict[str, float | None],
    top_flags: dict[str, int],
    domains: dict[str, DomainStats],
    n_records: int,
) -> list[str]:
    insights: list[str] = []

    if n_records == 0:
        return ["No responses scored yet — run a few queries to populate the self-eval log."]

    if overall is not None:
        if overall >= 0.80:
            insights.append(f"Overall quality is strong at {overall:.2f}/1.0 across {n_records} scored responses.")
        elif overall >= 0.65:
            insights.append(f"Overall quality is adequate at {overall:.2f}/1.0 — room to improve.")
        else:
            insights.append(f"Overall quality is low at {overall:.2f}/1.0 — immediate attention needed.")

    # Weakest axis
    valid_axes = [(v, k) for k, v in axes.items() if v is not None]
    if valid_axes:
        weakest_val, weakest_key = min(valid_axes, key=lambda kv: kv[0])
        if weakest_val < 0.70:
            insights.append(
                f"Weakest axis: {_AXIS_LABELS[weakest_key]} at {weakest_val:.2f} ({_label(weakest_val)}). "
                "This is the highest-leverage improvement target."
            )

    # Flag patterns
    if "verbose" in top_flags:
        cnt = top_flags["verbose"]
        insights.append(
            f"Verbosity flag appears {cnt}× — responses are running longer than the query type warrants."
        )
    if "filler_heavy" in top_flags:
        cnt = top_flags["filler_heavy"]
        insights.append(
            f"Filler-heavy flag appears {cnt}× — 04_BEHAVIORAL_RULES.md phrases like 'absolutely' "
            "or 'certainly' are leaking into responses."
        )
    if "routing_mismatch" in top_flags:
        cnt = top_flags["routing_mismatch"]
        insights.append(
            f"Routing mismatch flag appears {cnt}× — some queries are hitting wrong modules."
        )
    if "generic_response" in top_flags:
        cnt = top_flags["generic_response"]
        insights.append(
            f"Generic response flag appears {cnt}× — responses lack Aman-specific anchors (names, metrics, context)."
        )

    # Best and worst domains
    if len(domains) >= 2:
        by_quality = sorted(
            [(ds.quality(), route) for route, ds in domains.items() if ds.count >= 2],
            key=lambda kv: kv[0],
        )
        if by_quality:
            worst_q, worst_route = by_quality[0]
            if worst_q < 0.70:
                insights.append(
                    f"Domain '{worst_route}' is the weakest performer at {worst_q:.2f} avg quality "
                    f"({domains[worst_route].count} responses)."
                )
            best_q, best_route = by_quality[-1]
            if best_q >= 0.80:
                insights.append(
                    f"Domain '{best_route}' is performing well at {best_q:.2f} avg quality."
                )

    return insights[:6]


# ── Render the markdown document ──────────────────────────────────────────────

def _delta_str(delta: float | None, threshold: float = 0.005) -> str:
    """Format a delta for display. Returns '' for None or near-zero changes."""
    if delta is None or abs(delta) < threshold:
        return ""
    arrow = "↑" if delta > 0 else "↓"
    return f" {arrow}{abs(delta):.3f}"


def _render_markdown(result: ReflectionResult) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Jarvis Self-Eval Reflection",
        f"*Last updated: {now} — lookback window: {result.lookback_hours}h — {result.total_scored} responses scored*",
        "",
    ]

    # Overall quality
    lines.append("## Performance Summary")
    if result.overall_quality is not None:
        trend = _delta_str(result.overall_delta)
        lines.append(
            f"**Overall quality: {result.overall_quality:.2f}/1.0** "
            f"({_label(result.overall_quality)}){trend}"
        )
    else:
        lines.append("*No scored responses in the lookback window.*")
    lines.append("")

    # Axes with per-axis deltas
    lines.append("## Axis Breakdown")
    for axis, val in result.axis_averages.items():
        label = _AXIS_LABELS.get(axis, axis)
        val_str = f"{val:.2f}" if val is not None else "—"
        delta_s = _delta_str(result.axis_deltas.get(axis))
        lines.append(f"- **{label}:** {val_str} ({_label(val)}){delta_s}")
    lines.append("")

    # Changes vs previous (only when we have diff data)
    has_diff = result.overall_delta is not None or result.new_flags or result.resolved_flags
    if has_diff:
        lines.append("## Changes vs Previous Reflection")
        if result.new_flags:
            lines.append(f"- **New issues:** {', '.join(f'`{f}`' for f in result.new_flags)}")
        else:
            lines.append("- New issues: none")
        if result.resolved_flags:
            lines.append(f"- **Resolved:** {', '.join(f'`{f}`' for f in result.resolved_flags)}")
        else:
            lines.append("- Resolved: none")
        # Biggest mover
        valid_deltas = {k: v for k, v in result.axis_deltas.items() if v is not None}
        if valid_deltas:
            best_k = max(valid_deltas, key=lambda k: valid_deltas[k])
            worst_k = min(valid_deltas, key=lambda k: valid_deltas[k])
            if valid_deltas[best_k] > 0.005:
                lines.append(
                    f"- **Biggest improvement:** {_AXIS_LABELS.get(best_k, best_k)} "
                    f"({valid_deltas[best_k]:+.3f})"
                )
            if valid_deltas[worst_k] < -0.005:
                lines.append(
                    f"- **Biggest regression:** {_AXIS_LABELS.get(worst_k, worst_k)} "
                    f"({valid_deltas[worst_k]:+.3f})"
                )
        lines.append("")

    # Flags
    if result.top_flags:
        lines.append("## Recurring Issues (Flags)")
        for flag, cnt in result.top_flags.items():
            lines.append(f"- `{flag}`: {cnt}×")
        lines.append("")

    # Domain breakdown
    if result.domain_stats:
        lines.append("## Domain Breakdown")
        by_volume = sorted(
            result.domain_stats.items(),
            key=lambda kv: kv[1].count,
            reverse=True,
        )
        for route, ds in by_volume[:8]:
            quality = ds.quality()
            flag_summary = ""
            if ds.flags:
                top = Counter(ds.flags).most_common(2)
                flag_summary = " — flags: " + ", ".join(f"{f}({c}×)" for f, c in top)
            lines.append(
                f"- **{route}**: {ds.count} responses, avg quality {quality:.2f}{flag_summary}"
            )
        lines.append("")

    # Episodic context
    if result.episodic_context:
        lines.append("## Episodic Context (Active Threads)")
        for snippet in result.episodic_context[:4]:
            # Truncate for readability
            short = snippet[:120] + ("…" if len(snippet) > 120 else "")
            lines.append(f"- {short}")
        lines.append("")

    # Insights
    if result.insights:
        lines.append("## Insights")
        for insight in result.insights:
            lines.append(f"- {insight}")
        lines.append("")

    # Action items
    lines.append("## Action Items For Next Cycle")
    axes_sorted = sorted(
        [(v, k) for k, v in result.axis_averages.items() if v is not None],
        key=lambda kv: kv[0],
    )
    item_count = 0
    for val, axis in axes_sorted:
        if val is not None and val < 0.70:
            lines.append(f"{item_count + 1}. Improve {_AXIS_LABELS.get(axis, axis)} (currently {val:.2f}).")
            item_count += 1
    if "verbose" in result.top_flags and item_count < 3:
        lines.append(f"{item_count + 1}. Reduce verbosity — tighten responses to query-appropriate length.")
        item_count += 1
    if "filler_heavy" in result.top_flags and item_count < 3:
        lines.append(f"{item_count + 1}. Eliminate filler phrases per 04_BEHAVIORAL_RULES.md §1.")
        item_count += 1
    if item_count == 0:
        lines.append("1. Maintain current quality levels. No critical issues flagged.")
    lines.append("")

    lines.append(f"*Generated by harness/reflection.py at {result.generated_at}*")
    return "\n".join(lines)


# ── Snapshot history (lightweight numeric snapshots for diff) ─────────────────

def _load_prev_snapshot() -> dict[str, Any] | None:
    """Return the most recent valid snapshot from evals/reflection_history.jsonl."""
    path = _reflection_history_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
                if snap.get("overall_quality") is not None:
                    return snap
            except json.JSONDecodeError:
                continue
    except Exception:
        logging.debug("[Reflection] silent failure in _load_prev_snapshot", exc_info=True)
    return None


def _save_snapshot(result: ReflectionResult) -> None:
    """Append a compact numeric snapshot to evals/reflection_history.jsonl."""
    if result.overall_quality is None:
        return  # don't snapshot empty runs
    path = _reflection_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        snap = {
            "ts": result.generated_at,
            "total_scored": result.total_scored,
            "overall_quality": result.overall_quality,
            "axes": {k: v for k, v in result.axis_averages.items() if v is not None},
            "top_flags": result.top_flags,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap) + "\n")
    except Exception:
        log.debug("[reflection] _save_snapshot() failed", exc_info=True)


def _compute_diff(
    prev: dict[str, Any],
    overall: float | None,
    axes: dict[str, float | None],
    top_flags: dict[str, int],
) -> tuple[float | None, dict[str, float | None], list[str], list[str]]:
    """Compute deltas vs previous snapshot.

    Returns: (overall_delta, axis_deltas, new_flags, resolved_flags)
    """
    prev_overall = prev.get("overall_quality")
    overall_delta = None
    if overall is not None and prev_overall is not None:
        overall_delta = round(overall - prev_overall, 3)

    prev_axes = prev.get("axes", {})
    axis_deltas: dict[str, float | None] = {}
    for axis, val in axes.items():
        prev_val = prev_axes.get(axis)
        if val is not None and prev_val is not None:
            axis_deltas[axis] = round(val - prev_val, 3)
        else:
            axis_deltas[axis] = None

    prev_flag_set = set(prev.get("top_flags", {}).keys())
    curr_flag_set = set(top_flags.keys())
    new_flags = sorted(curr_flag_set - prev_flag_set)
    resolved_flags = sorted(prev_flag_set - curr_flag_set)

    return overall_delta, axis_deltas, new_flags, resolved_flags


# ── Main: run_reflection ──────────────────────────────────────────────────────

_reflection_lock = threading.Lock()


def run_reflection(hours: int = 168) -> ReflectionResult:
    """Run the reflection pipeline and write kb/core/jarvis_self_eval.md.

    Thread-safe. Never raises — on error returns a minimal result.
    """
    with _reflection_lock:
        try:
            records = _load_scores(hours)
            overall, axes, top_flags, domains = _compute_stats(records)
            episodic = _load_episodic_context()
            insights = _generate_insights(overall, axes, top_flags, domains, len(records))

            # Diff vs previous snapshot
            prev = _load_prev_snapshot()
            overall_delta, axis_deltas, new_flags, resolved_flags = (
                _compute_diff(prev, overall, axes, top_flags)
                if prev is not None
                else (None, {}, [], [])
            )

            output_path = _reflection_output()
            result = ReflectionResult(
                generated_at=_now_iso(),
                lookback_hours=hours,
                total_scored=len(records),
                overall_quality=overall,
                axis_averages=axes,
                top_flags=top_flags,
                domain_stats=domains,
                episodic_context=episodic,
                insights=insights,
                output_path=str(output_path),
                axis_deltas=axis_deltas,
                overall_delta=overall_delta,
                new_flags=new_flags,
                resolved_flags=resolved_flags,
            )

            md = _render_markdown(result)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(md, encoding="utf-8")

            # Persist snapshot for future diffs
            _save_snapshot(result)

            return result

        except Exception:
            log.exception("[reflection] run_reflection() failed")
            return ReflectionResult(
                generated_at=_now_iso(),
                lookback_hours=hours,
                total_scored=0,
                overall_quality=None,
                axis_averages={},
                top_flags={},
                domain_stats={},
                episodic_context=[],
                insights=["Reflection pipeline encountered an error — check logs."],
                output_path="",
            )


def reflect_text(hours: int = 168) -> str:
    """Short text summary for the /reflect router command."""
    result = run_reflection(hours=hours)
    n = result.total_scored
    if n == 0:
        return (
            f"Reflection complete — no responses scored in the last {hours}h window. "
            "Run a few queries first, then /reflect again.\n"
            f"Output: {result.output_path}"
        )

    quality_str = f"{result.overall_quality:.2f}/1.0" if result.overall_quality else "—"
    overall_trend = _delta_str(result.overall_delta) if result.overall_delta is not None else ""

    lines = [f"Reflection complete — {n} responses scored over the last {hours}h."]
    lines.append(f"Overall quality: {quality_str}{overall_trend}.")

    # Per-axis breakdown with deltas
    if result.axis_averages:
        lines.append("Axes:")
        for axis, val in result.axis_averages.items():
            label = _AXIS_LABELS.get(axis, axis)
            val_s = f"{val:.2f}" if val is not None else "—"
            delta_s = _delta_str(result.axis_deltas.get(axis))
            lines.append(f"  {label}: {val_s}{delta_s}")

    # Flag changes
    if result.new_flags:
        lines.append(f"New issues: {', '.join(result.new_flags)}")
    if result.resolved_flags:
        lines.append(f"Resolved: {', '.join(result.resolved_flags)}")

    # Top insight
    top_insight = result.insights[0] if result.insights else ""
    if top_insight:
        lines.append(f"Insight: {top_insight}")

    lines.append(f"Full report → {result.output_path}")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="harness.reflection")
    parser.add_argument("--hours", type=int, default=168, help="Lookback window in hours")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args(argv)

    result = run_reflection(hours=args.hours)

    if args.json:
        import dataclasses
        d = dataclasses.asdict(result)
        d.pop("domain_stats", None)  # DomainStats objects don't serialize cleanly
        print(json.dumps(d, indent=2, default=str))
    else:
        print(reflect_text(hours=args.hours))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
